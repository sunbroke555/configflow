package routes

import (
	"fmt"
	"net"
	"strings"

	"gopkg.in/yaml.v3"
)

// sing-tun's auto-redirect installs a fixed port-53 DNAT before its local-IP
// and route-address-set exclusions. dns-hijack: any:1053 does not disable it.
// Exclude only this Agent's LAN address (and loopback) before that DNAT so LAN
// clients reach OxiDNS. Adapt only explicitly marked gateway publications.
func adaptGatewayDNS(cfg *Config, content string) (string, error) {
	var doc map[string]interface{}
	if err := yaml.Unmarshal([]byte(content), &doc); err != nil {
		return "", err
	}
	marker, _ := doc["x-configflow-linux-gateway"].(map[string]interface{})
	if marker["enabled"] != true {
		return content, nil
	}
	address := net.ParseIP(strings.TrimSpace(cfg.AgentIP))
	if address == nil || address.To4() == nil || !address.IsGlobalUnicast() || address.IsLoopback() {
		return "", fmt.Errorf("选择性网关需要 Agent 配置中的有效 LAN IPv4 地址，才能为 OxiDNS 排除网关 DNS；请重新运行安装向导")
	}
	tun, tunOK := doc["tun"].(map[string]interface{})
	dns, dnsOK := doc["dns"].(map[string]interface{})
	if !tunOK || !dnsOK {
		return "", fmt.Errorf("选择性网关配置缺少 tun 或 dns 对象")
	}
	tun["route-exclude-address"] = []string{"127.0.0.0/8", address.String() + "/32"}
	tun["dns-hijack"] = []string{}
	dns["listen"] = "127.0.0.1:1053"
	result, err := yaml.Marshal(doc)
	return string(result), err
}
