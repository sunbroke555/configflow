package preflight

import (
	"agent/netfilter"
	"context"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"time"
)

const netAdminCapability = uint64(1 << 12)

type tunState uint8

const (
	tunUnknown tunState = iota
	tunDisabled
	tunEnabled
)

const tunDeviceFix = "普通 Linux/虚拟机先检查内核 TUN 驱动（modprobe tun）。PVE LXC 应先在 PVE 宿主机执行 modprobe tun，检查 pct config <CTID> 的空闲 devN 槽，再用 pct set <CTID> --devN /dev/net/tun 添加设备（例如未占用的 dev0）；完整停止并启动容器后，在容器内重新检查。不要改为特权容器或放宽整个设备目录权限。"
const tunCapabilityFix = "分别核对当前检查进程与 Mihomo 核心进程的 /proc/<PID>/status 中 CapEff、CapBnd 和启动权限；UID 为 root 不能证明具有 CAP_NET_ADMIN。若运行在容器内，还需核对容器保留的能力及 /dev/net/tun 的设备访问策略。按实际服务管理方式做最小调整。"
const systemdTunCapabilityFix = "检查 Mihomo 服务的 CapabilityBoundingSet 是否包含 CAP_NET_ADMIN；非 root 服务还需核对 AmbientCapabilities 或程序文件能力，并以核心进程的 CapEff 确认实际能力。设备受限制时，核对 DeviceAllow=/dev/net/tun rw。若运行在容器内，还需核对容器保留的能力。保留其他必要权限，按现有服务配置做最小调整。"
const openRCTunCapabilityFix = "核对 /etc/init.d/ 中对应的 Mihomo 服务脚本与 /etc/conf.d/ 配置，包括 command_user、capabilities 及其启动命令；从该服务实际运行进程的 /proc/<PID>/status 读取 CapEff，确认 CAP_NET_ADMIN，并核对该进程对 /dev/net/tun 的设备访问权限。UID 为 root 不能证明具备该能力；若运行在容器内，还需核对容器保留的能力和设备映射。"
const gatewayForwardingFix = "在运行网关的 Linux 内检查 /proc/sys 的写权限和 CAP_NET_ADMIN；LXC 需要宿主机管理员允许容器修改网络 sysctl。本安装不会修改 PVE 宿主机。"
const forwardingFix = "仅当此 Linux/LXC 承担旁路网关或路由转发时，在该系统内执行 sysctl -w net.ipv4.ip_forward=1，并把 net.ipv4.ip_forward=1 写入 /etc/sysctl.d/99-configflow-forward.conf；同时处理报告中的冲突设置及后加载文件，避免被改回 0。重启后用 sysctl net.ipv4.ip_forward 再验证。PVE 仅做二层网桥时不因此需要开启宿主机转发。"

type systemProbe struct {
	goos      string
	readFile  func(string) ([]byte, error)
	readDir   func(string) ([]os.DirEntry, error)
	stat      func(string) (os.FileInfo, error)
	glob      func(string) ([]string, error)
	tun       func(string) (uint32, error)
	showUnit  func(string) ([]byte, error)
	getenv    func(string) string
	writeFile func(string, []byte) error
	// forwardFilter inspects FORWARD filtering; nil skips the check.
	forwardFilter func(netfilter.Target) netfilter.Result
	lanAddresses  func() []net.IP
}

func defaultSystemProbe() systemProbe {
	return systemProbe{
		goos: runtime.GOOS, readFile: os.ReadFile, readDir: os.ReadDir, stat: os.Stat, glob: filepath.Glob, tun: systemTunFeatures,
		getenv: os.Getenv, writeFile: func(path string, data []byte) error { return os.WriteFile(path, data, 0o644) },
		forwardFilter: boundedForwardFilter, lanAddresses: netfilter.LANAddresses,
		showUnit: func(unit string) ([]byte, error) {
			ctx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
			defer cancel()
			return exec.CommandContext(ctx, "systemctl", "show",
				"--property=LoadState,MainPID,CapabilityBoundingSet,AmbientCapabilities,PrivateDevices,DevicePolicy,DeviceAllow,PrivateNetwork,NetworkNamespacePath,User,NoNewPrivileges,RootDirectory,RootImage",
				"--", unit).Output()
		},
	}
}

