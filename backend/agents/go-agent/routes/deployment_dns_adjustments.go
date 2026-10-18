package routes

import (
	"fmt"
	"net"
	"strconv"
	"strings"

	"gopkg.in/yaml.v3"
)

// Record only the port settings that AIO actually changes, never the original
// configuration. These notices survive a ready -> activate DNS recheck.
func mihomoAIODNSAdjustments(cfg *Config, original []byte) []DeploymentDNSCheck {
	var doc map[string]interface{}
	if yaml.Unmarshal(original, &doc) != nil {
		return nil
	}
	dns, _ := doc["dns"].(map[string]interface{})
	tun, _ := doc["tun"].(map[string]interface{})
	var changes []string
	if dns["listen"] != "0.0.0.0:1053" {
		changes = append(changes, "dns.listen："+aioDNSPortValue(dns["listen"])+" → '0.0.0.0:1053'")
	}
	hijacks, _ := tun["dns-hijack"].([]interface{})
	if len(hijacks) != 2 || hijacks[0] != "any:1053" || hijacks[1] != "tcp://any:1053" {
		changes = append(changes, "tun.dns-hijack："+aioDNSHijackValue(tun["dns-hijack"])+" → ['any:1053', 'tcp://any:1053']")
	}
	if len(changes) == 0 {
		return nil
	}
	peer := dnsPeerService(cfg)
	peerLabel := "同机 DNS 服务"
	if peer == "oxidns" {
		peerLabel = "OxiDNS"
	} else if peer == "mosdns" {
		peerLabel = "MosDNS"
	}
	message := "已自动调整待发布的 Mihomo DNS 配置，为 " + peerLabel + " 保留 53 端口：\n" + strings.Join(changes, "\n")
	if enabled, _ := tun["enable"].(bool); !enabled {
		message += "\n顶层 TUN 未开启；此次调整不会开启 TUN。"
	}
	message += "\n这是已完成的自动调整，不阻止发布。"
	// Even valid-looking endpoints could match a configured credential. Apply
	// the same secret removal before persisting the original-setting notice.
	secrets := append(configurationDiagnosticSecrets(doc), cfg.Token)
	for _, secret := range diagnosticSecretParts(secrets) {
		if secret != "" {
			message = strings.ReplaceAll(message, secret, "[REDACTED]")
		}
	}
	return []DeploymentDNSCheck{{Name: "All in One DNS 自动调整", Level: "warn", Message: boundedDiagnostic(message, 2048)}}
}

func aioDNSHijackValue(value interface{}) string {
	if value == nil {
		return "未配置"
	}
	values, ok := value.([]interface{})
	if !ok {
		return "自定义值（已隐藏）"
	}
	items := make([]string, 0, min(len(values), 8))
	for i, item := range values {
		if i == 8 {
			items = append(items, fmt.Sprintf("…（共 %d 项）", len(values)))
			break
		}
		items = append(items, aioDNSPortValue(item))
	}
	return "[" + strings.Join(items, ", ") + "]"
}

// Only display literal DNS endpoints. Unrecognized strings may be malformed
// URLs containing credentials, so do not echo or persist them in the notice.
func aioDNSPortValue(value interface{}) string {
	if value == nil {
		return "未配置"
	}
	text, ok := value.(string)
	if !ok {
		return "自定义值（已隐藏）"
	}
	address := text
	if strings.HasPrefix(address, "tcp://") {
		address = strings.TrimPrefix(address, "tcp://")
	} else {
		address = strings.TrimPrefix(address, "udp://")
	}
	host, port, err := net.SplitHostPort(address)
	number, numberErr := strconv.ParseUint(port, 10, 16)
	if err != nil || numberErr != nil || number == 0 || (host != "" && host != "any" && host != "localhost" && net.ParseIP(host) == nil) {
		return "自定义值（已隐藏）"
	}
	return "'" + text + "'"
}
