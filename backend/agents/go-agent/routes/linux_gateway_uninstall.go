package routes

import "fmt"

// Uninstall must not erase the only ownership and recovery record while host
// networking still depends on it. The explicit gateway restore action performs
// that cleanup before the ordinary Agent uninstall flow is allowed to proceed.
func checkLinuxGatewayUninstall(cfg *Config) error {
	if cfg == nil || cfg.ServiceType != "mihomo" || cfg.AgentConfigFile == "" {
		return nil
	}
	gatewayMu.Lock()
	defer gatewayMu.Unlock()
	state, err := loadGatewayState(cfg)
	if err != nil {
		return fmt.Errorf("无法核对 Linux 网关恢复记录，已停止卸载以保留网络状态：%w", err)
	}
	if state != nil {
		return fmt.Errorf("请先恢复客户端的网关和 DNS，再到 Linux 网关设置中“撤销网关转发”，完成后才能卸载 Mihomo Agent")
	}
	return nil
}
