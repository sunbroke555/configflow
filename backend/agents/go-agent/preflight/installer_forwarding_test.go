package preflight

import (
	"agent/clioutput"
	"agent/netfilter"
	"bytes"
	"context"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func installerProbe(fixture *systemFixture, owner string) systemProbe {
	probe := fixture.probe()
	probe.getenv = func(key string) string {
		switch key {
		case installerForwardingEnv:
			return "persist"
		case "CONFIGFLOW_FORWARDING_OWNER":
			return owner
		}
		return ""
	}
	probe.writeFile = func(path string, _ []byte) error {
		if path != "/proc/sys/net/ipv4/ip_forward" || owner != "gateway" {
			panic("installer-mode checks never write: " + path)
		}
		return nil
	}
	return probe
}

func TestInstallerManagedForwardingDescribesWhatTheInstallerWillDo(t *testing.T) {
	for _, tc := range []struct {
		name, value, level, message, compact string
		readErr                              error
	}{
		{"off", "0\n", "pass", "当前 net.ipv4.ip_forward=0，安装器将开启并持久化（/etc/sysctl.d/99-configflow-aio-forward.conf）；Mihomo Agent 每次启动前会再次检查。", "IPv4 转发  当前为 0，安装器将开启并持久化（/etc/sysctl.d/99-configflow-aio-forward.conf）", nil},
		{"on", "1\n", "pass", "当前 net.ipv4.ip_forward=1，安装器将持久化（/etc/sysctl.d/99-configflow-aio-forward.conf）；Mihomo Agent 每次启动前会再次检查。", "IPv4 转发  当前为 1，安装器将持久化（/etc/sysctl.d/99-configflow-aio-forward.conf）", nil},
		{"unreadable", "", "warn", "无法读取当前 net.ipv4.ip_forward；安装器稍后会再次检查，无法开启时会停止安装。", "", os.ErrPermission},
		{"odd value", "2\n", "warn", "无法读取当前 net.ipv4.ip_forward；安装器稍后会再次检查，无法开启时会停止安装。", "", nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			fixture := healthySystemFixture()
			delete(fixture.files, "/etc/sysctl.d/99-configflow.conf")
			fixture.files["/proc/sys/net/ipv4/ip_forward"] = []byte(tc.value)
			if tc.readErr != nil {
				fixture.fileErr["/proc/sys/net/ipv4/ip_forward"] = tc.readErr
			}
			checks := systemChecksWithProbe(nil, installerProbe(fixture, ""))
			check := namedSystemCheck(t, checks, installerForwardingName)
			if check.Level != tc.level || check.Message != tc.message || check.Fix != "" {
				t.Fatalf("check: %+v", check)
			}
			if tc.compact != "" && compactPass(check) != tc.compact {
				t.Fatalf("compact: %q", compactPass(check))
			}
			for _, other := range checks {
				if other.Name == "IPv4 转发" || other.Name == "IPv4 转发持久化" || other.Name == "IPv4 转发冲突" || strings.Contains(other.Fix, "99-configflow-forward.conf") {
					t.Fatalf("manual repair reminder in installer mode: %+v", other)
				}
			}
		})
	}
}

