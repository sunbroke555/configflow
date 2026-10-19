package preflight

import (
	"bytes"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

type tunPrepareFixture struct {
	files        map[string][]byte
	modes        map[string]os.FileMode
	readErrors   map[string]error
	commands     map[string]bool
	runErrors    map[string]error
	runOutput    map[string][]byte
	calls        []string
	writes       []string
	flags        uint32
	tunErr       error
	writeErr     error
	noWrite      bool
	noLoad       bool
	noBoot       bool
	namespace    string
	namespaceErr error
	pauses       int
	delayedLoad  bool
}

func newTUNPrepareFixture() *tunPrepareFixture {
	return &tunPrepareFixture{
		files: map[string][]byte{
			"/proc/1/environ":            []byte("HOME=/\x00"),
			"/proc/1/cgroup":             []byte("0::/init.scope\n"),
			"/proc/self/cgroup":          []byte("0::/system.slice/ssh.service\n"),
			"/proc/self/status":          []byte("CapEff:\t0000000000011000\n"),
			"/proc/modules":              []byte("tun 65536 0 - Live 0\n"),
			"/proc/sys/kernel/osrelease": []byte("6.12.0-test\n"),
			"/etc/init.d/modules":        []byte("#!/sbin/openrc-run\nfor f in /etc/modules-load.d/*.conf; do modprobe tun; done\n"),
		},
		modes:      map[string]os.FileMode{"/dev/net/tun": os.ModeDevice | os.ModeCharDevice | 0600, "/etc": os.ModeDir | 0755, "/etc/modules-load.d": os.ModeDir | 0755},
		readErrors: map[string]error{}, commands: map[string]bool{"modprobe": true},
		runErrors: map[string]error{}, runOutput: map[string][]byte{}, flags: 1, namespace: "pid:[4026531836]",
	}
}

func (f *tunPrepareFixture) read(path string) ([]byte, error) {
	if err := f.readErrors[path]; err != nil {
		return nil, err
	}
	data, ok := f.files[path]
	if !ok {
		return nil, os.ErrNotExist
	}
	return append([]byte(nil), data...), nil
}

func (f *tunPrepareFixture) probe() tunPrepareProbe {
	stat := func(path string) (os.FileInfo, error) {
		if mode, ok := f.modes[path]; ok {
			return fixtureDevice{mode}, nil
		}
		if _, ok := f.files[path]; ok {
			return fixtureDevice{0644}, nil
		}
		return nil, os.ErrNotExist
	}
	return tunPrepareProbe{
		goos: "linux", readFile: f.read, stat: stat, lstat: stat,
		readlink: func(string) (string, error) { return f.namespace, f.namespaceErr },
		pause: func(time.Duration) {
			f.pauses++
			if f.delayedLoad {
				f.modes["/dev/net/tun"] = os.ModeDevice | os.ModeCharDevice | 0600
			}
		},
		tun: func(string) (uint32, error) { return f.flags, f.tunErr },
		lookPath: func(name string) (string, error) {
			if f.commands[name] {
				return "/mock/" + name, nil
			}
			return "", os.ErrNotExist
		},
		run: func(name string, args ...string) ([]byte, error) {
			key := strings.Join(append([]string{name}, args...), " ")
			f.calls = append(f.calls, key)
			if err := f.runErrors[key]; err != nil {
				return f.runOutput[key], err
			}
			if output, ok := f.runOutput[key]; ok {
				return output, nil
			}
			switch key {
			case "modprobe tun":
				if !f.noLoad {
					f.modes["/dev/net/tun"] = os.ModeDevice | os.ModeCharDevice | 0600
					f.files["/proc/modules"] = []byte("tun 65536 0 - Live 0\n")
				}
			case "systemctl show systemd-modules-load.service --property=LoadState --value":
				return []byte("loaded\n"), nil
			case "rc-update add modules boot":
				if !f.noBoot {
					f.files["/etc/runlevels/boot/modules"] = append([]byte(nil), f.files["/etc/init.d/modules"]...)
				}
			default:
				return nil, fmt.Errorf("unexpected mock command: %s", key)
			}
			return nil, nil
		},
		persist: func(path string, before, after []byte, existed bool) error {
			if f.writeErr != nil {
				return f.writeErr
			}
			previous, ok := f.files[path]
			if ok != existed || !bytes.Equal(previous, before) {
				return fmt.Errorf("concurrent change")
			}
			f.writes = append(f.writes, path)
			if !f.noWrite {
				f.files[path] = append([]byte(nil), after...)
			}
			return nil
		},
	}
}

func tunPrepareConfig(t *testing.T, yaml, manager string) Config {
	t.Helper()
	path := filepath.Join(t.TempDir(), "config.yaml")
	if err := os.WriteFile(path, []byte(yaml), 0600); err != nil {
		t.Fatal(err)
	}
	return Config{ServiceType: "mihomo", ConfigPath: path, NativeAIO: true, ServiceManager: manager}
}

func assertTUNReport(t *testing.T, report Report, wantError bool, contains string) {
	t.Helper()
	if report.HasErrors() != wantError || !strings.Contains(fmt.Sprint(report), contains) {
		t.Fatalf("unexpected report: %+v", report)
	}
}

func TestPrepareTUNSkipsDisabledBeforeInspectingHost(t *testing.T) {
	for _, yaml := range []string{
		"mode: rule\n", "tun: {}\n", "tun: {enable: false}\n",
		"listeners: []\n",
		"listeners: null\n",
		"listeners: [{type: http, port: 8080}]\n",
		"listeners: [{type: tun, disabled: true}]\n",
		"tun: {enable: false}\nlisteners: [{type: tun, disabled: true}]\n",
	} {
		t.Run(yaml, func(t *testing.T) {
			cfg := tunPrepareConfig(t, yaml, "openrc")
			// Nil callbacks intentionally prove the disabled branch does not inspect
			// this test host, even on macOS or without installation privileges.
			report := prepareTUNWithProbe(cfg, tunPrepareProbe{goos: "darwin"})
			assertTUNReport(t, report, false, "跳过")
		})
	}
}

func TestPrepareTUNRejectsAmbiguousConfigurationWithoutHostAccess(t *testing.T) {
	for _, yaml := range []string{
		"", "[]", "tun: null\n", "tun: []\n", "tun: {enable: 'true'}\n", "tun: {enable: 1}\n", "tun: {enable: null}\n", "tun: [",
		"listeners: {}\n",
		"listeners: [null]\n",
		"listeners: [{type: tun, disabled: 'false'}]\n",
		"listeners: [{type: tun, disabled: null}]\n",
		"tun: {enable: true}\nlisteners: {}\n",
		"tun: null\nlisteners: [{type: tun}]\n",
		"listeners: [{type: tun}, {type: tun, disabled: invalid}]\n",
		"listeners: [{type: tun, disabled: invalid}, {type: tun}]\n",
	} {
		t.Run(yaml, func(t *testing.T) {
			cfg := tunPrepareConfig(t, yaml, "systemd")
			if report := prepareTUNWithProbe(cfg, tunPrepareProbe{}); !report.HasErrors() {
				t.Fatalf("invalid config accepted: %+v", report)
			}
		})
	}
}

func TestPrepareTUNActiveListenersPrepareModule(t *testing.T) {
	for _, manager := range []string{"systemd", "openrc"} {
		for _, yaml := range []string{
			"listeners: [{type: tun}]\n",
			"listeners: [{type: tun, disabled: false}]\n",
			"tun: {enable: false}\nlisteners: [{type: tun}]\n",
			"tun: {}\nlisteners: [{type: tun}]\n",
			"listeners: [{type: tun, disabled: true}, {type: tun}]\n",
			"tun: {enable: true}\nlisteners: [{type: tun, disabled: true}]\n",
		} {
			t.Run(manager+"/"+yaml, func(t *testing.T) {
				f := newTUNPrepareFixture()
				delete(f.modes, "/dev/net/tun")
				f.files["/proc/modules"] = nil
				cfg := tunPrepareConfig(t, yaml, manager)
				assertTUNReport(t, prepareTUNWithProbe(cfg, f.probe()), false, "开机加载配置")
				if strings.Count(strings.Join(f.calls, "\n"), "modprobe tun") != 1 || len(f.writes) != 1 || string(f.files[tunModulesFile]) != tunModulesContent {
					t.Fatalf("active listener not prepared: commands=%v writes=%v", f.calls, f.writes)
				}
			})
		}
	}
}

func TestPrepareTUNNativeLoadsMissingDriverAndPersistsOnce(t *testing.T) {
	for _, manager := range []string{"systemd", "openrc"} {
		t.Run(manager, func(t *testing.T) {
			f := newTUNPrepareFixture()
			delete(f.modes, "/dev/net/tun")
			f.files["/proc/modules"] = nil
			f.files["/etc/modules"] = []byte("# user modules\nvirtio_net\n")
			cfg := tunPrepareConfig(t, "tun: {enable: true}\n", manager)
			for attempt := 0; attempt < 2; attempt++ {
				assertTUNReport(t, prepareTUNWithProbe(cfg, f.probe()), false, "开机加载配置")
			}
			if strings.Count(strings.Join(f.calls, "\n"), "modprobe tun") != 1 || len(f.writes) != 1 {
				t.Fatalf("not idempotent: commands=%v writes=%v", f.calls, f.writes)
			}
			if string(f.files[tunModulesFile]) != tunModulesContent || string(f.files["/etc/modules"]) != "# user modules\nvirtio_net\n" {
				t.Fatalf("unexpected persistence: %v", f.files)
			}
			if manager == "openrc" && strings.Count(strings.Join(f.calls, "\n"), "rc-update add modules boot") != 1 {
				t.Fatalf("OpenRC startup not enabled exactly once: %v", f.calls)
			}
		})
	}
}

func TestPrepareTUNHealthyModularDeviceStillGetsBootPersistence(t *testing.T) {
	f := newTUNPrepareFixture()
	cfg := tunPrepareConfig(t, "tun: {enable: true}", "openrc")
	assertTUNReport(t, prepareTUNWithProbe(cfg, f.probe()), false, "开机加载配置")
	if strings.Contains(strings.Join(f.calls, " "), "modprobe") || len(f.writes) != 1 {
		t.Fatalf("unexpected commands=%v writes=%v", f.calls, f.writes)
	}
}

func TestPrepareTUNOpenRCLoadedModuleWithoutModprobeCannotClaimPersistence(t *testing.T) {
	for _, legacy := range []bool{false, true} {
		for _, alreadyPersistent := range []bool{false, true} {
			t.Run(fmt.Sprintf("legacy=%v/persistent=%v", legacy, alreadyPersistent), func(t *testing.T) {
				f := newTUNPrepareFixture()
				f.commands["modprobe"] = false
				moduleFile := tunModulesFile
				if legacy {
					f.files["/etc/init.d/modules"] = []byte("#!/sbin/openrc-run\ncat /etc/modules | while read m; do modprobe $m; done\n")
					moduleFile = "/etc/modules"
					f.files[moduleFile] = []byte("# administrator list\nvirtio_net\n")
				}
				if alreadyPersistent {
					f.files[moduleFile] = append(f.files[moduleFile], []byte("tun\n")...)
					f.files["/etc/runlevels/boot/modules"] = append([]byte(nil), f.files["/etc/init.d/modules"]...)
				}
				before := make(map[string][]byte, len(f.files))
				for path, content := range f.files {
					before[path] = append([]byte(nil), content...)
				}
				cfg := tunPrepareConfig(t, "tun: {enable: true}", "openrc")
				report := prepareTUNWithProbe(cfg, f.probe())
				assertTUNReport(t, report, true, "OpenRC modules 开机加载需要 modprobe")
				assertTUNReport(t, report, true, "请安装 kmod 或修复 PATH 后重试")
				assertTUNReport(t, report, true, "未完成开机持久化")
				if len(f.writes) != 0 || len(f.calls) != 0 {
					t.Fatalf("missing modprobe changed host: calls=%v writes=%v", f.calls, f.writes)
				}
				if len(f.files) != len(before) {
					t.Fatalf("missing modprobe added or removed files: %v", f.files)
				}
				for path, content := range before {
					if !bytes.Equal(f.files[path], content) {
						t.Fatalf("missing modprobe changed %s", path)
					}
				}
			})
		}
	}
}

func TestPrepareTUNSystemdLoadedModuleDoesNotRequireModprobe(t *testing.T) {
	f := newTUNPrepareFixture()
	f.commands["modprobe"] = false
	cfg := tunPrepareConfig(t, "tun: {enable: true}", "systemd")
	assertTUNReport(t, prepareTUNWithProbe(cfg, f.probe()), false, "开机加载配置")
	if strings.Join(f.calls, "\n") != "systemctl show systemd-modules-load.service --property=LoadState --value" {
		t.Fatalf("systemd used unexpected module loader: %v", f.calls)
	}
	if len(f.writes) != 1 || string(f.files[tunModulesFile]) != tunModulesContent {
		t.Fatalf("systemd module persistence missing: %v %v", f.writes, f.files)
	}
}

func TestPrepareTUNBuiltinNeedsNoModulePersistence(t *testing.T) {
	for _, source := range []string{"builtin-index", "modinfo"} {
		for _, manager := range []string{"systemd", "openrc"} {
			t.Run(source+"/"+manager, func(t *testing.T) {
				f := newTUNPrepareFixture()
				f.commands["modprobe"] = false
				f.files["/proc/modules"] = []byte("other 100 0 - Live 0\n")
				if source == "builtin-index" {
					f.files["/lib/modules/6.12.0-test/modules.builtin"] = []byte("kernel/drivers/net/tun.ko\n")
				} else {
					f.commands["modinfo"] = true
					f.runOutput["modinfo -F filename tun"] = []byte("(builtin)\n")
				}
				cfg := tunPrepareConfig(t, "tun: {enable: true}", manager)
				assertTUNReport(t, prepareTUNWithProbe(cfg, f.probe()), false, "内置")
				if len(f.writes) != 0 || strings.Contains(strings.Join(f.calls, " "), "modprobe") {
					t.Fatalf("builtin mutated: %v %v", f.calls, f.writes)
				}
			})
		}
	}
}

func TestPrepareTUNUnknownDriverTypeDoesNotAssumeBuiltin(t *testing.T) {
	f := newTUNPrepareFixture()
	f.readErrors["/proc/modules"] = os.ErrPermission
	f.readErrors["/lib/modules/6.12.0-test/modules.builtin"] = os.ErrPermission
	cfg := tunPrepareConfig(t, "tun: {enable: true}", "systemd")
	assertTUNReport(t, prepareTUNWithProbe(cfg, f.probe()), true, "无法确认开机加载方式")
	if len(f.writes) != 0 {
		t.Fatal("unknown driver created boot config")
	}
}

func TestPrepareTUNContainersNeverLoadOrPersistHostModule(t *testing.T) {
	cases := []struct{ name, path, value, kind string }{
		{"lxc", "/run/systemd/container", "lxc\n", "lxc"},
		{"docker-marker", "/.dockerenv", "", "docker"},
		{"podman-marker", "/run/.containerenv", "", "podman"},
		{"environment", "/proc/1/environ", "PATH=/bin\x00container=lxc\x00", "lxc"},
		{"cgroup", "/proc/1/cgroup", "0::/docker/abc\n", "docker"},
		{"self-cgroup", "/proc/self/cgroup", "0::/kubepods/burstable/abc\n", "kubepods"},
		{"unknown-manager", "/run/host/container-manager", "custom\n", "未知类型"},
	}
	for _, tc := range cases {
		for _, available := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s/available=%v", tc.name, available), func(t *testing.T) {
				f := newTUNPrepareFixture()
				f.commands["modprobe"] = false
				f.files[tc.path] = []byte(tc.value)
				if !available {
					delete(f.modes, "/dev/net/tun")
				}
				cfg := tunPrepareConfig(t, "tun: {enable: true}", "openrc")
				report := prepareTUNWithProbe(cfg, f.probe())
				assertTUNReport(t, report, !available, tc.kind)
				if len(f.calls) != 0 || len(f.writes) != 0 {
					t.Fatalf("container mutated: %v %v", f.calls, f.writes)
				}
				if tc.kind != "lxc" && strings.Contains(fmt.Sprint(report), "pct") {
					t.Fatal("non-LXC got PVE advice")
				}
			})
		}
	}
}

