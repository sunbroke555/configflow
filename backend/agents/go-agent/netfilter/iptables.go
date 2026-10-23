package netfilter

import (
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
	if len(rule.states) > 0 {
		result = and(result, stateMatch(rule.states, rule.statesNeg, f))
	}
	if rule.conditional {
		result = and(result, maybe)
	}
	return result
}

// Reply packets of a forwarded connection are ESTABLISHED; new connections
// from the LAN start as NEW, so state matches stay uncertain for them.
func stateMatch(states []string, negated bool, f flow) tri {
	if !f.reply || negated {
		return maybe
	}
	for _, state := range states {
		if strings.EqualFold(state, "ESTABLISHED") {
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
	found   []iptFinding
}

type iptFinding struct {
	chains  []string
	forward int // index of the FORWARD rule this was reached through
	policy  bool
	rule    string
	unknown bool
}

func (in *inspection) evaluateIptables(table *iptTable, binary, backend string) {
	if len(table.policies) == 0 && len(table.rules) == 0 {
		return
	}
	in.hooks++
	e := &iptEvaluation{in: in, table: table, binary: binary, backend: backend}
	for _, f := range in.flows() {
		e.chain("FORWARD", f, 0, []string{"FORWARD"}, -1)
	}
	if len(e.found) == 0 {
		return
	}
	kind := e.kind()
	label := "iptables"
	if backend == "legacy" && binary == "iptables-legacy" {
		label = "iptables（legacy）"
	}
	seen := map[string]bool{}
	for _, item := range e.found {
		detail := ""
		chain := item.chains[len(item.chains)-1]
		switch {
		case item.policy:
			detail = label + " FORWARD 默认策略为 DROP"
			if kind == "ufw" {
				detail = "UFW 已启用，默认拒绝转发（routed）；" + detail
			}
		case item.unknown:
			detail = label + " " + chain + " 链有无法判断的规则：" + item.rule
		default:
			detail = label + " " + chain + " 链有可能丢弃转发流量的规则：" + item.rule
		}
		if seen[detail] {
			continue
		}
		seen[detail] = true
		policy := ""
		if item.policy {
			policy = "drop"
		}
		in.findings = append(in.findings, finding{source: Source{Kind: kind, Backend: backend, Chain: chain, Policy: policy, Detail: clip(detail)}, policy: item.policy})
	}
	in.addRemedy(iptablesRemedy(kind, binary, in.target.Device))
}

// kind attributes the blocking of one backend to the tool the user manages it
// with, which also decides where the allow rules must go.
func (e *iptEvaluation) kind() string {
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
	if _, ok := e.table.policies["ufw-user-forward"]; !ok {
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

func (e *iptEvaluation) record(item iptFinding) {
	e.found = append(e.found, item)
}

func (e *iptEvaluation) chain(name string, f flow, depth int, path []string, forward int) verdict {
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
		switch target := rule.target; {
		case target == "ACCEPT":
			if m == yes {
				return accepted
			}
		case target == "DROP" || target == "REJECT":
			if rule.invalidOnly {
				continue
			}
			e.record(iptFinding{chains: path, forward: at, rule: rule.text})
			if m == yes {
				return dropped
			}
		case target == "RETURN":
			if m == yes {
				break rules
			}
		case target == "" || nonTerminatingTargets[target]:
		case e.isChain(target):
			if depth >= 16 {
				e.record(iptFinding{chains: path, forward: at, rule: rule.text, unknown: true})
				continue
			}
			result := e.chain(target, f, depth+1, append(append([]string{}, path...), target), at)
			if m != yes {
				continue
			}
			switch result {
			case accepted, dropped:
				return result
			default:
				if rule.isGoto {
					break rules
				}
			}
		default:
			e.record(iptFinding{chains: path, forward: at, rule: rule.text, unknown: true})
		}
	}
	// A builtin chain that returns or falls off its end applies its policy.
	if depth == 0 {
		if strings.EqualFold(e.table.policies[name], "DROP") {
			e.record(iptFinding{chains: path, forward: len(e.table.rules[name]), policy: true})
			return dropped
		}
		return accepted
	}
	return fallThrough
}

func (e *iptEvaluation) isChain(name string) bool {
	if _, ok := e.table.policies[name]; ok {
		return true
	}
	_, ok := e.table.rules[name]
	return ok
}
