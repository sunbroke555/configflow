package netfilter

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
)

// host is a fake Linux host: only listed tools exist and only the read-only
// commands of the contract may run.
type host struct {
	t        *testing.T
	tools    map[string]string // name -> resolved binary path
	outputs  map[string]string // "name args" -> stdout
	errs     map[string]error
	files    map[string]string
	modules  map[string]bool
	calls    []string
	block    bool
	ufwState string
}

var allowedCommands = map[string]bool{
	"nft -j list ruleset":                   true,
	"iptables-save -t filter":               true,
	"iptables-legacy-save -t filter":        true,
	"firewall-cmd --state":                  true,
	"firewall-cmd --get-zone-of-interface=": true,
	"ufw status":                            true,
}

func newHost(t *testing.T) *host {
	return &host{t: t, tools: map[string]string{}, outputs: map[string]string{}, errs: map[string]error{}, files: map[string]string{
		"/proc/net/route": "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\neth0\t00000000\tFE00610A\t0003\t0\t0\t0\t00000000\t0\t0\t0\neth0\t0000610A\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n",
	}, modules: map[string]bool{}}
}

func fixture(t *testing.T, name string) string {
	t.Helper()
	data, err := os.ReadFile(filepath.Join("testdata", name))
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

// nftHost has nf_tables loaded and an iptables-nft userspace, like Debian 13.
func nftHost(t *testing.T, ruleset, iptables string) *host {
	h := newHost(t)
	h.modules["/sys/module/nf_tables"] = true
	h.tools["nft"] = "/usr/sbin/nft"
	h.outputs["nft -j list ruleset"] = fixture(t, ruleset)
	if iptables != "" {
		h.tools["iptables-save"] = "/usr/sbin/xtables-nft-multi"
		h.tools["iptables-legacy-save"] = "/usr/sbin/xtables-legacy-multi"
		h.outputs["iptables-save -t filter"] = fixture(t, iptables)
	}
	return h
}

type fileInfo struct{}

func (fileInfo) Name() string       { return "module" }
func (fileInfo) Size() int64        { return 0 }
func (fileInfo) Mode() os.FileMode  { return os.ModeDir }
func (fileInfo) ModTime() time.Time { return time.Time{} }
func (fileInfo) IsDir() bool        { return true }
func (fileInfo) Sys() interface{}   { return nil }

func (h *host) probe() Probe {
	return Probe{
		LookPath: func(name string) (string, error) {
			if _, ok := h.tools[name]; ok {
				return "/usr/sbin/" + name, nil
			}
			return "", errors.New("not found")
		},
		EvalSymlinks: func(path string) (string, error) {
			if resolved, ok := h.tools[filepath.Base(path)]; ok {
				return resolved, nil
			}
			return path, nil
		},
		Run: func(ctx context.Context, name string, args ...string) (string, error) {
			command := name + " " + strings.Join(args, " ")
			h.calls = append(h.calls, command)
			key := command
			if strings.HasPrefix(command, "firewall-cmd --get-zone-of-interface=") {
				key = "firewall-cmd --get-zone-of-interface="
			}
			if !allowedCommands[key] {
				h.t.Fatalf("command outside the read-only contract: %s", command)
			}
			if h.block {
				<-ctx.Done()
				return "", ctx.Err()
			}
			if command == "ufw status" && h.ufwState != "" {
				return "Status: " + h.ufwState + "\n", nil
			}
			output, ok := h.outputs[command]
			if err := h.errs[command]; err != nil {
				return output, err
			}
			if !ok {
				h.t.Fatalf("unexpected command: %s", command)
			}
			return output, nil
		},
		ReadFile: func(path string) ([]byte, error) {
			if data, ok := h.files[path]; ok {
				return []byte(data), nil
			}
			return nil, os.ErrNotExist
		},
		Stat: func(path string) (os.FileInfo, error) {
			if h.modules[path] {
				return fileInfo{}, nil
			}
			return nil, os.ErrNotExist
		},
		Now: func() time.Time { return time.Date(2026, 10, 24, 8, 0, 0, 0, time.UTC) },
	}
}

func (h *host) inspect(device string) Result {
	return Inspect(context.Background(), h.probe(), Target{Device: device, AutoRedirect: true})
}

func (h *host) called(command string) bool {
	for _, call := range h.calls {
		if call == command {
			return true
		}
	}
	return false
}

func onlyRemedy(t *testing.T, result Result) Remedy {
	t.Helper()
	if result.State != StateMayDrop || len(result.Remedies) != 1 {
		t.Fatalf("want one remedy: %+v", result)
	}
	return result.Remedies[0]
}

func TestPlainForwardDropIsReportedOnceWithExactCommands(t *testing.T) {
	// Lab 71: iptables-nft shows the same policy in both views.
	h := nftHost(t, "plain-drop.nft.json", "plain-drop.iptables")
	result := h.inspect("Meta")
	if result.Policy != "drop" || len(result.Sources) != 1 || result.Sources[0].Kind != "iptables" || result.Sources[0].Backend != "nft" || result.Sources[0].Policy != "drop" {
		t.Fatalf("wrong sources: %+v", result)
	}
	remedy := onlyRemedy(t, result)
	want := []string{"iptables -I FORWARD -o Meta -j ACCEPT", "iptables -I FORWARD -i Meta -j ACCEPT"}
	if !reflect.DeepEqual(remedy.Commands, want) || remedy.Source != "iptables" || !strings.Contains(remedy.PersistNote, "netfilter-persistent save") {
		t.Fatalf("wrong remedy: %+v", remedy)
	}
	if !strings.Contains(result.Hint, "TCP 仍可用") || !strings.Contains(result.Hint, "Meta") || !strings.Contains(result.Verify, "ping") || result.CheckedAt != "2026-10-24T08:00:00Z" {
		t.Fatalf("wrong text: %+v", result)
	}
	if h.called("iptables-legacy-save -t filter") {
		t.Fatal("legacy table was not loaded; listing it would load it")
	}
}

func TestDeviceNameComesFromTheTarget(t *testing.T) {
	h := nftHost(t, "plain-drop.nft.json", "plain-drop.iptables")
	remedy := onlyRemedy(t, h.inspect("cf-tun0"))
	if remedy.Commands[0] != "iptables -I FORWARD -o cf-tun0 -j ACCEPT" || remedy.Device != "cf-tun0" {
		t.Fatalf("device not used: %+v", remedy)
	}
}

func TestWithoutIptablesTheNFTViewOfIPFilterIsEvaluated(t *testing.T) {
	h := nftHost(t, "plain-drop.nft.json", "")
	result := h.inspect("Meta")
	remedy := onlyRemedy(t, result)
	if result.Sources[0].Kind != "nftables" || result.Sources[0].Table != "filter" || result.Sources[0].Chain != "FORWARD" {
		t.Fatalf("wrong source: %+v", result.Sources)
	}
	want := []string{`nft 'insert rule ip filter FORWARD oifname "Meta" accept'`, `nft 'insert rule ip filter FORWARD iifname "Meta" accept'`}
	if !reflect.DeepEqual(remedy.Commands, want) {
		t.Fatalf("wrong commands: %q", remedy.Commands)
	}
}

func TestAcceptPolicyWithMihomoTablesIsOK(t *testing.T) {
	h := nftHost(t, "mihomo-only.nft.json", "plain-drop.iptables")
	result := h.inspect("Meta")
	if result.State != StateOK || result.Policy != "accept" || len(result.Sources) != 0 || len(result.Remedies) != 0 || result.Hint != "" {
		t.Fatalf("baseline should be ok: %+v", result)
	}
	// No ip filter table: iptables-nft has nothing to show and is not run.
	if h.called("iptables-save -t filter") {
		t.Fatal("iptables-nft listing without an ip filter table")
	}
}

func TestDockerDropPolicyUsesDockerUser(t *testing.T) {
	for _, lanKnown := range []bool{true, false} {
		h := nftHost(t, "ip-filter-present.nft.json", "docker-drop.iptables")
		if !lanKnown {
			delete(h.files, "/proc/net/route")
		}
		result := h.inspect("Meta")
		remedy := onlyRemedy(t, result)
		want := []string{"iptables -I DOCKER-USER -o Meta -j ACCEPT", "iptables -I DOCKER-USER -i Meta -j ACCEPT"}
		if !reflect.DeepEqual(remedy.Commands, want) || remedy.Source != "docker" || !strings.Contains(remedy.PersistNote, "DOCKER-USER") {
			t.Fatalf("lan=%v wrong remedy: %+v", lanKnown, remedy)
		}
		// Docker's own container rules never count; only the policy does.
		if len(result.Sources) != 1 || result.Sources[0].Kind != "docker" || result.Sources[0].Policy != "drop" {
			t.Fatalf("lan=%v wrong sources: %+v", lanKnown, result.Sources)
		}
	}
}

func TestDockerAcceptPolicyWithContainersIsOK(t *testing.T) {
	for _, name := range []string{"docker-accept.iptables", "docker-drop-fixed.iptables"} {
		for _, lanKnown := range []bool{true, false} {
			h := nftHost(t, "ip-filter-present.nft.json", name)
			if !lanKnown {
				delete(h.files, "/proc/net/route")
			}
			if result := h.inspect("Meta"); result.State != StateOK {
				t.Fatalf("%s lan=%v false positive: %+v", name, lanKnown, result)
			}
		}
	}
}

func TestLegacyBackendCommandsAndGuard(t *testing.T) {
	// iptables-save is legacy itself: plain iptables commands.
	h := newHost(t)
	h.tools["iptables-save"] = "/usr/sbin/xtables-legacy-multi"
	h.tools["iptables-legacy-save"] = "/usr/sbin/xtables-legacy-multi"
	h.files["/proc/net/ip_tables_names"] = "nat\nfilter\n"
	h.outputs["iptables-save -t filter"] = fixture(t, "plain-drop.iptables")
	result := h.inspect("Meta")
	remedy := onlyRemedy(t, result)
	if result.Sources[0].Backend != "legacy" || remedy.Commands[0] != "iptables -I FORWARD -o Meta -j ACCEPT" || h.called("iptables-legacy-save -t filter") {
		t.Fatalf("legacy default backend: %+v %v", result, h.calls)
	}

	// nft default plus loaded legacy tables (lab 70 warning): iptables-legacy.
	h = nftHost(t, "mihomo-only.nft.json", "plain-drop.iptables")
	h.files["/proc/net/ip_tables_names"] = "filter\n"
	h.outputs["iptables-legacy-save -t filter"] = fixture(t, "plain-drop.iptables")
	result = h.inspect("Meta")
	remedy = onlyRemedy(t, result)
	if remedy.Commands[1] != "iptables-legacy -I FORWARD -i Meta -j ACCEPT" || remedy.Label != "iptables（legacy）" {
		t.Fatalf("legacy view: %+v", remedy)
	}

	// The legacy filter table is not loaded: never list it.
	h = newHost(t)
	h.tools["iptables-save"] = "/usr/sbin/xtables-legacy-multi"
	h.tools["iptables-legacy-save"] = "/usr/sbin/xtables-legacy-multi"
	h.files["/proc/net/ip_tables_names"] = "nat\n"
	if result := h.inspect("Meta"); result.State != StateOK || len(h.calls) != 0 {
		t.Fatalf("legacy guard: %+v %v", result, h.calls)
	}
}

func TestWithoutNFTablesModuleNFTIsNotRun(t *testing.T) {
	h := newHost(t)
	h.tools["nft"] = "/usr/sbin/nft"
	h.tools["iptables-save"] = "/usr/sbin/xtables-nft-multi"
	if result := h.inspect("Meta"); result.State != StateOK || len(h.calls) != 0 {
		t.Fatalf("no module, nothing to inspect: %+v %v", result, h.calls)
	}
}

func TestUFWRoutedDenyAndRouteAllow(t *testing.T) {
	h := nftHost(t, "ip-filter-present.nft.json", "ufw-active.iptables")
	h.tools["ufw"] = "/usr/sbin/ufw"
	h.ufwState = "active"
	remedy := onlyRemedy(t, h.inspect("Meta"))
	want := []string{"ufw route allow in on Meta", "ufw route allow out on Meta"}
	if !reflect.DeepEqual(remedy.Commands, want) || remedy.Source != "ufw" || remedy.PersistNote != ufwNote {
		t.Fatalf("ufw remedy: %+v", remedy)
	}

	h = nftHost(t, "ip-filter-present.nft.json", "ufw-route-allowed.iptables")
	h.tools["ufw"] = "/usr/sbin/ufw"
	h.ufwState = "active"
	if result := h.inspect("Meta"); result.State != StateOK {
		t.Fatalf("route allow should clear the warning: %+v", result)
	}
}

func TestNativeNFTDropPolicy(t *testing.T) {
	h := nftHost(t, "nft-native-drop.nft.json", "")
	result := h.inspect("Meta")
	remedy := onlyRemedy(t, result)
	want := []string{`nft 'insert rule inet filter forward oifname "Meta" accept'`, `nft 'insert rule inet filter forward iifname "Meta" accept'`}
	if !reflect.DeepEqual(remedy.Commands, want) || remedy.PersistNote != nftPersistNote || result.Sources[0].Family != "inet" || result.Sources[0].Policy != "drop" {
		t.Fatalf("nft remedy: %+v", result)
	}
	h = nftHost(t, "nft-native-fixed.nft.json", "")
	if result := h.inspect("Meta"); result.State != StateOK {
		t.Fatalf("inserted accepts should clear the warning: %+v", result)
	}
}

func TestInvalidOnlyDropIsNotAProblem(t *testing.T) {
	h := nftHost(t, "nft-invalid-only.nft.json", "")
	if result := h.inspect("Meta"); result.State != StateOK {
		t.Fatalf("ct state invalid drop is harmless: %+v", result)
	}
	table, _ := parseIptablesSave("*filter\n:FORWARD ACCEPT [0:0]\n-A FORWARD -m conntrack --ctstate INVALID -j DROP\nCOMMIT\n")
	in := &inspection{ctx: context.Background(), target: Target{Device: "Meta"}, lan: []string{"eth0"}}
	in.evaluateIptables(table, "iptables", "nft")
	if len(in.findings) != 0 {
		t.Fatalf("iptables INVALID drop reported: %+v", in.findings)
	}
}

func evaluate(t *testing.T, rules string, lan []string) []finding {
	t.Helper()
	table, ok := parseIptablesSave("*filter\n" + rules + "COMMIT\n")
	if !ok {
		t.Fatalf("parse failed: %s", rules)
	}
	in := &inspection{ctx: context.Background(), target: Target{Device: "Meta"}, lan: lan}
	in.evaluateIptables(table, "iptables", "nft")
	return in.findings
}

func TestIptablesEvaluationIsConservative(t *testing.T) {
	for _, tc := range []struct {
		name  string
		rules string
		lan   []string
		drop  bool
	}{
		{"other interface drop, lan known", ":FORWARD ACCEPT [0:0]\n-A FORWARD -i eth1 -j DROP\n", []string{"eth0"}, false},
		{"other interface drop, lan unknown", ":FORWARD ACCEPT [0:0]\n-A FORWARD -i eth1 -j DROP\n", nil, true},
		{"tun drop", ":FORWARD ACCEPT [0:0]\n-A FORWARD -o Meta -p udp -j DROP\n", []string{"eth0"}, true},
		{"address-conditioned drop", ":FORWARD ACCEPT [0:0]\n-A FORWARD -d 10.0.0.0/8 -j REJECT --reject-with icmp-port-unreachable\n", []string{"eth0"}, true},
		{"unknown target", ":FORWARD ACCEPT [0:0]\n-A FORWARD -j NFQUEUE --queue-num 1\n", []string{"eth0"}, true},
		{"log only", ":FORWARD ACCEPT [0:0]\n-A FORWARD -j LOG --log-prefix \"fw drop \"\n", []string{"eth0"}, false},
		{"return applies drop policy", ":FORWARD DROP [0:0]\n-A FORWARD -j RETURN\n-A FORWARD -j ACCEPT\n", []string{"eth0"}, true},
		{"explicit accepts before drop policy", ":FORWARD DROP [0:0]\n-A FORWARD -o Meta -j ACCEPT\n-A FORWARD -i Meta -j ACCEPT\n", []string{"eth0"}, false},
		{"replies accepted by state", ":FORWARD DROP [0:0]\n-A FORWARD -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT\n-A FORWARD -i eth0 -o Meta -j ACCEPT\n", []string{"eth0"}, false},
		{"wildcard", ":FORWARD ACCEPT [0:0]\n-A FORWARD -o Me+ -j DROP\n", []string{"eth0"}, true},
		{"negated interface", ":FORWARD ACCEPT [0:0]\n-A FORWARD ! -o Meta -i Meta -j DROP\n", []string{"eth0"}, true},
		{"chain loop is bounded", ":FORWARD ACCEPT [0:0]\n:LOOP - [0:0]\n-A FORWARD -j LOOP\n-A LOOP -j LOOP\n", []string{"eth0"}, true},
		{"goto falls back to caller of caller", ":FORWARD DROP [0:0]\n:A - [0:0]\n-A FORWARD -g A\n-A FORWARD -j ACCEPT\n", []string{"eth0"}, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := len(evaluate(t, tc.rules, tc.lan)) > 0; got != tc.drop {
				t.Fatalf("drop=%v, want %v", got, tc.drop)
			}
		})
	}
}

