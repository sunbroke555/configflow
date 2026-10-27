/**
 * 策略规则的 OxiDNS DNS 处理：选项文案、显示标签、新规则的默认值、粘贴解析与字段提示。
 *
 * DNS 状态由 `GET /api/rules/dns` 计算，前端只负责显示与提交词汇表中的值；
 * 显式选择永远不随策略变化，只有「跟随策略」按策略推导。
 */
import { isAxiosError } from 'axios'
import { copyText } from '@/lib/clipboard'
import { choose, notify } from '@/lib/feedback'
import { optionCopy } from '@/lib/optionCopy'
import { isDnsRuleType, isOxidnsRule, isOxidnsRuleType } from '@/lib/oxidns'
import type { RuleDnsAgent, RuleDnsEngine, RuleDnsRoute, RuleDnsState, RuleDnsUpstream, RuleDnsValue } from '@/types'

export const RULE_TYPES = [
  'DOMAIN', 'DOMAIN-SUFFIX', 'DOMAIN-KEYWORD', 'IP-CIDR', 'IP-CIDR6', 'IP-SUFFIX', 'DST-PORT', 'SRC-PORT',
  'GEOIP', 'GEOSITE', 'RULE-SET', 'AND', 'OR', 'NOT', 'MATCH'
]
/** 值内含逗号或为空，不能按行粘贴，每次单独添加一条 */
export const SINGLE_RULE_TYPES = ['AND', 'OR', 'NOT', 'MATCH']
/** 只参与连接分流的单条 IP 规则类型 */
export const IP_DNS_RULE_TYPES = ['IP-CIDR', 'IP-CIDR6']
/** 显示 no-resolve 开关的类型；逻辑规则中仅对 IP 子条件生效 */
export const NO_RESOLVE_RULE_TYPES = ['IP-CIDR', 'IP-CIDR6', 'IP-SUFFIX', 'GEOIP', 'AND', 'OR', 'NOT']
export const PASTE_SUGGEST_RULESET = 20
export const PASTE_LIMIT = 200

export const BLOCK_ROUTES = ['refused', 'nxdomain', 'nodata', 'blackhole'] as const
export const ROUTE_LABELS: Record<string, string> = {
  direct: '直连 DNS', proxy: '代理 DNS', refused: '拒绝', nxdomain: '不存在', nodata: '空应答', blackhole: '黑洞'
}
export const CHOICE_LABELS: Record<string, string> = { follow: '跟随策略', default: '默认上游', upstream: '指定上游', ...ROUTE_LABELS }
const SHORT_LABELS: Record<string, string> = { direct: '直连', proxy: '代理' }
const NON_PROXY_POLICIES = ['REJECT', 'REJECT-DROP', 'PASS']

export const DNS_FIELD_HELP = 'DNS 用来把域名查成 IP 地址。混合规则集只使用域名部分；IP 规则仅用于连接分流。这里选择用哪组 DNS 服务器查询，网站怎么连接仍由连接策略决定。仅对 OxiDNS 生效。自定义 Hosts、AdGuard 规则和「全部直连」的设备优先于这里的设置。'
export const PINNED_HINT = '已固定为当前地址，之后修改上游列表不影响这条规则'

