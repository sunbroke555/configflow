package netfilter

import (
	"net/netip"
	"strings"
)

type iptRule struct {
	chain       string
	text        string
	in, out     string
	inNeg       bool
	outNeg      bool
	target      string
	isGoto      bool
	conditional bool
	invalidOnly bool
	states      []string
	statesNeg   bool
	proto       string
	protoNeg    bool
	dstTypes    []string
	dstTypesNeg bool
	dports      []portSpan // destination ports; nil when the rule has none
	dportsNeg   bool
	tcpFlags    *tcpFlags
	tcpFlagsNeg bool
	source      *netip.Prefix // -s; nil when the rule has none
	sourceNeg   bool
}

type iptTable struct {
	policies map[string]string // builtin chain -> policy; user chains -> "-"
	rules    map[string][]iptRule
}

// parseIptablesSave reads `iptables-save -t filter` output. It reports false
// when the output is not a usable filter table (e.g. an incompatible table).
func parseIptablesSave(output string) (*iptTable, bool) {
	table := &iptTable{policies: map[string]string{}, rules: map[string][]iptRule{}}
	inFilter := false
	for _, raw := range strings.Split(output, "\n") {
		line := strings.TrimSpace(raw)
		switch {
		case line == "":
		case strings.HasPrefix(line, "#"):
			if strings.Contains(line, "incompatible") {
				return nil, false
			}
		case strings.HasPrefix(line, "*"):
			inFilter = line == "*filter"
		case line == "COMMIT":
			inFilter = false
		case !inFilter:
		case strings.HasPrefix(line, ":"):
			fields := strings.Fields(line[1:])
			if len(fields) >= 2 {
				table.policies[fields[0]] = fields[1]
			}
		case strings.HasPrefix(line, "-A "):
			rule, ok := parseIptablesRule(line)
			if !ok {
				return nil, false
			}
			table.rules[rule.chain] = append(table.rules[rule.chain], rule)
		}
	}
	if len(table.policies) == 0 && len(table.rules) == 0 {
		// An empty listing means no filter table: nothing can drop.
		return table, true
	}
	if _, ok := table.policies["FORWARD"]; !ok && len(table.rules["FORWARD"]) > 0 {
		return nil, false
	}
	return table, true
}

// splitArgs follows iptables-save quoting: double quotes with backslash escapes.
func splitArgs(line string) ([]string, bool) {
	var args []string
	var current strings.Builder
	inQuote, escaped, started := false, false, false
	for _, r := range line {
		switch {
		case escaped:
			current.WriteRune(r)
			escaped = false
		case r == '\\':
			escaped, started = true, true
		case r == '"':
			inQuote, started = !inQuote, true
		case !inQuote && (r == ' ' || r == '\t'):
			if started {
				args = append(args, current.String())
				current.Reset()
				started = false
			}
		default:
			current.WriteRune(r)
			started = true
		}
	}
	if inQuote || escaped {
		return nil, false
	}
	if started {
		args = append(args, current.String())
	}
	return args, true
}

func parseIptablesRule(line string) (iptRule, bool) {
	args, ok := splitArgs(line)
	if !ok || len(args) < 2 {
		return iptRule{}, false
	}
	rule := iptRule{chain: args[1], text: strings.TrimPrefix(line, "-A ")}
	negated := false
	module := ""
	for i := 2; i < len(args); i++ {
		arg := args[i]
		next := func() string {
			if i+1 < len(args) {
				i++
				return args[i]
			}
			return ""
		}
		switch arg {
		case "!":
			negated = true
			continue
		case "-p", "--protocol":
			rule.proto, rule.protoNeg = strings.ToLower(next()), negated
		case "--dst-type":
			if module == "addrtype" {
				rule.dstTypes, rule.dstTypesNeg = strings.Split(strings.ToUpper(next()), ","), negated
			} else {
				next()
				rule.conditional = true
			}
		case "-s", "--source":
			if prefix, ok := parsePrefix(next()); ok {
				rule.source, rule.sourceNeg = &prefix, negated
			} else {
				rule.conditional = true
			}
		case "-i", "--in-interface":
			rule.in, rule.inNeg = next(), negated
		case "-o", "--out-interface":
			rule.out, rule.outNeg = next(), negated
		case "-j", "--jump", "-g", "--goto":
			rule.target, rule.isGoto = next(), arg == "-g" || arg == "--goto"
			// Remaining arguments configure the target, not the match.
			return rule.finish(), true
		case "-m", "--match":
			module = next()
		case "--comment":
			next()
		case "--ctstate", "--state":
			if (module == "conntrack" && arg == "--ctstate") || (module == "state" && arg == "--state") {
				rule.states, rule.statesNeg = strings.Split(strings.ToUpper(next()), ","), negated
			} else {
				next()
				rule.conditional = true
			}
		case "--dport", "--destination-port", "--dports", "--destination-ports":
			multi := arg == "--dports" || arg == "--destination-ports"
			value := next()
			if spans, ok := parsePortList(value, multi); ok && (multi == (module == "multiport")) {
				rule.dports, rule.dportsNeg = spans, negated
			} else {
				rule.conditional = true
			}
		case "--syn":
			// Saved as --tcp-flags FIN,SYN,RST,ACK SYN by current releases.
			rule.tcpFlags, rule.tcpFlagsNeg = &tcpFlags{mask: synMask, comp: tcpFlagSYN}, negated
		case "--tcp-flags":
			mask, maskOK := parseTCPFlags(next())
			comp, compOK := parseTCPFlags(next())
			if maskOK && compOK {
				rule.tcpFlags, rule.tcpFlagsNeg = &tcpFlags{mask: mask, comp: comp}, negated
			} else {
				rule.conditional = true
			}
		case "-c", "--set-counters":
			next()
			next()
		default:
			rule.conditional = true
			// Skip option values; a leading "-" starts the next option.
			for i+1 < len(args) && !strings.HasPrefix(args[i+1], "-") && args[i+1] != "!" {
				i++
			}
		}
		negated = false
	}
	return rule.finish(), true
}

