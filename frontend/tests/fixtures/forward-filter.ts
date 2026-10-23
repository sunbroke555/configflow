import type { ForwardFilter } from '@/lib/forwardFilter'

/** The shape backend/agents/linux_gateway.sanitize_forward_filter returns for a Docker host. */
export const DOCKER_HINT = '这台 Linux 的防火墙会拦截转发到 Mihomo TUN（Meta）的流量：TCP 仍可用，UDP（如 QUIC、语音通话、游戏）和 ping 会失败。ConfigFlow 不会改动防火墙，需要时在这台 Linux 上执行下面的命令放行。'
export const DOCKER_COMMANDS = ['iptables -I DOCKER-USER -o Meta -j ACCEPT', 'iptables -I DOCKER-USER -i Meta -j ACCEPT']
export const NFT_COMMANDS = [`nft 'insert rule inet filter forward oifname "Meta" accept'`, `nft 'insert rule inet filter forward iifname "Meta" accept'`]
export const VERIFY = '执行后等几秒再回到这里刷新状态（发现拦截时结果只缓存 5 秒）；再在客户端 ping 一个走代理的地址，或打开一个使用 UDP 的应用（如 QUIC 网页、语音通话）确认可用。'

export const mayDrop = (changes: Partial<ForwardFilter> = {}): ForwardFilter => ({
  protocol: 1, state: 'may_drop', reason: '', device: 'Meta', auto_redirect: true, policy: 'drop',
  sources: [
    { kind: 'docker', backend: 'nft', family: '', table: '', chain: '', policy: 'drop', detail: 'iptables FORWARD 默认策略为 DROP' },
    { kind: 'nftables', backend: '', family: 'inet', table: 'filter', chain: 'forward', policy: 'drop', detail: 'nftables inet filter forward 默认策略为 drop' },
  ],
  remedies: [
    { source: 'docker', label: 'Docker', device: 'Meta', commands: [...DOCKER_COMMANDS], persist_note: '这两条规则重启后会丢失（重启 Docker 不会清空 DOCKER-USER）。' },
    { source: 'nftables', label: 'nftables inet filter forward', device: 'Meta', commands: [...NFT_COMMANDS], persist_note: 'nft 命令只改当前规则，重启后会丢失。' },
  ],
  hint: DOCKER_HINT, verify: VERIFY, checked_at: '2026-10-24T03:00:00Z', cached: false, ...changes,
})

export const quiet = (state: 'ok' | 'unknown' | 'not_applicable', reason = ''): ForwardFilter => ({
  protocol: 1, state, reason, device: 'Meta', auto_redirect: true, policy: state === 'ok' ? 'accept' : '',
  sources: [], remedies: [], hint: '', verify: '', checked_at: '', cached: false,
})
