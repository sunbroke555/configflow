// Package netfilter inspects, read-only, whether the host firewall may drop
// the traffic a full-TUN Mihomo host handles for the LAN: packets forwarded
// between the LAN and the TUN device, TCP that auto-redirect hands to a local
// port, TCP that the TUN system stack delivers to Mihomo, and DNS that a
// local server on this host (OxiDNS on an all-in-one host) answers.
//
// It never changes firewall state. Every command it runs only lists rules, and
// it avoids listings with side effects: the legacy iptables filter table is
// only read when the kernel already has it loaded, and nftables views are only
// read when nf_tables is present (loaded module, built into the kernel, or in
// use by Mihomo's auto-redirect). Any failure degrades to "unknown".
package netfilter

import (
	"context"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"net"
	"net/netip"
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
	Protocol     int    `json:"protocol"`
	State        string `json:"state"`
	Reason       string `json:"reason"`
	Device       string `json:"device"`
	AutoRedirect bool   `json:"auto_redirect"`
	// Policy and InputPolicy are the real default policies of the inspected
	// forward and input hooks ("drop" when any of them drops), whatever the
	// verdict: accept rules can make a DROP policy harmless.
	Policy      string `json:"policy"`
	InputPolicy string `json:"input_policy"`
	// Flows lists the blocked paths: forward (LAN <-> TUN), dns (DNS to this
	// host that auto-redirect DNATs into the TUN), local_dns (DNS to this
	// host's LAN address that a local server answers, outside the TUN),
	// redirect_tcp (TCP that auto-redirect hands to a local port) and tun_tcp
	// (TCP the TUN system stack delivers to Mihomo).
	Flows     []string `json:"flows"`
	Sources   []Source `json:"sources"`
	Remedies  []Remedy `json:"remedies"`
	Hint      string   `json:"hint"`
	Verify    string   `json:"verify"`
	CheckedAt string   `json:"checked_at"`
	Cached    bool     `json:"cached"`
}

type Source struct {
	Kind    string `json:"kind"`    // docker | iptables | nftables | firewalld | ufw
	Backend string `json:"backend"` // nft | legacy for iptables-based sources
	Hook    string `json:"hook"`    // forward | input
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
	// Stack is tun.stack in lower case; "" is Mihomo's default (gvisor). The
	// system and mixed stacks hand TCP to a kernel listener of Mihomo.
	Stack string
	// DNSHijack: auto-redirect DNATs DNS that LAN devices send to this host
	// into the TUN (dns-hijack on port 53, host address not excluded).
	DNSHijack bool
	// TUNPeer is the source address the TUN system stack gives TCP it hands
	// to Mihomo: the address after tun.inet4-address. Zero means Mihomo's
	// default, 198.18.0.2 (inet4-address 198.18.0.1/30).
	TUNPeer netip.Addr
	// LAN lists this host's LAN-side IPv4 addresses (LANAddresses). A DNS
	// listener bound to one of them, or to the wildcard address, is reachable
	// from LAN devices.
	LAN []net.IP
	// ServesDNS: a DNS service installed on this host (OxiDNS or MosDNS of an
	// all-in-one install) answers port 53, even before it listens. Otherwise
	// a listener on port 53 is looked for in /proc/net.
	ServesDNS bool
}

var defaultTUNPeer = netip.MustParseAddr("198.18.0.2")

// Flow names reported in Result.Flows, in display order.
const (
	FlowForward     = "forward"
	FlowDNS         = "dns"
	FlowLocalDNS    = "local_dns"
	FlowRedirectTCP = "redirect_tcp"
	FlowTUNTCP      = "tun_tcp"
)

var flowOrder = []string{FlowForward, FlowDNS, FlowLocalDNS, FlowRedirectTCP, FlowTUNTCP}