// boundedForwardFilter never holds the environment check past the inspection
// budget, even if a command outlives its context; it then reports unknown.
func boundedForwardFilter(target netfilter.Target) netfilter.Result {
	return boundedForwardFilterWith(target, func(ctx context.Context) netfilter.Result {
		return netfilter.Inspect(ctx, netfilter.DefaultProbe(), target)
	})
}

func boundedForwardFilterWith(target netfilter.Target, inspect func(context.Context) netfilter.Result) netfilter.Result {
	budget := netfilter.TotalBudget
	ctx, cancel := context.WithTimeout(context.Background(), budget)
	defer cancel()
	done := make(chan netfilter.Result, 1)
	go func() { done <- inspect(ctx) }()
	timer := time.NewTimer(budget + 250*time.Millisecond)
	defer timer.Stop()
	select {
	case result := <-done:
		return result
	case <-timer.C:
		return netfilter.Result{Protocol: 1, State: netfilter.StateUnknown, Reason: "timeout", Device: target.Device, AutoRedirect: target.AutoRedirect}
	}
}

func systemChecks(configs []Config) []Check {
	return systemChecksWithProbe(configs, defaultSystemProbe())
}

func systemChecksWithProbe(configs []Config, probe systemProbe) []Check {
	if probe.goos != "linux" {
		return []Check{{Name: "Linux 网络环境", Level: "warn", Message: "当前不是 Linux；无法确认目标机的 TUN、Linux capabilities 与 IPv4 转发状态。", Fix: "请在实际安装 Agent 的 Linux/LXC 内运行环境检查。"}}
	}
	var checks []Check
	hasMihomo := false
	coreTunStates := make(map[int]tunState)
	for i, cfg := range configs {
		if cfg.ServiceType != "mihomo" {
			continue
		}
		hasMihomo = true
		coreTunStates[i] = tunUnknown
		doc, err := loadCoreConfig(cfg)
		if err != nil {
			// Run reports the configuration error separately; it is not safe to
			// infer TUN requirements from unreadable or invalid configuration.
			continue
		}
		state, configurationChecks := configuredMihomoTUN(doc)
		coreTunStates[i] = state
		checks = append(checks, configurationChecks...)
	}
	if hasMihomo {
		tunRequirement := tunDisabled
		var serviceChecks []Check
		allRequiredCoresHaveCapability := true
		for i, cfg := range configs {
			if cfg.ServiceType == "mihomo" {
				state := coreTunStates[i]
				if state == tunEnabled || state == tunUnknown && tunRequirement != tunEnabled {
					tunRequirement = state
				}
				coreChecks, hasCapability := inspectCoreService(probe, cfg, state)
				serviceChecks = append(serviceChecks, coreChecks...)
				if state == tunEnabled && !hasCapability {
					allRequiredCoresHaveCapability = false
				}
			}
		}
		capability := inspectInstallerCapability(probe, tunRequirement)
		if tunRequirement == tunEnabled && allRequiredCoresHaveCapability && capability.Level == "error" {
			capability.Level = "warn"
			capability.Message = "当前检查进程没有 CAP_NET_ADMIN，但已核实启用 TUN 的核心进程持有该能力；安装进程的权限不能代表核心服务，不据此阻止安装。"
		}
		checks = append(checks, inspectTunDevice(probe, tunRequirement), capability)
		checks = append(checks, serviceChecks...)
	} else {
		checks = append(checks, Check{Name: "TUN / CAP_NET_ADMIN", Level: "pass", Message: "本次仅接管 DNS 核心，不需要 TUN 或网络管理能力。"})
	}
	switch {
	case probe.getenv != nil && probe.getenv("CONFIGFLOW_FORWARDING_OWNER") == "gateway":
		checks = append(checks, inspectGatewayForwarding(probe))
	case probe.getenv != nil && probe.getenv(installerForwardingEnv) == "persist":
		checks = append(checks, inspectInstallerForwarding(probe)...)
		if hasMihomo {
			if check, ok := inspectForwardFilter(probe, configs); ok {
				checks = append(checks, check)
			}
		}
	default:
		checks = append(checks, inspectIPv4Forwarding(probe)...)
	}
	return checks
}

