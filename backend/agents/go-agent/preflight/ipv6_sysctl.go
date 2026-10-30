package preflight

import (
	"os"
	"strings"
)

// SysctlAssignment is a boot loader assignment in application order. Keys use
// /proc/sys slash notation; a leading '-' (ignore write errors) is removed.
// Persistence is file evidence, never a claim that a reboot was tested.
type SysctlAssignment struct {
	Key        string `json:"key"`
	Value      string `json:"value"`
	File       string `json:"file"`
	Line       int    `json:"line"`
	Persistent bool   `json:"persistent"`
}

// IPv6SysctlAssignments shares the existing loader detection, filename
// precedence, masks and Alpine ordering used by IPv4 persistence checks.
// pending is a read-only simulation; it never writes configuration.
func IPv6SysctlAssignments(pending map[string][]byte) ([]SysctlAssignment, bool) {
	return ipv6SysctlAssignmentsWith(defaultSystemProbe(), pending)
}

func ipv6SysctlAssignmentsWith(probe systemProbe, pending map[string][]byte) ([]SysctlAssignment, bool) {
	extra := make([]string, 0, len(pending))
	for path := range pending {
		extra = append(extra, path)
	}
	files, unreadable := sysctlBootFiles(probe, detectSysctlLoader(probe), extra)
	result := []SysctlAssignment{}
	seen := map[string]bool{}
	for _, path := range files {
		if seen[path] {
			continue
		}
		seen[path] = true
		data, ok := pending[path]
		if !ok {
			var err error
			data, err = probe.readFile(path)
			if err != nil {
				unreadable = unreadable || !os.IsNotExist(err)
				continue
			}
		}
		for n, raw := range strings.Split(string(data), "\n") {
			line := strings.TrimSpace(raw)
			if line == "" || strings.HasPrefix(line, "#") || strings.HasPrefix(line, ";") {
				continue
			}
			parts := strings.SplitN(line, "=", 2)
			key := strings.TrimSpace(strings.TrimPrefix(parts[0], "-"))
			// sysctl treats dots as separators only when its first separator is a
			// dot. Slash-form names preserve dots in VLAN/interface names.
			if i := strings.IndexAny(key, "./"); i >= 0 && key[i] == '.' {
				key = strings.NewReplacer(".", "/", "/", ".").Replace(key)
			}
			key = strings.TrimPrefix(key, "/")
			if !strings.HasPrefix(key, "net/ipv6/") {
				continue
			}
			value := ""
			if len(parts) == 2 {
				value = strings.TrimSpace(strings.SplitN(strings.SplitN(parts[1], "#", 2)[0], ";", 2)[0])
			}
			result = append(result, SysctlAssignment{Key: key, Value: value, File: path, Line: n + 1, Persistent: !strings.HasPrefix(path, "/run/")})
		}
	}
	return result, unreadable
}
