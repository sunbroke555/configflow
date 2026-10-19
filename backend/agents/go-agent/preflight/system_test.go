package preflight

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

type systemFixture struct {
	files    map[string][]byte
	fileErr  map[string]error
	devices  map[string]os.FileMode
	tunFlags uint32
	tunErr   error
	tunCalls int
	unit     string
	unitErr  error
}

type fixtureDevice struct{ mode os.FileMode }

type systemDirectoryEntry struct{ name string }

func (e systemDirectoryEntry) Name() string               { return e.name }
func (e systemDirectoryEntry) IsDir() bool                { return false }
func (e systemDirectoryEntry) Type() os.FileMode          { return 0 }
func (e systemDirectoryEntry) Info() (os.FileInfo, error) { return nil, os.ErrNotExist }

func (f fixtureDevice) Name() string       { return "tun" }
func (f fixtureDevice) Size() int64        { return 0 }
func (f fixtureDevice) Mode() os.FileMode  { return f.mode }
func (f fixtureDevice) ModTime() time.Time { return time.Time{} }
func (f fixtureDevice) IsDir() bool        { return f.mode.IsDir() }
func (f fixtureDevice) Sys() interface{}   { return nil }

func healthySystemFixture() *systemFixture {
	return &systemFixture{
		files: map[string][]byte{
			"/proc/self/status":                []byte("Uid:\t0\t0\t0\t0\nCapEff:\t0000000000001000\n"),
			"/proc/42/status":                  []byte("CapEff:\t0000000000001000\n"),
			"/proc/42/stat":                    []byte("42 (mihomo) S " + strings.Repeat("0 ", 18) + "123\n"),
			"/proc/42/fdinfo/7":                []byte("pos:\t0\niff:\tMeta\n"),
			"/proc/42/net/dev":                 []byte("  Meta: " + strings.Repeat("0 ", 16) + "\n"),
			"/proc/sys/net/ipv4/ip_forward":    []byte("1\n"),
			"/etc/sysctl.d/99-configflow.conf": []byte("net.ipv4.ip_forward=1\n"),
		},
		fileErr: map[string]error{},
		devices: map[string]os.FileMode{
			"/dev/net/tun":              os.ModeDevice | os.ModeCharDevice | 0600,
			"/proc/42/root/dev/net/tun": os.ModeDevice | os.ModeCharDevice | 0600,
		},
		tunFlags: 1,
		unit:     "LoadState=loaded\nMainPID=42\nCapabilityBoundingSet=cap_net_admin cap_net_bind_service\nPrivateDevices=no\nDevicePolicy=auto\nPrivateNetwork=no\n",
	}
}

func (f *systemFixture) probe() systemProbe {
	return systemProbe{
		goos: "linux",
		readFile: func(path string) ([]byte, error) {
			if err := f.fileErr[path]; err != nil {
				return nil, err
			}
			if data, ok := f.files[path]; ok {
				return data, nil
			}
			return nil, os.ErrNotExist
		},
		readDir: func(directory string) ([]os.DirEntry, error) {
			if err := f.fileErr[directory]; err != nil {
				return nil, err
			}
			var entries []os.DirEntry
			for path := range f.files {
				if filepath.Dir(path) == directory {
					entries = append(entries, systemDirectoryEntry{name: filepath.Base(path)})
				}
			}
			return entries, nil
		},
		stat: func(path string) (os.FileInfo, error) {
			if mode, ok := f.devices[path]; ok {
				return fixtureDevice{mode}, nil
			}
			return nil, os.ErrNotExist
		},
		glob: func(pattern string) ([]string, error) {
			var matched []string
			for path := range f.files {
				if match, _ := filepath.Match(pattern, path); match {
					matched = append(matched, path)
				}
			}
			return matched, nil
		},
		tun: func(string) (uint32, error) {
			f.tunCalls++
			return f.tunFlags, f.tunErr
		},
		showUnit: func(string) ([]byte, error) { return []byte(f.unit), f.unitErr },
	}
}

