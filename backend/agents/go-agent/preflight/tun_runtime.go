package preflight

import (
	"fmt"
	"os"
	"sort"
	"strconv"
	"strings"
)

const tunRuntimeFix = "查看 Mihomo 核心运行日志中的 TUN 创建结果，并核对实际运行配置；本检查不会重启核心、创建接口或修改路由。"

func tunRuntimeWarning(message, fix string) Check {
	return Check{Name: "TUN 实际启动", Level: "warn", Message: message, Fix: fix}
}

// inspectCoreTUNRuntime reads the kernel's association between an open TUN fd
// and its interface. A matching name in net/dev alone is not ownership evidence.
// Read the core's network namespace, not the installer's, and guard the snapshot
// against process exit/PID reuse. No ioctl, setns, service action or writes occur.
func inspectCoreTUNRuntime(probe systemProbe, pid int) Check {
	if pid <= 0 {
		return tunRuntimeWarning("Mihomo 服务未提供有效的运行中核心 PID，无法检查实际 TUN 接口。", tunRuntimeFix)
	}
	root := fmt.Sprintf("/proc/%d", pid)
	before, err := probe.readFile(root + "/stat")
	if err != nil {
		return tunRuntimeReadFailure(pid, "进程信息", err)
	}
	started, ok := tunProcessStartTime(before, pid)
	if !ok {
		return tunRuntimeWarning(fmt.Sprintf("无法解析 Mihomo 核心 PID %d 的启动时间，无法可靠核对 TUN 接口归属。", pid), tunRuntimeFix)
	}
	entries, err := probe.readDir(root + "/fdinfo")
	if err != nil {
		return tunRuntimeReadFailure(pid, "文件描述符目录 fdinfo", err)
	}
	interfaces := map[string][]string{}
	unbound := false
	var fdReadErr error
	for _, entry := range entries {
		fd, err := strconv.Atoi(entry.Name())
		if err != nil || fd < 0 || entry.IsDir() {
			continue
		}
		info, err := probe.readFile(root + "/fdinfo/" + entry.Name())
		if err != nil {
			// Other fds may close during inspection. Do not discard an already
			// verified TUN fd, but do not report absence from a partial scan.
			if fdReadErr == nil || os.IsPermission(err) {
				fdReadErr = err
			}
			continue
		}
		if name, found := tunFDInterface(info); found {
			if name == "" {
				unbound = true
			} else {
				interfaces[name] = append(interfaces[name], entry.Name())
			}
		}
	}

	var netdev []byte
	var netErr error
	if len(interfaces) > 0 {
		netdev, netErr = probe.readFile(root + "/net/dev")
	}
	// The same core may reload and release a TUN fd without changing its PID.
	// A persistent interface left in net/dev is not proof it still owns the fd.
	var released []string
	var recheckErr error
	for name, fds := range interfaces {
		owned := false
		var readErr error
		for _, fd := range fds {
			info, err := probe.readFile(root + "/fdinfo/" + fd)
			if err != nil {
				if readErr == nil || os.IsPermission(err) {
					readErr = err
				}
				continue
			}
			if current, found := tunFDInterface(info); found && current == name {
				owned = true
				break
			}
		}
		if !owned {
			released = append(released, name)
			if readErr != nil && (recheckErr == nil || os.IsPermission(readErr)) {
				recheckErr = readErr
			}
		}
	}
	after, err := probe.readFile(root + "/stat")
	if err != nil {
		return tunRuntimeReadFailure(pid, "检查结束时的进程信息", err)
	}
	ended, ok := tunProcessStartTime(after, pid)
	if !ok || ended != started {
		return tunRuntimeWarning(fmt.Sprintf("检查期间 Mihomo 核心 PID %d 的进程身份发生变化，TUN 接口结果已丢弃，请重新检查。", pid), tunRuntimeFix)
	}
	if len(interfaces) == 0 {
		if fdReadErr != nil {
			return tunRuntimeReadFailure(pid, "部分文件描述符 fdinfo，未取得已绑定 TUN 接口的证据", fdReadErr)
		}
		if unbound {
			return tunRuntimeWarning(fmt.Sprintf("Mihomo 核心 PID %d 已打开 TUN/TAP 设备，但 fdinfo 中的 iff 为空，尚未绑定接口。", pid), tunRuntimeFix)
		}
		return tunRuntimeWarning(fmt.Sprintf("Mihomo 核心 PID %d 的 fdinfo 未发现已绑定 TUN 接口的 iff 信息；核心可能尚未创建接口，或当前内核未提供该信息。", pid), tunRuntimeFix)
	}
	if netErr != nil {
		return tunRuntimeReadFailure(pid, "网络命名空间接口列表 net/dev", netErr)
	}
	if len(released) > 0 {
		sort.Strings(released)
		if recheckErr != nil {
			return tunRuntimeReadFailure(pid, "接口 "+strings.Join(released, "、")+" 的文件描述符复核信息 fdinfo", recheckErr)
		}
		return tunRuntimeWarning(fmt.Sprintf("检查期间 Mihomo 核心 PID %d 的 TUN 文件描述符关联发生变化，已无法确认其仍持有接口 %s，请重新检查。", pid, strings.Join(released, "、")), tunRuntimeFix)
	}
	present := map[string]bool{}
	for _, line := range strings.Split(string(netdev), "\n") {
		name, counters, found := strings.Cut(line, ":")
		if found && len(strings.Fields(counters)) == 16 {
			present[strings.TrimSpace(name)] = true
		}
	}
	var names, missing []string
	for name := range interfaces {
		names = append(names, name)
		if !present[name] {
			missing = append(missing, name)
		}
	}
	sort.Strings(names)
	sort.Strings(missing)
	if len(missing) > 0 {
		return tunRuntimeWarning(fmt.Sprintf("Mihomo 核心 PID %d 的 TUN 文件描述符关联接口 %s，但其网络命名空间的 net/dev 中未找到该接口；接口可能已变化或位于其他网络命名空间。", pid, strings.Join(missing, "、")), tunRuntimeFix)
	}
	return Check{Name: "TUN 实际启动", Level: "pass", Message: fmt.Sprintf("Mihomo 核心 PID %d 正持有内核 TUN/TAP 接口 %s，已确认接口存在于该进程的网络命名空间；本项仅确认接口创建与持有，不代表路由或流量转发验证。", pid, strings.Join(names, "、"))}
}