type tunCommandExit int

func (e tunCommandExit) Error() string { return fmt.Sprintf("exit %d", e) }
func (e tunCommandExit) ExitCode() int { return int(e) }

func TestPrepareTUNContainerDetectionFailsClosedAndHandlesNativeVM(t *testing.T) {
	for _, tc := range []struct {
		name      string
		setup     func(*tunPrepareFixture)
		wantError bool
	}{
		{"native-openrc", func(f *tunPrepareFixture) {}, false},
		{"no-capability", func(f *tunPrepareFixture) { f.files["/proc/self/status"] = []byte("CapEff:\t0\n") }, true},
		{"unreadable-proc", func(f *tunPrepareFixture) { f.readErrors["/proc/1/environ"] = os.ErrPermission }, true},
		{"unreadable-marker", func(f *tunPrepareFixture) { f.readErrors["/run/systemd/container"] = os.ErrPermission }, true},
		{"unreadable-namespace", func(f *tunPrepareFixture) { f.namespaceErr = os.ErrPermission }, true},
		{"invalid-namespace", func(f *tunPrepareFixture) { f.namespace = "unrecognized" }, true},
		{"native-vm-detected", func(f *tunPrepareFixture) {
			f.commands["systemd-detect-virt"] = true
			f.runOutput["systemd-detect-virt --container"] = []byte("none\n")
			f.runErrors["systemd-detect-virt --container"] = tunCommandExit(1)
		}, false},
		{"detector-error", func(f *tunPrepareFixture) {
			f.commands["systemd-detect-virt"] = true
			f.runErrors["systemd-detect-virt --container"] = errors.New("timeout")
		}, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := newTUNPrepareFixture()
			tc.setup(f)
			cfg := tunPrepareConfig(t, "tun: {enable: true}", "openrc")
			report := prepareTUNWithProbe(cfg, f.probe())
			if report.HasErrors() != tc.wantError {
				t.Fatalf("unexpected report: %+v", report)
			}
			if tc.wantError && len(f.writes) != 0 {
				t.Fatal("uncertain container detection mutated persistence")
			}
		})
	}
}

