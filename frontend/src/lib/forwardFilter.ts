/**
 * 完整 TUN 主机的转发过滤提醒（Agent 1.8.42-go 起在 Linux 网关状态里提供 `forward_filter`）。
 * 只作提醒：不参与任何完成判定。命令由 Agent 生成、后端净化，前端只原样展示，不自行拼接。
 */

export type ForwardFilterState = 'ok' | 'may_drop' | 'unknown' | 'not_applicable'
export type ForwardFilterSourceKind = 'docker' | 'iptables' | 'nftables' | 'firewalld' | 'ufw'
/**
 * 被拦截的流量：forward＝主路由转来、进出 TUN 的转发（UDP、ping；没有 auto-redirect 时也包括 TCP），
 * dns＝把这台 Linux 当 DNS 的查询（auto-redirect 劫持进 TUN），redirect_tcp＝auto-redirect 转给本机端口的 TCP，
 * tun_tcp＝TUN 交给本机 Mihomo 的 TCP（system / mixed 栈）。
 */
export type ForwardFilterFlow = 'forward' | 'dns' | 'redirect_tcp' | 'tun_tcp'

export interface ForwardFilterSource {
  kind: ForwardFilterSourceKind
  backend: '' | 'nft' | 'legacy'
  /** 拦截所在的挂载点（Agent 1.8.42-go 修订版起） */
  hook?: '' | 'forward' | 'input'
  family: '' | 'ip' | 'inet'
  table: string
  chain: string
  policy: '' | 'drop' | 'accept'
  detail: string
}

export interface ForwardFilterRemedy {
  source: ForwardFilterSourceKind
  label: string
  device: string
  commands: string[]
  persist_note: string
}

export interface ForwardFilter {
  protocol: number
  state: ForwardFilterState
  reason: string
  device: string
  auto_redirect: boolean
  /** 转发与入站挂载点真实的默认策略；放行规则生效后仍可能是 drop，结论以 state 为准 */
  policy: '' | 'drop' | 'accept'
  input_policy?: '' | 'drop' | 'accept'
  /** 被拦截的流量；缺省（较早的 Agent）时按 auto_redirect 推断 */
  flows?: ForwardFilterFlow[]
  sources: ForwardFilterSource[]
  remedies: ForwardFilterRemedy[]
  hint: string
  verify: string
  checked_at: string
  cached: boolean
}

export interface ForwardFilterGroup {
  key: string
  /** 来源名称，如 Docker、nftables inet filter forward */
  source: string
  /** 标签行文字：在这台 Linux 上执行（Docker） */
  label: string
  commands: string[]
  /** 复制到剪贴板的原文，每条命令一行 */
  text: string
  persistNote: string
}

export interface ForwardFilterWarning {
  /** 状态行文字 */
  status: string
  /** 上游路由列的短句（102px 内一行放下） */
  short: string
  hint: string
  verify: string
  groups: ForwardFilterGroup[]
  /** 检测到的来源说明，放进折叠详情 */
  details: string[]
}

const SOURCE_LABELS: Record<ForwardFilterSourceKind, string> = {
  docker: 'Docker', iptables: 'iptables', nftables: 'nftables', firewalld: 'firewalld', ufw: 'UFW',
}

const isText = (value: unknown): value is string => typeof value === 'string'
const FLOWS: readonly ForwardFilterFlow[] = ['forward', 'dns', 'redirect_tcp', 'tun_tcp']

/**
 * 状态短句按被拦截的流量：TCP 也受影响时为「可能拦截转发」，只有 UDP / ping 时为「可能拦截 UDP」，
 * 只影响把这台 Linux 当 DNS 的查询时为「可能拦截 DNS」。
 */
function impactLabel(filter: ForwardFilter): string {
  const redirect = filter.auto_redirect === true
  const flows = Array.isArray(filter.flows) ? filter.flows.filter(flow => FLOWS.includes(flow)) : []
  if (!flows.length) return redirect ? '可能拦截 UDP' : '可能拦截转发'
  const tcp = flows.includes('redirect_tcp') || (!redirect && (flows.includes('forward') || flows.includes('tun_tcp')))
  if (tcp) return '可能拦截转发'
  return flows.includes('forward') ? '可能拦截 UDP' : '可能拦截 DNS'
}

export function forwardFilterSourceLabel(kind: ForwardFilterSourceKind, label?: string): string {
  return (isText(label) && label.trim()) || SOURCE_LABELS[kind] || kind
}

/** 只有 may_drop 且结构有效时返回提醒；旧 Agent、ok、unknown、not_applicable 一律为 null。 */
export function forwardFilterWarning(filter: ForwardFilter | null | undefined): ForwardFilterWarning | null {
  if (!filter || typeof filter !== 'object' || filter.state !== 'may_drop' || !isText(filter.hint) || !filter.hint.trim()) return null
  const remedies = Array.isArray(filter.remedies) ? filter.remedies : []
  const groups: ForwardFilterGroup[] = []
  remedies.forEach((remedy, index) => {
    if (!remedy || typeof remedy !== 'object' || !(remedy.source in SOURCE_LABELS)) return
    const commands = Array.isArray(remedy.commands) ? remedy.commands.filter(command => isText(command) && command.length > 0) : []
    if (!commands.length) return
    const source = forwardFilterSourceLabel(remedy.source, remedy.label)
    groups.push({
      key: `${index}:${remedy.source}:${source}`, source, label: `在这台 Linux 上执行（${source}）`,
      commands, text: commands.join('\n'), persistNote: isText(remedy.persist_note) ? remedy.persist_note : '',
    })
  })
  const sources = Array.isArray(filter.sources) ? filter.sources : []
  const details = sources.map(source => (source && isText(source.detail) ? source.detail.trim() : '')).filter(Boolean)
  const label = impactLabel(filter)
  return {
    status: label,
    short: label,
    hint: filter.hint.trim(),
    verify: isText(filter.verify) ? filter.verify.trim() : '',
    groups,
    details: [...new Set(details)],
  }
}
