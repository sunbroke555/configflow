package routes

// Native AIO IPv4 forwarding persistence, matching aio-ipv4-forwarding.sh so a
// host installed through the gateway wizard can be moved to a full TUN without
// reinstalling. The installer and this file must produce identical artifacts:
// the sysctl.d file, the boot hook and the in-place patched Agent service.
import (
	"agent/preflight"
	"agent/upgrade"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

// Paths are variables only so tests can redirect them into a temporary root.
var aioSysctlDir = "/etc/sysctl.d"
var aioSystemdUnitDir = "/etc/systemd/system"
var aioOpenRCDir = "/etc/init.d"
var aioAgentRoot = "/opt/configflow-agent"
var aioSysfsNetRoot = "/sys/class/net"
var aioPrepareTUN = preflight.PrepareTUNAssumeEnabled

// daemon-reload and the hook may exceed the 3s diagnostic command timeout.
var aioRun = func(name string, args ...string) (string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, name, args...).Output()
	var exitErr *exec.ExitError
	if errors.As(err, &exitErr) {
		if detail := strings.TrimSpace(string(exitErr.Stderr)); detail != "" {
			err = fmt.Errorf("%w: %s", err, detail)
		}
	}
	return string(out), err
}

const aioForwardingMarker = "# Managed by ConfigFlow Shell AIO IPv4 forwarding v1"
const aioForwardingContent = aioForwardingMarker + "\nnet.ipv4.ip_forward=1\n"
const aioForwardingHookName = "enable-ipv4-forwarding.sh"

// Byte-for-byte the heredoc in aio-ipv4-forwarding.sh (a test enforces it).
const aioForwardingHook = `#!/bin/sh
set -eu
config=/etc/sysctl.d/99-configflow-aio-forward.conf
# The installer commits this file only after both Agents start successfully.
[ -e "$config" ] || exit 0
expected='# Managed by ConfigFlow Shell AIO IPv4 forwarding v1
net.ipv4.ip_forward=1'
if [ -L "$config" ] || [ ! -f "$config" ] || [ "$(cat "$config")" != "$expected" ]; then
    echo "ConfigFlow IPv4 转发持久化配置已被修改，拒绝覆盖：$config" >&2
    exit 1
fi
current=$(cat /proc/sys/net/ipv4/ip_forward)
case "$current" in
    1) exit 0 ;;
    0) ;;
    *) echo "无法确认当前网络命名空间的 IPv4 转发状态" >&2; exit 1 ;;
esac
# Apply only this key, not unrelated sysctl files. Changing 0 -> 1 also has the
# normal Linux host/router side effects; do not toggle it on every Agent restart.
if ! sysctl -q -w net.ipv4.ip_forward=1; then
    echo "无法开启 IPv4 转发，请检查当前 Linux/容器的 CAP_NET_ADMIN 和 /proc/sys 写权限" >&2
    exit 1
fi
if [ "$(cat /proc/sys/net/ipv4/ip_forward)" != 1 ]; then
    echo "IPv4 转发写入后读回不是 1，未能确认开启" >&2
    exit 1
fi
`

func aioForwardingConfigPath() string {
	return filepath.Join(aioSysctlDir, "99-configflow-aio-forward.conf")
}

func aioForwardingHookPath() string {
	return filepath.Join(aioAgentRoot, "mihomo", aioForwardingHookName)
}

func aioSystemdHookLine() string { return "ExecStartPre=/bin/sh " + aioForwardingHookPath() }
func aioOpenRCHookLine() string  { return "    /bin/sh " + aioForwardingHookPath() + " || return 1" }

// aioForwardingConfigManaged reports whether IPv4 forwarding is owned by the
// persisted AIO configuration. Gateway removal then leaves forwarding alone.
func aioForwardingConfigManaged() bool {
	state, _ := aioForwardingConfigState()
	return state == "managed"
}

func aioForwardingConfigState() (string, error) {
	path := aioForwardingConfigPath()
	info, err := os.Lstat(path)
	if os.IsNotExist(err) {
		return "absent", nil
	}
	if err != nil {
		return "foreign", err
	}
	if info.Mode()&os.ModeSymlink != 0 {
		return "symlink", nil
	}
	if !info.Mode().IsRegular() {
		return "foreign", nil
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return "foreign", err
	}
	if string(data) == aioForwardingContent {
		return "managed", nil
	}
	return "foreign", nil
}

