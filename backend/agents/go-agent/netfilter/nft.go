package netfilter

import (
	"encoding/json"
	"fmt"
	"strings"
)

type nftChain struct {
	family, table, name string
	hook, policy        string
	base                bool
	rules               [][]interface{}
}

type nftRuleset struct {
	tables map[string]bool
	chains map[string]*nftChain
	order  []*nftChain
	sets   map[string][]interface{} // named sets and maps -> elements
}

func nftKey(parts ...string) string { return strings.Join(parts, "|") }

func (r *nftRuleset) hasTable(family, name string) bool { return r.tables[nftKey(family, name)] }

// set returns the elements of a named set or map of the chain's table.
func (r *nftRuleset) set(chain *nftChain, name string) ([]interface{}, bool) {
	elements, ok := r.sets[nftKey(chain.family, chain.table, name)]
	return elements, ok
}

func parseNFT(output string) (*nftRuleset, error) {
	var doc struct {
		Nftables []map[string]json.RawMessage `json:"nftables"`
	}
	if err := json.Unmarshal([]byte(output), &doc); err != nil {
		return nil, err
	}
	if doc.Nftables == nil {
		return nil, fmt.Errorf("nft ruleset 返回格式无效")
	}
	ruleset := &nftRuleset{tables: map[string]bool{}, chains: map[string]*nftChain{}, sets: map[string][]interface{}{}}
	type ruleObject struct {
		Family string        `json:"family"`
		Table  string        `json:"table"`
		Chain  string        `json:"chain"`
		Expr   []interface{} `json:"expr"`
	}
	var rules []ruleObject
	for _, object := range doc.Nftables {
		if raw, ok := object["table"]; ok {
			var table struct {
				Family string `json:"family"`
				Name   string `json:"name"`
			}
			if err := json.Unmarshal(raw, &table); err != nil {
				return nil, err
			}
			ruleset.tables[nftKey(table.Family, table.Name)] = true
		}
		if raw, ok := object["chain"]; ok {
			var chain struct {
				Family string `json:"family"`
				Table  string `json:"table"`
				Name   string `json:"name"`
				Type   string `json:"type"`
				Hook   string `json:"hook"`
				Policy string `json:"policy"`
			}
			if err := json.Unmarshal(raw, &chain); err != nil {
				return nil, err
			}
			value := &nftChain{family: chain.Family, table: chain.Table, name: chain.Name, hook: chain.Hook, policy: chain.Policy, base: chain.Hook != ""}
			ruleset.chains[nftKey(chain.Family, chain.Table, chain.Name)] = value
			ruleset.order = append(ruleset.order, value)
		}
		for _, kind := range []string{"set", "map"} {
			raw, ok := object[kind]
			if !ok {
				continue
			}
			var set struct {
				Family string        `json:"family"`
				Table  string        `json:"table"`
				Name   string        `json:"name"`
				Elem   []interface{} `json:"elem"`
			}
			if err := json.Unmarshal(raw, &set); err != nil {
				return nil, err
			}
			if set.Elem == nil {
				set.Elem = []interface{}{}
			}
			ruleset.sets[nftKey(set.Family, set.Table, set.Name)] = set.Elem
		}
		if raw, ok := object["rule"]; ok {
			var rule ruleObject
			if err := json.Unmarshal(raw, &rule); err != nil {
				return nil, err
			}
			rules = append(rules, rule)
		}
	}
	for _, rule := range rules {
		chain := ruleset.chains[nftKey(rule.Family, rule.Table, rule.Chain)]
		if chain == nil {
			return nil, fmt.Errorf("nft 规则引用了不存在的链")
		}
		chain.rules = append(chain.rules, rule.Expr)
	}
	return ruleset, nil
}

type nftEvaluation struct {
	ruleset *nftRuleset
	base    *nftChain
	found   []nftFinding
}

type nftFinding struct {
	chain   string
	policy  bool
	unknown bool
	flow    string
}

// record keeps a possible drop; strict flows only count certain ones.
func (e *nftEvaluation) record(item nftFinding, f flow, m tri, certain bool) {
	if f.strict && !(certain && m == yes) {
		return
	}
	item.flow = f.name
	e.found = append(e.found, item)
}

