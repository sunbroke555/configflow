package netfilter

import (
	"net/netip"
	"sort"
	"strconv"
	"strings"
)

// portSpan is an inclusive range of TCP/UDP ports.
type portSpan struct{ lo, hi int }

// defaultLocalPorts is Linux's default net.ipv4.ip_local_port_range.
var defaultLocalPorts = portSpan{32768, 60999}

// localPortRange reads the kernel's ephemeral port range. auto-redirect and
// the TUN system stack listen on a port the kernel picks from it, so input
// rules for fixed service ports do not concern those flows.
func localPortRange(probe Probe) portSpan {
	if probe.ReadFile == nil {
		return defaultLocalPorts
	}
	data, err := probe.ReadFile("/proc/sys/net/ipv4/ip_local_port_range")
	if err != nil {
		return defaultLocalPorts
	}
	fields := strings.Fields(string(data))
	if len(fields) != 2 {
		return defaultLocalPorts
	}
	lo, errLo := strconv.Atoi(fields[0])
	hi, errHi := strconv.Atoi(fields[1])
	if errLo != nil || errHi != nil || lo < 1 || hi > 65535 || lo > hi {
		return defaultLocalPorts
	}
	return portSpan{lo, hi}
}

func parsePort(value string) (int, bool) {
	port, err := strconv.Atoi(value)
	return port, err == nil && port >= 0 && port <= 65535
}

// parsePortList reads iptables port syntax: "22", "1024:65535", "1024:" and,
// for multiport, comma-separated lists of those.
func parsePortList(value string, multi bool) ([]portSpan, bool) {
	parts := []string{value}
	if multi {
		parts = strings.Split(value, ",")
	}
	var spans []portSpan
	for _, part := range parts {
		lo, hi := part, part
		if index := strings.Index(part, ":"); index >= 0 {
			lo, hi = part[:index], part[index+1:]
			if lo == "" {
				lo = "0"
			}
			if hi == "" {
				hi = "65535"
			}
		}
		from, okLo := parsePort(lo)
		to, okHi := parsePort(hi)
		if !okLo || !okHi {
			return nil, false
		}
		if from > to {
			from, to = to, from
		}
		spans = append(spans, portSpan{from, to})
	}
	return spans, len(spans) > 0
}

// portMatch compares a rule's destination ports with the flow's: yes when
// they cover every port the flow may use, no when they share none.
func portMatch(spans []portSpan, f flow) tri {
	if f.dport.hi == 0 {
		return maybe
	}
	sorted := append([]portSpan{}, spans...)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i].lo < sorted[j].lo })
	overlap := false
	next := f.dport.lo // first port not yet covered
	for _, span := range sorted {
		if span.hi < f.dport.lo || span.lo > f.dport.hi {
			continue
		}
		overlap = true
		if span.lo <= next && span.hi >= next {
			next = span.hi + 1
		}
	}
	switch {
	case next > f.dport.hi:
		return yes
	case overlap:
		return maybe
	}
	return no
}

// TCP header flags, as iptables --tcp-flags and nft tcp flags name them.
const (
	tcpFlagFIN = 1 << iota
	tcpFlagSYN
	tcpFlagRST
	tcpFlagPSH
	tcpFlagACK
	tcpFlagURG
	tcpFlagECE
	tcpFlagCWR

	synMask      = tcpFlagFIN | tcpFlagSYN | tcpFlagRST | tcpFlagACK
	allTCPFlags  = tcpFlagFIN | tcpFlagSYN | tcpFlagRST | tcpFlagPSH | tcpFlagACK | tcpFlagURG
	ecnTCPFlags  = tcpFlagECE | tcpFlagCWR
	everyTCPFlag = allTCPFlags | ecnTCPFlags
)

var tcpFlagNames = map[string]int{
	"fin": tcpFlagFIN, "syn": tcpFlagSYN, "rst": tcpFlagRST, "psh": tcpFlagPSH, "ack": tcpFlagACK, "urg": tcpFlagURG,
	"ece": tcpFlagECE, "cwr": tcpFlagCWR, "all": allTCPFlags, "none": 0,
}

func parseTCPFlags(value string) (int, bool) {
	flags := 0
	for _, name := range strings.Split(value, ",") {
		bit, ok := tcpFlagNames[strings.ToLower(strings.TrimSpace(name))]
		if !ok {
			return 0, false
		}
		flags |= bit
	}
	return flags, true
}

// tcpFlags matches (flags & mask) == comp.
type tcpFlags struct{ mask, comp int }

// match evaluates the first packet of a new TCP connection: SYN only, while
// ECE and CWR depend on whether the client asks for ECN.
func (t tcpFlags) match(f flow) tri {
	if f.proto != "tcp" || !f.ct.known || !f.ct.isNew {
		return maybe
	}
	known := t.mask &^ ecnTCPFlags
	if tcpFlagSYN&known != t.comp&known {
		return no
	}
	if t.mask&ecnTCPFlags != 0 {
		return maybe
	}
	return yes
}

// parsePrefix reads an IPv4 address or network ("10.0.0.0/8", "1.2.3.4").
func parsePrefix(value string) (netip.Prefix, bool) {
	if prefix, err := netip.ParsePrefix(value); err == nil && prefix.Addr().Is4() {
		return prefix.Masked(), true
	}
	if addr, err := netip.ParseAddr(value); err == nil && addr.Is4() {
		return netip.PrefixFrom(addr, 32), true
	}
	return netip.Prefix{}, false
}

// sourceMatch compares source networks with the flow's source address, which
// is only known for packets the TUN stack sends to Mihomo.
func sourceMatch(prefixes []netip.Prefix, f flow) tri {
	if !f.saddr.IsValid() {
		return maybe
	}
	for _, prefix := range prefixes {
		if prefix.Contains(f.saddr) {
			return yes
		}
	}
	return no
}
