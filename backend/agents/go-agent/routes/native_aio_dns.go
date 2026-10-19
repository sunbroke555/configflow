package routes

import (
	"fmt"
	"net/netip"

	"gopkg.in/yaml.v3"
)

// auto-redirect installs its port-53 DNAT before checking local destinations.
// Reserve this native AIO host's DNS addresses before that rule so clients can
// reach OxiDNS, but only while both TUN and auto-redirect are enabled. The
// marked selective gateway has its own stricter adaptation.
func adaptNativeAIODNS(cfg *Config, content string) (string, error) {
	if !cfg.NativeAIO {
		return content, nil
	}
	// Decode aliases and merge keys first. Editing an anchor definition in a
	// yaml.Node tree would also change unrelated fields that reference it.
	var doc map[string]interface{}
	if err := yaml.Unmarshal([]byte(content), &doc); err != nil {
		return "", err
	}
	if len(doc) == 0 {
		return "", fmt.Errorf("mihomo config must be a nonempty YAML mapping")
	}
	marker, _ := doc["x-configflow-linux-gateway"].(map[string]interface{})
	if marker["enabled"] == true {
		return content, nil
	}
	if doc["tun"] == nil {
		return content, nil
	}
	tun, ok := doc["tun"].(map[string]interface{})
	if !ok {
		return "", fmt.Errorf("mihomo tun must be a mapping")
	}
	if tun["enable"] != true || tun["auto-redirect"] != true {
		return content, nil
	}
	addresses, err := agentLANIPv4s(cfg)
	if err != nil {
		return "", fmt.Errorf("原生 AIO 启用 TUN 自动重定向时为 OxiDNS 排除本机 DNS 失败：%w", err)
	}
	var excluded []interface{}
	if tun["route-exclude-address"] != nil {
		values, ok := tun["route-exclude-address"].([]interface{})
		if !ok {
			return "", fmt.Errorf("mihomo tun.route-exclude-address must be a CIDR list")
		}
		excluded = append(excluded, values...)
	}
	var existing []netip.Prefix
	for _, entry := range excluded {
		value, ok := entry.(string)
		if !ok {
			return "", fmt.Errorf("mihomo tun.route-exclude-address must contain CIDR strings")
		}
		prefix, err := netip.ParsePrefix(value)
		if err != nil {
			return "", fmt.Errorf("invalid mihomo tun.route-exclude-address CIDR %q", value)
		}
		existing = append(existing, prefix.Masked())
	}
	requiredExclusions := []string{"127.0.0.0/8"}
	for _, address := range addresses {
		requiredExclusions = append(requiredExclusions, address.String()+"/32")
	}
	for _, value := range requiredExclusions {
		required := netip.MustParsePrefix(value)
		covered := false
		for _, prefix := range existing {
			if prefix.Bits() <= required.Bits() && prefix.Contains(required.Addr()) {
				covered = true
				break
			}
		}
		if !covered {
			excluded = append(excluded, value)
			existing = append(existing, required)
		}
	}
	tun["route-exclude-address"] = excluded
	result, err := yaml.Marshal(doc)
	return string(result), err
}