func fixtureMihomo(t *testing.T, enable bool) Config {
	t.Helper()
	path := filepath.Join(t.TempDir(), "config.yaml")
	if err := os.WriteFile(path, []byte(fmt.Sprintf("tun:\n  enable: %t\n", enable)), 0600); err != nil {
		t.Fatal(err)
	}
	return Config{ServiceType: "mihomo", ConfigPath: path, ServiceManager: "systemd", ServiceUnit: "mihomo"}
}

func namedSystemCheck(t *testing.T, checks []Check, name string) Check {
	t.Helper()
	for _, check := range checks {
		if check.Name == name {
			return check
		}
	}
	t.Fatalf("missing check %q: %+v", name, checks)
	return Check{}
}

func TestSystemTUNPrerequisitesRespectConfiguredMode(t *testing.T) {
	for _, tc := range []struct {
		name   string
		mutate func(*systemFixture)
		check  string
	}{
		{"missing-device", func(f *systemFixture) { delete(f.devices, "/dev/net/tun") }, "TUN 设备"},
		{"regular-file", func(f *systemFixture) { f.devices["/dev/net/tun"] = 0600 }, "TUN 设备"},
		{"device-denied", func(f *systemFixture) { f.tunErr = os.ErrPermission }, "TUN 设备"},
		{"driver-without-tun", func(f *systemFixture) { f.tunFlags = 2 }, "TUN 设备"},
		{"root-without-capability", func(f *systemFixture) {
			f.files["/proc/self/status"] = []byte("Uid:\t0\t0\t0\t0\nCapEff:\t0000000000000000\nCapBnd:\t0000000000000000\n")
			f.files["/proc/42/status"] = []byte("CapEff:\t0000000000000000\n")
		}, "当前进程 CAP_NET_ADMIN"},
	} {
		for _, enabled := range []bool{true, false} {
			t.Run(fmt.Sprintf("%s/tun=%t", tc.name, enabled), func(t *testing.T) {
				fixture := healthySystemFixture()
				tc.mutate(fixture)
				checks := systemChecksWithProbe([]Config{fixtureMihomo(t, enabled)}, fixture.probe())
				want := "warn"
				if enabled {
					want = "error"
				}
				if got := namedSystemCheck(t, checks, tc.check); got.Level != want || got.Fix == "" {
					t.Fatalf("wrong TUN diagnostic: %+v", got)
				}
				if tc.name == "regular-file" && fixture.tunCalls != 0 {
					t.Fatal("attempted ioctl on an ordinary file")
				}
			})
		}
	}
}

func TestSystemUnknownTUNConfigurationDoesNotClaimDisabled(t *testing.T) {
	for _, tc := range []struct {
		name string
		yaml string
	}{
		{"missing-core-config", ""},
		{"invalid-core-yaml", "tun: ["},
		{"tun-scalar", "tun: true\n"},
		{"tun-list", "tun: [true]\n"},
		{"tun-null", "tun: null\n"},
		{"enable-string", "tun:\n  enable: \"false\"\n"},
		{"enable-number", "tun:\n  enable: 0\n"},
		{"enable-null", "tun:\n  enable: null\n"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			cfg := fixtureMihomo(t, false)
			if tc.yaml == "" {
				if err := os.Remove(cfg.ConfigPath); err != nil {
					t.Fatal(err)
				}
			} else if err := os.WriteFile(cfg.ConfigPath, []byte(tc.yaml), 0600); err != nil {
				t.Fatal(err)
			}
			fixture := healthySystemFixture()
			delete(fixture.devices, "/dev/net/tun")
			fixture.files["/proc/self/status"] = []byte("CapEff:\t0\nCapBnd:\t0\n")
			probe := fixture.probe()
			probe.showUnit = func(string) ([]byte, error) {
				t.Fatal("service TUN requirements inferred from unknown core configuration")
				return nil, nil
			}
			checks := systemChecksWithProbe([]Config{cfg}, probe)
			service := namedSystemCheck(t, checks, "Mihomo 服务权限")
			if service.Level != "warn" || !strings.Contains(service.Message, "无法从核心配置判断") || service.Fix == "" {
				t.Fatalf("unknown TUN service requirements not reported: %+v", service)
			}
			for _, name := range []string{"TUN 设备", "当前进程 CAP_NET_ADMIN"} {
				got := namedSystemCheck(t, checks, name)
				if got.Level != "warn" || !strings.Contains(got.Message, "状态未知") {
					t.Fatalf("unknown TUN prerequisite diagnosed as a known mode: %+v", got)
				}
			}
			for _, check := range checks {
				if strings.Contains(check.Message, "未启用 TUN") {
					t.Fatalf("unknown TUN state reported as disabled: %+v", check)
				}
			}
		})
	}
}