func TestPrepareTUNHiddenPrivilegedContainerAndOpenVZNeverLoadModules(t *testing.T) {
	for _, setup := range []func(*tunPrepareFixture){
		func(f *tunPrepareFixture) { f.namespace = "pid:[4026534000]" },
		func(f *tunPrepareFixture) { f.modes["/proc/vz"] = os.ModeDir | 0555 },
	} {
		f := newTUNPrepareFixture()
		setup(f)
		delete(f.modes, "/dev/net/tun")
		cfg := tunPrepareConfig(t, "tun: {enable: true}", "openrc")
		assertTUNReport(t, prepareTUNWithProbe(cfg, f.probe()), true, "容器")
		if len(f.calls) != 0 || len(f.writes) != 0 {
			t.Fatalf("hidden container mutated: %v %v", f.calls, f.writes)
		}
	}
}

func TestPrepareTUNRejectsInvalidDedicatedModuleLinesAndSymlinkDirectory(t *testing.T) {
	for _, data := range []string{"tun bad_option\n", "tun # inline comment\n", "tun\nother_module\n"} {
		t.Run(data, func(t *testing.T) {
			f := newTUNPrepareFixture()
			f.files[tunModulesFile] = []byte(data)
			cfg := tunPrepareConfig(t, "tun: {enable: true}", "systemd")
			assertTUNReport(t, prepareTUNWithProbe(cfg, f.probe()), true, "拒绝覆盖")
			if len(f.writes) != 0 || string(f.files[tunModulesFile]) != data {
				t.Fatal("foreign module list modified")
			}
		})
	}
	f := newTUNPrepareFixture()
	f.files[tunModulesFile] = []byte("tun\n")
	f.modes["/etc/modules-load.d"] = os.ModeSymlink | 0777
	cfg := tunPrepareConfig(t, "tun: {enable: true}", "systemd")
	assertTUNReport(t, prepareTUNWithProbe(cfg, f.probe()), true, "持久化目录不是普通目录")
	if len(f.writes) != 0 {
		t.Fatal("symlink directory modified")
	}
}