func TestFirewalldZone(t *testing.T) {
	for _, tc := range []struct {
		zone, out string
		err       error
		commands  []string
		state     string
	}{
		{"public", "public\n", nil, []string{"firewall-cmd --permanent --zone=trusted --change-interface=Meta && firewall-cmd --reload"}, StateMayDrop},
		{"none", "no zone\n", errors.New("exit status 2"), []string{"firewall-cmd --permanent --zone=trusted --add-interface=Meta && firewall-cmd --reload"}, StateMayDrop},
		{"trusted", "trusted\n", nil, nil, StateOK},
	} {
		h := nftHost(t, "firewalld.nft.json", "")
		h.tools["firewall-cmd"] = "/usr/bin/firewall-cmd"
		h.outputs["firewall-cmd --state"] = "running\n"
		h.outputs["firewall-cmd --get-zone-of-interface=Meta"] = tc.out
		if tc.err != nil {
			h.errs["firewall-cmd --get-zone-of-interface=Meta"] = tc.err
		}
		result := h.inspect("Meta")
		if result.State != tc.state {
			t.Fatalf("%s: %+v", tc.zone, result)
		}
		if tc.commands != nil {
			remedy := onlyRemedy(t, result)
			if !reflect.DeepEqual(remedy.Commands, tc.commands) || remedy.PersistNote != firewalldNote || result.Sources[0].Kind != "firewalld" {
				t.Fatalf("%s: %+v", tc.zone, result)
			}
		}
	}
	// firewalld stopped: its leftover table is evaluated like any other.
	h := nftHost(t, "firewalld.nft.json", "")
	h.tools["firewall-cmd"] = "/usr/bin/firewall-cmd"
	h.outputs["firewall-cmd --state"] = "not running\n"
	h.errs["firewall-cmd --state"] = errors.New("exit status 252")
	if result := h.inspect("Meta"); result.State != StateMayDrop || result.Sources[0].Kind != "nftables" {
		t.Fatalf("stopped firewalld: %+v", result)
	}
}