// dnsPort is where LAN devices send DNS to this host.
const dnsPort = 53

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
	maxCommands = 12
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
	// skipped: a strict flow passed a drop that may apply, or a drop after a
	// rule that may already have accepted it; the view of it is not complete.
	skipped   bool
	ports     portSpan // ephemeral ports auto-redirect and the TUN stack listen on
	hooks     int      // inspected forward and input hooks
	policies  map[string]string
	ufwActive bool
	firewalld bool // firewalld is running and its state was read
	remedies  []*remedyPlan
	// localDNS lists the protocols (udp, tcp) of a DNS server on this host
	// that LAN devices reach outside the TUN; empty when there is none.
	localDNS []string
}

type finding struct {
	source Source
	policy bool
	flow   string
	proto  string // layer 4 protocol of the flow, "" when not split by protocol
}

// remedyPlan collects the blocked flows of one place allow rules can go; its
// commands are generated once every hook has been evaluated.
type remedyPlan struct {
	source, binary, family, table, chain string
	label                                string
	flows                                map[string]bool
	// firewalld: the zones of the LAN interfaces, where DNS is allowed.
	lanZones []string
}

// notePolicy keeps the strictest real default policy per hook.
func (in *inspection) notePolicy(hook, policy string) {
	if in.policies == nil {
		in.policies = map[string]string{}
	}
	policy = strings.ToLower(policy)
	if policy != "drop" {
		policy = "accept"
	}
	if in.policies[hook] != "drop" {
		in.policies[hook] = policy
	}
}