func TestInstallerManagedForwardingReportsLaterZero(t *testing.T) {
	for _, tc := range []struct {
		name  string
		files map[string]string
		where string
	}{
		{"later sysctl.d file", map[string]string{"/etc/sysctl.d/99-zz-test.conf": "net.ipv4.ip_forward = 0\n"}, "/etc/sysctl.d/99-zz-test.conf:1"},
		{"debian 99-sysctl.conf link", map[string]string{"/etc/sysctl.d/99-sysctl.conf": "# x\nnet.ipv4.ip_forward=0\n", "/etc/sysctl.conf": "# x\nnet.ipv4.ip_forward=0\n"}, "/etc/sysctl.d/99-sysctl.conf:2"},
		{"sysctl.conf", map[string]string{"/etc/sysctl.conf": "net/ipv4/ip_forward=0\n"}, "/etc/sysctl.conf:1"},
		{"vendor file", map[string]string{"/usr/lib/sysctl.d/99-zzz-vendor.conf": "net.ipv4.ip_forward=0\n"}, "/usr/lib/sysctl.d/99-zzz-vendor.conf:1"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			fixture := healthySystemFixture()
			for path, data := range tc.files {
				fixture.files[path] = []byte(data)
			}
			checks := systemChecksWithProbe(nil, installerProbe(fixture, ""))
			check := namedSystemCheck(t, checks, "IPv4 转发冲突")
			want := tc.where + " 在 ConfigFlow 的持久化文件之后加载，并设置 net.ipv4.ip_forward=0，重启后转发可能被关闭。"
			if check.Level != "warn" || check.Message != want || check.Fix != "把该行改为 1 或删除，重启后用 sysctl net.ipv4.ip_forward 确认为 1。" {
				t.Fatalf("conflict: %+v", check)
			}
			if strings.HasPrefix(check.Name, "IPv4 转发持久化：") {
				t.Fatal("name must not join the forwarding note")
			}
		})
	}
	// Earlier '=0' files are overridden by the installer's file: no warning.
	fixture := healthySystemFixture()
	fixture.files["/etc/sysctl.d/10-early.conf"] = []byte("net.ipv4.ip_forward=0\n")
	fixture.files["/etc/sysctl.d/99-configflow-aio-forward.conf"] = []byte("net.ipv4.ip_forward=1\n")
	for _, check := range systemChecksWithProbe(nil, installerProbe(fixture, "")) {
		if check.Name == "IPv4 转发冲突" {
			t.Fatalf("earlier file is not a conflict: %+v", check)
		}
	}
}

func TestSelectiveGatewayOwnerStillWinsOverInstallerMode(t *testing.T) {
	fixture := healthySystemFixture()
	fixture.files["/proc/sys/net/ipv4/ip_forward"] = []byte("0\n")
	probe := installerProbe(fixture, "gateway")
	called := false
	probe.forwardFilter = func(netfilter.Target) netfilter.Result { called = true; return netfilter.Result{} }
	checks := systemChecksWithProbe([]Config{fixtureMihomo(t, true)}, probe)
	namedSystemCheck(t, checks, "IPv4 转发权限")
	for _, check := range checks {
		if check.Name == installerForwardingName || check.Name == "转发过滤" {
			t.Fatalf("gateway installs keep their output: %+v", check)
		}
	}
	if called {
		t.Fatal("selective gateway must not inspect forward filtering")
	}
}

func TestDefaultModeNeverInspectsForwardFiltering(t *testing.T) {
	fixture := healthySystemFixture()
	probe := fixture.probe()
	probe.forwardFilter = func(netfilter.Target) netfilter.Result { t.Fatal("inspected"); return netfilter.Result{} }
	checks := systemChecksWithProbe([]Config{fixtureMihomo(t, true)}, probe)
	namedSystemCheck(t, checks, "IPv4 转发")
	namedSystemCheck(t, checks, "IPv4 转发持久化")
}

func mihomoWithTUN(t *testing.T, body string) Config {
	t.Helper()
	path := filepath.Join(t.TempDir(), "config.yaml")
	if err := os.WriteFile(path, []byte(body), 0600); err != nil {
		t.Fatal(err)
	}
	return Config{ServiceType: "mihomo", ConfigPath: path, ServiceManager: "systemd", ServiceUnit: "mihomo"}
}

const fullTUN = "tun:\n  enable: true\n  auto-route: true\n  auto-redirect: true\n"