func TestPrepareTUNDeviceManagerDelayIsBoundedAndReadOnly(t *testing.T) {
	for _, delayed := range []bool{false, true} {
		t.Run(fmt.Sprint(delayed), func(t *testing.T) {
			f := newTUNPrepareFixture()
			delete(f.modes, "/dev/net/tun")
			f.noLoad, f.delayedLoad = true, delayed
			cfg := tunPrepareConfig(t, "tun: {enable: true}", "systemd")
			report := prepareTUNWithProbe(cfg, f.probe())
			if report.HasErrors() == delayed || f.pauses < 1 || f.pauses > 4 {
				t.Fatalf("incorrect bounded retry: pauses=%d report=%+v", f.pauses, report)
			}
			if strings.Contains(strings.Join(f.calls, " "), "udevadm") || strings.Contains(strings.Join(f.calls, " "), "mknod") {
				t.Fatal("device creation command used")
			}
		})
	}
}

func TestPrepareTUNModuleLoadFailuresDoNotPersist(t *testing.T) {
	for _, tc := range []struct {
		name  string
		setup func(*tunPrepareFixture)
	}{
		{"missing-modprobe", func(f *tunPrepareFixture) { f.commands["modprobe"] = false }},
		{"failed-modprobe", func(f *tunPrepareFixture) { f.runErrors["modprobe tun"] = os.ErrPermission }},
		{"no-device-after-load", func(f *tunPrepareFixture) { f.noLoad = true }},
		{"device-access-denied", func(f *tunPrepareFixture) { f.tunErr = os.ErrPermission }},
		{"no-iff-tun", func(f *tunPrepareFixture) { f.flags = 2 }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := newTUNPrepareFixture()
			delete(f.modes, "/dev/net/tun")
			tc.setup(f)
			cfg := tunPrepareConfig(t, "tun: {enable: true}", "systemd")
			if report := prepareTUNWithProbe(cfg, f.probe()); !report.HasErrors() {
				t.Fatalf("failure reported success: %+v", report)
			}
			if len(f.writes) != 0 {
				t.Fatal("failed device validation persisted module")
			}
		})
	}
}