func TestFailuresAndTimeoutsAreUnknown(t *testing.T) {
	h := nftHost(t, "mihomo-only.nft.json", "")
	h.errs["nft -j list ruleset"] = errors.New("exit status 1: Operation not permitted")
	if result := h.inspect("Meta"); result.State != StateUnknown || result.Reason != "inspect_failed" || result.Hint != "" {
		t.Fatalf("failure: %+v", result)
	}

	h = newHost(t)
	h.modules["/sys/module/nf_tables"] = true
	if result := h.inspect("Meta"); result.State != StateUnknown || result.Reason != "nft_unavailable" {
		t.Fatalf("nft missing: %+v", result)
	}

	old := TotalBudget
	TotalBudget = 150 * time.Millisecond
	t.Cleanup(func() { TotalBudget = old })
	h = nftHost(t, "mihomo-only.nft.json", "plain-drop.iptables")
	h.block = true
	start := time.Now()
	result := h.inspect("Meta")
	if result.State != StateUnknown || result.Reason != "timeout" || time.Since(start) > time.Second {
		t.Fatalf("timeout: %+v after %s", result, time.Since(start))
	}
}

func TestFindingsWinOverPartialFailures(t *testing.T) {
	h := nftHost(t, "nft-native-drop.nft.json", "")
	h.tools["firewall-cmd"] = "/usr/bin/firewall-cmd"
	h.errs["firewall-cmd --state"] = errors.New("exit status 1")
	if result := h.inspect("Meta"); result.State != StateMayDrop {
		t.Fatalf("known drop must still be reported: %+v", result)
	}
}