// The selective gateway turns forwarding on when it is applied, restores the
// value it saved when it is removed, and reapplies its journal at boot. A
// manual sysctl change would only become the "original" value it restores, so
// only the permission to change it is checked here.
func inspectGatewayForwarding(probe systemProbe) Check {
	const path = "/proc/sys/net/ipv4/ip_forward"
	check := Check{Name: "IPv4 转发权限", Level: "pass"}
	data, err := probe.readFile(path)
	value := strings.TrimSpace(string(data))
	if err != nil || value != "0" && value != "1" {
		check.Level = "warn"
		check.Message = "无法确认 net.ipv4.ip_forward 的当前值；应用网关转发时会再次检查。"
		return check
	}
	// Writing the current value proves the kernel permission without toggling it.
	if probe.writeFile == nil || probe.writeFile(path, []byte(value+"\n")) != nil {
		check.Level = "error"
		check.Message = "无法写入 net.ipv4.ip_forward（当前为 " + value + "），应用网关转发时将无法开启转发。"
		check.Fix = gatewayForwardingFix
		return check
	}
	check.Message = "当前 net.ipv4.ip_forward=" + value + "；网页「应用网关转发」时开启，撤销时恢复原值，重启后由 Agent 自动恢复，无需手动修改或写入 sysctl.d。"
	return check
}

// A configured TUN listener is active independently of the legacy top-level
// tun.enable switch. Keep an unknown state for malformed settings, but one
// definitely active TUN entry is enough to require its environment prerequisites.
func configuredMihomoTUN(doc map[string]interface{}) (tunState, []Check) {
	state := tunDisabled
	var checks []Check
	unknown := func(message, fix string) {
		if state != tunEnabled {
			state = tunUnknown
		}
		checks = append(checks, Check{Name: "Mihomo TUN 配置", Level: "warn", Message: message, Fix: fix})
	}
	if rawTun, present := doc["tun"]; present {
		tun, ok := rawTun.(map[string]interface{})
		if !ok {
			unknown("tun 不是有效的配置对象，无法判断是否需要 TUN。", "检查 Mihomo 配置中的 tun 结构，并将 tun.enable 设置为 true 或 false。")
		} else if rawEnabled, present := tun["enable"]; present {
			if enabled, ok := rawEnabled.(bool); !ok {
				unknown("tun.enable 不是明确的布尔值，无法判断是否需要 TUN。", "检查 Mihomo 配置中的 tun.enable，使用 true 或 false。")
			} else if enabled {
				state = tunEnabled
			}
		}
	}
	if rawListeners, present := doc["listeners"]; present && rawListeners != nil {
		listeners, ok := rawListeners.([]interface{})
		if !ok {
			unknown("listeners 不是有效的列表，无法判断是否配置了 TUN 入口。", "检查 Mihomo 配置中的 listeners 列表及 type: tun 入口。")
			return state, checks
		}
		for _, rawListener := range listeners {
			listener, ok := rawListener.(map[string]interface{})
			if !ok {
				unknown("listeners 中存在无效入口，无法完整判断 TUN 状态。", "检查 Mihomo listeners 中每个入口的配置对象。")
				continue
			}
			if kind, _ := listener["type"].(string); kind != "tun" {
				continue
			}
			if rawDisabled, present := listener["disabled"]; present {
				disabled, ok := rawDisabled.(bool)
				if !ok {
					unknown("TUN 入口的 disabled 不是明确的布尔值，无法判断该入口是否启用。", "检查 listeners 中 type: tun 入口的 disabled，使用 true 或 false。")
					continue
				}
				if disabled {
					continue
				}
			}
			state = tunEnabled
		}
	}
	return state, checks
}