func TestPrepareTUNPersistenceFailureReportsLoadedButNotPersistent(t *testing.T) {
	for _, tc := range []struct {
		name  string
		setup func(*tunPrepareFixture)
	}{
		{"write-denied", func(f *tunPrepareFixture) { f.writeErr = os.ErrPermission }},
		{"write-no-effect", func(f *tunPrepareFixture) { f.noWrite = true }},
		{"foreign-file", func(f *tunPrepareFixture) { f.files[tunModulesFile] = []byte("other_module\n") }},
		{"symlink-file", func(f *tunPrepareFixture) { f.modes[tunModulesFile] = os.ModeSymlink | 0777 }},
		{"service-masked", func(f *tunPrepareFixture) {
			f.runOutput["systemctl show systemd-modules-load.service --property=LoadState --value"] = []byte("masked\n")
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := newTUNPrepareFixture()
			delete(f.modes, "/dev/net/tun")
			tc.setup(f)
			cfg := tunPrepareConfig(t, "tun: {enable: true}", "systemd")
			assertTUNReport(t, prepareTUNWithProbe(cfg, f.probe()), true, "本次已加载并验证 TUN，但未完成开机持久化")
			if strings.Contains(strings.Join(f.calls, " "), "modprobe -r") {
				t.Fatal("failure unloaded module")
			}
		})
	}
}

