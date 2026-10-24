import type { ForwardFilter } from '@/lib/forwardFilter'

/** The shape backend/agents/linux_gateway.sanitize_forward_filter returns for a Docker host. */
export const DOCKER_HINT = '这台 Linux 的防火墙可能拦截经过 Mihomo TUN（Meta）的流量：主路由转来的 UDP（如 QUIC、语音通话、游戏）和 ping 会失败；其他 TCP 连接不受影响。ConfigFlow 不会改动防火墙，需要时在这台 Linux 上执行下面的命令放行。'
export const DOCKER_COMMANDS = ['iptables -I DOCKER-USER -o Meta -j ACCEPT', 'iptables -I DOCKER-USER -i Meta -j ACCEPT']
export const NFT_COMMANDS = [`nft 'insert rule inet filter forward oifname "Meta" accept'`, `nft 'insert rule inet filter forward iifname "Meta" accept'`]
export const VERIFY = '执行后等 5 秒以上再回到这里刷新状态（发现拦截时检查结果缓存 5 秒）；再在客户端 ping 一个走代理的地址，或打开一个使用 UDP 的应用（如 QUIC 网页、语音通话）确认可用。'

export const mayDrop = (changes: Partial<ForwardFilter> = {}): ForwardFilter => ({
  protocol: 1, state: 'may_drop', reason: '', device: 'Meta', auto_redirect: true, policy: 'drop', input_policy: 'accept', flows: ['forward'], forward_protocols: ['udp', 'icmp'],
  sources: [
    { kind: 'docker', backend: 'nft', hook: 'forward', family: '', table: '', chain: '', policy: 'drop', detail: 'iptables FORWARD 默认策略为 DROP' },
    { kind: 'nftables', backend: '', hook: 'forward', family: 'inet', table: 'filter', chain: 'forward', policy: 'drop', detail: 'nftables inet filter forward 默认策略为 drop' },
  ],
  remedies: [
    { source: 'docker', label: 'Docker', device: 'Meta', commands: [...DOCKER_COMMANDS], persist_note: '这两条规则重启后会丢失（重启 Docker 不会清空 DOCKER-USER）。' },
    { source: 'nftables', label: 'nftables inet filter forward', device: 'Meta', commands: [...NFT_COMMANDS], persist_note: 'nft 命令只改当前规则，重启后会丢失。' },
  ],
  hint: DOCKER_HINT, verify: VERIFY, checked_at: '2026-10-24T03:00:00Z', cached: false, ...changes,
})

export const quiet = (state: 'ok' | 'unknown' | 'not_applicable', reason = ''): ForwardFilter => ({
  protocol: 1, state, reason, device: 'Meta', auto_redirect: true, policy: state === 'ok' ? 'accept' : '', input_policy: state === 'ok' ? 'accept' : '', flows: [],
  sources: [], remedies: [], hint: '', verify: '', checked_at: '', cached: false,
})

/** A UFW host as Agent 1.8.42-go reports it after the lab fix: forwarded UDP, DNS and redirected TCP blocked (UFW accepts forwarded ping). */
export const UFW_COMMANDS = [
  'ufw route allow in on Meta', 'ufw route allow out on Meta', 'ufw allow in on Meta',
  "grep -q -- '-A ufw-before-input -m conntrack --ctstate DNAT -j ACCEPT' /etc/ufw/before.rules || sed -i '/^# End required lines/a -A ufw-before-input -m conntrack --ctstate DNAT -j ACCEPT' /etc/ufw/before.rules",
  'ufw reload',
]
export const ufwDrop = (changes: Partial<ForwardFilter> = {}): ForwardFilter => mayDrop({
  policy: 'drop', input_policy: 'drop', flows: ['forward', 'dns', 'redirect_tcp', 'tun_tcp'], forward_protocols: ['udp'],
  sources: [{ kind: 'ufw', backend: 'nft', hook: 'input', family: '', table: '', chain: 'INPUT', policy: 'drop', detail: 'UFW 已启用，默认拒绝入站（incoming）；iptables INPUT 默认策略为 DROP' }],
  remedies: [{ source: 'ufw', label: 'UFW', device: 'Meta', commands: [...UFW_COMMANDS], persist_note: 'UFW 规则会自动保存；/etc/ufw/before.rules 的改动在 ufw reload 后生效，重启后仍有效。' }],
  hint: '这台 Linux 的防火墙可能拦截经过 Mihomo TUN（Meta）的流量：主路由转来的 UDP（如 QUIC、语音通话、游戏）会失败；主路由转来的 TCP 连接会失败（auto-redirect 把它们转给本机端口，被本机入站规则拦下）；把这台 Linux 当作 DNS 服务器的设备查不到域名（UDP 和 TCP 方式都会失败）。ConfigFlow 不会改动防火墙，需要时在这台 Linux 上执行下面的命令放行。',
  ...changes,
})

/** An all-in-one host with UFW (lab v6-d-aio): DNS to OxiDNS on this host is dropped on INPUT as well. */
export const AIO_UFW_COMMANDS = [
  'ufw route allow in on Meta', 'ufw route allow out on Meta', 'ufw allow in on eth0 to any port 53', UFW_COMMANDS[3]!, 'ufw reload',
]
export const aioUfwDrop = (changes: Partial<ForwardFilter> = {}): ForwardFilter => ufwDrop({
  flows: ['forward', 'local_dns', 'redirect_tcp'],
  remedies: [{ source: 'ufw', label: 'UFW', device: 'Meta', commands: [...AIO_UFW_COMMANDS], persist_note: 'UFW 规则会自动保存；/etc/ufw/before.rules 的改动在 ufw reload 后生效，重启后仍有效。' }],
  hint: '这台 Linux 的防火墙可能拦截经过 Mihomo TUN（Meta）的流量和发到本机 53 端口的 DNS 查询：主路由转来的 UDP（如 QUIC、语音通话、游戏）会失败；主路由转来的 TCP 连接会失败（auto-redirect 把它们转给本机端口，被本机入站规则拦下）；客户端查不到域名（UDP 和 TCP 方式都失败）。ConfigFlow 不会改动防火墙，需要时在这台 Linux 上执行下面的命令放行。',
  ...changes,
})