func (rule iptRule) finish() iptRule {
	if len(rule.states) == 1 && rule.states[0] == "INVALID" && !rule.statesNeg {
		rule.invalidOnly = true
	}
	return rule
}

func (rule iptRule) match(f flow) tri {
	result := yes
	if rule.in != "" {
		result = and(result, negate(matchInterface(rule.in, f.in, '+'), rule.inNeg))
	}
	if rule.out != "" {
		result = and(result, negate(matchInterface(rule.out, f.out, '+'), rule.outNeg))
	}
	if rule.proto != "" {
		result = and(result, negate(protoMatch([]string{rule.proto}, f), rule.protoNeg))
	}
	if len(rule.dstTypes) > 0 {
		result = and(result, negate(addressTypeMatch(rule.dstTypes, f), rule.dstTypesNeg))
	}
	if len(rule.states) > 0 {
		result = and(result, stateMatch(rule.states, rule.statesNeg, f))
	}
	if rule.dports != nil {
		result = and(result, negate(portMatch(rule.dports, f), rule.dportsNeg))
	}
	if rule.source != nil {
		result = and(result, negate(sourceMatch([]netip.Prefix{*rule.source}, f), rule.sourceNeg))
	}
	if rule.tcpFlags != nil {
		result = and(result, negate(rule.tcpFlags.match(f), rule.tcpFlagsNeg))
	}
	if rule.conditional {
		result = and(result, maybe)
	}
	return result
}

// protoMatch compares layer 4 protocol names; flows without a known protocol
// may match anything but "all".
func protoMatch(protocols []string, f flow) tri {
	for _, protocol := range protocols {
		switch protocol = strings.ToLower(protocol); protocol {
		case "all", "0":
			return yes
		case "6":
			protocol = "tcp"
		case "17":
			protocol = "udp"
		}
		if f.proto == "" {
			return maybe
		}
		if protocol == f.proto {
			return yes
		}
	}
	return no
}

// addressTypeMatch evaluates addrtype --dst-type (and nft fib daddr type):
// input flows go to a local unicast address, forward flows to a remote one.
func addressTypeMatch(types []string, f flow) tri {
	result := no
	for _, kind := range types {
		value := maybe
		switch strings.ToUpper(kind) {
		case "LOCAL":
			value = f.local
		case "UNICAST":
			value = yes
		case "BROADCAST", "MULTICAST", "ANYCAST", "BLACKHOLE", "UNREACHABLE", "PROHIBIT", "THROW", "NAT", "XRESOLVE":
			value = no
		}
		if value > result {
			result = value
		}
	}
	return result
}

// stateMatch evaluates conntrack states. Input flows are new connections,
// DNAT-ed or not. For forward flows, replies are ESTABLISHED and new
// connections from the LAN stay uncertain.
func stateMatch(states []string, negated bool, f flow) tri {
	if f.ct.known {
		result := no
		for _, state := range states {
			switch strings.ToUpper(state) {
			case "NEW":
				if f.ct.isNew {
					result = yes
				}
			case "DNAT":
				if f.dnat > result {
					result = f.dnat
				}
			case "ESTABLISHED", "RELATED", "INVALID", "UNTRACKED", "SNAT":
			default:
				if result < maybe {
					result = maybe
				}
			}
		}
		return negate(result, negated)
	}
	if negated {
		return maybe
	}
	for _, state := range states {
		if strings.EqualFold(state, "ESTABLISHED") && f.reply {
			return yes
		}
		if strings.EqualFold(state, "DNAT") && f.dnat == yes {
			return yes
		}
	}
	return maybe
}