/** 选项文案：拒绝、不存在、空应答、黑洞四条简述由用户确认，保持原样 */
export const DNS_COPY = {
  follow: optionCopy('按连接策略自动选 DNS，修改策略时一起变', '希望 DNS 自动跟随时。',
    '按这里保存的策略选择 DNS，无法判断时用代理 DNS。连接策略选「拒绝」只拦连接，DNS 按「默认上游」处理。在 Mihomo 面板切换节点或策略组，不会改变 DNS。'),
  default: optionCopy('不为网站指定 DNS，沿用其他 DNS 设置', '不想为网站单独指定 DNS 时。',
    '访问网站要不要走代理，仍按这条规则设置。向哪台 DNS 服务器查询网站 IP 地址，按其他 DNS 规则决定；都没匹配时，使用「未匹配时转发到」选中的服务器。'),
  direct: optionCopy('固定用直连 DNS 查询，不随连接策略改变', '希望域名一直用直连 DNS 查询时，常用于国内网站或内网服务。',
    '使用 OxiDNS「DNS 上游」中直连列表的服务器查询地址。以后把连接策略改成代理，这条规则仍用直连 DNS。'),
  proxy: optionCopy('固定用代理 DNS 查询，不随连接策略改变', '希望域名一直用代理 DNS 查询时，常用于需要代理访问的网站。',
    '使用 OxiDNS「DNS 上游」中代理列表的服务器查询地址。以后把连接策略改成直连，这条规则仍用代理 DNS。'),
  upstream: optionCopy('只用你选的这一个上游解析，适合内网域名', '公司内网等必须用特定 DNS 的域名。',
    '为该上游生成独立转发和缓存，不与其他上游竞速；直连列表的上游同样做 IP 优选和 RouterOS 联动，代理列表的改写 TTL；失败时返回 SERVFAIL。'),
  refused: optionCopy('直接拒绝查询，客户端立即失败', '明确禁止解析的域名。',
    '多数客户端立即失败；返回 REFUSED，不会被缓存，但部分系统会改问下一个 DNS 服务器，下发多个 DNS 时可能被绕过。'),
  nxdomain: optionCopy('告诉客户端这个域名不存在', '屏蔽广告、跟踪域名。',
    '返回 NXDOMAIN（不带 SOA），对该域名的所有查询类型生效，客户端通常只短时缓存。'),
  nodata: optionCopy('域名存在但没有地址，常用于屏蔽 IPv6', '需要让应用认为没有可用地址时。',
    '返回 NOERROR 且无记录，对所有查询类型生效，包括 IPv4；只想关 IPv6 请用「屏蔽查询类型」。'),
  blackhole: optionCopy('返回 0.0.0.0 / ::，连接立刻失败', '遇到 NXDOMAIN 会反复重试的应用。',
    'A 返回 0.0.0.0、AAAA 返回 ::（开启 IPv4 优先时为空），其他类型空应答；客户端缓存 300 秒。')
} satisfies Record<string, { hint: string; help: string }>

export const isUpstreamValue = (value: unknown): value is `upstream:${string}` =>
  typeof value === 'string' && value.startsWith('upstream:')
export const isBlockRoute = (value: unknown) => (BLOCK_ROUTES as readonly unknown[]).includes(value)
/** 选项文案的键：所有指定上游共用一条 */
export const copyKey = (value: RuleDnsValue) => (isUpstreamValue(value) ? 'upstream' : value) as keyof typeof DNS_COPY

interface PolicyItem { itemType?: string; rule_type?: string; policy?: string; behavior?: string }

/** 策略组使用服务端解析的默认出口；缺少映射时兼容旧版本，按代理处理。 */
export function followRoute(item: PolicyItem, policyRoutes?: RuleDnsEngine['policy_routes']): 'direct' | 'proxy' | null {
  if (!isOxidnsRule(item)) return null
  const policy = item.policy
  if (policy === 'DIRECT') return 'direct'
  if (!policy || NON_PROXY_POLICIES.includes(policy)) return null
  if (policyRoutes && Object.prototype.hasOwnProperty.call(policyRoutes, policy)) return policyRoutes[policy] ?? null
  return 'proxy'
}

/** 未匹配时转发到的上游，用于说明「默认上游」的实际去向 */
export const forwardLabel = (defaultForward?: string) =>
  defaultForward === 'forward_local' ? '直连 DNS' : defaultForward === 'forward_fallback' ? '备用 DNS' : '代理 DNS'

/** 未命中任何域名规则时的去向 */
export const defaultRoute = (defaultForward?: string): 'direct' | 'proxy' =>
  defaultForward === 'forward_local' ? 'direct' : 'proxy'

/** 指定上游标签（「直连 · 地址」）中的地址部分 */
export const upstreamAddress = (label: string) => label.replace(/^(直连|代理|备用) · /, '')

