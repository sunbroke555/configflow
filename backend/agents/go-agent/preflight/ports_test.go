package preflight

import (
	"errors"
	"fmt"
	"net/netip"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type fakePortProbe struct {
	listeners []portListener
	err       error
	configs   map[string]Config
}

func (p fakePortProbe) Listeners() ([]portListener, error) { return p.listeners, p.err }
func (p fakePortProbe) Executable(name string) (string, error) {
	if filepath.IsAbs(name) {
		return portCanonicalPath(name), nil
	}
	return "/usr/bin/" + name, nil
}
func (p fakePortProbe) AgentConfig(path string) (Config, error) {
	if cfg, ok := p.configs[path]; ok {
		return cfg, nil
	}
	return Config{}, errors.New("unknown config")
}

func portTestConfig(t *testing.T, service, content string) Config {
	t.Helper()
	path := filepath.Join(t.TempDir(), "config.yaml")
	if err := os.WriteFile(path, []byte(content), 0600); err != nil {
		t.Fatal(err)
	}
	return Config{AgentHost: "0.0.0.0", AgentPort: 8080, ServiceType: service, ServiceBinary: service, ConfigPath: path, ServiceUnit: service}
}

func portTestListener(address, protocol string, owners ...portProcess) portListener {
	bindings, err := parsePortAddress(address, protocol)
	if err != nil {
		panic(err)
	}
	return portListener{portBinding: bindings[0], Inode: "12345", Owners: owners}
}

func portTestCore(cfg Config) portProcess {
	return portProcess{PID: 101, Exe: "/usr/bin/" + cfg.ServiceType, Args: []string{cfg.ServiceType, "-c", cfg.ConfigPath}}
}

func portErrors(checks []Check) bool { return Report(checks).HasErrors() }

func TestPortRequirementsUseActualDNSListeners(t *testing.T) {
	for _, service := range []string{"oxidns", "mosdns"} {
		t.Run(service, func(t *testing.T) {
			cfg := portTestConfig(t, service, "plugins:\n- type: udp_server\n  args: {listen: '127.0.0.1:5353'}\n- type: tcp_server\n  args: {listen: '[::1]:5354'}\n")
			probe := fakePortProbe{listeners: []portListener{portTestListener("0.0.0.0:53", "udp", portProcess{PID: 55, Exe: "/usr/bin/other"})}}
			checks := portChecksWithProbe([]Config{cfg}, probe)
			if portErrors(checks) || len(checks) != 3 || !strings.Contains(checks[1].Name, "udp/5353") || !strings.Contains(checks[2].Name, "tcp/5354") {
				t.Fatalf("actual listeners not respected: %+v", checks)
			}
		})
	}
}

func TestMihomoNativeAIOChecksTargetPortWithoutEnablingDNS(t *testing.T) {
	cfg := portTestConfig(t, "mihomo", "dns: {enable: true, listen: '127.0.0.1:5353'}\n")
	cfg.NativeAIO = true
	checks := portChecksWithProbe([]Config{cfg}, fakePortProbe{})
	if len(checks) != 3 || checks[1].Name != "mihomo DNS tcp/1053" || checks[2].Name != "mihomo DNS udp/1053" {
		t.Fatalf("missing AIO target: %+v", checks)
	}
	for _, content := range []string{"dns: {enable: false, listen: ':53'}", "mixed-port: 7890"} {
		os.WriteFile(cfg.ConfigPath, []byte(content), 0600)
		checks = portChecksWithProbe([]Config{cfg}, fakePortProbe{})
		if len(checks) != 2 || !strings.Contains(checks[0].Message, "不适用") {
			t.Fatalf("disabled DNS should be inapplicable: %+v", checks)
		}
	}
}

func TestMihomoInternalDNSWithoutListenDoesNotRequireExternalPort(t *testing.T) {
	for _, listen := range []string{"", "  listen: ''\n", "  listen: null\n"} {
		t.Run(fmt.Sprintf("listen=%q", listen), func(t *testing.T) {
			cfg := portTestConfig(t, "mihomo", "tun: {enable: true, dns-hijack: ['any:53']}\ndns:\n  enable: true\n  nameserver: [1.1.1.1]\n"+listen)
			foreign := portProcess{PID: 50, Exe: "/usr/bin/other-dns"}
			probe := fakePortProbe{listeners: []portListener{portTestListener(":53", "udp", foreign), portTestListener(":1053", "tcp", foreign)}}
			checks := portChecksWithProbe([]Config{cfg}, probe)
			if portErrors(checks) || len(checks) != 2 || !strings.Contains(checks[0].Message, "内部解析") {
				t.Fatalf("internal DNS incorrectly required an external socket: %+v", checks)
			}
			cfg.NativeAIO = true
			checks = portChecksWithProbe([]Config{cfg}, probe)
			if !portErrors(checks) || len(checks) != 3 || checks[1].Name != "mihomo DNS tcp/1053" || checks[2].Name != "mihomo DNS udp/1053" {
				t.Fatalf("AIO did not check planned 1053 listeners: %+v", checks)
			}
		})
	}
	cfg := portTestConfig(t, "mihomo", "dns: {enable: true, listen: 53}")
	if !portErrors(portChecksWithProbe([]Config{cfg}, fakePortProbe{})) {
		t.Fatal("invalid listen type treated as absent")
	}
}

func TestPortBindingsRespectProtocolAndBindAddress(t *testing.T) {
	cases := []struct {
		a, b, protocol string
		overlap        bool
	}{
		{"127.0.0.1:53", "192.0.2.1:53", "udp", false},
		{"0.0.0.0:53", "192.0.2.1:53", "udp", true},
		{"[::]:53", "127.0.0.1:53", "udp", true},
		{"[::1]:53", "127.0.0.1:53", "udp", false},
		{"[::ffff:127.0.0.1]:53", "127.0.0.1:53", "udp", true},
		{"127.0.0.1:53", "127.0.0.1:53", "tcp", false},
		{"127.0.0.1:53", "127.0.0.1:1053", "udp", false},
	}
	for _, test := range cases {
		a, _ := parsePortAddress(test.a, "udp")
		b, _ := parsePortAddress(test.b, test.protocol)
		if got := portBindingsOverlap(a[0], b[0]); got != test.overlap {
			t.Errorf("%s udp versus %s %s = %v", test.a, test.b, test.protocol, got)
		}
	}
}

func TestCandidatePortsConflictBeforeInstallation(t *testing.T) {
	cfg := portTestConfig(t, "mihomo", "dns: {enable: true, listen: ':8080'}")
	if !portErrors(portChecksWithProbe([]Config{cfg}, fakePortProbe{})) {
		t.Fatal("Agent/core candidate collision passed")
	}
	cfg = portTestConfig(t, "mihomo", "dns: {enable: false}")
	peer := portTestConfig(t, "oxidns", "plugins: []")
	if !portErrors(portChecksWithProbe([]Config{cfg, peer}, fakePortProbe{})) {
		t.Fatal("two Agents on the same port passed")
	}
	peer.AgentHost = "192.0.2.2"
	cfg.AgentHost = "192.0.2.1"
	if portErrors(portChecksWithProbe([]Config{cfg, peer}, fakePortProbe{})) {
		t.Fatal("nonoverlapping candidate addresses were rejected")
	}
}

func TestOnlyVerifiedManagedOwnersAreAllowed(t *testing.T) {
	cfg := portTestConfig(t, "mihomo", "dns: {enable: true, listen: ':1053'}")
	managed := portTestCore(cfg)
	otherInstance := managed
	otherInstance.Args = []string{"mihomo", "-f", "/other/config.yaml"}
	otherBinary := managed
	otherBinary.Exe = "/tmp/mihomo"
	uncertain := managed
	uncertain.Uncertain = true
	cases := []struct {
		name   string
		owners []portProcess
		fail   bool
	}{
		{"managed instance", []portProcess{managed}, false},
		{"same executable other config", []portProcess{otherInstance}, true},
		{"same name other executable", []portProcess{otherBinary}, true},
		{"mixed ownership", []portProcess{managed, otherInstance}, true},
		{"unknown owner", nil, true},
		{"incomplete process details", []portProcess{uncertain}, true},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			checks := portChecksWithProbe([]Config{cfg}, fakePortProbe{listeners: []portListener{portTestListener("0.0.0.0:1053", "udp", test.owners...)}})
			if portErrors(checks) != test.fail {
				t.Fatalf("unexpected ownership result: %+v", checks)
			}
		})
	}
}