func TestSystemOmittedTUNSettingsKeepDisabledDefault(t *testing.T) {
	for _, yaml := range []string{"mode: rule\n", "tun: {}\n", "tun:\n  stack: mixed\n"} {
		cfg := fixtureMihomo(t, false)
		if err := os.WriteFile(cfg.ConfigPath, []byte(yaml), 0600); err != nil {
			t.Fatal(err)
		}
		checks := systemChecksWithProbe([]Config{cfg}, healthySystemFixture().probe())
		if got := namedSystemCheck(t, checks, "Mihomo 服务权限"); got.Level != "pass" || !strings.Contains(got.Message, "未启用 TUN") {
			t.Fatalf("valid omitted TUN setting changed default behavior: %+v", got)
		}
	}
}

func TestSystemDNSOnlyDoesNotNeedTUNOrCapabilities(t *testing.T) {
	fixture := healthySystemFixture()
	fixture.devices = map[string]os.FileMode{}
	fixture.files["/proc/self/status"] = []byte("CapEff:\t0000000000000000\n")
	checks := systemChecksWithProbe([]Config{{ServiceType: "oxidns"}}, fixture.probe())
	if Report(checks).HasErrors() || fixture.tunCalls != 0 {
		t.Fatalf("DNS installation required TUN: %+v", checks)
	}
}

func TestSystemReportsUnknownCapabilitiesWithoutGuessingFromUID(t *testing.T) {
	fixture := healthySystemFixture()
	fixture.files["/proc/self/status"] = []byte("Uid:\t0\t0\t0\t0\n")
	checks := systemChecksWithProbe([]Config{fixtureMihomo(t, true)}, fixture.probe())
	if got := namedSystemCheck(t, checks, "当前进程 CAP_NET_ADMIN"); got.Level != "warn" || !strings.Contains(got.Message, "无法读取") {
		t.Fatalf("unproven permission treated as success: %+v", got)
	}
}

func TestSystemInstallerCapabilityDoesNotOverrideVerifiedCore(t *testing.T) {
	fixture := healthySystemFixture()
	fixture.files["/proc/self/status"] = []byte("CapEff:\t0\nCapBnd:\t0\n")
	checks := systemChecksWithProbe([]Config{fixtureMihomo(t, true)}, fixture.probe())
	if Report(checks).HasErrors() {
		t.Fatalf("installer permissions overrode verified core capabilities: %+v", checks)
	}
	if got := namedSystemCheck(t, checks, "当前进程 CAP_NET_ADMIN"); got.Level != "warn" || !strings.Contains(got.Message, "已核实") {
		t.Fatalf("missing distinction between installer and service: %+v", got)
	}

	fixture.files["/proc/self/status"] = []byte("CapEff:\t0\nCapBnd:\t1000\n")
	fixture.unit = strings.ReplaceAll(fixture.unit, "MainPID=42", "MainPID=0")
	checks = systemChecksWithProbe([]Config{fixtureMihomo(t, true)}, fixture.probe())
	if got := namedSystemCheck(t, checks, "当前进程 CAP_NET_ADMIN"); got.Level != "warn" || !strings.Contains(got.Message, "不能确认") {
		t.Fatalf("absence of an effective capability became proof it cannot be granted: %+v", got)
	}
}