var nonTerminatingTargets = map[string]bool{
	"LOG": true, "NFLOG": true, "ULOG": true, "MARK": true, "CONNMARK": true, "TCPMSS": true, "CT": true,
	"NOTRACK": true, "CLASSIFY": true, "DSCP": true, "TOS": true, "TTL": true, "HL": true, "AUDIT": true,
	"TRACE": true, "SET": true, "CHECKSUM": true, "CONNSECMARK": true, "SECMARK": true, "IDLETIMER": true,
	"LED": true, "TEE": true, "RATEEST": true,
}

type verdict int

const (
	fallThrough verdict = iota
	accepted
	dropped
	returned
)

type iptEvaluation struct {
	in      *inspection
	table   *iptTable
	binary  string
	backend string
	hook    string
	found   []iptFinding
}

type iptFinding struct {
	chains  []string
	forward int // index of the builtin chain's rule this was reached through
	policy  bool
	rule    string
	unknown bool
	flow    string
}

var iptablesHooks = []struct{ hook, chain string }{{"forward", "FORWARD"}, {"input", "INPUT"}}

func (in *inspection) evaluateIptables(table *iptTable, binary, backend string) {
	if len(table.policies) == 0 && len(table.rules) == 0 {
		return
	}
	label := "iptables"
	if backend == "legacy" && binary == "iptables-legacy" {
		label = "iptables（legacy）"
	}
	for _, hook := range iptablesHooks {
		flows := in.flows(hook.hook)
		if len(flows) == 0 {
			continue
		}
		in.hooks++
		if policy, ok := table.policies[hook.chain]; ok {
			in.notePolicy(hook.hook, policy)
		}
		e := &iptEvaluation{in: in, table: table, binary: binary, backend: backend, hook: hook.hook}
		for _, f := range flows {
			e.chain(hook.chain, f, 0, []string{hook.chain}, -1, true)
		}
		if len(e.found) == 0 {
			continue
		}
		kind := e.kind()
		var plan *remedyPlan
		if kind == "firewalld" {
			plan = in.plan(kind, "", "", "", "", "firewalld")
		} else {
			plan = in.plan(kind, binary, "", "", "", SourceLabel(kind, backend, binary))
		}
		for _, item := range e.found {
			plan.flows[item.flow] = true
			detail := ""
			chain := item.chains[len(item.chains)-1]
			switch {
			case item.policy:
				detail = label + " " + hook.chain + " 默认策略为 DROP"
				switch {
				case kind == "ufw" && hook.hook == "forward":
					detail = "UFW 已启用，默认拒绝转发（routed）；" + detail
				case kind == "ufw":
					detail = "UFW 已启用，默认拒绝入站（incoming）；" + detail
				}
			case item.unknown:
				detail = label + " " + chain + " 链有无法判断的规则：" + item.rule
			case hook.hook == "input":
				detail = label + " " + chain + " 链有会丢弃入站流量的规则：" + item.rule
			default:
				detail = label + " " + chain + " 链有可能丢弃转发流量的规则：" + item.rule
			}
			policy := ""
			if item.policy {
				policy = "drop"
			}
			// Repeated details are merged when the result is assembled; each
			// finding still marks its flow as blocked.
			in.record(finding{source: Source{Kind: kind, Backend: backend, Hook: hook.hook, Chain: chain, Policy: policy, Detail: clip(detail)}, policy: item.policy, flow: item.flow})
		}
	}
}

// kind attributes the blocking of one hook of one backend to the tool the
// user manages it with, which also decides where the allow rules must go.
func (e *iptEvaluation) kind() string {
	if e.in.firewalld && e.isChain(strings.ToUpper(e.hook)+"_direct") {
		// firewalld's iptables backend owns this table.
		return "firewalld"
	}
	if e.hook == "forward" {
		first := len(e.table.rules["FORWARD"])
		for _, item := range e.found {
			if item.forward < first {
				first = item.forward
			}
		}
		if _, ok := e.table.policies["DOCKER-USER"]; ok {
			for index, rule := range e.table.rules["FORWARD"] {
				if index > first {
					break
				}
				if rule.target == "DOCKER-USER" && !rule.isGoto && rule.in == "" && rule.out == "" && !rule.conditional && len(rule.states) == 0 {
					return "docker"
				}
			}
		}
	}
	for _, item := range e.found {
		for _, chain := range item.chains {
			if strings.HasPrefix(chain, "ufw-") || strings.HasPrefix(chain, "ufw6-") {
				return "ufw"
			}
		}
		if item.policy && e.ufwActive() {
			return "ufw"
		}
	}
	return "iptables"
}

