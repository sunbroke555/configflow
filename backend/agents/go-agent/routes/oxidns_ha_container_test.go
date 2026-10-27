package routes

import (
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
)

type haNamespaceInfo struct {
	os.FileInfo
	stat syscall.Stat_t
}

func (info haNamespaceInfo) Sys() interface{} { return &info.stat }

type haContainerFixture struct {
	*haFixture
	files           map[string][]byte
	links           string
	peerError       error
	helperAvailable bool
}

func newHAContainerFixture(t *testing.T, mode string) *haContainerFixture {
	t.Helper()
	base := newHAFixture(t)
	f := &haContainerFixture{haFixture: base, files: map[string][]byte{}, helperAvailable: true,
		links: `[{"ifname":"lo"},{"ifname":"eth0","link_netnsid":0,"linkinfo":{"info_kind":"macvlan"}}]`}
	t.Setenv("CONFIGFLOW_DNS_HA", "1")
	t.Setenv("CONFIGFLOW_DNS_HA_NETWORK", mode)
	f.cfg.ServiceManager, f.cfg.AgentIP = "supervisor", f.spec.SelfIP
	oldRead, oldStat, oldPeer := haEnvironmentRead, haEnvironmentStat, haCompanionPeerCheck
	t.Cleanup(func() { haEnvironmentRead, haEnvironmentStat, haCompanionPeerCheck = oldRead, oldStat, oldPeer })
	for _, path := range []string{filepath.Dir(f.cfg.AgentConfigFile), filepath.Dir(f.cfg.ConfigPath), haKeepalivedDir} {
		f.files["/proc/self/mountinfo"] = append(f.files["/proc/self/mountinfo"], []byte("1 0 8:1 / "+path+" rw - ext4 /dev/vda rw\n")...)
	}
	for _, path := range []string{haHostIDPath, haHostNetNSPath} {
		f.files["/proc/self/mountinfo"] = append(f.files["/proc/self/mountinfo"], []byte("2 0 8:1 / "+path+" ro - nsfs nsfs rw\n")...)
	}
	f.files[haHostIDPath] = []byte("0123456789abcdef0123456789abcdef\n")
	f.files["/proc/self/status"] = []byte("CapEff:\t0000000000003000\n")
	haEnvironmentRead = func(path string) ([]byte, error) {
		if data, ok := f.files[path]; ok {
			return data, nil
		}
		return os.ReadFile(path)
	}
	haEnvironmentStat = func(path string) (os.FileInfo, error) {
		switch path {
		case "/proc/self/ns/net", haHostNetNSPath:
			return haNamespaceInfo{stat: syscall.Stat_t{Dev: 1, Ino: 10}}, nil
		case "/proc/self/ns/pid":
			return haNamespaceInfo{stat: syscall.Stat_t{Dev: 1, Ino: 20}}, nil
		}
		return os.Stat(path)
	}
	haCompanionPeerCheck = func(string) error { return f.peerError }
	f.setEvidence(t, haCompanionEvidence{Protocol: 1, Mode: "host", HostID: haHostIdentity(haHostIDPath),
		CapEff: "3000", Network: haNamespace{1, 10}, PID: haNamespace{1, 21}})
	baseRun := haRun
	haRun = func(name string, args ...string) (string, error) {
		if name == "ip" && strings.Join(args, " ") == "-j -d link show" {
			return f.links, nil
		}
		if name == "supervisorctl" {
			if strings.Join(args[:len(args)-2], " ") != strings.Join(haSupervisorArgs("status")[:2], " ") || args[len(args)-1] != haSupervisorProgram {
				t.Fatalf("unexpected Supervisor target: %v", args)
			}
			action := args[len(args)-2]
			if !f.helperAvailable {
				return "", fmt.Errorf("companion unavailable")
			}
			if action == "status" {
				if f.running {
					return haSupervisorProgram + " RUNNING pid 10", nil
				}
				return haSupervisorProgram + " STOPPED", fmt.Errorf("stopped")
			}
			f.commands = append(f.commands, name+" "+strings.Join(args, " "))
			switch action {
			case "start", "restart":
				f.running = true
			case "stop":
				f.running = false
			default:
				t.Fatalf("unexpected action %s", action)
			}
			return "ok", nil
		}
		return baseRun(name, args...)
	}
	return f
}