func TestSystemCoreServiceSeparatesCapabilitiesAndSandbox(t *testing.T) {
	fixture := healthySystemFixture()
	fixture.unit = strings.ReplaceAll(fixture.unit, "cap_net_admin ", "") + "PrivateDevices=yes\nDevicePolicy=closed\nPrivateNetwork=yes\n"
	delete(fixture.devices, "/proc/42/root/dev/net/tun")
	checks := systemChecksWithProbe([]Config{fixtureMihomo(t, true)}, fixture.probe())
	for _, name := range []string{"Mihomo 服务权限上限", "Mihomo 设备视图"} {
		if got := namedSystemCheck(t, checks, name); got.Level != "error" {
			t.Fatalf("known core restriction did not block enabled TUN: %+v", got)
		}
	}
	for _, name := range []string{"Mihomo 设备隔离", "Mihomo 网络隔离"} {
		if got := namedSystemCheck(t, checks, name); got.Level != "warn" {
			t.Fatalf("sandbox uncertainty not reported: %+v", got)
		}
	}
	if got := namedSystemCheck(t, checks, "TUN 实际启动"); got.Level != "pass" || !strings.Contains(got.Message, "Meta") {
		t.Fatalf("verified core-owned interface was replaced with a generic sandbox warning: %+v", got)
	}
	if got := namedSystemCheck(t, checks, "当前进程 CAP_NET_ADMIN"); got.Level != "pass" {
		t.Fatalf("confused installer and core permissions: %+v", got)
	}
}

func TestSystemCoreMissingPIDOrDroppedCapabilityIsUncertain(t *testing.T) {
	for _, mode := range []string{"stopped", "dropped", "openrc", "unreadable", "exited"} {
		t.Run(mode, func(t *testing.T) {
			fixture := healthySystemFixture()
			cfg := fixtureMihomo(t, true)
			wantCheck := "Mihomo 运行权限"
			switch mode {
			case "stopped":
				fixture.unit = strings.ReplaceAll(fixture.unit, "MainPID=42", "MainPID=0")
			case "dropped":
				fixture.files["/proc/42/status"] = []byte("CapEff:\t0\n")
			case "openrc":
				cfg.ServiceManager = "openrc"
				wantCheck = "Mihomo 服务权限"
			case "unreadable":
				fixture.unitErr = os.ErrPermission
				wantCheck = "Mihomo 服务权限"
			case "exited":
				delete(fixture.files, "/proc/42/status")
				delete(fixture.files, "/proc/42/stat")
				delete(fixture.devices, "/proc/42/root/dev/net/tun")
			}
			checks := systemChecksWithProbe([]Config{cfg}, fixture.probe())
			if Report(checks).HasErrors() {
				t.Fatalf("uncertainty became a false hard failure: %+v", checks)
			}
			if got := namedSystemCheck(t, checks, wantCheck); got.Level != "warn" {
				t.Fatalf("unknown service permissions silently accepted: %+v", got)
			}
		})
	}
}

func TestSystemForwardingZeroWarnsWithoutAssumingGatewayRole(t *testing.T) {
	fixture := healthySystemFixture()
	fixture.files["/proc/sys/net/ipv4/ip_forward"] = []byte("0\n")
	checks := inspectIPv4Forwarding(fixture.probe())
	check := namedSystemCheck(t, checks, "IPv4 转发")
	if check.Level != "warn" || !strings.Contains(check.Message, "仅本机 TUN") || !strings.Contains(check.Fix, "PVE 仅做二层网桥") {
		t.Fatalf("incorrect gateway implication: %+v", check)
	}
}