func aioHookScriptState() (string, error) {
	path := aioForwardingHookPath()
	info, err := os.Lstat(path)
	if os.IsNotExist(err) {
		return "absent", nil
	}
	if err != nil {
		return "modified", err
	}
	if !info.Mode().IsRegular() {
		return "modified", nil
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return "modified", err
	}
	if string(data) == aioForwardingHook {
		return "managed", nil
	}
	return "modified", nil
}

// aioInstallation accepts only the fixed native AIO layout that the installer
// writes; any other installation keeps its own forwarding management.
func aioInstallation(cfg *Config) (nativeAIOInstallation, error) {
	if err := gatewayPlatform(cfg); err != nil {
		return nativeAIOInstallation{}, err
	}
	instance, err := nativeAIOInstallationFor(cfg)
	if err != nil {
		return instance, fmt.Errorf("仅支持普通 Shell 同机安装（systemd 或 OpenRC）的 Mihomo Agent 自动持久化 IPv4 转发；其他安装方式请手动配置")
	}
	return instance, nil
}

// The merged unit view also covers drop-ins outside /etc. update-agent.sh
// accepts exactly one ExecStartPre, and only this hook.
func aioSystemdStartupHook(unit string) (string, error) {
	out, err := aioRun("systemctl", "show", "-p", "ExecStartPre", "--value", unit+".service")
	if err != nil {
		return "unknown", fmt.Errorf("无法读取 Agent 服务的 ExecStartPre：%w", err)
	}
	value := strings.TrimSpace(out)
	if value == "" {
		return "absent", nil
	}
	if strings.Count(value, "{ path=") == 1 && strings.Contains(value, "{ path=/bin/sh ; argv[]=/bin/sh "+aioForwardingHookPath()+" ;") {
		return "registered", nil
	}
	return "foreign", nil
}

func aioOpenRCStartupHook(unit string) (string, error) {
	data, err := os.ReadFile(filepath.Join(aioOpenRCDir, unit))
	if err != nil {
		return "unknown", fmt.Errorf("无法读取 Agent 服务脚本：%w", err)
	}
	lines := strings.Split(string(data), "\n")
	for index, line := range lines {
		if line != "start_pre() {" || index+1 >= len(lines) {
			continue
		}
		switch lines[index+1] {
		case aioOpenRCHookLine():
			return "registered", nil
		case "    ":
			return "absent", nil
		}
		return "unknown", nil
	}
	return "unknown", nil
}

func aioStartupHookState(instance nativeAIOInstallation) (string, error) {
	switch instance.Manager {
	case "systemd":
		return aioSystemdStartupHook(instance.Unit)
	case "openrc":
		return aioOpenRCStartupHook(instance.Unit)
	}
	return "unknown", nil
}

func aioForwardingStatus(cfg *Config) map[string]interface{} {
	result := map[string]interface{}{"protocol": 1, "current": "", "supported": true}
	errs := []string{}
	instance, supportErr := aioInstallation(cfg)
	if supportErr != nil {
		result["supported"], result["unsupported_reason"] = false, supportErr.Error()
	}
	if value, err := gatewayReadSysctl("net/ipv4/ip_forward"); err != nil {
		errs = append(errs, "无法读取 IPv4 转发状态："+err.Error())
	} else {
		result["current"] = value
	}
	config, err := aioForwardingConfigState()
	if err != nil {
		errs = append(errs, "无法检查 IPv4 转发持久化配置："+err.Error())
	}
	hook, err := aioHookScriptState()
	if err != nil {
		errs = append(errs, "无法检查 IPv4 转发启动脚本："+err.Error())
	}
	startup := "unknown"
	if supportErr == nil {
		startup, err = aioStartupHookState(instance)
		if err != nil {
			errs = append(errs, err.Error())
		}
	}
	result["config"], result["hook_script"], result["startup_hook"] = config, hook, startup
	result["persisted"] = config == "managed" && hook == "managed" && startup == "registered"
	if len(errs) != 0 {
		result["error"] = strings.Join(errs, "；")
	}
	return result
}

var aioDevicePattern = regexp.MustCompile(`^[A-Za-z0-9_][A-Za-z0-9_.-]{0,14}$`)

