// Package netfilter inspects, read-only, whether the host firewall may drop
// traffic forwarded between the LAN and a full-TUN Mihomo device.
//
// It never changes firewall state. Every command it runs only lists rules, and
// it avoids listings with side effects: the legacy iptables filter table is
// only read when the kernel already has it loaded, and nftables views are only
// read when nf_tables is present (loaded module, built into the kernel, or in
// use by Mihomo's auto-redirect). Any failure degrades to "unknown".
package netfilter

import (
	"context"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

const (
	StateOK            = "ok"
	StateMayDrop       = "may_drop"
	StateUnknown       = "unknown"
	StateNotApplicable = "not_applicable"
)

// Result is the agent status contract (field forward_filter). Protocol 1.
type Result struct {
	Protocol     int      `json:"protocol"`
	State        string   `json:"state"`
	Reason       string   `json:"reason"`
	Device       string   `json:"device"`
	AutoRedirect bool     `json:"auto_redirect"`
	Policy       string   `json:"policy"`
	Sources      []Source `json:"sources"`
	Remedies     []Remedy `json:"remedies"`
	Hint         string   `json:"hint"`
	Verify       string   `json:"verify"`
	CheckedAt    string   `json:"checked_at"`
	Cached       bool     `json:"cached"`
}

type Source struct {
	Kind    string `json:"kind"`    // docker | iptables | nftables | firewalld | ufw
	Backend string `json:"backend"` // nft | legacy for iptables-based sources
	Family  string `json:"family"`
	Table   string `json:"table"`
	Chain   string `json:"chain"`
	Policy  string `json:"policy"` // drop | accept
	Detail  string `json:"detail"`
}

type Remedy struct {
	Source      string   `json:"source"`
	Label       string   `json:"label"`
	Device      string   `json:"device"`
	Commands    []string `json:"commands"`
	PersistNote string   `json:"persist_note"`
}

type Target struct {
	Device       string
	AutoRedirect bool
}

// Probe isolates every host access so tests never run real commands.
type Probe struct {
	LookPath     func(string) (string, error)
	Run          func(ctx context.Context, name string, args ...string) (string, error)
	ReadFile     func(string) ([]byte, error)
	Stat         func(string) (os.FileInfo, error)
	EvalSymlinks func(string) (string, error)
	Now          func() time.Time
}

// Budgets are variables only so tests can shorten them.
var (
	TotalBudget   = 4 * time.Second
	commandBudget = 2 * time.Second
)

const (
	maxSources  = 8
	maxRemedies = 6
	maxDetail   = 160
)

// DevicePattern matches the TUN device names commands may be generated for.
var DevicePattern = regexp.MustCompile(`^[A-Za-z0-9_][A-Za-z0-9_.-]{0,14}$`)

var nftNamePattern = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_.-]{0,63}$`)

func DefaultProbe() Probe {
	return Probe{
		LookPath: exec.LookPath,
		Run: func(ctx context.Context, name string, args ...string) (string, error) {
			cmd := exec.CommandContext(ctx, name, args...)
			// ufw and firewall-cmd translate their output through LANG/LANGUAGE.
			cmd.Env = append(os.Environ(), "LC_ALL=C", "LANG=C", "LANGUAGE=C")
			// A wrapper's child may keep the pipe open after the wrapper is
			// killed; do not wait for it beyond the command budget.
			cmd.WaitDelay = 250 * time.Millisecond
			out, err := cmd.Output()
			return string(out), err
		},
		ReadFile:     os.ReadFile,
		Stat:         os.Stat,
		EvalSymlinks: filepath.EvalSymlinks,
		Now:          time.Now,
	}
}

type inspection struct {
	ctx        context.Context
	probe      Probe
	target     Target
	lan        []string // default-route interfaces; nil when unknown
	findings   []finding
	incomplete string // first reason the view is not complete
	hooks      int    // inspected forward hooks
	ufwActive  bool
	remedies   []Remedy
}

type finding struct {
	source Source
	policy bool
}

func (in *inspection) fail(reason string) {
	if in.incomplete == "" {
		in.incomplete = reason
	}
}

func (in *inspection) run(name string, args ...string) (string, error) {
	if err := in.ctx.Err(); err != nil {
		return "", err
	}
	ctx, cancel := context.WithTimeout(in.ctx, commandBudget)
	defer cancel()
	out, err := in.probe.Run(ctx, name, args...)
	if err == nil && ctx.Err() != nil {
		err = ctx.Err()
	}
	return out, err
}

func (in *inspection) failed(err error) {
	if errors.Is(err, context.DeadlineExceeded) || errors.Is(err, context.Canceled) || in.ctx.Err() != nil {
		in.fail("timeout")
		return
	}
	in.fail("inspect_failed")
}

func (in *inspection) available(name string) bool {
	if in.probe.LookPath == nil {
		return false
	}
	_, err := in.probe.LookPath(name)
	return err == nil
}

// Inspect evaluates the forward hooks for two flows: LAN -> TUN device (what
// the main router hands to this host) and TUN device -> LAN (the replies).
func Inspect(ctx context.Context, probe Probe, target Target) Result {
	now := time.Now
	if probe.Now != nil {
		now = probe.Now
	}
	ctx, cancel := context.WithTimeout(ctx, TotalBudget)
	defer cancel()
	in := &inspection{ctx: ctx, probe: probe, target: target}
	in.lan = defaultRouteInterfaces(probe, target.Device)

	nftLoaded, nftKnown := nfTablesPresent(in)
	legacyLoaded := legacyFilterLoaded(in)

	var nftDoc *nftRuleset
	if nftLoaded {
		if in.available("nft") {
			out, err := in.run("nft", "-j", "list", "ruleset")
			if err != nil {
				in.failed(err)
			} else if doc, err := parseNFT(out); err != nil {
				in.fail("inspect_failed")
			} else {
				nftDoc = doc
			}
		} else {
			in.fail("nft_unavailable")
		}
	}

	iptablesNFTRead := false
	backend := ""
	if in.available("iptables-save") {
		backend = iptablesBackend(probe)
		// iptables-nft has nothing to show without an ip filter table, and
		// listing it anyway could create base chains on old iptables releases.
		if backend == "nft" && nftLoaded && (nftDoc == nil || nftDoc.hasTable("ip", "filter")) {
			iptablesNFTRead = in.readIptables("iptables", backend)
		} else if backend == "legacy" && legacyLoaded {
			in.readIptables("iptables", backend)
		}
	}
	if backend != "legacy" && legacyLoaded {
		if in.available("iptables-legacy-save") {
			in.readIptables("iptables-legacy", "legacy")
		} else if backend == "" {
			in.fail("inspect_failed")
		}
	}

	firewalldOwned := in.inspectFirewalld()
	if nftDoc != nil {
		in.evaluateNFT(nftDoc, iptablesNFTRead, firewalldOwned)
	}
	if !nftKnown && in.hooks == 0 {
		// Nothing was inspected and nf_tables may still be filtering.
		in.fail("nft_state_unknown")
	}
	return in.result(now())
}

// readIptables evaluates one backend's filter table; binary is the command
// name the remedies use (iptables or iptables-legacy).
func (in *inspection) readIptables(binary, backend string) bool {
	out, err := in.run(binary+"-save", "-t", "filter")
	if err != nil {
		in.failed(err)
		return false
	}
	table, ok := parseIptablesSave(out)
	if !ok {
		in.fail("inspect_failed")
		return false
	}
	in.evaluateIptables(table, binary, backend)
	return true
}

func statOK(probe Probe, path string) bool {
	_, err := probe.Stat(path)
	return err == nil
}

// nfTablesPresent reports whether nf_tables is in the kernel, and whether that
// answer is known. A built-in nf_tables has no /sys/module entry, so the
// kernel's modules.builtin list is read too; auto-redirect itself uses it.
func nfTablesPresent(in *inspection) (present, known bool) {
	if in.probe.Stat != nil && statOK(in.probe, "/sys/module/nf_tables") {
		return true, true
	}
	if in.target.AutoRedirect {
		return true, true
	}
	if in.probe.ReadFile == nil {
		return false, false
	}
	release, err := in.probe.ReadFile("/proc/sys/kernel/osrelease")
	name := strings.TrimSpace(string(release))
	if err != nil || name == "" || strings.ContainsAny(name, "/ \t\n") {
		return false, false
	}
	data, err := in.probe.ReadFile("/lib/modules/" + name + "/modules.builtin")
	if err != nil {
		return false, false
	}
	for _, line := range strings.Split(string(data), "\n") {
		if line = strings.TrimSpace(line); line == "nf_tables.ko" || strings.HasSuffix(line, "/nf_tables.ko") {
			return true, true
		}
	}
	return false, true
}

// exitCode is the command's exit status, or -1 when it did not exit normally.
func exitCode(err error) int {
	var coded interface{ ExitCode() int }
	if errors.As(err, &coded) {
		return coded.ExitCode()
	}
	return -1
}

// firewall-cmd exit statuses (firewalld errors.py). Messages go to stderr.
const (
	firewalldNoZone     = 2
	firewalldNotRunning = 252
)

// A legacy filter table appears in ip_tables_names only once loaded. Running
// iptables-legacy-save before that would load it as a side effect.
func legacyFilterLoaded(in *inspection) bool {
	if in.probe.ReadFile == nil {
		return false
	}
	data, err := in.probe.ReadFile("/proc/net/ip_tables_names")
	if err != nil {
		if !os.IsNotExist(err) {
			in.fail("inspect_failed")
		}
		return false
	}
	for _, line := range strings.Split(string(data), "\n") {
		if strings.TrimSpace(line) == "filter" {
			return true
		}
	}
	return false
}

func iptablesBackend(probe Probe) string {
	path := "iptables-save"
	if probe.LookPath != nil {
		if found, err := probe.LookPath("iptables-save"); err == nil {
			path = found
		}
	}
	if probe.EvalSymlinks != nil {
		if resolved, err := probe.EvalSymlinks(path); err == nil {
			path = resolved
		}
	}
	base := filepath.Base(path)
	if strings.Contains(base, "nft") {
		return "nft"
	}
	// Releases before 1.8 only had the legacy backend and no -nft/-legacy name.
	return "legacy"
}

func defaultRouteInterfaces(probe Probe, device string) []string {
	if probe.ReadFile == nil {
		return nil
	}
	data, err := probe.ReadFile("/proc/net/route")
	if err != nil {
		return nil
	}
	seen := map[string]bool{}
	var result []string
	for _, line := range strings.Split(string(data), "\n")[1:] {
		fields := strings.Fields(line)
		if len(fields) < 8 || fields[1] != "00000000" || fields[7] != "00000000" || fields[0] == device || seen[fields[0]] {
			continue
		}
		seen[fields[0]] = true
		result = append(result, fields[0])
	}
	return result
}

// inspectFirewalld reports whether nft's firewalld table is accounted for:
// either judged here through the zone of the TUN device, or left out because
// firewalld's state could not be read.
func (in *inspection) inspectFirewalld() bool {
	if !in.available("firewall-cmd") {
		return false
	}
	out, err := in.run("firewall-cmd", "--state")
	state := strings.TrimSpace(out)
	switch {
	case err == nil && state == "running":
	case exitCode(err) == firewalldNotRunning || (err == nil && state == "not running"):
		return false // its leftover table is evaluated like any other
	default:
		in.commandFailed(err)
		return true
	}
	in.hooks++
	device := in.target.Device
	zone := ""
	if DevicePattern.MatchString(device) {
		out, err := in.run("firewall-cmd", "--get-zone-of-interface="+device)
		value := strings.TrimSpace(out)
		switch {
		case err == nil && value == "trusted":
			return true
		case err == nil && value != "" && value != "no zone" && !strings.ContainsAny(value, " \t\n"):
			zone = value
		case exitCode(err) == firewalldNoZone || (err == nil && value == "no zone"):
		default:
			// A timeout or D-Bus error says nothing about the zone.
			in.commandFailed(err)
			return true
		}
	}
	current := "未指定"
	if zone != "" {
		current = zone
	}
	in.findings = append(in.findings, finding{source: Source{Kind: "firewalld", Detail: clip("firewalld 正在运行，" + displayDevice(device) + " 不在 trusted 区域（当前：" + current + "）")}})
	in.addRemedy(firewalldRemedy(device, zone))
	return true
}

func (in *inspection) commandFailed(err error) {
	if err != nil {
		in.failed(err)
		return
	}
	in.fail("inspect_failed")
}

func (in *inspection) addRemedy(remedy Remedy) {
	if len(remedy.Commands) == 0 && remedy.Source == "" {
		return
	}
	for _, existing := range in.remedies {
		if strings.Join(existing.Commands, "\n") == strings.Join(remedy.Commands, "\n") && existing.Source == remedy.Source {
			return
		}
	}
	in.remedies = append(in.remedies, remedy)
}

func (in *inspection) result(now time.Time) Result {
	result := Result{Protocol: 1, Device: in.target.Device, AutoRedirect: in.target.AutoRedirect, Sources: []Source{}, Remedies: []Remedy{}, CheckedAt: now.UTC().Format(time.RFC3339)}
	if !DevicePattern.MatchString(result.Device) {
		result.Device = ""
	}
	dropPolicy := false
	seen := map[string]bool{}
	for _, f := range in.findings {
		dropPolicy = dropPolicy || f.policy
		key := f.source.Kind + "|" + f.source.Backend + "|" + f.source.Detail
		if seen[key] || len(result.Sources) >= maxSources {
			continue
		}
		seen[key] = true
		result.Sources = append(result.Sources, f.source)
	}
	switch {
	case len(in.findings) > 0:
		result.State = StateMayDrop
		for _, remedy := range in.remedies {
			if len(result.Remedies) >= maxRemedies {
				break
			}
			if len(remedy.Commands) > 0 {
				result.Remedies = append(result.Remedies, remedy)
			}
		}
		result.Hint = hint(in.target, len(result.Remedies) > 0)
		result.Verify = verifyText
	case in.incomplete != "":
		result.State, result.Reason = StateUnknown, in.incomplete
	default:
		result.State = StateOK
	}
	if dropPolicy {
		result.Policy = "drop"
	} else if in.hooks > 0 || result.State == StateOK {
		result.Policy = "accept"
	}
	return result
}

func displayDevice(device string) string {
	if DevicePattern.MatchString(device) {
		return device
	}
	return "TUN 设备"
}

func clip(value string) string {
	value = strings.Map(func(r rune) rune {
		if r < 0x20 || r == 0x7f {
			return ' '
		}
		return r
	}, value)
	runes := []rune(value)
	if len(runes) > maxDetail {
		return string(runes[:maxDetail-1]) + "…"
	}
	return value
}

// tri is a three-valued match: a rule definitely does not, may, or definitely
// does match a flow.
type tri int8

const (
	no tri = iota
	maybe
	yes
)

func and(a, b tri) tri {
	if a < b {
		return a
	}
	return b
}

func negate(value tri, negated bool) tri {
	if !negated || value == maybe {
		return value
	}
	if value == yes {
		return no
	}
	return yes
}

// Interfaces that are never the LAN uplink of a router-split host. Used only
// when the default route cannot be read, so container bridges do not look like
// possible LAN interfaces.
var nonLANPrefixes = []string{"docker", "br-", "veth", "virbr", "lxcbr", "lxdbr", "cni", "podman", "flannel", "cali", "kube-"}

type side struct {
	device string   // set for the TUN side
	lan    []string // nil = unknown LAN
}

type flow struct {
	in, out side
	reply   bool // TUN -> LAN packets answer forwarded connections
}

func (in *inspection) flows() []flow {
	device := side{device: in.target.Device}
	lan := side{lan: in.lan}
	return []flow{{in: lan, out: device}, {in: device, out: lan, reply: true}}
}

func interfaceMatches(pattern, name string, wildcard byte) bool {
	if n := len(pattern); n > 0 && pattern[n-1] == wildcard {
		return strings.HasPrefix(name, pattern[:n-1])
	}
	return pattern == name
}

func matchInterface(pattern string, s side, wildcard byte) tri {
	if s.device != "" {
		if interfaceMatches(pattern, s.device, wildcard) {
			return yes
		}
		return no
	}
	if s.lan == nil {
		if pattern == "lo" {
			return no
		}
		for _, prefix := range nonLANPrefixes {
			if strings.HasPrefix(pattern, prefix) {
				return no
			}
		}
		return maybe
	}
	matched := 0
	for _, name := range s.lan {
		if interfaceMatches(pattern, name, wildcard) {
			matched++
		}
	}
	switch matched {
	case 0:
		return no
	case len(s.lan):
		return yes
	}
	return maybe
}

func matchAnyInterface(patterns []string, s side, wildcard byte) tri {
	result := no
	for _, pattern := range patterns {
		if value := matchInterface(pattern, s, wildcard); value > result {
			result = value
		}
	}
	return result
}