var mayDrop = netfilter.Result{Protocol: 1, State: netfilter.StateMayDrop, Device: "Meta", AutoRedirect: true,
	Hint:     "这台 Linux 的防火墙可能拦截经过 Mihomo TUN（Meta）的流量：主路由转来的 UDP（如 QUIC、语音通话、游戏）和 ping 会失败；其他 TCP 连接不受影响。ConfigFlow 不会改动防火墙，需要时在这台 Linux 上执行下面的命令放行。",
	Remedies: []netfilter.Remedy{{Source: "docker", Label: "Docker", Commands: []string{"iptables -I DOCKER-USER -o Meta -j ACCEPT", "iptables -I DOCKER-USER -i Meta -j ACCEPT"}, PersistNote: "这两条规则重启后会丢失。"}}}

func TestInstallerForwardFilterWarnsWithCommandsAndNeverFails(t *testing.T) {
	for _, tc := range []struct {
		name   string
		config string
		device string
		auto   bool
	}{
		{"tun disabled uses full TUN default", "tun:\n  enable: false\n", "Meta", true},
		{"configured device", "tun:\n  enable: true\n  auto-route: true\n  device: cf0\n  auto-redirect: false\n  dns-hijack: []\n", "cf0", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			fixture := healthySystemFixture()
			probe := installerProbe(fixture, "")
			var target netfilter.Target
			probe.forwardFilter = func(value netfilter.Target) netfilter.Result { target = value; return mayDrop }
			report := Report(systemChecksWithProbe([]Config{mihomoWithTUN(t, tc.config)}, probe))
			check := namedSystemCheck(t, report, "转发过滤")
			if target.Device != tc.device || target.AutoRedirect != tc.auto {
				t.Fatalf("target: %+v", target)
			}
			if check.Level != "warn" || len(check.Groups) != 1 || len(check.Groups[0].Commands) != 2 || check.Groups[0].Label != "Docker" || check.Groups[0].Note != "这两条规则重启后会丢失。" ||
				!strings.Contains(check.Fix, "以 ConfigFlow 页面显示的命令为准") || strings.Contains(check.Fix, "重启后会丢失") || strings.Contains(check.Message, "下面的命令放行") {
				t.Fatalf("check: %+v", check)
			}
			if report.HasErrors() {
				t.Fatal("forward filtering must never fail the environment check")
			}
		})
	}
	for _, state := range []string{netfilter.StateUnknown, netfilter.StateOK} {
		fixture := healthySystemFixture()
		probe := installerProbe(fixture, "")
		probe.forwardFilter = func(netfilter.Target) netfilter.Result {
			return netfilter.Result{State: state, Reason: "timeout"}
		}
		report := Report(systemChecksWithProbe([]Config{mihomoWithTUN(t, fullTUN)}, probe))
		check := namedSystemCheck(t, report, "转发过滤")
		want := map[string]string{netfilter.StateUnknown: "info", netfilter.StateOK: "pass"}[state]
		if check.Level != want || report.HasErrors() || compactPass(check) != "" {
			t.Fatalf("%s: %+v", state, check)
		}
	}
	// DNS-only installs never inspect forwarding filters.
	probe := installerProbe(healthySystemFixture(), "")
	probe.forwardFilter = func(netfilter.Target) netfilter.Result { t.Fatal("inspected"); return netfilter.Result{} }
	for _, check := range systemChecksWithProbe([]Config{{ServiceType: "oxidns"}}, probe) {
		if check.Name == "转发过滤" {
			t.Fatalf("DNS-only: %+v", check)
		}
	}
}

