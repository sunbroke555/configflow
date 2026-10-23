package netfilter

const (
	iptablesPersistNote = "这两条规则重启后会丢失。Debian/Ubuntu 可安装 iptables-persistent 后执行 netfilter-persistent save；Alpine 执行 rc-service iptables save。"
	dockerPersistNote   = "这两条规则重启后会丢失（重启 Docker 不会清空 DOCKER-USER）。Debian/Ubuntu 可安装 iptables-persistent 后执行 netfilter-persistent save；Alpine 执行 rc-service iptables save。"
	nftPersistNote      = "nft 命令只改当前规则，重启后会丢失；请把这两条规则写进 /etc/nftables.conf（或系统加载的 nftables 配置）里对应的链。"
	firewalldNote       = "已用 --permanent 保存，重启后仍有效。"
	ufwNote             = "UFW 会自动保存，重启后仍有效。"

	verifyText = "执行后回到这里刷新状态；再在客户端 ping 一个走代理的地址，或打开一个使用 UDP 的应用（如 QUIC 网页、语音通话）确认可用。"
)

// SourceLabel names a source in operator-facing text.
func SourceLabel(kind, backend, binary string) string {
	switch kind {
	case "docker":
		return "Docker"
	case "ufw":
		return "UFW"
	case "firewalld":
		return "firewalld"
	case "nftables":
		return "nftables"
	}
	if binary == "iptables-legacy" {
		return "iptables（legacy）"
	}
	return "iptables"
}

func iptablesRemedy(kind, binary, device string) Remedy {
	remedy := Remedy{Source: kind, Label: SourceLabel(kind, "", binary), Device: device}
	if !DevicePattern.MatchString(device) {
		return remedy
	}
	switch kind {
	case "ufw":
		remedy.Commands = []string{"ufw route allow in on " + device, "ufw route allow out on " + device}
		remedy.PersistNote = ufwNote
	case "docker":
		remedy.Commands = []string{binary + " -I DOCKER-USER -o " + device + " -j ACCEPT", binary + " -I DOCKER-USER -i " + device + " -j ACCEPT"}
		remedy.PersistNote = dockerPersistNote
	default:
		remedy.Commands = []string{binary + " -I FORWARD -o " + device + " -j ACCEPT", binary + " -I FORWARD -i " + device + " -j ACCEPT"}
		remedy.PersistNote = iptablesPersistNote
	}
	return remedy
}

func nftRemedy(family, table, chain, device string) Remedy {
	remedy := Remedy{Source: "nftables", Label: "nftables " + family + " " + table + " " + chain, Device: device}
	if !DevicePattern.MatchString(device) || !nftNamePattern.MatchString(table) || !nftNamePattern.MatchString(chain) || (family != "ip" && family != "inet") {
		return remedy
	}
	// One shell argument keeps nft's own double quotes around the device name.
	prefix := "nft 'insert rule " + family + " " + table + " " + chain + " "
	remedy.Commands = []string{prefix + `oifname "` + device + `" accept'`, prefix + `iifname "` + device + `" accept'`}
	remedy.PersistNote = nftPersistNote
	return remedy
}

func firewalldRemedy(device, zone string) Remedy {
	remedy := Remedy{Source: "firewalld", Label: "firewalld", Device: device}
	if !DevicePattern.MatchString(device) {
		return remedy
	}
	// An interface already bound to another zone has to be moved, not added.
	operation := "--add-interface="
	if zone != "" {
		operation = "--change-interface="
	}
	remedy.Commands = []string{"firewall-cmd --permanent --zone=trusted " + operation + device + " && firewall-cmd --reload"}
	remedy.PersistNote = firewalldNote
	return remedy
}

// Hint is the one-sentence explanation shown with a may_drop result.
func hint(target Target, commands bool) string {
	device := displayDevice(target.Device)
	text := "这台 Linux 的防火墙会拦截转发到 Mihomo TUN（" + device + "）的流量："
	if target.AutoRedirect {
		text += "TCP 仍可用，UDP（如 QUIC、语音通话、游戏）和 ping 会失败。"
	} else {
		text += "主路由转来的连接（包括 TCP、UDP 和 ping）都可能失败。"
	}
	if commands {
		return text + "ConfigFlow 不会改动防火墙，需要时在这台 Linux 上执行下面的命令放行。"
	}
	return text + "ConfigFlow 不会改动防火墙，请在这台 Linux 上手动放行进出 " + device + " 的转发流量。"
}