/** 一条有效路由的完整名称；null 即默认上游 */
export function routeLabel(route: RuleDnsRoute | null | undefined, upstreamLabel?: string) {
  if (!route) return '默认上游'
  if (isUpstreamValue(route)) return upstreamLabel ? `指定上游 · ${upstreamLabel}` : '指定上游'
  return ROUTE_LABELS[route] || route
}

/** 表单选择器触发器上的名称；跟随策略带出当前策略对应的去向 */
export function choiceLabel(value: RuleDnsValue, context: { policyRoute?: string | null; upstreamLabel?: string } = {}) {
  if (value === 'follow') return `跟随策略 · ${context.policyRoute ? ROUTE_LABELS[context.policyRoute] : '默认上游'}`
  if (value === 'default') return '默认上游'
  return routeLabel(value, context.upstreamLabel)
}

/** 显示状态对应的选择器初值；停用的规则保留暂存的选择 */
export function stateValue(entry: RuleDnsState | undefined): RuleDnsValue {
  if (!entry) return 'default'
  if (entry.state === 'follow' || entry.state === 'follow_drift' || entry.follow) return 'follow'
  if (entry.state === 'unsupported' || entry.state === 'default') return 'default'
  return entry.route || 'default'
}

export type DnsChipTone = 'muted' | 'neutral' | 'bad' | 'warn'
export interface DnsChip {
  text: string
  tone: DnsChipTone
  icon?: 'direct' | 'proxy' | 'upstream' | 'drift' | 'lock'
  /** 读屏与悬停时的完整说明 */
  title: string
}

/** 列表中一条规则的 DNS chip；只有拦截和「与策略不一致」使用颜色 */
export function dnsChip(entry: RuleDnsState | undefined, engine: Pick<RuleDnsEngine, 'default_forward' | 'editable'>): DnsChip {
  if (entry?.state === 'unsupported') {
    const text = entry.reason || '该类型规则不参与 DNS 分流'
    return { text: '不参与', tone: 'muted', title: text }
  }
  if (entry?.state === 'disabled') {
    const after = entry.follow ? '跟随策略' : entry.route ? `为${routeLabel(entry.route, entry.upstream_label)}` : ''
    const text = after ? `已停用 · 启用后${after}` : '已停用'
    return { text, tone: 'muted', title: text }
  }
  if (!engine.editable) {
    // 列宽有限：chip 只写「完整参数」配锁图标，完整说明在 title 与 FieldHelp 中。
    return { text: '完整参数', tone: 'muted', icon: 'lock', title: `${routeLabel(entry?.route, entry?.upstream_label)}，需在完整参数修改` }
  }
  const route = entry?.route || null
  if (entry?.state === 'follow') {
    const text = `跟随策略 · ${route ? SHORT_LABELS[route] || routeLabel(route) : '默认'}`
    return { text, tone: 'muted', title: `跟随策略：${routeLabel(route)}` }
  }
  if (entry?.state === 'follow_drift') {
    const actual = !route ? '默认' : isUpstreamValue(route) ? (entry.upstream_label ? upstreamAddress(entry.upstream_label) : '指定上游')
      : SHORT_LABELS[route] || routeLabel(route)
    return { text: `${actual} · 与策略不一致`, tone: 'warn', icon: 'drift',
      title: `跟随策略，但 DNS 仍为${routeLabel(route, entry.upstream_label)}，与策略不一致` }
  }
  if (!route) {
    const text = `默认上游 · ${forwardLabel(engine.default_forward).replace(/ DNS$/, '')}`
    return { text, tone: 'muted', title: `其他 DNS 规则都未匹配时，使用${forwardLabel(engine.default_forward)}` }
  }
  if (isUpstreamValue(route)) {
    // 只显示地址，直连 / 代理来源放在 title 与读屏文字中，避免地址被截断。
    const text = entry?.upstream_label ? upstreamAddress(entry.upstream_label) : '指定上游'
    return { text, tone: 'neutral', icon: 'upstream', title: routeLabel(route, entry?.upstream_label) }
  }
  if (isBlockRoute(route)) return { text: ROUTE_LABELS[route], tone: 'bad', title: ROUTE_LABELS[route] }
  return { text: `指定 · ${SHORT_LABELS[route] || ROUTE_LABELS[route] || route}`, tone: 'neutral', title: ROUTE_LABELS[route] || route }
}