func (in *inspection) plan(source, binary, family, table, chain, label string) *remedyPlan {
	for _, existing := range in.remedies {
		if existing.source == source && existing.binary == binary && existing.family == family && existing.table == table && existing.chain == chain {
			return existing
		}
	}
	plan := &remedyPlan{source: source, binary: binary, family: family, table: table, chain: chain, label: label, flows: map[string]bool{}}
	in.remedies = append(in.remedies, plan)
	return plan
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
	in := &inspection{ctx: ctx, probe: probe, target: target, ports: localPortRange(probe)}
	in.lan = defaultRouteInterfaces(probe, target.Device)
	if !target.DNSHijack {
		// Hijacked DNS is DNAT-ed into the TUN (flow dns); otherwise DNS to
		// this host's LAN address reaches a local server through the input hook.
		in.localDNS = localDNSProtocols(probe, target)
	}

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

	firewalldSkipped := in.inspectFirewalld()
	if nftDoc != nil {
		in.evaluateNFT(nftDoc, iptablesNFTRead, firewalldSkipped)
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

// firewall-cmd exit status when firewalld is not running (firewalld
// errors.py). Messages go to stderr.
const firewalldNotRunning = 252

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

// inspectFirewalld reads whether firewalld runs. Its own nftables table, or
// its iptables chains, are then evaluated like any other rules but attributed
// to firewalld. The result reports that firewalld's table must be left out
// because its state could not be read.
func (in *inspection) inspectFirewalld() bool {
	if !in.available("firewall-cmd") {
		return false
	}
	out, err := in.run("firewall-cmd", "--state")
	state := strings.TrimSpace(out)
	switch {
	case err == nil && state == "running":
		in.firewalld = true
		return false
	case exitCode(err) == firewalldNotRunning || (err == nil && state == "not running"):
		return false // its leftover table is evaluated like any other
	default:
		in.commandFailed(err)
		return true
	}
}

func (in *inspection) commandFailed(err error) {
	if err != nil {
		in.failed(err)
		return
	}
	in.fail("inspect_failed")
}

func (in *inspection) record(f finding) {
	in.findings = append(in.findings, f)
}

func (in *inspection) result(now time.Time) Result {
	result := Result{Protocol: 1, Device: in.target.Device, AutoRedirect: in.target.AutoRedirect, Flows: []string{}, Sources: []Source{}, Remedies: []Remedy{}, CheckedAt: now.UTC().Format(time.RFC3339)}
	if !DevicePattern.MatchString(result.Device) {
		result.Device = ""
	}
	blocked := map[string]bool{}
	seen := map[string]bool{}
	for _, f := range in.findings {
		blocked[f.flow] = true
		if f.proto != "" {
			blocked[f.flow+"/"+f.proto] = true
		}
		key := f.source.Kind + "|" + f.source.Backend + "|" + f.source.Hook + "|" + f.source.Detail
		if seen[key] || len(result.Sources) >= maxSources {
			continue
		}
		seen[key] = true
		result.Sources = append(result.Sources, f.source)
	}
	for _, name := range flowOrder {
		if blocked[name] {
			result.Flows = append(result.Flows, name)
		}
	}
	switch {
	case len(in.findings) > 0:
		result.State = StateMayDrop
		for _, plan := range in.remedies {
			if len(result.Remedies) >= maxRemedies {
				break
			}
			if remedy := in.remedy(plan); len(remedy.Commands) > 0 {
				result.Remedies = append(result.Remedies, remedy)
			}
		}
		result.Hint = hint(in.target, blocked, in.incomplete == "" && !in.skipped, len(result.Remedies) > 0)
		result.Verify = verify(in.target, blocked)
	case in.incomplete != "":
		result.State, result.Reason = StateUnknown, in.incomplete
	default:
		result.State = StateOK
	}
	result.Policy, result.InputPolicy = in.policies["forward"], in.policies["input"]
	if result.State == StateOK {
		if result.Policy == "" {
			result.Policy = "accept"
		}
		if result.InputPolicy == "" {
			result.InputPolicy = "accept"
		}
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
	none   bool     // no interface: the output side of an input hook
}

// ctFacts describe what conntrack reports for a flow's packets. Forward flows
// only know whether they are replies; input flows are new connections.
type ctFacts struct {
	known bool // states below are exact
	isNew bool
}

type flow struct {
	name    string
	hook    string // forward | input
	in, out side
	reply   bool // TUN -> LAN packets answer forwarded connections
	proto   string
	dnat    tri // conntrack status DNAT
	local   tri // destination is an address of this host
	ct      ctFacts
	dport   portSpan   // destination ports the flow may use; zero when any
	saddr   netip.Addr // source address when known
	// strict flows report only drops that certainly apply. Input chains are
	// full of port and address conditions that rarely concern these flows.
	strict bool
}

// kernelStack reports whether the TUN stack hands TCP to a kernel listener of
// Mihomo, which the input hook then filters (iifname is the TUN device).
func kernelStack(stack string) bool {
	return stack == "system" || stack == "mixed"
}

func (in *inspection) flows(hook string) []flow {
	device := side{device: in.target.Device}
	lan := side{lan: in.lan}
	if hook == "input" {
		var flows []flow
		input := ctFacts{known: true, isNew: true}
		// Both listen on a port the kernel picks from its ephemeral range.
		ports := in.ports
		if ports.hi == 0 {
			ports = defaultLocalPorts
		}
		if in.target.AutoRedirect {
			flows = append(flows, flow{name: FlowRedirectTCP, hook: hook, in: lan, out: side{none: true}, proto: "tcp", dnat: yes, local: yes, ct: input, dport: ports, strict: true})
		}
		if kernelStack(in.target.Stack) && (!in.target.AutoRedirect || in.target.DNSHijack) {
			peer := in.target.TUNPeer
			if !peer.IsValid() {
				peer = defaultTUNPeer
			}
			flows = append(flows, flow{name: FlowTUNTCP, hook: hook, in: device, out: side{none: true}, proto: "tcp", dnat: no, local: yes, ct: input, dport: ports, saddr: peer, strict: true})
		}
		// New DNS queries from the LAN to this host's own address, port 53.
		for _, proto := range in.localDNS {
			flows = append(flows, flow{name: FlowLocalDNS, hook: hook, in: lan, out: side{none: true}, proto: proto, dnat: no, local: yes, ct: input, dport: portSpan{dnsPort, dnsPort}, strict: true})
		}
		return flows
	}
	// Forwarded traffic per protocol: auto-redirect takes TCP out of the
	// forward hook, and some firewalls (UFW) accept forwarded ping only.
	protocols := []string{"udp", "icmp"}
	if !in.target.AutoRedirect {
		protocols = []string{"tcp", "udp", "icmp"}
	}
	var flows []flow
	for _, proto := range protocols {
		flows = append(flows,
			flow{name: FlowForward, hook: hook, in: lan, out: device, proto: proto, dnat: no, local: no},
			flow{name: FlowForward, hook: hook, in: device, out: lan, reply: true, proto: proto, dnat: no, local: no})
	}
	if in.target.DNSHijack {
		flows = append(flows,
			flow{name: FlowDNS, hook: hook, in: lan, out: device, dnat: yes, local: no},
			flow{name: FlowDNS, hook: hook, in: device, out: lan, reply: true, dnat: yes, local: no})
	}
	return flows
}

func interfaceMatches(pattern, name string, wildcard byte) bool {
	if n := len(pattern); n > 0 && pattern[n-1] == wildcard {
		return strings.HasPrefix(name, pattern[:n-1])
	}
	return pattern == name
}

func matchInterface(pattern string, s side, wildcard byte) tri {
	if s.none {
		return no
	}
	if s.device != "" {
		if interfaceMatches(pattern, s.device, wildcard) {
			return yes
		}
		return no
	}
	if s.lan == nil {
		if pattern == "lo" || nonLANInterface(pattern) {
			return no
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

// localDNSProtocols lists the protocols of a DNS server on this host that LAN
// devices reach: a socket on port 53 bound to the wildcard address or a LAN
// address (a loopback-only stub such as systemd-resolved does not count).
// An installed DNS service counts for both before it listens.
func localDNSProtocols(probe Probe, target Target) []string {
	if target.ServesDNS {
		return []string{"udp", "tcp"}
	}
	if probe.ReadFile == nil {
		return nil
	}
	var udp, tcp bool
	for _, table := range []struct {
		path string
		tcp  bool
	}{{"/proc/net/udp", false}, {"/proc/net/tcp", true}, {"/proc/net/udp6", false}, {"/proc/net/tcp6", true}} {
		data, err := probe.ReadFile(table.path)
		if err != nil {
			continue
		}
		if dnsSocket(string(data), table.tcp, target.LAN) {
			if table.tcp {
				tcp = true
			} else {
				udp = true
			}
		}
	}
	var result []string
	if udp {
		result = append(result, "udp")
	}
	if tcp {
		result = append(result, "tcp")
	}
	return result
}

// dnsSocket reads one /proc/net/{tcp,udp}[6] table for a listening TCP socket
// (state 0A) or an unconnected UDP socket on port 53 that LAN devices reach.
func dnsSocket(table string, tcp bool, lan []net.IP) bool {
	lines := strings.Split(table, "\n")
	for _, line := range lines[1:] {
		fields := strings.Fields(line)
		if len(fields) < 4 {
			continue
		}
		local, remote, state := fields[1], fields[2], fields[3]
		if tcp && state != "0A" {
			continue
		}
		if !tcp && strings.Trim(strings.ReplaceAll(remote, ":", ""), "0") != "" {
			continue // a connected UDP socket
		}
		index := strings.LastIndex(local, ":")
		if index < 0 || local[index+1:] != "0035" {
			continue
		}
		addr, ok := procAddress(local[:index])
		if !ok {
			continue
		}
		if addr.IsUnspecified() {
			return true
		}
		addr = addr.Unmap()
		for _, ip := range lan {
			if candidate, ok := netip.AddrFromSlice(ip.To4()); ok && candidate == addr {
				return true
			}
		}
	}
	return false
}

// procAddress decodes a /proc/net address: 32-bit words in hex, each printed
// in the host's byte order.
func procAddress(value string) (netip.Addr, bool) {
	raw, err := hex.DecodeString(value)
	if err != nil || (len(raw) != 4 && len(raw) != 16) {
		return netip.Addr{}, false
	}
	bytes := make([]byte, len(raw))
	for i := 0; i < len(raw); i += 4 {
		binary.NativeEndian.PutUint32(bytes[i:], binary.BigEndian.Uint32(raw[i:]))
	}
	addr, ok := netip.AddrFromSlice(bytes)
	return addr, ok
}