func TestCommandsArePrintedUnwrappedAndInfoIsLogOnly(t *testing.T) {
	long := "nft 'insert rule inet filter forward oifname \"Meta\" accept'"
	report := Report{
		{Name: installerForwardingName, Level: "pass", Message: "当前 net.ipv4.ip_forward=0，安装器将开启并持久化（/etc/sysctl.d/99-configflow-aio-forward.conf）；Mihomo Agent 每次启动前会再次检查。"},
		{Name: "转发过滤", Level: "warn", Message: "防火墙会拦截。", Fix: "执行下面的命令。", Groups: []CommandGroup{{Label: "nftables inet filter forward", Commands: []string{long, "nft\x1b[2J x"}, Note: "重启后会丢失。"}}},
		{Name: "转发过滤检查", Level: "info", Message: "未能确认转发过滤（timeout），不影响安装。"},
	}
	var terminal, details bytes.Buffer
	report.PrintWithOptions(&terminal, clioutput.Options{Details: &details, Width: 40}, false)
	text := terminal.String()
	if !strings.Contains(text, "    在这台 Linux 上执行（nftables inet filter forward）：\n      "+long+"\n") || !strings.Contains(text, "      重启后会丢失。\n") || strings.Contains(text, "\x1b") {
		t.Fatalf("commands wrapped or unescaped: %q", text)
	}
	if !strings.Contains(text, "1 项通过，1 项提醒") || strings.Contains(text, "未能确认") {
		t.Fatalf("info must be neither counted nor shown: %s", text)
	}
	if !strings.Contains(text, "✓ IPv4 转发  当前为 0，安装器将开启并") {
		t.Fatalf("missing compact forwarding row: %s", text)
	}
	log := details.String()
	for _, want := range []string{"[通过] IPv4 转发（安装器管理）：当前 net.ipv4.ip_forward=0，安装器将开启并持久化", "[提醒] 转发过滤：防火墙会拦截。\n  处理方法：执行下面的命令。\n  在这台 Linux 上执行（nftables inet filter forward）：\n  命令：" + long + "\n", "[信息] 转发过滤检查：未能确认转发过滤（timeout），不影响安装。", "环境检查完成（1 项提醒）"} {
		if !strings.Contains(log, want) {
			t.Fatalf("log missing %q:\n%s", want, log)
		}
	}
}

func TestInstallerForwardFilterTargetMatchesThePublishedTUN(t *testing.T) {
	for _, tc := range []struct {
		name    string
		configs func(t *testing.T) []Config
		want    netfilter.Target
	}{
		// Standalone Mihomo: ConfigFlow's full TUN hijacks DNS to this host.
		{"standalone default", func(t *testing.T) []Config { return []Config{mihomoWithTUN(t, "mode: rule\n")} },
			netfilter.Target{Device: "Meta", AutoRedirect: true, Stack: "mixed", DNSHijack: true}},
		// AIO: OxiDNS on this host answers DNS; the publish excludes the host.
		{"aio default", func(t *testing.T) []Config {
			return []Config{mihomoWithTUN(t, "mode: rule\n"), {ServiceType: "oxidns"}}
		},
			netfilter.Target{Device: "Meta", AutoRedirect: true, Stack: "mixed"}},
		{"configured tun", func(t *testing.T) []Config {
			return []Config{mihomoWithTUN(t, fullTUN+"  stack: gvisor\n  dns-hijack: [any:53]\n  route-exclude-address: [10.97.0.20/32]\n")}
		}, netfilter.Target{Device: "Meta", AutoRedirect: true, Stack: "gvisor"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			probe := installerProbe(healthySystemFixture(), "")
			probe.lanAddresses = func() []net.IP { return []net.IP{net.ParseIP("10.97.0.20").To4()} }
			var target netfilter.Target
			probe.forwardFilter = func(value netfilter.Target) netfilter.Result { target = value; return mayDrop }
			systemChecksWithProbe(tc.configs(t), probe)
			if target != tc.want {
				t.Fatalf("target %+v, want %+v", target, tc.want)
			}
		})
	}
}

func TestInstallerForwardFilterFollowsTheFullTUNScope(t *testing.T) {
	for _, tc := range []struct{ name, config string }{
		{"selective gateway marker", "tun:\n  enable: true\n  auto-route: true\n  route-address-set: [cf]\nx-configflow-linux-gateway:\n  enabled: true\n"},
		{"disabled gateway marker", "tun:\n  enable: false\nx-configflow-linux-gateway:\n  enabled: false\n"},
		{"route-address-set", "tun:\n  enable: true\n  auto-route: true\n  route-address-set: [cf]\n"},
		{"route-address", "tun:\n  enable: true\n  auto-route: true\n  route-address: [198.18.0.0/15]\n"},
		{"auto-route off", "tun:\n  enable: true\n  auto-route: false\n"},
		{"auto-route unset", "tun:\n  enable: true\n"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			probe := installerProbe(healthySystemFixture(), "")
			probe.forwardFilter = func(netfilter.Target) netfilter.Result { t.Fatal("inspected"); return mayDrop }
			for _, check := range systemChecksWithProbe([]Config{mihomoWithTUN(t, tc.config)}, probe) {
				if check.Name == "转发过滤" {
					t.Fatalf("not full TUN: %+v", check)
				}
			}
		})
	}
}

