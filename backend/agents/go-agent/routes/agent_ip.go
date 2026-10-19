package routes

import (
	"fmt"
	"net"
	"strings"

	"agent/internal/localip"
)

var detectAgentIPv4 = localip.Detect

// Resolve at publication time, including for already registered Agents whose
// config intentionally leaves agent_ip empty. Do not persist an automatically
// detected address: DHCP and interface availability can change between publishes.
func agentLANIPv4(cfg *Config) (net.IP, error) {
	value := strings.TrimSpace(cfg.AgentIP)
	if value == "" {
		detected, err := detectAgentIPv4()
		if err != nil {
			return nil, fmt.Errorf("自动获取本机 LAN IPv4 失败：%w；请检查网络接口或在 Agent 配置中填写 agent_ip", err)
		}
		value = strings.TrimSpace(detected)
	}
	address := net.ParseIP(value)
	if address == nil || address.To4() == nil || !address.IsGlobalUnicast() || address.IsLoopback() {
		return nil, fmt.Errorf("无法获得有效的本机 LAN IPv4；请检查 Agent 配置中的 agent_ip 和网络接口")
	}
	return address.To4(), nil
}