// evaluateNFT evaluates every IPv4 forward and input base chain independently:
// accept only ends its own chain, while a drop in any of them drops the packet.
func (in *inspection) evaluateNFT(ruleset *nftRuleset, iptablesNFTRead, firewalldSkipped bool) {
	for _, chain := range ruleset.order {
		if !chain.base || (chain.hook != "forward" && chain.hook != "input") || (chain.family != "ip" && chain.family != "inet") {
			continue
		}
		switch {
		case iptablesNFTRead && chain.family == "ip" && chain.table == "filter":
			continue // already evaluated through iptables-save
		case firewalldSkipped && chain.table == "firewalld":
			continue // firewalld's state is unknown; reported as unknown
		case chain.family == "ip" && chain.table == "configflow_gateway":
			continue // selective gateway resources are judged by the gateway itself
		}
		flows := in.flows(chain.hook)
		if len(flows) == 0 {
			continue
		}
		in.hooks++
		in.notePolicy(chain.hook, chain.policy)
		e := &nftEvaluation{ruleset: ruleset, base: chain}
		for _, f := range flows {
			e.chain(chain, f, 0, true)
		}
		if len(e.found) == 0 {
			continue
		}
		firewalld := in.firewalld && chain.family == "inet" && chain.table == "firewalld"
		name := "nftables " + chain.family + " " + chain.table + " " + chain.name
		var plan *remedyPlan
		kind := "nftables"
		if firewalld {
			kind = "firewalld"
			plan = in.plan("firewalld", "", "", "", "", "firewalld")
			if _, ok := ruleset.chains[nftKey("inet", "firewalld", "filter_FWD_policy_"+FirewalldPolicy)]; ok {
				plan.policyExists = true
			}
		} else {
			plan = in.plan("nftables", "", chain.family, chain.table, chain.name, name)
		}
		for _, item := range e.found {
			plan.flows[item.flow] = true
			detail := ""
			switch {
			case item.policy:
				detail = name + " 链默认策略为 drop"
			case item.unknown:
				detail = name + " 链有无法判断的规则（" + item.chain + "）"
			default:
				detail = name + " 链有 drop 或 reject 规则（" + item.chain + "）"
			}
			if firewalld {
				if chain.hook == "input" {
					detail = "firewalld 正在运行，会拒绝从 " + displayDevice(in.target.Device) + " 进入本机的连接（" + chain.name + "）"
				} else {
					detail = "firewalld 正在运行，会拒绝转发到 " + displayDevice(in.target.Device) + " 的流量（" + chain.name + "）"
				}
				if item.flow == FlowRedirectTCP {
					detail = "firewalld 正在运行，会拒绝被重定向到本机端口的 TCP（" + chain.name + "）"
				}
			}
			policy := ""
			if item.policy {
				policy = "drop"
			}
			in.record(finding{source: Source{Kind: kind, Hook: chain.hook, Family: chain.family, Table: chain.table, Chain: chain.name, Policy: policy, Detail: clip(detail)}, policy: item.policy, flow: item.flow})
		}
	}
}

func (e *nftEvaluation) chain(chain *nftChain, f flow, depth int, certain bool) verdict {
	for _, expressions := range chain.rules {
		result := e.rule(chain, expressions, f, depth, certain)
		if result == returned {
			break // a base chain then applies its policy
		}
		if result != fallThrough {
			return result
		}
	}
	if chain.base {
		if chain.policy == "drop" {
			e.record(nftFinding{chain: chain.name, policy: true}, f, yes, true)
			return dropped
		}
		return accepted
	}
	return fallThrough
}

// rule returns fallThrough to continue with the next rule of the chain.
func (e *nftEvaluation) rule(chain *nftChain, expressions []interface{}, f flow, depth int, certain bool) verdict {
	m := yes
	invalidOnly := false
	for _, raw := range expressions {
		expression, ok := raw.(map[string]interface{})
		if !ok || len(expression) != 1 {
			m = and(m, maybe)
			continue
		}
		for name, body := range expression {
			switch name {
			case "match":
				value, invalid := nftMatch(body, f, func(name string) ([]interface{}, bool) { return e.ruleset.set(chain, name) })
				m = and(m, value)
				invalidOnly = invalidOnly || invalid
			case "counter", "log", "mangle", "notrack", "comment":
			case "xt":
				spec, _ := body.(map[string]interface{})
				if spec["type"] == "target" {
					if target, _ := spec["name"].(string); !nonTerminatingTargets[target] {
						return e.verdict(chain, "unknown", "", m, invalidOnly, f, depth, certain)
					}
					continue
				}
				m = and(m, maybe)
			case "accept", "drop", "return", "continue":
				return e.verdict(chain, name, "", m, invalidOnly, f, depth, certain)
			case "reject":
				return e.verdict(chain, "drop", "", m, invalidOnly, f, depth, certain)
			case "jump", "goto":
				spec, _ := body.(map[string]interface{})
				target, _ := spec["target"].(string)
				return e.verdict(chain, name, target, m, invalidOnly, f, depth, certain)
			case "vmap":
				return e.vmap(chain, body, m, invalidOnly, f, depth, certain)
			case "queue":
				return e.verdict(chain, "unknown", "", m, invalidOnly, f, depth, certain)
			default:
				m = and(m, maybe)
			}
		}
		if m == no {
			return fallThrough
		}
	}
	return fallThrough
}