func (f *haContainerFixture) setEvidence(t *testing.T, evidence haCompanionEvidence) {
	t.Helper()
	data, err := json.Marshal(evidence)
	if err != nil {
		t.Fatal(err)
	}
	f.files[filepath.Join(haKeepalivedDir, "run", "companion.json")] = data
}

func TestHADockerNotOptedInDoesNotProbeEnvironment(t *testing.T) {
	f := newHAFixture(t)
	f.cfg.ServiceManager = "supervisor"
	t.Setenv("CONFIGFLOW_DNS_HA", "")
	previous := haEnvironmentRead
	t.Cleanup(func() { haEnvironmentRead = previous })
	haEnvironmentRead = func(string) ([]byte, error) {
		t.Fatal("ordinary Docker capability inspected HA files")
		return nil, nil
	}
	haRun = func(string, ...string) (string, error) {
		t.Fatal("ordinary Docker capability probed services")
		return "", nil
	}
	result := haEnvironment(f.cfg)
	if result.Kind != "container" || result.Ready || len(result.Blockers) != 1 {
		t.Fatalf("unexpected environment: %+v", result)
	}
}

func TestHADockerEnvironmentChecksActualNetworkAndPrerequisites(t *testing.T) {
	for _, mode := range []string{"host", "macvlan"} {
		t.Run(mode, func(t *testing.T) {
			f := newHAContainerFixture(t, mode)
			if env := haEnvironment(f.cfg); !env.Ready || env.Interface != "eth0" || len(env.HostID) != 64 {
				t.Fatalf("valid environment rejected: %+v", env)
			}
			response := httptest.NewRecorder()
			CapabilitiesHandler(f.cfg)(response, httptest.NewRequest(http.MethodGet, "/api/capabilities", nil))
			if !strings.Contains(response.Body.String(), `"oxidns_ha_container_protocols":[1]`) {
				t.Fatal(response.Body.String())
			}
			f.files["/proc/self/mountinfo"] = nil
			if env := haEnvironment(f.cfg); env.Ready {
				t.Fatal("ephemeral config/state mounts accepted")
			}
		})
	}
	for _, bad := range []string{"host-parent", "bridge", "extra-interface", "missing-caps", "wrong-ip", "identity"} {
		t.Run(bad, func(t *testing.T) {
			f := newHAContainerFixture(t, "macvlan")
			switch bad {
			case "host-parent":
				f.links = `[{"ifname":"lo"},{"ifname":"eth0","linkinfo":{"info_kind":"macvlan"}}]`
			case "bridge":
				f.links = `[{"ifname":"lo"},{"ifname":"eth0","link_netnsid":0,"linkinfo":{"info_kind":"veth"}}]`
			case "extra-interface":
				f.links = `[{"ifname":"lo"},{"ifname":"eth0","link_netnsid":0,"linkinfo":{"info_kind":"macvlan"}},{"ifname":"eth1"}]`
			case "missing-caps":
				f.files["/proc/self/status"] = []byte("CapEff: 2000")
			case "wrong-ip":
				f.cfg.AgentIP = "192.168.0.88"
			case "identity":
				f.files[haHostIDPath] = []byte("bad")
			}
			if env := haEnvironment(f.cfg); env.Ready {
				t.Fatalf("unsafe %s accepted", bad)
			}
			if err := applyOxiDNSHA(f.cfg, f.spec); err == nil || len(f.commands) != 0 {
				t.Fatalf("rejected environment modified services: %v %v", err, f.commands)
			}
		})
	}
}

func TestHAHostRequiresIndependentLiveCompanion(t *testing.T) {
	for _, bad := range []string{"same-pidns", "different-netns", "missing-cap", "wrong-host", "dead-socket"} {
		t.Run(bad, func(t *testing.T) {
			f := newHAContainerFixture(t, "host")
			evidence := haCompanionEvidence{Protocol: 1, Mode: "host", HostID: haHostIdentity(haHostIDPath), CapEff: "3000", Network: haNamespace{1, 10}, PID: haNamespace{1, 21}}
			switch bad {
			case "same-pidns":
				evidence.PID.Inode = 20
			case "different-netns":
				evidence.Network.Inode = 99
			case "missing-cap":
				evidence.CapEff = "2000"
			case "wrong-host":
				evidence.HostID = strings.Repeat("f", 64)
			case "dead-socket":
				f.peerError = fmt.Errorf("unavailable")
			}
			f.setEvidence(t, evidence)
			if env := haEnvironment(f.cfg); env.Ready {
				t.Fatalf("unsafe companion accepted: %s", bad)
			}
		})
	}
}