func TestInstallerForwardFilterKeepsEachSourceWithItsNote(t *testing.T) {
	probe := installerProbe(healthySystemFixture(), "")
	result := mayDrop
	result.Remedies = []netfilter.Remedy{
		{Source: "docker", Label: "Docker", Commands: []string{"iptables -I DOCKER-USER -o Meta -j ACCEPT", "iptables -I DOCKER-USER -i Meta -j ACCEPT"}, PersistNote: "这两条规则重启后会丢失。"},
		{Source: "firewalld", Label: "firewalld", Commands: []string{"firewall-cmd --permanent --zone=trusted --change-interface=Meta", "firewall-cmd --reload"}, PersistNote: "已用 --permanent 保存，重启后仍有效。"},
	}
	probe.forwardFilter = func(netfilter.Target) netfilter.Result { return result }
	report := Report(systemChecksWithProbe([]Config{mihomoWithTUN(t, fullTUN)}, probe))
	check := namedSystemCheck(t, report, "转发过滤")
	if len(check.Groups) != 2 || check.Groups[1].Label != "firewalld" || len(check.Groups[1].Commands) != 2 || strings.Contains(check.Fix, "重启后") {
		t.Fatalf("groups: %+v", check)
	}
	var terminal, details bytes.Buffer
	Report{check}.PrintWithOptions(&terminal, clioutput.Options{Details: &details, Width: 100}, false)
	want := "    在这台 Linux 上执行（Docker）：\n      iptables -I DOCKER-USER -o Meta -j ACCEPT\n      iptables -I DOCKER-USER -i Meta -j ACCEPT\n      这两条规则重启后会丢失。\n" +
		"    在这台 Linux 上执行（firewalld）：\n      firewall-cmd --permanent --zone=trusted --change-interface=Meta\n      firewall-cmd --reload\n      已用 --permanent 保存，重启后仍有效。\n"
	if !strings.Contains(terminal.String(), want) {
		t.Fatalf("terminal:\n%s", terminal.String())
	}
	log := "  在这台 Linux 上执行（Docker）：\n  命令：iptables -I DOCKER-USER -o Meta -j ACCEPT\n  命令：iptables -I DOCKER-USER -i Meta -j ACCEPT\n  说明：这两条规则重启后会丢失。\n" +
		"  在这台 Linux 上执行（firewalld）：\n  命令：firewall-cmd --permanent --zone=trusted --change-interface=Meta\n  命令：firewall-cmd --reload\n  说明：已用 --permanent 保存，重启后仍有效。\n"
	if !strings.Contains(details.String(), log) {
		t.Fatalf("log:\n%s", details.String())
	}
}

func TestInstallerForwardFilterIsBoundedEvenIfInspectionHangs(t *testing.T) {
	old := netfilter.TotalBudget
	netfilter.TotalBudget = 100 * time.Millisecond
	t.Cleanup(func() { netfilter.TotalBudget = old })
	release := make(chan struct{})
	defer close(release)
	start := time.Now()
	result := boundedForwardFilterWith(netfilter.Target{Device: "Meta", AutoRedirect: true}, func(context.Context) netfilter.Result {
		<-release // ignores its context, like a command whose child keeps the pipe
		return mayDrop
	})
	if result.State != netfilter.StateUnknown || result.Reason != "timeout" || time.Since(start) > 2*time.Second {
		t.Fatalf("%+v after %s", result, time.Since(start))
	}
}