func TestPrepareTUNLegacyOpenRCPreservesOtherModulesAndEnablesBoot(t *testing.T) {
	f := newTUNPrepareFixture()
	f.files["/etc/init.d/modules"] = []byte("#!/sbin/openrc-run\ncat /etc/modules | while read m; do modprobe $m; done\n")
	original := "# administrator list\nvirtio_net option=1"
	f.files["/etc/modules"] = []byte(original)
	cfg := tunPrepareConfig(t, "tun: {enable: true}", "openrc")
	for i := 0; i < 2; i++ {
		assertTUNReport(t, prepareTUNWithProbe(cfg, f.probe()), false, "开机加载配置")
	}
	data := string(f.files["/etc/modules"])
	if !strings.HasPrefix(data, original+"\n") || strings.Count(data, "\ntun\n") != 1 || len(f.writes) != 1 {
		t.Fatalf("legacy list not preserved/idempotent: %q %v", data, f.writes)
	}
	if _, ok := f.files[tunModulesFile]; ok {
		t.Fatal("legacy loader got unsupported modules-load.d file")
	}
}

func TestPrepareTUNOpenRCBootFailuresCannotClaimPersistence(t *testing.T) {
	for _, tc := range []struct {
		name  string
		setup func(*tunPrepareFixture)
	}{
		{"no-modules-service", func(f *tunPrepareFixture) { delete(f.files, "/etc/init.d/modules") }},
		{"unknown-modules-service", func(f *tunPrepareFixture) { f.files["/etc/init.d/modules"] = []byte("custom loader\n") }},
		{"temporary-override", func(f *tunPrepareFixture) { f.files["/run/modules-load.d/99-configflow-tun.conf"] = []byte("other\n") }},
		{"rc-update-fails", func(f *tunPrepareFixture) { f.runErrors["rc-update add modules boot"] = os.ErrPermission }},
		{"rc-update-no-effect", func(f *tunPrepareFixture) { f.noBoot = true }},
		{"foreign-boot-entry", func(f *tunPrepareFixture) { f.files["/etc/runlevels/boot/modules"] = []byte("foreign\n") }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := newTUNPrepareFixture()
			tc.setup(f)
			cfg := tunPrepareConfig(t, "tun: {enable: true}", "openrc")
			assertTUNReport(t, prepareTUNWithProbe(cfg, f.probe()), true, "未完成开机持久化")
		})
	}
}