// aioTUNStatus describes the deployed configuration; the device name is never
// assumed because old gateway profiles used Mihomo and full TUN uses Meta.
func aioTUNStatus(cfg *Config) map[string]interface{} {
	result := map[string]interface{}{"configured": false, "device": "Meta", "present": false, "gateway_marker": false}
	if cfg == nil || cfg.ServiceType != "mihomo" {
		result["error"] = "仅 Mihomo Agent 使用 TUN"
		return result
	}
	routing, err := readMihomoRoutingStrict(cfg.ConfigPath)
	if err != nil {
		result["error"] = err.Error()
		return result
	}
	marker, doc, err := gatewayConfigEnabled(cfg)
	if err != nil {
		result["error"] = err.Error()
		return result
	}
	tun, _ := doc["tun"].(map[string]interface{})
	result["device"], result["gateway_marker"] = routing.tunDevice, marker
	result["configured"] = routing.tunEnabled && tun["auto-route"] == true && tun["auto-redirect"] == true
	if aioDevicePattern.MatchString(routing.tunDevice) {
		_, err := os.Stat(filepath.Join(aioSysfsNetRoot, routing.tunDevice))
		result["present"] = err == nil
	}
	return result
}

// aioPatchSystemd returns the patched unit, or nil when already registered.
func aioPatchSystemd(data []byte, directory string) ([]byte, error) {
	lines := strings.Split(string(data), "\n")
	execStart := "ExecStart=" + directory + "/configflow-agent -config " + directory + "/config.json"
	hook, starts, at := aioSystemdHookLine(), 0, -1
	for index, line := range lines {
		if strings.HasPrefix(line, "ExecStartPre=") && line != hook {
			return nil, fmt.Errorf("Agent 服务存在未知 ExecStartPre，无法自动注册 IPv4 转发检查")
		}
		if strings.HasPrefix(line, "ExecStart=") {
			starts++
			at = index
		}
	}
	if starts != 1 || lines[at] != execStart || strings.Count(string(data), hook+"\n") > 1 {
		return nil, fmt.Errorf("Agent 服务的 ExecStart 不是同机安装写入的格式，无法自动注册 IPv4 转发检查")
	}
	previous := ""
	if at > 0 {
		previous = lines[at-1]
	}
	switch {
	case previous == hook:
		return nil, nil
	case strings.Contains(string(data), hook+"\n"):
		return nil, fmt.Errorf("Agent 服务的 IPv4 转发检查位置异常，请用普通同机安装重新安装 Agent")
	case at > 0 && previous == "":
		lines[at-1] = hook
	case at > 0 && previous == "WorkingDirectory="+directory:
		lines = append(lines[:at], append([]string{hook}, lines[at:]...)...)
	default:
		return nil, fmt.Errorf("Agent 服务文件结构无法识别，无法自动注册 IPv4 转发检查；请用普通同机安装重新安装 Agent")
	}
	return []byte(strings.Join(lines, "\n")), nil
}

func aioPatchOpenRC(data []byte) ([]byte, error) {
	lines := strings.Split(string(data), "\n")
	found := -1
	for index, line := range lines {
		if line == "start_pre() {" {
			if found >= 0 {
				found = -2
				break
			}
			found = index
		}
	}
	if found >= 0 && found+1 < len(lines) {
		switch lines[found+1] {
		case aioOpenRCHookLine():
			return nil, nil
		case "    ":
			lines[found+1] = aioOpenRCHookLine()
			return []byte(strings.Join(lines, "\n")), nil
		}
	}
	return nil, fmt.Errorf("Agent 服务脚本已被修改，无法自动注册 IPv4 转发检查；请用普通同机安装重新安装 Agent")
}

func aioRegularFile(path, label string) error {
	info, err := os.Lstat(path)
	if err != nil {
		return fmt.Errorf("无法检查%s：%w", label, err)
	}
	if !info.Mode().IsRegular() {
		return fmt.Errorf("%s不是普通文件（不能是符号链接）：%s", label, path)
	}
	return nil
}

func aioSystemdDropInsWithPre(unit string) error {
	directory := filepath.Join(aioSystemdUnitDir, unit+".service.d")
	entries, err := os.ReadDir(directory)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("无法检查 Agent 服务的附加配置：%w", err)
	}
	for _, entry := range entries {
		data, err := os.ReadFile(filepath.Join(directory, entry.Name()))
		if err != nil || strings.Contains(string(data), "ExecStartPre") {
			return fmt.Errorf("Agent 服务的附加配置 %s 含 ExecStartPre 或无法读取，无法自动注册 IPv4 转发检查", filepath.Join(directory, entry.Name()))
		}
	}
	return nil
}