func TestCoreIdentityAcceptsServiceCgroupAndCanonicalPaths(t *testing.T) {
	cfg := portTestConfig(t, "mihomo", "dns: {enable: false}")
	binaryPath := filepath.Join(t.TempDir(), "core")
	os.WriteFile(binaryPath, []byte("binary"), 0700)
	alias := binaryPath + "-link"
	os.Symlink(binaryPath, alias)
	executable, err := (procPortProbe{}).Executable(alias)
	if err != nil || executable != portCanonicalPath(binaryPath) {
		t.Fatalf("executable symlink not resolved: %s %v", executable, err)
	}
	owner := portProcess{Exe: executable, Args: []string{"mihomo"}, Cgroups: "0::/system.slice/mihomo.service/worker"}
	if !portOwnerIsCore(owner, cfg, executable) {
		t.Fatal("managed cgroup rejected")
	}
	owner.Cgroups = "0::/system.slice/not-mihomo.service"
	if portOwnerIsCore(owner, cfg, executable) {
		t.Fatal("substring cgroup accepted")
	}
	owner.Cwd = filepath.Dir(cfg.ConfigPath)
	owner.Args = []string{"mihomo", "-f=config.yaml"}
	if !portOwnerIsCore(owner, cfg, executable) {
		t.Fatal("relative config rejected")
	}
	owner.Args = []string{"mihomo", "-d", owner.Cwd}
	if !portOwnerIsCore(owner, cfg, executable) {
		t.Fatal("managed working directory rejected")
	}
	owner.Cwd = "/unrelated-working-directory"
	owner.Args = []string{"oxidns", "-d=" + filepath.Dir(cfg.ConfigPath), "-c=config.yaml"}
	if !portOwnerIsCore(owner, cfg, executable) {
		t.Fatal("relative config was not resolved against -d")
	}
}