func TestSystemForwardingPersistenceHonorsPrecedenceAndUncertainty(t *testing.T) {
	for _, tc := range []struct {
		name    string
		files   map[string][]byte
		level   string
		message string
	}{
		{"later-disables", map[string][]byte{"/etc/sysctl.d/10-enable.conf": []byte("net.ipv4.ip_forward=1"), "/usr/lib/sysctl.d/99-disable.conf": []byte("net.ipv4.ip_forward=0")}, "warn", "ip_forward=0"},
		{"same-name-etc-wins", map[string][]byte{"/etc/sysctl.d/99-forward.conf": []byte("net.ipv4.ip_forward=1"), "/usr/lib/sysctl.d/99-forward.conf": []byte("net.ipv4.ip_forward=0")}, "pass", "重启后仍需验证"},
		{"last-line-wins", map[string][]byte{"/etc/sysctl.d/99-forward.conf": []byte("net.ipv4.ip_forward=1\nnet/ipv4/ip_forward=0\n")}, "warn", "ip_forward=0"},
		{"legacy-only", map[string][]byte{"/etc/sysctl.conf": []byte("net.ipv4.ip_forward=1")}, "warn", "取决于发行版"},
		{"legacy-conflict", map[string][]byte{"/etc/sysctl.d/99-forward.conf": []byte("net.ipv4.ip_forward=1"), "/etc/sysctl.conf": []byte("net.ipv4.ip_forward=0")}, "warn", "设置不一致"},
		{"runtime-only", map[string][]byte{"/run/sysctl.d/99-forward.conf": []byte("net.ipv4.ip_forward=1")}, "warn", "临时目录 /run"},
		{"comment-only", map[string][]byte{"/etc/sysctl.d/99-forward.conf": []byte("# net.ipv4.ip_forward=1")}, "warn", "未在常见"},
		{"masked-vendor", map[string][]byte{"/etc/sysctl.d/99-forward.conf": nil, "/usr/lib/sysctl.d/99-forward.conf": []byte("net.ipv4.ip_forward=1")}, "warn", "未在常见"},
		{"legacy-lib-directory", map[string][]byte{"/lib/sysctl.d/99-forward.conf": []byte("/net/ipv4/ip_forward=1")}, "pass", "重启后仍需验证"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			fixture := healthySystemFixture()
			fixture.files = tc.files
			check := inspectForwardingPersistence(fixture.probe())
			if check.Level != tc.level || !strings.Contains(check.Message, tc.message) {
				t.Fatalf("persistence overclaimed or ignored precedence: %+v", check)
			}
		})
	}
}

func TestSystemUnsupportedPlatformDoesNotProbeLinuxDevices(t *testing.T) {
	checks := systemChecksWithProbe(nil, systemProbe{goos: "darwin"})
	if len(checks) != 1 || checks[0].Level != "warn" {
		t.Fatalf("unexpected non-Linux result: %+v", checks)
	}
}

func TestSystemTUNListenersRequireDeviceAndCapabilities(t *testing.T) {
	for _, tc := range []struct {
		name    string
		content string
		active  bool
	}{
		{"listener-only", "listeners: [{name: tun-in, type: tun, stack: mixed}]", true},
		{"top-level-disabled", "tun: {enable: false}\nlisteners: [{name: tun-in, type: tun}]", true},
		{"explicit-active", "listeners: [{name: tun-in, type: tun, disabled: false}]", true},
		{"disabled-listener", "listeners: [{name: tun-in, type: tun, disabled: true}]", false},
		{"ordinary-proxy", "listeners: [{name: proxy-in, type: mixed, port: 7890}]", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			cfg := fixtureMihomo(t, false)
			if err := os.WriteFile(cfg.ConfigPath, []byte(tc.content), 0600); err != nil {
				t.Fatal(err)
			}
			fixture := healthySystemFixture()
			delete(fixture.devices, "/dev/net/tun")
			fixture.files["/proc/self/status"] = []byte("CapEff:\t0\nCapBnd:\t0\n")
			fixture.files["/proc/42/status"] = []byte("CapEff:\t0\n")
			fixture.unit = "MainPID=42\nCapabilityBoundingSet=cap_net_bind_service\n"
			checks := systemChecksWithProbe([]Config{cfg}, fixture.probe())
			want := "warn"
			if tc.active {
				want = "error"
			}
			for _, name := range []string{"TUN 设备", "当前进程 CAP_NET_ADMIN"} {
				check := namedSystemCheck(t, checks, name)
				if check.Level != want || tc.active && strings.Contains(check.Message, "未启用 TUN") {
					t.Fatalf("listener prerequisites misclassified: %+v", check)
				}
			}
			if tc.active {
				if check := namedSystemCheck(t, checks, "Mihomo 服务权限上限"); check.Level != "error" {
					t.Fatalf("active listener skipped core service permissions: %+v", check)
				}
			}
		})
	}
}