func aioForwardingFailure(message string, err error) error {
	return fmt.Errorf("%s：%w。请检查当前 Linux/容器的 CAP_NET_ADMIN 和 /proc/sys 写权限；LXC 权限受限时需由宿主机管理员调整", message, err)
}

func summarizeTUNPrepare(report preflight.Report) map[string]interface{} {
	level, messages := "pass", []string{}
	for _, check := range report {
		switch check.Level {
		case "error":
			level = "error"
		case "warn":
			if level == "pass" {
				level = "warn"
			}
		}
		message := check.Message
		if check.Level != "pass" && check.Fix != "" {
			message += " " + check.Fix
		}
		messages = append(messages, message)
	}
	return map[string]interface{}{"level": level, "message": strings.Join(messages, "\n")}
}

// persistAIOForwarding enables and persists IPv4 forwarding the way the
// ordinary AIO installer does. The caller holds gatewayMu, returnRoutingMu and
// the service operation lock. Every step is checked first, so a retry after
// any interruption converges. Forwarding is never written to 0.
func persistAIOForwarding(cfg *Config, prepareTUN bool) (result map[string]interface{}, resultErr error) {
	instance, err := aioInstallation(cfg)
	if err != nil {
		return nil, err
	}
	if err := instance.validateDirectory(); err != nil {
		return nil, fmt.Errorf("Agent 安装目录无效：%w", err)
	}
	if info, err := os.Lstat(aioSysctlDir); os.IsNotExist(err) {
		if err := os.Mkdir(aioSysctlDir, 0755); err != nil {
			return nil, fmt.Errorf("无法创建 IPv4 转发持久化目录：%w", err)
		}
	} else if err != nil || !info.IsDir() {
		return nil, fmt.Errorf("sysctl.d 路径不是目录或是符号链接：%s", aioSysctlDir)
	}
	target := aioForwardingConfigPath()
	config, err := aioForwardingConfigState()
	if err != nil || (config != "absent" && config != "managed") {
		return nil, fmt.Errorf("IPv4 转发持久化路径已有非 ConfigFlow 配置，请先核对：%s", target)
	}
	// Writing the current value tests the real kernel permission without
	// toggling forwarding; a root uid is not sufficient inside LXC.
	const key = "net/ipv4/ip_forward"
	current, err := gatewayReadSysctl(key)
	if err != nil {
		return nil, aioForwardingFailure("无法读取 IPv4 转发状态", err)
	}
	if err := gatewayWriteSysctl(key, current); err != nil {
		return nil, aioForwardingFailure("无法写入 IPv4 转发参数", err)
	}
	if value, err := gatewayReadSysctl(key); err != nil || value != current {
		return nil, aioForwardingFailure("IPv4 转发权限检查后读回值不一致", fmt.Errorf("读回 %q：%v", value, err))
	}
	hook := aioForwardingHookPath()
	hookState, err := aioHookScriptState()
	if err != nil {
		return nil, fmt.Errorf("无法检查 IPv4 转发启动脚本：%w", err)
	}
	if hookState == "modified" {
		return nil, fmt.Errorf("IPv4 转发启动脚本已被修改，拒绝覆盖：%s", hook)
	}

	// Inspect the service before changing anything; patched is nil when the
	// file already carries the hook in the installer's position.
	unitPath := filepath.Join(aioOpenRCDir, instance.Unit)
	if instance.Manager == "systemd" {
		unitPath = filepath.Join(aioSystemdUnitDir, instance.Unit+".service")
	}
	if err := aioRegularFile(unitPath, "Agent 服务文件"); err != nil {
		return nil, err
	}
	snapshot, err := upgrade.Capture(unitPath)
	if err != nil {
		return nil, fmt.Errorf("无法读取 Agent 服务文件：%w", err)
	}
	var patched []byte
	loaded := ""
	if instance.Manager == "systemd" {
		if err := aioSystemdDropInsWithPre(instance.Unit); err != nil {
			return nil, err
		}
		if patched, err = aioPatchSystemd(snapshot.Data, instance.Directory); err != nil {
			return nil, err
		}
		if loaded, err = aioSystemdStartupHook(instance.Unit); err != nil {
			return nil, err
		}
		if loaded == "foreign" || (patched != nil && loaded != "absent") {
			return nil, fmt.Errorf("Agent 服务已从其他位置加载 ExecStartPre，无法自动注册 IPv4 转发检查")
		}
	} else if patched, err = aioPatchOpenRC(snapshot.Data); err != nil {
		return nil, err
	}

	// Until the sysctl.d commit point, undo this call's hook and unit changes.
	committed, hookCreated, reloaded := false, false, false
	var unitSnapshot *upgrade.Snapshot
	defer func() {
		if resultErr == nil || committed {
			return
		}
		rollback := []string{}
		if unitSnapshot != nil {
			if err := upgrade.Restore([]upgrade.Snapshot{*unitSnapshot}); err != nil {
				rollback = append(rollback, "恢复 Agent 服务文件失败："+err.Error())
			} else if reloaded {
				if _, err := aioRun("systemctl", "daemon-reload"); err != nil {
					rollback = append(rollback, "恢复后 systemctl daemon-reload 失败："+err.Error())
				}
			}
		}
		if hookCreated {
			if err := os.Remove(hook); err != nil && !os.IsNotExist(err) {
				rollback = append(rollback, "删除本次写入的启动脚本失败："+err.Error())
			}
		}
		if len(rollback) != 0 {
			resultErr = fmt.Errorf("%v；%s", resultErr, strings.Join(rollback, "；"))
		}
	}()
	if hookState == "absent" {
		if err := durableWrite(hook, []byte(aioForwardingHook), 0755); err != nil {
			return nil, fmt.Errorf("无法写入 IPv4 转发启动脚本：%w", err)
		}
		hookCreated = true
	}
	if patched != nil {
		if err := upgrade.AtomicWrite(unitPath, patched, snapshot.Mode); err != nil {
			return nil, fmt.Errorf("无法更新 Agent 服务文件：%w", err)
		}
		unitSnapshot = &snapshot
	}
	// A previous attempt may have patched the file but not reloaded systemd.
	if instance.Manager == "systemd" && (patched != nil || loaded != "registered") {
		reloaded = true
		if _, err := aioRun("systemctl", "daemon-reload"); err != nil {
			return nil, fmt.Errorf("systemctl daemon-reload 失败：%w", err)
		}
		if state, err := aioSystemdStartupHook(instance.Unit); err != nil || state != "registered" {
			return nil, fmt.Errorf("重新加载后 Agent 服务未注册 IPv4 转发检查（%s）：%v", state, err)
		}
	}

	if config == "absent" {
		if err := aioCommitForwardingConfig(target); err != nil {
			return nil, err
		}
	}
	committed = true
	if _, err := aioRun("/bin/sh", hook); err != nil {
		return nil, fmt.Errorf("IPv4 转发未能成功开启并验证：%w", err)
	}
	if !aioForwardingConfigManaged() {
		return nil, fmt.Errorf("IPv4 转发持久化配置在应用期间发生变化：%s", target)
	}
	if value, err := gatewayReadSysctl(key); err != nil || value != "1" {
		return nil, aioForwardingFailure("最终 IPv4 转发状态不是 1", fmt.Errorf("读回 %q：%v", value, err))
	}
	result = map[string]interface{}{"tun_prepare": nil}
	if prepareTUN {
		// TUN preparation is advisory here: forwarding is already persisted.
		result["tun_prepare"] = summarizeTUNPrepare(aioPrepareTUN(deploymentDNSConfig(cfg)))
	}
	return result, nil
}

// The hard link is an atomic commit that refuses a file created concurrently.
func aioCommitForwardingConfig(target string) error {
	file, err := os.CreateTemp(aioSysctlDir, ".configflow-aio-forward.")
	if err != nil {
		return fmt.Errorf("IPv4 转发持久化目录不可写：%w", err)
	}
	temp := file.Name()
	defer os.Remove(temp)
	_, err = file.WriteString(aioForwardingContent)
	if err == nil {
		err = file.Chmod(0644)
	}
	if err == nil {
		err = file.Sync()
	}
	if closeErr := file.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		return fmt.Errorf("无法写入 IPv4 转发持久化文件：%w", err)
	}
	if err := os.Link(temp, target); err != nil {
		if aioForwardingConfigManaged() {
			return nil
		}
		return fmt.Errorf("无法提交 IPv4 转发持久化配置：%w", err)
	}
	return syncDirectory(aioSysctlDir)
}