func TestLegacyAndIsolatedAgentsRequireMatchingInstalledConfig(t *testing.T) {
	cfg := portTestConfig(t, "mihomo", "dns: {enable: false}")
	for _, path := range []string{"/opt/configflow-agent/config-mihomo.json", "/opt/configflow-agent/config.json", "/opt/configflow-agent/mihomo/config.json"} {
		t.Run(path, func(t *testing.T) {
			cfg.NativeAIO = strings.Contains(path, "/mihomo/")
			binaryPath := "/usr/local/bin/configflow-agent"
			if cfg.NativeAIO {
				binaryPath = "/opt/configflow-agent/mihomo/configflow-agent"
			}
			owner := portProcess{PID: 77, Exe: portCanonicalPath(binaryPath), Args: []string{binaryPath, "-config", path}}
			probe := fakePortProbe{listeners: []portListener{portTestListener(":8080", "tcp", owner)}, configs: map[string]Config{path: cfg}}
			if portErrors(portChecksWithProbe([]Config{cfg}, probe)) {
				t.Fatal("same installed Agent rejected")
			}
			// AIO can take over the corresponding legacy Agent. Its executable,
			// installed configuration path, service and port still must match.
			cfg.NativeAIO = true
			if portErrors(portChecksWithProbe([]Config{cfg}, probe)) {
				t.Fatal("AIO reinstall rejected its own legacy Agent")
			}
			previous := cfg
			previous.ConfigPath = "/previous/core-directory/config.yaml"
			probe.configs[path] = previous
			if portErrors(portChecksWithProbe([]Config{cfg}, probe)) {
				t.Fatal("managed Agent core configuration directory migration rejected")
			}
			for _, field := range []string{"service", "port"} {
				wrong := cfg
				switch field {
				case "service":
					wrong.ServiceType = "mosdns"
				case "port":
					wrong.AgentPort++
				}
				probe.configs[path] = wrong
				if !portErrors(portChecksWithProbe([]Config{cfg}, probe)) {
					t.Fatalf("mismatched %s accepted", field)
				}
			}
		})
	}
}