func tunFailureLevel(state tunState) string {
	if state == tunEnabled {
		return "error"
	}
	return "warn"
}

func inspectTunDevice(probe systemProbe, state tunState) Check {
	check := Check{Name: "TUN 设备", Level: tunFailureLevel(state), Fix: tunDeviceFix}
	info, err := probe.stat("/dev/net/tun")
	if err != nil {
		check.Message = "无法访问 /dev/net/tun；设备未提供或访问受限。"
	} else if info.Mode()&os.ModeCharDevice == 0 {
		check.Message = "/dev/net/tun 不是字符设备，不能作为 TUN 使用。"
	} else if features, err := probe.tun("/dev/net/tun"); err != nil {
		check.Message = "TUN 字符设备存在，但当前进程打开设备或读取驱动能力失败；设备映射、cgroup 或安全策略可能限制访问。"
		check.Fix += " 同时核对核心服务的启动用户、设备映射及访问策略；检查进程的访问结果不能代表核心进程。"
	} else if features&1 == 0 { // IFF_TUN, shared by supported Linux architectures.
		check.Message = "设备可打开，但驱动没有报告 IFF_TUN 支持。"
	} else {
		return Check{Name: check.Name, Level: "pass", Message: "/dev/net/tun 为字符设备，当前进程可以打开并读取 TUNGETFEATURES；未创建网络接口。"}
	}
	if state == tunEnabled {
		check.Message += " 当前 Mihomo 配置已启用 TUN。"
	} else if state == tunUnknown {
		check.Message += " 当前配置的 TUN 状态未知；需先修复核心配置后重新检查。"
	} else {
		check.Message += " 当前配置未启用 TUN，不阻止安装；以后开启前需处理。"
	}
	return check
}

func capabilityMask(data []byte, field string) (uint64, error) {
	for _, line := range strings.Split(string(data), "\n") {
		name, value, ok := strings.Cut(line, ":")
		if ok && name == field {
			return strconv.ParseUint(strings.TrimSpace(value), 16, 64)
		}
	}
	return 0, fmt.Errorf("capability field unavailable")
}

func inspectInstallerCapability(probe systemProbe, state tunState) Check {
	data, err := probe.readFile("/proc/self/status")
	mask, parseErr := capabilityMask(data, "CapEff")
	if err != nil || parseErr != nil {
		return Check{Name: "当前进程 CAP_NET_ADMIN", Level: "warn", Message: "无法读取当前进程的有效能力，不能确认是否可创建 TUN 或调整路由。", Fix: tunCapabilityFix}
	}
	if mask&netAdminCapability != 0 {
		return Check{Name: "当前进程 CAP_NET_ADMIN", Level: "pass", Message: "当前检查进程持有有效 CAP_NET_ADMIN；核心服务权限另行检查，不以 root 身份替代能力检查。"}
	}
	message := "当前检查进程未持有有效 CAP_NET_ADMIN。"
	level := "warn"
	if state == tunEnabled {
		bounding, boundErr := capabilityMask(data, "CapBnd")
		if boundErr == nil && bounding&netAdminCapability == 0 {
			level = "error"
			message += " 当前进程的 CapBnd 也不包含该能力，且尚未证明核心具备 TUN 所需权限。"
		} else {
			message += " 仅凭检查进程的 CapEff 不能确认核心缺少权限；需结合核心进程及服务能力设置验证。"
		}
	} else if state == tunUnknown {
		message += " 当前配置的 TUN 状态未知；需先修复核心配置后重新检查。"
	} else {
		message += " 当前未启用 TUN，不阻止安装；以后开启前需处理。"
	}
	return Check{Name: "当前进程 CAP_NET_ADMIN", Level: level, Message: message, Fix: tunCapabilityFix}
}

func systemdProperties(data []byte) map[string]string {
	values := make(map[string]string)
	for _, line := range strings.Split(string(data), "\n") {
		key, value, ok := strings.Cut(line, "=")
		if ok {
			values[key] = value
		}
	}
	return values
}