func (e *iptEvaluation) ufwActive() bool {
	if _, ok := e.table.policies["ufw-user-"+e.hook]; !ok {
		return false
	}
	return e.in.checkUFW()
}

// checkUFW prefers ufw's own setting over `ufw status`, whose output is
// translated (zh_CN: "状态： 激活"); the command runs with LC_ALL=C otherwise.
func (in *inspection) checkUFW() bool {
	if in.ufwActive {
		return true
	}
	if in.probe.ReadFile != nil {
		if data, err := in.probe.ReadFile("/etc/ufw/ufw.conf"); err == nil {
			if enabled, ok := ufwEnabled(string(data)); ok {
				in.ufwActive = enabled
				return enabled
			}
		}
	}
	if !in.available("ufw") {
		return false
	}
	out, err := in.run("ufw", "status")
	in.ufwActive = err == nil && strings.Contains(out, "Status: active")
	return in.ufwActive
}

// ufwEnabled reads ENABLED=yes|no from /etc/ufw/ufw.conf.
func ufwEnabled(conf string) (bool, bool) {
	for _, line := range strings.Split(conf, "\n") {
		line = strings.TrimSpace(line)
		if !strings.HasPrefix(line, "ENABLED=") {
			continue
		}
		switch strings.ToLower(strings.Trim(strings.TrimPrefix(line, "ENABLED="), `"' `)) {
		case "yes":
			return true, true
		case "no":
			return false, true
		}
	}
	return false, false
}

// record keeps a possible drop. Strict flows only count drops that certainly
// apply: certain is false once the path went through a conditional jump, or
// past a rule that may have accepted the packet. A drop left out that way
// still means the view of the flow is not complete.
func (e *iptEvaluation) record(item iptFinding, f flow, m tri, certain bool) {
	if f.strict && !(certain && m == yes) {
		e.in.skipped = true
		return
	}
	item.flow = f.name
	e.found = append(e.found, item)
}

// chain evaluates one chain for a flow. The second result reports that a rule
// on the way may have accepted the packet (a conditional ACCEPT, or a jump
// whose chain may accept): no later drop, policy included, is then certain.
// Port conditions are resolved against the flow's ports first, so a service
// port allowed by the user does not count as a possible accept.
func (e *iptEvaluation) chain(name string, f flow, depth int, path []string, forward int, certain bool) (verdict, bool) {
	mayAccept, mayReturn := false, false
rules:
	for index, rule := range e.table.rules[name] {
		at := forward
		if depth == 0 {
			at = index
		}
		m := rule.match(f)
		if m == no {
			continue
		}
		sure := certain && !mayAccept && !mayReturn
		switch target := rule.target; {
		case target == "ACCEPT":
			if m == yes {
				return accepted, mayAccept
			}
			mayAccept = true
		case target == "DROP" || target == "REJECT":
			if rule.invalidOnly {
				continue
			}
			e.record(iptFinding{chains: path, forward: at, rule: rule.text}, f, m, sure)
			if m == yes {
				return dropped, mayAccept
			}
		case target == "RETURN":
			if m == yes {
				break rules
			}
			mayReturn = true
		case target == "" || nonTerminatingTargets[target]:
		case e.isChain(target):
			if depth >= 16 {
				e.record(iptFinding{chains: path, forward: at, rule: rule.text, unknown: true}, f, m, sure)
				mayAccept = true
				continue
			}
			result, inner := e.chain(target, f, depth+1, append(append([]string{}, path...), target), at, sure && m == yes)
			if inner || (m != yes && result == accepted) {
				mayAccept = true
			}
			if m != yes {
				continue
			}
			switch result {
			case accepted, dropped:
				return result, mayAccept
			default:
				if rule.isGoto {
					break rules
				}
			}
		default:
			// An unknown target may drop or accept.
			e.record(iptFinding{chains: path, forward: at, rule: rule.text, unknown: true}, f, m, sure)
			mayAccept = true
		}
	}
	// A builtin chain that returns or falls off its end applies its policy.
	if depth == 0 {
		if strings.EqualFold(e.table.policies[name], "DROP") {
			e.record(iptFinding{chains: path, forward: len(e.table.rules[name]), policy: true}, f, yes, certain && !mayAccept)
			return dropped, mayAccept
		}
		return accepted, mayAccept
	}
	return fallThrough, mayAccept
}

func (e *iptEvaluation) isChain(name string) bool {
	if _, ok := e.table.policies[name]; ok {
		return true
	}
	_, ok := e.table.rules[name]
	return ok
}