func TestPersistTUNFileUsesOnlyIsolatedPathsAndPreservesLegacyContent(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "modules-load.d", "99-configflow-tun.conf")
	if err := persistTUNFile(path, nil, []byte(tunModulesContent), false); err != nil {
		t.Fatal(err)
	}
	if err := persistTUNFile(path, nil, []byte("overwrite\n"), false); err == nil {
		t.Fatal("exclusive creation overwrote existing file")
	}
	legacy := filepath.Join(dir, "modules")
	before := []byte("# keep\nvirtio_net\n")
	if err := os.WriteFile(legacy, before, 0600); err != nil {
		t.Fatal(err)
	}
	after := append(append([]byte(nil), before...), []byte("tun\n")...)
	if err := persistTUNFile(legacy, before, after, true); err != nil {
		t.Fatal(err)
	}
	data, _ := os.ReadFile(legacy)
	info, _ := os.Stat(legacy)
	if !bytes.Equal(data, after) || info.Mode().Perm() != 0600 {
		t.Fatalf("legacy file damaged: %q %v", data, info.Mode())
	}
	if err := persistTUNFile(legacy, before, []byte("stale overwrite\n"), true); err == nil {
		t.Fatal("stale update overwrote changed file")
	}
	link := filepath.Join(dir, "link")
	if err := os.Symlink(legacy, link); err != nil {
		t.Fatal(err)
	}
	if err := persistTUNFile(link, after, []byte("evil\n"), true); err == nil {
		t.Fatal("followed symlink")
	}
	parentLink := filepath.Join(dir, "linked-directory")
	if err := os.Symlink(filepath.Dir(path), parentLink); err != nil {
		t.Fatal(err)
	}
	if err := persistTUNFile(filepath.Join(parentLink, "new.conf"), nil, []byte("tun\n"), false); err == nil {
		t.Fatal("followed directory symlink")
	}
}
