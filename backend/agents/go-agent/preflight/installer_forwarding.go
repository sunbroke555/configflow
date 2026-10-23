package preflight

import (
	"agent/netfilter"
	"path/filepath"
	"sort"
	"strings"
)

// installerForwardingEnv is set, for the environment check only, by installers
// that enable and persist IPv4 forwarding themselves (ordinary Shell AIO, the
// gateway wizard's full TUN install and standalone Mihomo by default).
const installerForwardingEnv = "CONFIGFLOW_INSTALLER_FORWARDING"

const installerForwardingConfig = "/etc/sysctl.d/99-configflow-aio-forward.conf"

const installerForwardingName = "IPv4 转发（安装器管理）"

// The installer turns forwarding on itself, so the current value is a fact to
// report, not a manual repair to ask for.
func inspectInstallerForwarding(probe systemProbe) []Check {
	data, err := probe.readFile("/proc/sys/net/ipv4/ip_forward")
	value := strings.TrimSpace(string(data))
	check := Check{Name: installerForwardingName, Level: "pass"}
	switch {
	case err == nil && value == "0":
		check.Message = "当前 net.ipv4.ip_forward=0，安装器将开启并持久化（" + installerForwardingConfig + "）；Mihomo Agent 每次启动前会再次检查。"
	case err == nil && value == "1":
		check.Message = "当前 net.ipv4.ip_forward=1，安装器将持久化（" + installerForwardingConfig + "）；Mihomo Agent 每次启动前会再次检查。"
	default:
		check.Level = "warn"
		check.Message = "无法读取当前 net.ipv4.ip_forward；安装器稍后会再次检查，无法开启时会停止安装。"
	}
	checks := []Check{check}
	if conflict, ok := inspectInstallerForwardingConflict(probe); ok {
		checks = append(checks, conflict)
	}
	return checks
}

// inspectInstallerForwardingConflict recomputes the boot-time value as if the
// installer's file already existed, and reports only a later '=0'.
func inspectInstallerForwardingConflict(probe systemProbe) (Check, bool) {
	selected := map[string]string{filepath.Base(installerForwardingConfig): installerForwardingConfig}
	for _, dir := range []string{"/etc/sysctl.d", "/run/sysctl.d", "/usr/local/lib/sysctl.d", "/usr/lib/sysctl.d", "/lib/sysctl.d"} {
		paths, _ := probe.glob(dir + "/*.conf")
		for _, path := range paths {
			base := filepath.Base(path)
			if _, found := selected[base]; !found {
				selected[base] = path
			}
		}
	}
	names := make([]string, 0, len(selected))
	for name := range selected {
		names = append(names, name)
	}
	sort.Strings(names)
	var effective forwardingSetting
	for _, name := range names {
		path := selected[name]
		if path == installerForwardingConfig {
			effective = forwardingSetting{value: "1", where: path}
			continue
		}
		data, err := probe.readFile(path)
		if err != nil {
			continue
		}
		if setting := parseForwardingSetting(data, path); setting.where != "" {
			effective = setting
		}
	}
	conflict := ""
	if effective.value == "0" {
		conflict = effective.where
	} else if data, err := probe.readFile("/etc/sysctl.conf"); err == nil {
		if legacy := parseForwardingSetting(data, "/etc/sysctl.conf"); legacy.value == "0" {
			conflict = legacy.where
		}
	}
	if conflict == "" {
		return Check{}, false
	}
	return Check{
		Name:    "IPv4 转发冲突",
		Level:   "warn",
		Message: conflict + " 在 ConfigFlow 的持久化文件之后加载，并设置 net.ipv4.ip_forward=0，重启后转发可能被关闭。",
		Fix:     "把该行改为 1 或删除，重启后用 sysctl net.ipv4.ip_forward 确认为 1。",
	}, true
}

const forwardFilterFix = "需要放行时在这台 Linux 上执行下面的命令；发布 Mihomo 配置后以 ConfigFlow 页面显示的命令为准（TUN 设备名可能不同）。"

// installerForwardTarget uses the install configuration's TUN device when it
// enables TUN, otherwise ConfigFlow's full-TUN default. It follows the scope of
// the Agent status (routes/forward_filter.go): a selective Linux gateway
// configuration (marker present, enabled or not), a route-restricted TUN and
// an enabled TUN without auto-route are not full TUN and are not inspected.
func installerForwardTarget(configs []Config) (netfilter.Target, bool) {
	target := netfilter.Target{Device: "Meta", AutoRedirect: true}
	for _, cfg := range configs {
		if cfg.ServiceType != "mihomo" {
			continue
		}
		doc, err := loadCoreConfig(cfg)
		if err != nil {
			break
		}
		if _, marker := doc["x-configflow-linux-gateway"]; marker {
			return netfilter.Target{}, false
		}
		tun, _ := doc["tun"].(map[string]interface{})
		for _, key := range []string{"route-address", "route-address-set", "inet4-route-address"} {
			if entries, ok := tun[key].([]interface{}); ok && len(entries) > 0 {
				return netfilter.Target{}, false
			}
		}
		if tun["enable"] == true {
			if tun["auto-route"] != true {
				return netfilter.Target{}, false
			}
			if device, ok := tun["device"].(string); ok && device != "" {
				target.Device = device
			}
			target.AutoRedirect = tun["auto-redirect"] == true
		}
		break
	}
	return target, true
}

// inspectForwardFilter only ever warns; an unknown result is informational.
// Each source keeps its own commands and persistence note.
func inspectForwardFilter(probe systemProbe, configs []Config) (Check, bool) {
	if probe.forwardFilter == nil {
		return Check{}, false
	}
	target, applicable := installerForwardTarget(configs)
	if !applicable {
		return Check{}, false
	}
	result := probe.forwardFilter(target)
	check := Check{Name: "转发过滤"}
	switch result.State {
	case netfilter.StateMayDrop:
		check.Level, check.Fix = "warn", forwardFilterFix
		check.Message = strings.Replace(result.Hint, "，需要时在这台 Linux 上执行下面的命令放行。", "。", 1)
		for _, remedy := range result.Remedies {
			if len(remedy.Commands) > 0 {
				check.Groups = append(check.Groups, CommandGroup{Label: remedy.Label, Commands: remedy.Commands, Note: remedy.PersistNote})
			}
		}
		if len(check.Groups) == 0 {
			check.Fix = "请在这台 Linux 上手动放行进出 TUN 设备的转发流量；发布 Mihomo 配置后可在 ConfigFlow 页面查看检查结果。"
		}
	case netfilter.StateOK:
		check.Level, check.Message = "pass", "未发现会拦截 TUN 转发的防火墙规则。"
	default:
		reason := result.Reason
		if reason == "" {
			reason = "unknown"
		}
		check.Level, check.Message = "info", "未能确认转发过滤（"+reason+"），不影响安装。"
	}
	return check, true
}