func TestInvalidDeviceOrNamesGiveNoCommands(t *testing.T) {
	h := nftHost(t, "plain-drop.nft.json", "plain-drop.iptables")
	result := h.inspect("bad name;rm")
	if result.State != StateMayDrop || len(result.Remedies) != 0 || result.Device != "" || !strings.Contains(result.Hint, "手动放行") {
		t.Fatalf("invalid device: %+v", result)
	}
	if remedy := nftRemedy("inet", "bad'table", "forward", "Meta"); len(remedy.Commands) != 0 {
		t.Fatalf("unsafe table name produced commands: %+v", remedy)
	}
}

func TestHintWithoutAutoRedirectCoversTCP(t *testing.T) {
	h := nftHost(t, "plain-drop.nft.json", "plain-drop.iptables")
	result := Inspect(context.Background(), h.probe(), Target{Device: "Meta"})
	if !strings.Contains(result.Hint, "包括 TCP、UDP 和 ping") || result.AutoRedirect {
		t.Fatalf("hint: %+v", result)
	}
}

func TestIptablesQuotedArguments(t *testing.T) {
	args, ok := splitArgs(`-A FORWARD -m comment --comment "a \"quoted\" -j DROP" -j ACCEPT`)
	if !ok || len(args) != 8 || args[5] != `a "quoted" -j DROP` {
		t.Fatalf("split: %q", args)
	}
	if _, ok := parseIptablesSave("# Table `filter' is incompatible, use 'nft' tool.\n"); ok {
		t.Fatal("incompatible table must not be read as empty")
	}
}