func TestLegacyShellAgentUpgradeRequiresExactManagedSocat(t *testing.T) {
	cfg := portTestConfig(t, "mihomo", "dns: {enable: false}")
	cfg.ServiceManager = "custom" // Core lifecycle does not identify the Agent's init system.
	managed := portProcess{
		PID: 77, Exe: "/usr/bin/socat", Cwd: "/opt/configflow-agent",
		Args:    []string{"socat", "TCP-LISTEN:8080,fork,reuseaddr", "SYSTEM:/opt/configflow-agent/agent.sh __handle_request__"},
		Cgroups: "0::/system.slice/configflow-agent.service",
	}
	cases := []struct {
		name   string
		change func(*portProcess, *Config, *Config)
		fail   bool
	}{
		{"systemd upgrade", func(*portProcess, *Config, *Config) {}, false},
		{"systemd cgroup proves identity without cwd", func(p *portProcess, _, _ *Config) { p.Cwd = "" }, false},
		{"OpenRC upgrade", func(p *portProcess, _, _ *Config) { p.Cgroups = "0::/" }, false},
		{"AIO cannot upgrade legacy shell", func(_ *portProcess, c, _ *Config) { c.NativeAIO = true }, true},
		{"unrelated executable", func(p *portProcess, _, _ *Config) { p.Exe = "/tmp/socat" }, true},
		{"extra socat option", func(p *portProcess, _, _ *Config) { p.Args = append(p.Args, "-d") }, true},
		{"different TCP port", func(p *portProcess, _, _ *Config) { p.Args[1] = "TCP-LISTEN:8082,fork,reuseaddr" }, true},
		{"modified TCP options", func(p *portProcess, _, _ *Config) { p.Args[1] += ",bind=127.0.0.1" }, true},
		{"different script", func(p *portProcess, _, _ *Config) { p.Args[2] = "SYSTEM:/tmp/agent.sh __handle_request__" }, true},
		{"extra shell command", func(p *portProcess, _, _ *Config) { p.Args[2] += "; echo other-command" }, true},
		{"different handler", func(p *portProcess, _, _ *Config) { p.Args[2] = "SYSTEM:/opt/configflow-agent/agent.sh other-handler" }, true},
		{"different systemd service", func(p *portProcess, _, _ *Config) { p.Cgroups = "0::/system.slice/other.service" }, true},
		{"unit name substring", func(p *portProcess, _, _ *Config) { p.Cgroups = "0::/system.slice/other-configflow-agent.service" }, true},
		{"interactive scope", func(p *portProcess, _, _ *Config) { p.Cgroups = "0::/user.slice/session-1.scope" }, true},
		{"OpenRC wrong cwd", func(p *portProcess, _, _ *Config) { p.Cgroups, p.Cwd = "0::/", "/tmp" }, true},
		{"different configured service", func(_ *portProcess, _, installed *Config) { installed.ServiceType = "mosdns" }, true},
		{"different configured port", func(_ *portProcess, _, installed *Config) { installed.AgentPort = 8082 }, true},
		{"unverified process", func(p *portProcess, _, _ *Config) { p.Uncertain = true }, true},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			owner, candidate, installed := managed, cfg, cfg
			owner.Args = append([]string(nil), managed.Args...)
			test.change(&owner, &candidate, &installed)
			probe := fakePortProbe{
				listeners: []portListener{portTestListener(":8080", "tcp", owner)},
				configs:   map[string]Config{"/opt/configflow-agent/config.json": installed},
			}
			if checks := portChecksWithProbe([]Config{candidate}, probe); portErrors(checks) != test.fail {
				t.Fatalf("unexpected legacy upgrade result: %+v", checks)
			}
		})
	}
	probe := fakePortProbe{listeners: []portListener{portTestListener(":8080", "tcp", managed)}}
	if !portErrors(portChecksWithProbe([]Config{cfg}, probe)) {
		t.Fatal("legacy shell listener accepted without installed configuration")
	}
}