func TestSystemTUNListenerInspectsActualCoreInterfaceReadOnly(t *testing.T) {
	for _, content := range []string{
		"listeners: [{name: tun-in, type: tun, stack: mixed}]\n",
		"tun: {enable: false}\nlisteners: [{name: tun-in, type: tun}]\n",
	} {
		for _, held := range []bool{true, false} {
			t.Run(fmt.Sprintf("%s/held=%t", strings.Split(content, "\n")[0], held), func(t *testing.T) {
				cfg := fixtureMihomo(t, false)
				if err := os.WriteFile(cfg.ConfigPath, []byte(content), 0600); err != nil {
					t.Fatal(err)
				}
				fixture := healthySystemFixture()
				if !held {
					// Keep Meta in net/dev: its mere presence must not establish
					// that the current Mihomo core owns the interface.
					delete(fixture.files, "/proc/42/fdinfo/7")
				}
				probe := fixture.probe()
				readDirectory := probe.readDir
				fdScans := 0
				probe.readDir = func(path string) ([]os.DirEntry, error) {
					if path == "/proc/42/fdinfo" {
						fdScans++
					}
					return readDirectory(path)
				}
				checks := systemChecksWithProbe([]Config{cfg}, probe)
				check := namedSystemCheck(t, checks, "TUN 实际启动")
				if fdScans != 1 {
					t.Fatalf("active TUN listener skipped actual ownership inspection: %d scans", fdScans)
				}
				if held {
					if check.Level != "pass" || !strings.Contains(check.Message, "Meta") || !strings.Contains(check.Message, "42") {
						t.Fatalf("listener's live core-owned interface not confirmed: %+v", check)
					}
				} else if check.Level != "warn" || !strings.Contains(check.Message, "TUN") || check.Fix == "" {
					t.Fatalf("unowned interface incorrectly passed or lost repair advice: %+v", check)
				}
				if after, err := os.ReadFile(cfg.ConfigPath); err != nil || string(after) != content {
					t.Fatalf("read-only listener check changed core configuration: %v", err)
				}
			})
		}
	}
}

func TestSystemMalformedTUNListenerDoesNotClaimDisabled(t *testing.T) {
	for _, content := range []string{
		"listeners: invalid",
		"listeners: [invalid]",
		"listeners: [{name: tun-in, type: tun, disabled: 'false'}]",
	} {
		cfg := fixtureMihomo(t, false)
		if err := os.WriteFile(cfg.ConfigPath, []byte(content), 0600); err != nil {
			t.Fatal(err)
		}
		fixture := healthySystemFixture()
		delete(fixture.devices, "/dev/net/tun")
		checks := systemChecksWithProbe([]Config{cfg}, fixture.probe())
		check := namedSystemCheck(t, checks, "TUN 设备")
		if check.Level != "warn" || !strings.Contains(check.Message, "状态未知") {
			t.Fatalf("malformed listener incorrectly inferred disabled: %s %+v", content, check)
		}
	}
}

