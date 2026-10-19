package routes

import (
	"fmt"
	"net"
	"strings"

	"agent/internal/localip"
)

var detectAgentIPv4s = localip.DetectAll

// Resolve at publication time, including for already registered Agents whose
// config intentionally leaves agent_ip empty. Do not persist an automatically
// detected address: DHCP and interface availability can change between publishes.
func agentLANIPv4s(cfg *Config) ([]net.IP, error) {
	value := strings.TrimSpace(cfg.AgentIP)
	values := []string{value}
	if value == "" {
		detected, err := detectAgentIPv4s()
		if err != nil {
			return nil, fmt.Errorf("自动获取本机 LAN IPv4 失败：%w；请检查网络接口或在 Agent 配置中填写 agent_ip", err)
		}
		values = detected
	}
	if len(values) == 0 {
		return nil, fmt.Errorf("无法获得有效的本机 LAN IPv4；请检查 Agent 配置中的 agent_ip 和网络接口")
	}
	addresses := make([]net.IP, 0, len(values))
	seen := map[string]bool{}
	for _, value := range values {
		address := net.ParseIP(strings.TrimSpace(value))
		if address == nil || address.To4() == nil || !address.IsGlobalUnicast() || address.IsLoopback() {
			return nil, fmt.Errorf("无法获得有效的本机 LAN IPv4；请检查 Agent 配置中的 agent_ip 和网络接口")
		}
		address = address.To4()
		if !seen[address.String()] {
			addresses = append(addresses, address)
			seen[address.String()] = true
		}
	}
	return addresses, nil
}