func TestAIOCurrentMihomoOn53IsMigrationNotExternalConflict(t *testing.T) {
	mihomo := portTestConfig(t, "mihomo", "dns: {enable: true, listen: ':53'}")
	mihomo.NativeAIO = true
	oxidns := portTestConfig(t, "oxidns", "plugins:\n- type: udp_server\n  args: {listen: ':53'}\n")
	oxidns.NativeAIO, oxidns.AgentPort = true, 8082
	listener := portTestListener(":53", "udp", portTestCore(mihomo))
	probe := fakePortProbe{listeners: []portListener{listener}}
	checks := portChecksWithProbe([]Config{mihomo, oxidns}, probe)
	if portErrors(checks) || checks[len(checks)-1].Level != "warn" || !strings.Contains(checks[len(checks)-1].Fix, "先发布 Mihomo") {
		t.Fatalf("migration not identified: %+v", checks)
	}
	probe.listeners[0].Owners = append(probe.listeners[0].Owners, portProcess{PID: 88, Exe: "/usr/bin/external"})
	if !portErrors(portChecksWithProbe([]Config{mihomo, oxidns}, probe)) {
		t.Fatal("foreign shared owner must block migration")
	}
}

func TestMissingProcAndUnparseableListenerCannotPass(t *testing.T) {
	cfg := portTestConfig(t, "oxidns", "plugins:\n- type: udp_server\n  args: {}")
	if !portErrors(portChecksWithProbe([]Config{cfg}, fakePortProbe{})) {
		t.Fatal("missing listen passed")
	}
	checks := portChecksWithProbe([]Config{cfg}, procPortProbe{Root: filepath.Join(t.TempDir(), "missing")})
	if !portErrors(checks) || !strings.Contains(checks[len(checks)-1].Message, "/proc") {
		t.Fatalf("missing proc passed: %+v", checks)
	}
}

func TestProcSnapshotDecodesSocketsAndFindsEveryOwner(t *testing.T) {
	root := t.TempDir()
	os.MkdirAll(filepath.Join(root, "net"), 0700)
	row := func(address, state, inode string) string {
		return fmt.Sprintf("0: %s 00000000:0000 %s 00000000:00000000 00:00000000 00000000 0 0 %s\n", address, state, inode)
	}
	for name, content := range map[string]string{
		"tcp":  row("0100007F:1F90", "0A", "11") + row("0100007F:1F91", "01", "12"),
		"tcp6": row("00000000000000000000000001000000:0035", "0A", "13"),
		"udp":  row("00000000:041D", "07", "14"),
		"udp6": "",
	} {
		os.WriteFile(filepath.Join(root, "net", name), []byte("sl local_address rem_address st\n"+content), 0600)
	}
	for _, pid := range []string{"100", "101"} {
		dir := filepath.Join(root, pid)
		os.MkdirAll(filepath.Join(dir, "fd"), 0700)
		os.Symlink("socket:[11]", filepath.Join(dir, "fd", "3"))
		os.Symlink("socket:[14]", filepath.Join(dir, "fd", "4"))
		os.Symlink("/usr/bin/mihomo", filepath.Join(dir, "exe"))
		os.WriteFile(filepath.Join(dir, "cmdline"), []byte("mihomo\x00-f\x00/etc/mihomo/config.yaml\x00"), 0600)
	}
	listeners, err := (procPortProbe{Root: root}).Listeners()
	if err != nil || len(listeners) != 3 {
		t.Fatalf("snapshot parse: %+v %v", listeners, err)
	}
	if listeners[0].Address != netip.MustParseAddr("127.0.0.1") || len(listeners[0].Owners) != 2 || listeners[0].Uncertain {
		t.Fatalf("IPv4 owners: %+v", listeners[0])
	}
	if listeners[1].Address != netip.IPv6Loopback() || !listeners[1].Uncertain {
		t.Fatalf("IPv6 unknown owner: %+v", listeners[1])
	}
	if listeners[2].Port != 1053 || listeners[2].Protocol != "udp" {
		t.Fatalf("UDP bound socket: %+v", listeners[2])
	}
	os.Mkdir(filepath.Join(root, "999"), 0700)
	os.WriteFile(filepath.Join(root, "999", "fd"), []byte("not a readable fd directory"), 0600)
	listeners, err = (procPortProbe{Root: root}).Listeners()
	if err != nil || listeners[0].Uncertain || !listeners[0].IncompleteView || !listeners[1].Uncertain {
		t.Fatal("incomplete PID scan should warn for verified owners, but reject missing owners")
	}
}