func hasNamedCapability(value, capability string) bool {
	for _, name := range strings.Fields(strings.ToLower(value)) {
		if name == capability {
			return true
		}
	}
	return false
}

func inspectCoreService(probe systemProbe, cfg Config, state tunState) ([]Check, bool) {
	name := "Mihomo 服务权限"
	if state == tunUnknown {
		return []Check{{Name: name, Level: "warn", Message: "无法从核心配置判断 TUN 是否启用，尚未验证对应服务的 TUN 权限。", Fix: "先修复核心配置路径、读取权限或 tun 配置结构，再重新运行环境检查。"}}, false
	}
	if state == tunDisabled {
		return []Check{{Name: name, Level: "pass", Message: "当前配置未启用 TUN；不以 TUN 服务权限作为安装前提。"}}, false
	}
	if cfg.ServiceManager == "openrc" {
		return []Check{{Name: name, Level: "warn", Message: "核心由 OpenRC 管理；尚未取得并验证其实际运行 PID，无法确认核心获得的能力与设备访问权限。当前检查进程的权限不能代表核心服务。", Fix: openRCTunCapabilityFix},
			tunRuntimeWarning("未取得 OpenRC 管理的 Mihomo 核心 PID，无法读取其持有的 TUN 接口。", openRCTunCapabilityFix)}, false
	}
	if cfg.ServiceManager != "systemd" {
		return []Check{{Name: name, Level: "warn", Message: "当前服务管理方式未提供可验证的核心 PID，无法确认核心实际获得的能力与设备访问权限；当前检查进程的权限不能代表核心服务。", Fix: tunCapabilityFix},
			tunRuntimeWarning("当前服务管理方式未提供可验证的 Mihomo 核心 PID，无法读取其持有的 TUN 接口。", tunCapabilityFix)}, false
	}
	if cfg.ServiceUnit == "" {
		return []Check{{Name: name, Level: "warn", Message: "未指定 Mihomo systemd 服务单元，无法读取核心实际获得的能力与设备访问权限；当前检查进程的权限不能代表核心服务。", Fix: systemdTunCapabilityFix},
			tunRuntimeWarning("未指定 Mihomo systemd 服务单元，无法确定核心 PID 并检查其 TUN 接口。", systemdTunCapabilityFix)}, false
	}
	data, err := probe.showUnit(cfg.ServiceUnit)
	if err != nil {
		return []Check{{Name: name, Level: "warn", Message: "无法读取 Mihomo systemd 服务的权限与隔离配置，不能确认核心能够访问 TUN。", Fix: systemdTunCapabilityFix},
			tunRuntimeWarning("无法读取 Mihomo systemd 服务信息，无法确定核心 PID 并检查其 TUN 接口。", systemdTunCapabilityFix)}, false
	}
	properties := systemdProperties(data)
	var checks []Check
	coreHasCapability := false
	if bound, present := properties["CapabilityBoundingSet"]; !present {
		checks = append(checks, Check{Name: name + "上限", Level: "warn", Message: "服务没有返回 CapabilityBoundingSet，无法确认能力上限。", Fix: systemdTunCapabilityFix})
	} else if !hasNamedCapability(bound, "cap_net_admin") {
		checks = append(checks, Check{Name: name + "上限", Level: "error", Message: "Mihomo 已启用 TUN，但服务的 CapabilityBoundingSet 未包含 CAP_NET_ADMIN。", Fix: systemdTunCapabilityFix})
	} else {
		checks = append(checks, Check{Name: name + "上限", Level: "pass", Message: "服务 CapabilityBoundingSet 允许 CAP_NET_ADMIN；能力上限本身不等于已授予有效能力。"})
	}
	pid, _ := strconv.Atoi(properties["MainPID"])
	if pid <= 0 {
		checks = append(checks, Check{Name: "Mihomo 运行权限", Level: "warn", Message: "未取得运行中的核心 PID，无法验证实际 CapEff 或服务内的 TUN 设备视图。", Fix: systemdTunCapabilityFix})
	} else {
		status, readErr := probe.readFile(fmt.Sprintf("/proc/%d/status", pid))
		mask, parseErr := capabilityMask(status, "CapEff")
		if readErr != nil || parseErr != nil {
			checks = append(checks, Check{Name: "Mihomo 运行权限", Level: "warn", Message: "无法读取核心进程的有效能力，不能用安装进程的权限推断核心权限。", Fix: systemdTunCapabilityFix})
		} else if mask&netAdminCapability == 0 {
			checks = append(checks, Check{Name: "Mihomo 运行权限", Level: "warn", Message: "运行中的核心未持有有效 CAP_NET_ADMIN；它可能已降权，无法据此确认重启后能重新创建 TUN。", Fix: systemdTunCapabilityFix})
		} else {
			coreHasCapability = true
			checks = append(checks, Check{Name: "Mihomo 运行权限", Level: "pass", Message: "当前核心进程持有有效 CAP_NET_ADMIN。"})
		}
		device, statErr := probe.stat(fmt.Sprintf("/proc/%d/root/dev/net/tun", pid))
		if readErr != nil {
			checks = append(checks, Check{Name: "Mihomo 设备视图", Level: "warn", Message: "核心 PID 已退出或无法检查，不能确认运行服务内的 TUN 设备视图。", Fix: systemdTunCapabilityFix})
		} else if os.IsNotExist(statErr) || statErr == nil && device.Mode()&os.ModeCharDevice == 0 {
			checks = append(checks, Check{Name: "Mihomo 设备视图", Level: "error", Message: "已启用 TUN，但当前核心进程的文件系统视图内没有 TUN 字符设备。", Fix: "检查服务 PrivateDevices、RootDirectory/RootImage 和设备绑定；仅向该服务提供 /dev/net/tun，并核对 DeviceAllow。"})
		} else if statErr != nil {
			checks = append(checks, Check{Name: "Mihomo 设备视图", Level: "warn", Message: "无法读取核心服务内的设备视图，不能确认服务能够访问 /dev/net/tun。", Fix: systemdTunCapabilityFix})
		} else {
			checks = append(checks, Check{Name: "Mihomo 设备视图", Level: "pass", Message: "当前核心进程的文件系统视图内可见 TUN 字符设备；设备访问策略仍可能限制打开。"})
		}
	}
	policy := properties["DevicePolicy"]
	if properties["PrivateDevices"] == "yes" || policy == "closed" || policy == "strict" || properties["DeviceAllow"] != "" {
		checks = append(checks, Check{Name: "Mihomo 设备隔离", Level: "warn", Message: "服务启用了设备隔离或访问规则；检查进程成功打开 TUN，不能证明核心服务也可打开。", Fix: "核对 PrivateDevices 及设备绑定是否提供 /dev/net/tun，并检查 DevicePolicy/DeviceAllow 是否允许该字符设备读写；不要整体关闭服务沙箱。"})
	}
	if properties["PrivateNetwork"] == "yes" || properties["NetworkNamespacePath"] != "" {
		checks = append(checks, Check{Name: "Mihomo 网络隔离", Level: "warn", Message: "核心服务使用独立网络命名空间；外部检查的路由和 ip_forward 不代表服务内部状态，不能确认可承担 LAN 旁路网关。", Fix: "核对服务的 PrivateNetwork/NetworkNamespacePath 和预期网络拓扑，在对应网络命名空间内验证转发。"})
	}
	checks = append(checks, inspectCoreTUNRuntime(probe, pid))
	return checks, coreHasCapability
}