func (e *nftEvaluation) verdict(chain *nftChain, kind, target string, m tri, invalidOnly bool, f flow, depth int, certain bool) verdict {
	if m == no {
		return fallThrough
	}
	switch kind {
	case "accept":
		if m == yes {
			return accepted
		}
	case "drop":
		if invalidOnly {
			return fallThrough
		}
		e.record(nftFinding{chain: chain.name}, f, m, certain)
		if m == yes {
			return dropped
		}
	case "return":
		if m == yes {
			return returned
		}
	case "jump", "goto":
		next := e.ruleset.chains[nftKey(chain.family, chain.table, target)]
		if next == nil || next.base || depth >= 16 {
			e.record(nftFinding{chain: chain.name, unknown: true}, f, m, certain)
			return fallThrough
		}
		result := e.chain(next, f, depth+1, certain && m == yes)
		if m != yes {
			return fallThrough
		}
		switch result {
		case accepted, dropped:
			return result
		}
		if kind == "goto" {
			return returned
		}
	case "continue":
	default:
		e.record(nftFinding{chain: chain.name, unknown: true}, f, m, certain)
	}
	return fallThrough
}

// vmap resolves elements keyed by an interface name or a conntrack state per
// flow; any other key may match each element. Jump and goto targets are
// followed like those of an ordinary rule.
func (e *nftEvaluation) vmap(chain *nftChain, body interface{}, m tri, invalidOnly bool, f flow, depth int, certain bool) verdict {
	spec, _ := body.(map[string]interface{})
	elements, ok := e.elements(chain, spec["data"])
	if !ok {
		e.found = append(e.found, nftFinding{chain: chain.name, unknown: true})
		return fallThrough
	}
	key := nftMetaKey(spec["key"])
	ctState := nftCTKey(spec["key"]) == "state"
	for _, raw := range elements {
		pair, _ := raw.([]interface{})
		if len(pair) != 2 {
			e.found = append(e.found, nftFinding{chain: chain.name, unknown: true})
			continue
		}
		element, elementInvalid := maybe, false
		value := nftElementValue(pair[0])
		switch {
		case key == "iifname" || key == "iif" || key == "oifname" || key == "oif":
			s := f.out
			if key == "iifname" || key == "iif" {
				s = f.in
			}
			if name, ok := value.(string); ok && !strings.HasPrefix(name, "@") {
				element = matchInterface(name, s, '*')
			}
		case key == "l4proto":
			if values, ok := nftStrings(value); ok {
				element = protoMatch(values, f)
			}
		case ctState:
			if values, ok := nftStrings(value); ok {
				if len(values) == 1 && values[0] == "invalid" {
					elementInvalid = true
				} else {
					element = stateMatch(values, false, f)
				}
			}
		}
		kind, target := nftVerdictOf(pair[1])
		if result := e.verdict(chain, kind, target, and(m, element), invalidOnly || elementInvalid, f, depth, certain); result != fallThrough {
			return result
		}
	}
	return fallThrough
}

// elements reads an anonymous verdict map or a named map reference (@name).
func (e *nftEvaluation) elements(chain *nftChain, value interface{}) ([]interface{}, bool) {
	switch v := value.(type) {
	case map[string]interface{}:
		elements, ok := v["set"].([]interface{})
		return elements, ok
	case string:
		if strings.HasPrefix(v, "@") {
			return e.ruleset.set(chain, v[1:])
		}
	}
	return nil, false
}

// nftElementValue unwraps an element carrying options ({"elem": {"val": ...}}).
func nftElementValue(value interface{}) interface{} {
	if wrapped, ok := value.(map[string]interface{}); ok {
		if elem, ok := wrapped["elem"].(map[string]interface{}); ok {
			return elem["val"]
		}
	}
	return value
}

func nftVerdictOf(value interface{}) (string, string) {
	verdict, _ := value.(map[string]interface{})
	for name, body := range verdict {
		switch name {
		case "accept", "drop", "return", "continue":
			return name, ""
		case "reject":
			return "drop", ""
		case "jump", "goto":
			spec, _ := body.(map[string]interface{})
			target, _ := spec["target"].(string)
			return name, target
		}
	}
	return "unknown", ""
}

func nftMetaKey(value interface{}) string {
	expression, _ := value.(map[string]interface{})
	meta, _ := expression["meta"].(map[string]interface{})
	key, _ := meta["key"].(string)
	return key
}