func TestHACompanionSocketRejectsSamePIDNamespace(t *testing.T) {
	dir, err := os.MkdirTemp("", "cfha-")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(dir)
	path := filepath.Join(dir, "s")
	listener, err := net.Listen("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	if err := checkHACompanionPeer(path); err == nil {
		t.Fatal("local Supervisor impersonated an independent companion")
	}
}

func TestHADockerApplyHandoffRecoveryAndDisableStayOwned(t *testing.T) {
	for _, mode := range []string{"host", "macvlan"} {
		t.Run(mode, func(t *testing.T) {
			f := newHAContainerFixture(t, mode)
			if err := applyOxiDNSHA(f.cfg, f.spec); err != nil {
				t.Fatal(err)
			}
			state := mustHAState(t, f.cfg)
			if state.Service.Manager != "supervisor" || state.ContainerHostID != haHostIdentity(haHostIDPath) {
				t.Fatalf("ownership not retained: %+v", state)
			}
			if err := changeHAHandoff(f.cfg, "publish-1", false); err != nil {
				t.Fatal(err)
			}
			if !haYieldToPeer(&Config{AgentConfigFile: f.cfg.AgentConfigFile, ConfigPath: f.cfg.ConfigPath}, state) {
				t.Fatal("CLI-sized config lost Docker handoff")
			}
			f.running = false
			f.commands = nil
			f.helperAvailable = false
			if err := reconcileOxiDNSHA(f.cfg); err == nil || len(f.commands) != 0 {
				t.Fatalf("unavailable helper changed service: %v", err)
			}
			f.helperAvailable = true
			if err := reconcileOxiDNSHA(f.cfg); err != nil || !f.running || len(f.commands) != 1 {
				t.Fatalf("recreated helper not recovered once: %v %v", err, f.commands)
			}
			state = mustHAState(t, f.cfg)
			state.Pending = true
			if err := saveHAState(f.cfg, state); err != nil {
				t.Fatal(err)
			}
			f.running = false
			f.commands = nil
			if err := reconcileOxiDNSHA(f.cfg); err != nil || len(f.commands) != 0 {
				t.Fatalf("pending operation was resurrected: %v %v", err, f.commands)
			}
			state.Pending = false
			state.ContainerHostID = strings.Repeat("f", 64)
			if err := saveHAState(f.cfg, state); err != nil {
				t.Fatal(err)
			}
			if err := reconcileOxiDNSHA(f.cfg); err == nil || len(f.commands) != 0 {
				t.Fatal("cloned journal activated on another Docker host")
			}
			state.ContainerHostID = haHostIdentity(haHostIDPath)
			saveHAState(f.cfg, state)
			if _, err := removeOxiDNSHA(f.cfg); err != nil {
				t.Fatal(err)
			}
			f.commands = nil
			if err := reconcileOxiDNSHA(f.cfg); err != nil || len(f.commands) != 0 {
				t.Fatalf("disabled journal was resurrected: %v", err)
			}
			for _, command := range f.commands {
				if strings.Contains(command, "systemctl") || strings.Contains(command, "rc-service") {
					t.Fatalf("host service mutated: %s", command)
				}
			}
		})
	}
}

func TestHAContainerStatusAndRecoveryRejectChangedEnvironment(t *testing.T) {
	for _, change := range []string{"helper-lost", "host-changed", "native-journal"} {
		t.Run(change, func(t *testing.T) {
			f := newHAContainerFixture(t, "host")
			if err := applyOxiDNSHA(f.cfg, f.spec); err != nil {
				t.Fatal(err)
			}
			state := mustHAState(t, f.cfg)
			switch change {
			case "helper-lost":
				f.peerError = fmt.Errorf("helper disconnected")
			case "host-changed":
				state.ContainerHostID = strings.Repeat("f", 64)
			case "native-journal":
				state.Service.Manager = "systemd"
				state.ContainerHostID = ""
			}
			if err := saveHAState(f.cfg, state); err != nil {
				t.Fatal(err)
			}
			if result := haStatus(f.cfg); result["error"] == "" {
				t.Fatalf("unsafe environment looked healthy: %+v", result)
			}
			f.commands = nil
			if err := reconcileOxiDNSHA(f.cfg); err == nil || len(f.commands) != 0 {
				t.Fatalf("unsafe recovery: %v %v", err, f.commands)
			}
		})
	}
}