/** 一行文字的 DNS 摘要，用于命中模拟与确认文案 */
export const dnsLabel = (entry: RuleDnsState | undefined, engine: Pick<RuleDnsEngine, 'default_forward' | 'editable'>) => {
  const chip = dnsChip(entry, { ...engine, editable: true })
  if (entry?.state === 'follow') return `跟随 · ${routeLabel(entry.route)}`
  return chip.text
}

export const chipClass = (tone: DnsChipTone) =>
  tone === 'bad' ? 'chip-bad' : tone === 'warn' ? 'chip-warn' : tone === 'muted' ? 'text-muted-foreground' : 'text-foreground'

/** 来源内容的类型：domain 为域名部分，ip 为连接 IP 规则（包括 classical 规则集） */
export interface DnsSourceParts { domain: boolean; ip: boolean }

/**
 * 新规则的默认 DNS：域名部分跟随策略；不使用 OxiDNS 或纯 IP 来源保持默认上游。
 * 混合粘贴的 IP 行始终不参与 DNS，连接规则原样保留。
 */
export function defaultDnsChoice(source: DnsSourceParts, engine: Pick<RuleDnsEngine, 'oxidns_in_use'> | null | undefined): RuleDnsValue {
  if (!engine?.oxidns_in_use || !source.domain) return 'default'
  return 'follow'
}

export interface PasteRow {
  line: number
  rule_type: string
  value: string
  no_resolve: boolean
}

export interface PasteResult {
  rows: PasteRow[]
  invalid: Array<{ line: number; text: string; message: string }>
  /** 粘贴内容内部的重复行，只添加一次 */
  repeated: number[]
  /** 行尾策略等被忽略的字段 */
  ignored: Array<{ line: number; value: string }>
  notes: string[]
  /** 非空、非注释的行数 */
  count: number
  overLimit: boolean
}