func nftCTKey(value interface{}) string {
	expression, _ := value.(map[string]interface{})
	ct, _ := expression["ct"].(map[string]interface{})
	key, _ := ct["key"].(string)
	return key
}

func nftStrings(value interface{}) ([]string, bool) {
	switch v := value.(type) {
	case string:
		return []string{v}, true
	case []interface{}:
		result := []string{}
		for _, item := range v {
			text, ok := nftElementValue(item).(string)
			if !ok {
				return nil, false
			}
			result = append(result, text)
		}
		return result, true
	case map[string]interface{}:
		if set, ok := v["set"]; ok {
			return nftStrings(set)
		}
	}
	return nil, false
}

// nftResolve reads the right-hand values of a match. A named set (@name) is
// read from the ruleset; one that cannot be resolved to names is not usable.
func nftResolve(value interface{}, lookup func(string) ([]interface{}, bool)) ([]string, bool) {
	if name, ok := value.(string); ok && strings.HasPrefix(name, "@") {
		elements, found := lookup(name[1:])
		if !found {
			return nil, false
		}
		return nftStrings(elements)
	}
	values, ok := nftStrings(value)
	if !ok {
		return nil, false
	}
	for _, item := range values {
		if strings.HasPrefix(item, "@") {
			return nil, false
		}
	}
	return values, true
}

// nftMatch evaluates interface, protocol, address type and conntrack
// matches; any other match is conditional. The second result marks a match on
// ct state invalid only.
func nftMatch(body interface{}, f flow, lookup func(string) ([]interface{}, bool)) (tri, bool) {
	spec, _ := body.(map[string]interface{})
	op, _ := spec["op"].(string)
	negated := op == "!="
	left, _ := spec["left"].(map[string]interface{})
	// A header match carries an implicit protocol dependency: on a packet of
	// another family or layer 4 protocol it never matches, negated or not.
	if payload, ok := left["payload"].(map[string]interface{}); ok {
		protocol, _ := payload["protocol"].(string)
		field, _ := payload["field"].(string)
		switch protocol {
		case "ip6", "icmpv6":
			return no, false
		case "tcp", "udp", "udplite", "sctp", "dccp", "icmp":
			if f.proto != "" && f.proto != protocol {
				return no, false
			}
		case "ip":
			if field == "protocol" && (op == "==" || op == "!=" || op == "in") {
				if values, ok := nftResolve(spec["right"], lookup); ok {
					return negate(protoMatch(values, f), negated), false
				}
			}
		}
		return maybe, false
	}
	if op != "==" && op != "!=" && op != "in" {
		return maybe, false
	}
	if key := nftMetaKey(left); key != "" {
		values, ok := nftResolve(spec["right"], lookup)
		if !ok {
			return maybe, false
		}
		switch key {
		case "iifname", "iif":
			return negate(matchAnyInterface(values, f.in, '*'), negated), false
		case "oifname", "oif":
			return negate(matchAnyInterface(values, f.out, '*'), negated), false
		case "l4proto":
			return negate(protoMatch(values, f), negated), false
		case "nfproto", "protocol":
			// IPv4 flows only: ipv4 (nfproto) or ip (ether protocol).
			result := no
			for _, value := range values {
				if value == "ipv4" || value == "ip" {
					result = yes
				}
			}
			return negate(result, negated), false
		}
		return maybe, false
	}
	if fib, ok := left["fib"].(map[string]interface{}); ok {
		flags, _ := fib["flags"].([]interface{})
		if result, _ := fib["result"].(string); result == "type" && len(flags) == 1 && flags[0] == "daddr" {
			if values, ok := nftResolve(spec["right"], lookup); ok {
				return negate(addressTypeMatch(values, f), negated), false
			}
		}
		return maybe, false
	}
	if ct, ok := left["ct"].(map[string]interface{}); ok && ct["key"] == "state" {
		values, ok := nftResolve(spec["right"], lookup)
		if !ok {
			return maybe, false
		}
		if !negated && len(values) == 1 && values[0] == "invalid" {
			if f.ct.known {
				return no, false
			}
			return maybe, true
		}
		return stateMatch(values, negated, f), false
	}
	if ct, ok := left["ct"].(map[string]interface{}); ok && ct["key"] == "status" {
		values, ok := nftResolve(spec["right"], lookup)
		if !ok {
			return maybe, false
		}
		// Status flags: "ct status dnat" tests the DNAT bit.
		if len(values) == 1 && values[0] == "dnat" {
			return negate(f.dnat, negated), false
		}
		return maybe, false
	}
	return maybe, false
}