func tunFDInterface(data []byte) (string, bool) {
	for _, line := range strings.Split(string(data), "\n") {
		key, value, found := strings.Cut(line, ":")
		if found && key == "iff" {
			return strings.TrimSpace(value), true
		}
	}
	return "", false
}

func tunRuntimeReadFailure(pid int, resource string, err error) Check {
	reason := "读取失败"
	if os.IsPermission(err) {
		reason = "权限不足"
	} else if os.IsNotExist(err) {
		reason = "路径不存在，进程可能已退出或内核未提供此信息"
	}
	return tunRuntimeWarning(fmt.Sprintf("无法读取 Mihomo 核心 PID %d 的%s：%s，无法确认实际 TUN 接口。", pid, resource, reason), tunRuntimeFix)
}

func tunProcessStartTime(data []byte, pid int) (string, bool) {
	// comm (field 2) can contain spaces and ')'. starttime is field 22,
	// i.e. index 19 after the final ')' delimiting comm.
	text := string(data)
	open, close := strings.IndexByte(text, '('), strings.LastIndexByte(text, ')')
	if open < 0 || close <= open || strings.TrimSpace(text[:open]) != strconv.Itoa(pid) {
		return "", false
	}
	fields := strings.Fields(text[close+1:])
	if len(fields) <= 19 {
		return "", false
	}
	if _, err := strconv.ParseUint(fields[19], 10, 64); err != nil {
		return "", false
	}
	return fields[19], true
}