func TestIncompleteProcViewWarnsWithoutRejectingVerifiedOwner(t *testing.T) {
	cfg := portTestConfig(t, "mihomo", "dns: {enable: true, listen: ':1053'}")
	listener := portTestListener(":1053", "udp", portTestCore(cfg))
	listener.IncompleteView = true
	checks := portChecksWithProbe([]Config{cfg}, fakePortProbe{listeners: []portListener{listener}})
	if portErrors(checks) || checks[0].Name != "进程可见性" || checks[0].Level != "warn" {
		t.Fatalf("incomplete view rejected verified owner or missed warning: %+v", checks)
	}
	listener.Owners = append(listener.Owners, portProcess{PID: 90, Uncertain: true})
	if !portErrors(portChecksWithProbe([]Config{cfg}, fakePortProbe{listeners: []portListener{listener}})) {
		t.Fatal("an observed unverified owner was accepted")
	}
}

func TestUnrecognizedDNSConfigurationDoesNotProduceFalsePass(t *testing.T) {
	cases := []struct{ service, content string }{
		{"mihomo", "dns: {enable: 'yes', listen: ':53'}"},
		{"mihomo", "dns: {enable: 1}"},
		{"mihomo", "dns: []"},
		{"oxidns", "plugins: {}"},
		{"mosdns", "log: {level: info}"},
		{"oxidns", "plugins: [invalid]"},
		{"mosdns", "plugins: [{tag: unknown}]"},
		{"oxidns", "include: [extra.yaml]\nplugins: []"},
		{"mosdns", "include: extra.yaml\nplugins: []"},
		{"oxidns", "plugins: [{type: udp_server, args: {listen: []}}]"},
	}
	for _, test := range cases {
		t.Run(test.service+"/"+test.content, func(t *testing.T) {
			cfg := portTestConfig(t, test.service, test.content)
			if !portErrors(portChecksWithProbe([]Config{cfg}, fakePortProbe{})) {
				t.Fatal("unrecognized configuration passed")
			}
		})
	}
	for _, service := range []string{"oxidns", "mosdns"} {
		cfg := portTestConfig(t, service, "include: []\nplugins: []")
		if portErrors(portChecksWithProbe([]Config{cfg}, fakePortProbe{})) {
			t.Fatal("explicitly empty listeners rejected")
		}
		cfg = portTestConfig(t, service, "plugins: [{type: udp_server, enabled: false, disabled: true, args: {listen: ':53'}}]")
		probe := fakePortProbe{listeners: []portListener{portTestListener(":53", "udp", portProcess{PID: 55, Exe: "/other"})}}
		if !portErrors(portChecksWithProbe([]Config{cfg}, probe)) {
			t.Fatal("unsupported disabling keys suppressed listener check")
		}
	}
}