func inspectIPv4Forwarding(probe systemProbe) []Check {
	data, err := probe.readFile("/proc/sys/net/ipv4/ip_forward")
	check := Check{Name: "IPv4 转发", Level: "warn", Fix: forwardingFix}
	if err != nil {
		check.Message = "无法读取当前网络命名空间的 net.ipv4.ip_forward，转发状态未确认。"
	} else if strings.TrimSpace(string(data)) == "1" {
		check.Level = "pass"
		check.Message = "当前网络命名空间的 net.ipv4.ip_forward=1；这里只确认运行值，不代表重启后仍开启。"
		check.Fix = ""
	} else if strings.TrimSpace(string(data)) == "0" {
		check.Message = "当前 net.ipv4.ip_forward=0；作为旁路网关转发其他设备流量时需开启。仅本机 TUN 代理或 DNS 服务并不因此需要开启。"
	} else {
		check.Message = "当前 net.ipv4.ip_forward 不是可识别的 0/1 值，转发状态未确认。"
	}
	return []Check{check, inspectForwardingPersistence(probe)}
}

type forwardingSetting struct {
	value string
	where string
}

func parseForwardingSetting(data []byte, path string) forwardingSetting {
	var result forwardingSetting
	for i, raw := range strings.Split(string(data), "\n") {
		line := strings.TrimSpace(strings.SplitN(strings.SplitN(raw, "#", 2)[0], ";", 2)[0])
		key, value, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		key = strings.ReplaceAll(strings.TrimPrefix(strings.TrimPrefix(strings.TrimSpace(key), "-"), "/"), "/", ".")
		if key == "net.ipv4.ip_forward" {
			value = strings.TrimSpace(value)
			if value != "0" && value != "1" {
				value = "未知值"
			}
			result = forwardingSetting{value: value, where: fmt.Sprintf("%s:%d", path, i+1)}
		}
	}
	return result
}