/** Full Mihomo parser vocabulary; the picker above intentionally shows common types only. */
export const SUPPORTED_RULE_TYPES = new Set([
  'DOMAIN', 'DOMAIN-SUFFIX', 'DOMAIN-KEYWORD', 'DOMAIN-REGEX', 'DOMAIN-WILDCARD',
  'GEOSITE', 'GEOIP', 'SRC-GEOIP', 'IP-ASN', 'SRC-IP-ASN',
  'IP-CIDR', 'IP-CIDR6', 'SRC-IP-CIDR', 'IP-SUFFIX', 'SRC-IP-SUFFIX',
  'SRC-PORT', 'DST-PORT', 'IN-PORT', 'DSCP',
  'PROCESS-NAME', 'PROCESS-PATH', 'PROCESS-NAME-REGEX', 'PROCESS-PATH-REGEX',
  'PROCESS-NAME-WILDCARD', 'PROCESS-PATH-WILDCARD',
  'NETWORK', 'UID', 'IN-TYPE', 'IN-USER', 'IN-NAME', 'REMATCH-NAME',
  'SUB-RULE', 'AND', 'OR', 'NOT', 'RULE-SET', 'MATCH'
])
const COMMA_PAYLOAD_TYPES = new Set(['AND', 'OR', 'NOT', 'SUB-RULE', 'DOMAIN-REGEX', 'PROCESS-NAME-REGEX', 'PROCESS-PATH-REGEX'])
const DOMAIN = /^(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)*[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$/
const IPV4 = /^(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)$/

const isIpv6 = (value: string) => {
  if (!value.includes(':') || !/^[0-9a-fA-F:.]+$/.test(value)) return false
  try {
    return new URL(`http://[${value}]/`).hostname.length > 2
  } catch {
    return false
  }
}
const isIp = (value: string) => IPV4.test(value) || isIpv6(value)

function isNetwork(value: string) {
  const [address, prefix, ...rest] = value.split('/')
  if (rest.length || !address || prefix === undefined) return false
  if (!/^(?:0|[1-9]\d{0,2})$/.test(prefix)) return false
  const bits = Number(prefix)
  if (IPV4.test(address)) return bits <= 32
  return isIpv6(address) && bits <= 128
}

/** Check obvious input mistakes locally; the backend also validates advanced core syntax. */
export function ruleValueError(kind: string, value: string): string {
  if (!kind) return '无前缀的行需要选择规则类型'
  if (!SUPPORTED_RULE_TYPES.has(kind)) return `不支持的规则类型 ${kind}`
  if (/[\r\n]/.test(value)) return '规则值必须是单行文本'
  if (!COMMA_PAYLOAD_TYPES.has(kind) && value.includes(',')) return '此规则类型的值不能包含逗号；请勿在值中填写策略或附加参数'
  if (kind === 'MATCH') return value ? 'MATCH 不接受规则值' : ''
  if (!value) return '规则值为空'
  if (kind === 'DOMAIN' || kind === 'DOMAIN-SUFFIX') {
    let domain = value
    // URL's hostname parser supplies IDNA conversion, but must not accept URL syntax or IPs as domains.
    if (/[^\x00-\x7f]/.test(value) && !/[\s/:?#@]/.test(value)) {
      try { domain = new URL(`http://${value}`).hostname } catch { return '不是有效域名' }
    }
    if (isIp(value) || /^[\d.]+$/.test(value) || /(?:^|\.)\d+$/.test(domain) || domain.length > 253 || !DOMAIN.test(domain)) return '不是有效域名'
  }
  if (['IP-CIDR', 'IP-CIDR6', 'SRC-IP-CIDR', 'IP-SUFFIX', 'SRC-IP-SUFFIX'].includes(kind) && !isNetwork(value)) return value.includes('/') ? '不是有效 IP 网段' : '不是有效 IP 网段（需要 CIDR 前缀长度）'
  const maximum = ['SRC-PORT', 'DST-PORT', 'IN-PORT'].includes(kind) ? 65535 : kind === 'DSCP' ? 63 : kind === 'UID' ? 4294967295 : null
  if (maximum !== null && !(kind === 'DSCP' && value === '*')) {
    const ranges = value.split(/[\/,]/)
    if (ranges.length > 28 || ranges.some(range => {
      const match = /^(\d+)(?:-(\d+))?$/.exec(range)
      return !match || Number(match[1]) > Number(match[2] ?? match[1]) || Number(match[2] ?? match[1]) > maximum
    })) return `请填写 0–${maximum} 内的数字或范围，以 / 分隔`
  }
  if (['IP-ASN', 'SRC-IP-ASN'].includes(kind) && !/^\d+$/.test(value)) return 'ASN 应为非负整数'
  if (kind === 'NETWORK' && !['tcp', 'udp'].includes(value.toLowerCase())) return 'NETWORK 只接受 tcp 或 udp'
  return ''
}

/**
 * 按行解析粘贴的规则：忽略空行和 # / // 注释；`TYPE,value` 保留行尾 no-resolve、忽略策略等其他字段；
 * `+.x`、`*.x` 为 DOMAIN-SUFFIX，`full:x` 为 DOMAIN，其余行按 defaultType。重复行只添加一次。
 */
export function parsePastedRules(text: string, defaultType: string): PasteResult {
  const lines = text.split(/\r?\n/).map((raw, index) => ({ line: index + 1, text: raw.trim() }))
    .filter(entry => entry.text && !entry.text.startsWith('#') && !entry.text.startsWith('//'))
  const result: PasteResult = { rows: [], invalid: [], repeated: [], ignored: [], notes: [], count: lines.length, overLimit: lines.length > PASTE_LIMIT }
  const seen = new Set<string>()
  for (const { line, text: entry } of lines) {
    let kind = defaultType
    let value = entry
    let noResolve = false
    if (entry.includes(',')) {
      const fields = entry.split(',').map(field => field.trim())
      kind = fields[0]!.toUpperCase()
      value = kind === 'MATCH' ? '' : COMMA_PAYLOAD_TYPES.has(kind) ? entry.slice(entry.indexOf(',') + 1).trim() : fields[1] || ''
      for (const extra of COMMA_PAYLOAD_TYPES.has(kind) ? [] : fields.slice(kind === 'MATCH' ? 1 : 2)) {
        if (extra.toLowerCase() === 'no-resolve') noResolve = true
        else if (extra) result.ignored.push({ line, value: extra })
      }
    } else if (entry.startsWith('+.')) {
      kind = 'DOMAIN-SUFFIX'
      value = entry.slice(2).trim()
    } else if (entry.startsWith('*.')) {
      kind = 'DOMAIN-SUFFIX'
      value = entry.slice(2).trim()
      result.notes.push(`${entry} 将按 DOMAIN-SUFFIX 添加，也包含 ${value} 本身`)
    } else if (entry.toLowerCase().startsWith('full:')) {
      kind = 'DOMAIN'
      value = entry.slice(5).trim()
    }
    const message = ruleValueError(kind, value)
    if (message) {
      result.invalid.push({ line, text: entry, message })
      continue
    }
    const key = `${kind}\n${value}`
    if (seen.has(key)) {
      result.repeated.push(line)
      continue
    }
    seen.add(key)
    result.rows.push({ line, rule_type: kind, value, no_resolve: noResolve })
  }
  return result
}

/** 按用途拆分：DNS 可用的域名行、连接 IP 行，以及其他连接规则 */
export function splitPaste(rows: PasteRow[]) {
  return {
    domain: rows.filter(row => isOxidnsRuleType(row.rule_type)),
    ip: rows.filter(row => IP_DNS_RULE_TYPES.includes(row.rule_type)),
    unsupported: rows.filter(row => !isDnsRuleType(row.rule_type))
  }
}

/** 粘贴改存为规则集：全为 DOMAIN / DOMAIN-SUFFIX 时为 domain 格式，否则为 classical */
export function pasteToRuleset(rows: PasteRow[]): { behavior: 'domain' | 'classical'; content: string } {
  if (rows.length && rows.every(row => row.rule_type === 'DOMAIN' || row.rule_type === 'DOMAIN-SUFFIX')) {
    return { behavior: 'domain', content: rows.map(row => (row.rule_type === 'DOMAIN-SUFFIX' ? `+.${row.value}` : row.value)).join('\n') }
  }
  return { behavior: 'classical', content: rows.map(row => `${row.rule_type},${row.value}${row.no_resolve ? ',no-resolve' : ''}`).join('\n') }
}

export type DnsWarningKind = 'locked' | 'unloaded' | 'mosdns' | 'policy'
export interface DnsWarning {
  kind: DnsWarningKind
  tone: 'warning' | 'info'
  text: string
  action?: 'open-config' | 'follow' | 'resync'
  actionLabel?: string
}
const WARNING_ORDER: DnsWarningKind[] = ['locked', 'unloaded', 'mosdns', 'policy']

export interface DnsWarningContext {
  engine?: RuleDnsEngine | null
  /** DNS 状态未加载成功 */
  unloaded?: boolean
  value: RuleDnsValue
  policy: string
  /** 当前策略对应的跟随路由 */
  policyRoute: 'direct' | 'proxy' | null
  /** 绑定了 MosDNS 时，这条规则在 MosDNS 中的路由 */
  mosdns?: { route: 'direct' | 'proxy' | null } | null
  /** 编辑时的已有偏离：跟随策略但实际路由不同 */
  drift?: { route: RuleDnsRoute | null; label?: string } | null
  /** 已选择「按策略更新 DNS」 */
  resync?: boolean
  /** 显式值是否需要提示「不随策略变化」 */
  explainExplicit?: boolean
  upstreamLabel?: string
}

/** 字段下方警告槽的内容，按优先级排序：锁定、未加载、MosDNS、策略说明 */
export function dnsWarnings(context: DnsWarningContext): DnsWarning[] {
  const warnings: DnsWarning[] = []
  const { engine, value, policyRoute } = context
  if (engine && !engine.editable) {
    warnings.push({ kind: 'locked', tone: 'warning', text: `DNS 处理需在完整参数修改：${engine.reason || 'OxiDNS 分流含自定义结构'}`,
      action: 'open-config', actionLabel: '打开完整参数' })
  }
  if (context.unloaded) warnings.push({ kind: 'unloaded', tone: 'warning', text: 'DNS 状态未加载，本次不修改 DNS' })
  const route = value === 'follow' ? policyRoute : value === 'default' ? null : value
  if (!context.unloaded && engine?.editable !== false) {
    // MosDNS 只有直连 / 代理两种分流；拦截和指定上游在 MosDNS 中没有对应行为。
    const comparable = route === 'direct' || route === 'proxy' || route === null ? route : 'other'
    if (context.mosdns && context.mosdns.route !== comparable) {
      const mosdns = context.mosdns.route ? ROUTE_LABELS[context.mosdns.route] : '默认上游'
      warnings.push({ kind: 'mosdns', tone: 'info',
        text: `MosDNS 仍按${mosdns}处理此规则，与这里的「${routeLabel(route, context.upstreamLabel)}」不同；本字段只对 OxiDNS 生效。` })
    }
    if (context.drift && value === 'follow' && !context.resync) {
      warnings.push({ kind: 'policy', tone: 'warning',
        text: `策略已是 ${context.policy}，DNS 仍为${routeLabel(context.drift.route, context.drift.label)}（与策略不一致）`,
        action: 'resync', actionLabel: '按策略更新 DNS' })
    } else if (context.drift && value === 'follow' && context.resync) {
      warnings.push({ kind: 'policy', tone: 'info', text: `保存后 DNS 按策略更新为${routeLabel(policyRoute)}` })
    }
    if (value === 'follow' && !policyRoute && context.policy && NON_PROXY_POLICIES.includes(context.policy)) {
      warnings.push({ kind: 'policy', tone: 'info', text: '需要同时拦截解析请选「拒绝」或「黑洞」' })
    } else if (value !== 'follow' && context.explainExplicit && route !== policyRoute) {
      warnings.push({ kind: 'policy', tone: 'info', text: `DNS 为单独设置的${routeLabel(route, context.upstreamLabel)}，不随策略变化`,
        action: 'follow', actionLabel: '改为跟随策略' })
    }
    for (const text of engine?.warnings || []) warnings.push({ kind: 'policy', tone: 'warning', text })
  }
  return warnings.sort((a, b) => WARNING_ORDER.indexOf(a.kind) - WARNING_ORDER.indexOf(b.kind))
}

/**
 * 保存后的发布说明：开启了对应范围规则自动同步的设备会自动更新，其余需发布。
 * pinned 表示服务端确认有指定上游被自动同步暂扣（不在设备已发布的上游列表中），
 * 只有 OxiDNS 会暂扣，此时需要手动发布。
 */
export function publishNote(agents: RuleDnsAgent[] | undefined, scopes: Array<'rules' | 'library'>, pinned = false) {
  const list = agents || []
  const automatic = list.filter(agent => scopes.some(scope => agent.auto_sync?.[scope]))
  const manual = list.length - automatic.length
  const withheld = pinned && automatic.some(agent => agent.service_type === 'oxidns')
  const parts: string[] = []
  if (automatic.length) parts.push(`${automatic.length} 台已开启规则自动同步，将自动更新`)
  if (manual) parts.push(`${manual} 台需发布后生效`)
  if (!list.length) parts.push('发布后生效')
  if (withheld) parts.push('指定上游需手动发布')
  return { text: parts.join('；'), publish: manual > 0 || !list.length || withheld }
}

/**
 * 保存响应中「指定上游被自动同步暂扣」的标记（pinned_withheld）。
 * 服务端未返回该字段时退回旧判断：本次选择了指定上游即提示手动发布。
 */
export const pinnedWithheld = (data: { pinned_withheld?: unknown } | undefined | null, fallback: boolean) =>
  typeof data?.pinned_withheld === 'boolean' ? data.pinned_withheld : fallback

/**
 * 规则或 DNS 保存后的统一通知（§6.4.8）：发布说明作为描述，需要手动发布时附「去发布」。
 * extra 为服务端警告等需要一起显示的内容；warn 时用警告色。
 */
export function notifyRuleSaved(title: string, options: {
  agents?: RuleDnsAgent[]; scopes: Array<'rules' | 'library'>; pinned?: boolean; extra?: string[]; warn?: boolean
}) {
  const note = publishNote(options.agents, options.scopes, options.pinned)
  const description = [...(options.extra || []), note.text].filter(Boolean).join('\n')
  const action = note.publish
    ? { label: '去发布', onClick: () => { void import('@/router').then(({ default: router }) => router.push('/generate')) } }
    : undefined
  return (options.warn || options.extra?.length ? notify.warning : notify.success)(title, description, action)
}

type DnsErrorBody = { message?: string; usages?: Array<{ code?: string; reason?: string }> }

/** 保存失败的可读原因；native 分流锁定时为识别器给出的具体原因 */
export const dnsErrorReason = (error: unknown) => (isAxiosError<DnsErrorBody>(error)
  ? error.response?.data?.usages?.[0]?.reason || error.response?.data?.message || ''
  : error instanceof Error ? error.message : '')

/** 409 oxidns_routing_custom：错误码在 usages[0].code，不靠 HTTP 状态区分 */
export const isRoutingLocked = (error: unknown) =>
  isAxiosError<DnsErrorBody>(error) && error.response?.data?.usages?.[0]?.code === 'oxidns_routing_custom'

/**
 * native 分流在保存时变为自定义结构（竞态，锁定状态平时已提前禁用字段）：
 * 可改用默认上游保存，或复制原因。返回 true 表示改用默认上游重发。
 */
export async function confirmSaveWithoutDns(error: unknown): Promise<boolean> {
  const reason = dnsErrorReason(error) || 'OxiDNS 分流含自定义结构'
  const choice = await choose(reason, { title: 'DNS 处理需在完整参数修改', confirmText: '仍保存（DNS 默认上游）', altText: '复制原因' })
  if (choice === 'alt') {
    try {
      await copyText(reason)
      notify.success('已复制原因')
    } catch {
      notify.error('复制失败，请手动选择文字')
    }
  }
  return choice === 'confirm'
}

/** 列表中只读的上游选项：当前固定但已不在上游列表中的地址 */
export function upstreamOptions(upstreams: RuleDnsUpstream[] | undefined, current?: { value: RuleDnsValue; label?: string }) {
  const options = [...(upstreams || [])].map(item => ({ value: `upstream:${item.key}` as RuleDnsValue, label: item.label, linked: item.linked, stale: false }))
  if (current && isUpstreamValue(current.value) && !options.some(option => option.value === current.value)) {
    const label = current.label ? upstreamAddress(current.label) : current.value.slice(9)
    options.push({ value: current.value, label: `${label}（已不在上游列表）`, linked: false, stale: true })
  }
  return options
}

/**
 * 规则集引用只提交配置空间自己的字段，共享来源由规则库维护。
 * DNS 状态单独存放，绝不合并进规则对象（启停时会整对象提交）。
 */
export const ruleSetPayload = (item: Record<string, any>) => ({
  id: item.id,
  itemType: 'ruleset',
  library_rule_id: item.library_rule_id,
  policy: item.policy,
  enabled: item.enabled,
  order: item.order,
  remark: item.remark,
  no_resolve: item.no_resolve,
  group_name: item.group_name,
  formats: item.formats,
  mosdns: item.mosdns
})

/** 单条规则的可编辑字段；列表展示用的 uniqueId、分组标记等不提交 */
const DISPLAY_FIELDS = ['uniqueId', 'isGroup', 'isExpandedGroupItem', 'isFirstInGroup', 'isLastInGroup', 'groupId', 'groupPolicy', 'groupLabel', 'dns']
export const rulePayload = (item: Record<string, any>) =>
  Object.fromEntries(Object.entries(item).filter(([key]) => !DISPLAY_FIELDS.includes(key)))