func TestSystemOpenRCPermissionsDoNotRecommendSystemdOptions(t *testing.T) {
	for _, mode := range []string{"healthy", "device-denied", "installer-capability-missing", "installer-capability-unreadable"} {
		t.Run(mode, func(t *testing.T) {
			fixture := healthySystemFixture()
			cfg := fixtureMihomo(t, true)
			cfg.ServiceManager = "openrc"
			switch mode {
			case "device-denied":
				fixture.tunErr = os.ErrPermission
			case "installer-capability-missing":
				fixture.files["/proc/self/status"] = []byte("Uid:\t0\t0\t0\t0\nCapEff:\t0\nCapBnd:\t0\n")
			case "installer-capability-unreadable":
				fixture.fileErr["/proc/self/status"] = os.ErrPermission
			}
			probe := fixture.probe()
			probe.showUnit = func(string) ([]byte, error) {
				t.Fatal("OpenRC diagnostic invoked systemctl")
				return nil, nil
			}
			checks := systemChecksWithProbe([]Config{cfg}, probe)
			for _, check := range checks {
				text := check.Message + " " + check.Fix
				for _, systemdOnly := range []string{"systemd", "systemctl", "CapabilityBoundingSet", "AmbientCapabilities", "DeviceAllow", "PrivateDevices"} {
					if strings.Contains(text, systemdOnly) {
						t.Fatalf("OpenRC report recommends %s: %+v", systemdOnly, check)
					}
				}
			}
			service := namedSystemCheck(t, checks, "Mihomo 服务权限")
			if service.Level != "warn" || !strings.Contains(service.Message, "尚未取得并验证") || !strings.Contains(service.Message, "不能代表核心服务") {
				t.Fatalf("OpenRC service permissions inferred from checking process: %+v", service)
			}
			for _, expected := range []string{"/etc/init.d/", "/etc/conf.d/", "command_user", "capabilities", "/proc/<PID>/status", "CapEff", "/dev/net/tun"} {
				if !strings.Contains(service.Fix, expected) {
					t.Fatalf("missing OpenRC guidance %q: %+v", expected, service)
				}
			}
			if mode == "installer-capability-missing" {
				check := namedSystemCheck(t, checks, "当前进程 CAP_NET_ADMIN")
				if check.Level == "pass" || !strings.Contains(check.Message, "未持有") {
					t.Fatalf("root incorrectly implied CAP_NET_ADMIN: %+v", check)
				}
			}
		})
	}
}

func TestSystemNonSystemdCoreDoesNotQueryUnitOrInferEffectiveCapability(t *testing.T) {
	for _, manager := range []string{"openrc", "custom", ""} {
		t.Run(manager, func(t *testing.T) {
			cfg := fixtureMihomo(t, true)
			cfg.ServiceManager = manager
			probe := healthySystemFixture().probe()
			probe.showUnit = func(string) ([]byte, error) {
				t.Fatal("non-systemd service invoked systemctl")
				return nil, nil
			}
			checks, verifiedCapability := inspectCoreService(probe, cfg, tunEnabled)
			if verifiedCapability {
				t.Fatal("checking process capability substituted for an unidentified core process")
			}
			check := namedSystemCheck(t, checks, "Mihomo 服务权限")
			if check.Level != "warn" || strings.Contains(check.Message+check.Fix, "systemd") {
				t.Fatalf("inaccurate service-manager diagnostic: %+v", check)
			}
		})
	}
}

func TestSystemSystemdPermissionFailuresKeepApplicableGuidance(t *testing.T) {
	for _, mode := range []string{"unit-unreadable", "unit-unspecified", "bounding-capability-missing"} {
		t.Run(mode, func(t *testing.T) {
			fixture := healthySystemFixture()
			cfg := fixtureMihomo(t, true)
			wantName := "Mihomo 服务权限"
			switch mode {
			case "unit-unreadable":
				fixture.unitErr = os.ErrPermission
			case "unit-unspecified":
				cfg.ServiceUnit = ""
			case "bounding-capability-missing":
				fixture.unit = strings.ReplaceAll(fixture.unit, "cap_net_admin ", "")
				wantName += "上限"
			}
			checks, _ := inspectCoreService(fixture.probe(), cfg, tunEnabled)
			check := namedSystemCheck(t, checks, wantName)
			for _, required := range []string{"CapabilityBoundingSet", "AmbientCapabilities", "DeviceAllow", "CapEff"} {
				if !strings.Contains(check.Fix, required) {
					t.Fatalf("systemd diagnostic lost %q: %+v", required, check)
				}
			}
			if strings.Contains(check.Fix, "command_user") {
				t.Fatalf("systemd diagnostic recommends OpenRC options: %+v", check)
			}
		})
	}
}