func inspectForwardingPersistence(probe systemProbe) Check {
	// systemd-sysctl picks the highest-priority directory for each basename,
	// then processes selected files in lexical filename order. Merely finding
	// an earlier '=1' is not evidence that a later '=0' will not replace it.
	selected := make(map[string]string)
	unreadable := false
	for _, dir := range []string{"/etc/sysctl.d", "/run/sysctl.d", "/usr/local/lib/sysctl.d", "/usr/lib/sysctl.d", "/lib/sysctl.d"} {
		paths, err := probe.glob(dir + "/*.conf")
		unreadable = unreadable || err != nil
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
		data, err := probe.readFile(path)
		if err != nil {
			unreadable = true
			continue
		}
		if setting := parseForwardingSetting(data, path); setting.where != "" {
			effective = setting
		}
	}
	legacyData, legacyErr := probe.readFile("/etc/sysctl.conf")
	legacy := parseForwardingSetting(legacyData, "/etc/sysctl.conf")
	if legacyErr != nil && !os.IsNotExist(legacyErr) {
		unreadable = true
	}
	check := Check{Name: "IPv4 转发持久化", Level: "warn", Fix: forwardingFix}
	if effective.value == "1" && !strings.HasPrefix(effective.where, "/run/") && !unreadable && (legacy.where == "" || legacy.value == "1") {
		check.Level = "pass"
		check.Message = "在 sysctl.d 中发现最后可读设置为 net.ipv4.ip_forward=1（" + effective.where + "）；仅确认文件设置，启动服务仍可能覆盖，重启后仍需验证。"
		check.Fix = ""
	} else if effective.where != "" {
		check.Message = "sysctl.d 中最后可读设置为 net.ipv4.ip_forward=" + effective.value + "（" + effective.where + "）；不能确认重启后的转发状态。"
	} else if legacy.where != "" {
		check.Message = "仅在 /etc/sysctl.conf 发现 net.ipv4.ip_forward=" + legacy.value + "；该文件是否加载及其顺序取决于发行版和服务，不能确认重启后生效。"
	} else {
		check.Message = "未在常见 sysctl 配置中找到明确的 IPv4 转发设置；可能由其他服务管理，无法确认重启后的状态。"
	}
	if unreadable {
		check.Message += " 部分配置无法读取。"
	}
	if strings.HasPrefix(effective.where, "/run/") {
		check.Message += " 该设置位于临时目录 /run，重启后不保证保留。"
	}
	if legacy.where != "" && effective.where != "" && legacy.value != effective.value {
		check.Message += " /etc/sysctl.conf 与 sysctl.d 的设置不一致，需核对启动加载顺序。"
	}
	return check
}
