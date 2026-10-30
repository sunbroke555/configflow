import type { IPv6ReadinessCheck, MihomoIPv6Readiness } from '@/types/mihomoIPv6'
import type { MihomoIPv6Mode } from './mihomoIPv6'
import { issueFor, type IPv6SummaryGroup } from './mihomoIPv6Summary'

/**
 * The checklist under the IPv6 mode. Every step is derived from data that the
 * existing checks already return (device readiness, the stored verification,
 * the router difference report and the upstream pending reminder); nothing
 * here probes a device or writes to one.
 */
export type IPv6StepTone = 'success' | 'warning' | 'danger' | 'progress' | 'neutral'
export type IPv6StepId = 'host' | 'publish' | 'upstream' | 'firewall' | 'oxidns' | 'verify'
export interface IPv6Step {
  id: IPv6StepId
  label: string
  status: string
  tone: IPv6StepTone
  /** Direct reasons, shown in the body text color. */
  issues: string[]
  /** The next action when there is no problem to explain. */
  note?: string
  /** Read-only commands from the backend, never assembled here. */
  commands?: Array<{ label: string; command: string }>
}

export type UpstreamPendingState = 'none' | 'synced' | 'pending' | 'unknown'
export interface UpstreamPendingStatus { state: UpstreamPendingState; pending: number }

export interface IPv6StepContext {
  /** The draft differs from the saved configuration every check reads. */
  dirty: boolean
  /** Readiness for the saved configuration, or why there is none. */
  readiness: MihomoIPv6Readiness | null
  checking: boolean
}

interface RoutePlanReport { enabled?: boolean; status?: string; blockers?: string[]; warnings?: string[]; fix_command?: string }
export interface RouterReportRead {
  id: string
  name: string
  error?: string
  report?: { ipv6_fakeip?: RoutePlanReport; ipv6_routes?: RoutePlanReport }
}
export interface OxiDNSLink { router: string; agent: string }

export const IPV6_STEP_LABELS: Record<IPv6StepId, string> = {
  host: 'Mihomo 主机准备',
  publish: '保存并发布 Mihomo',
  upstream: '上级路由发布',
  firewall: '路由器防火墙 / NAT',
  oxidns: 'OxiDNS 返回 IPv6 地址',
  verify: '验收',
}

const step = (id: IPv6StepId, status: string, tone: IPv6StepTone, issues: string[] = [], note?: string): IPv6Step =>
  ({ id, label: IPV6_STEP_LABELS[id], status, tone, issues, ...(note ? { note } : {}) })
const unique = (values: string[]) => [...new Set(values)]
const problem = (check: IPv6ReadinessCheck) => check.status === 'error' || check.status === 'warning'

/** A step that reads the saved configuration has nothing to show yet. */
function waiting(id: IPv6StepId, context: IPv6StepContext): IPv6Step {
  return context.checking ? step(id, '检查中', 'progress') : step(id, context.dirty ? '保存后检查' : '待检查', 'neutral')
}

export function hostStep(context: IPv6StepContext): IPv6Step {
  const readiness = context.readiness
  if (!readiness) return waiting('host', context)
  const problems = readiness.checks.filter(check => (check.id.startsWith('host_') || check.id === 'prepare_tun_recovery') && problem(check))
  const issues = unique(problems.map(check => issueFor(check, readiness.can_prepare)))
  if (problems.some(check => check.status === 'error')) return step('host', '未通过', 'danger', issues)
  if (problems.length) return step('host', '需处理', 'warning', issues)
  return step('host', '已就绪', 'success')
}

export function publishStep(context: IPv6StepContext): IPv6Step {
  if (context.dirty) return step('publish', '待保存', 'neutral', [], '点击底部「保存并发布」，把当前模式应用到设备。')
  const readiness = context.readiness
  if (!readiness) return waiting('publish', context)
  const saved = readiness.checks.find(check => check.id === 'configuration' && check.status === 'error')
  if (saved) return step('publish', '需处理', 'warning', [saved.message])
  const deployed = readiness.checks.find(check => check.id === 'deployed_config' && check.status !== 'ok')
  if (deployed) return step('publish', '待发布', 'neutral', [deployed.message])
  const core = readiness.host?.core
  if (!core || typeof core !== 'object' || 'error' in core) {
    return step('publish', '无法确认', 'neutral', [], '设备没有返回当前 Mihomo 配置，发布后重新检查；仍无法确认时请更新 Agent。')
  }
  return step('publish', '已发布', 'success')
}

export function upstreamStep(status: UpstreamPendingStatus | null, dirty: boolean): IPv6Step {
  if (dirty) return step('upstream', '保存后核对', 'neutral', [], '上级路由按已保存的配置核对，请先保存草稿。')
  if (!status) return step('upstream', '检查中', 'progress')
  if (status.state === 'none') {
    return step('upstream', '未关联上级路由', 'warning', ['没有路由器把代理网关设为这台 Mihomo；客户端的 IPv6 流量需要上级路由转给它。'])
  }
  if (status.state === 'synced') return step('upstream', '已同步', 'success')
  if (status.state === 'pending') return step('upstream', status.pending > 0 ? `${status.pending} 项待发布` : '待发布', 'neutral')
  return step('upstream', '无法确认', 'neutral')
}

type PolicyCheck = IPv6ReadinessCheck & { decision?: string }

export function firewallStep(context: IPv6StepContext, reports: RouterReportRead[], loadingReports: boolean): IPv6Step {
  const readiness = context.readiness
  // Router reports follow the saved Mihomo configuration, not the draft.
  if (!readiness || context.dirty) return waiting('firewall', context)
  if (!readiness.routers.length) return step('firewall', '未关联上级路由', 'neutral')
  const issues: string[] = []
  const unknowns: string[] = []
  const commands: Array<{ label: string; command: string }> = []
  for (const router of readiness.routers) {
    const name = `「${router.name}」`
    const checks = router.checks as PolicyCheck[]
    const manual = checks.find(check => check.id === 'manual')
    if (manual) {
      unknowns.push(`${name}：${manual.message}。`)
      continue
    }
    // #444: the client → Fake-IP flow through the router's own LAN port.
    const flows = checks.filter(check => /^fake6_\d+$/.test(check.id))
    if (flows.some(check => check.decision === 'translate')) {
      issues.push(`${name}的 IPv6 NAT 会改写客户端发往 Mihomo 的连接。请给 masquerade 限定出口接口，不要命中 LAN 进 LAN 出的流量。`)
    }
    if (flows.some(check => check.decision === 'drop')) {
      issues.push(`${name}的 IPv6 防火墙会阻断客户端发往 Mihomo 的连接，请检查 forward 链的放行顺序。`)
    }
    // #452: the same-port forward hint of the router difference report.
    const read = reports.find(item => item.id === router.id)
    if (!read) continue
    if (read.error) {
      unknowns.push(`${name}的差异报告读取失败：${read.error}`)
      continue
    }
    for (const plan of [read.report?.ipv6_fakeip, read.report?.ipv6_routes]) {
      if (!plan) continue
      const warnings = (plan.warnings || []).filter(text => text.includes('防火墙'))
      if (plan.fix_command) {
        if (commands.some(item => item.command === plan.fix_command)) continue
        issues.push(...warnings.map(text => `${name}：${text}`))
        commands.push({ label: `在 RouterOS 终端执行（${router.name}）`, command: plan.fix_command })
      } else {
        unknowns.push(...warnings.map(text => `${name}：${text}`))
      }
    }
    const fakeIP = read.report?.ipv6_fakeip
    if (fakeIP?.enabled && fakeIP.blockers?.length && !fakeIP.warnings?.length) {
      unknowns.push(`${name}的 IPv6 Fake-IP 路由暂时不能发布，处理「上级路由发布」中的问题后才能确认防火墙。`)
    }
  }
  if (issues.length || commands.length) {
    return { ...step('firewall', '需处理', 'warning', unique([...issues, ...unknowns]), 'ConfigFlow 不修改防火墙和 NAT，执行后点击「重新检查」。'), commands }
  }
  if (unknowns.length) return step('firewall', '无法确认', 'neutral', unique(unknowns))
  if (loadingReports) return step('firewall', '检查中', 'progress')
  return step('firewall', '无需处理', 'success')
}

export function oxidnsStep(links: OxiDNSLink[]): IPv6Step | null {
  if (!links.length) return null
  const issues = links.map(link => `「${link.router}」的客户端 DNS 使用 OxiDNS「${link.agent}」。`)
  return step('oxidns', '请核对', 'neutral', unique(issues),
    '请确认 OxiDNS 没有开启「优先 IPv4」或屏蔽 AAAA（类型 28），否则客户端拿不到 IPv6 地址。ConfigFlow 不会修改这些设置。')
}

export function verifyStep(context: IPv6StepContext, groups: IPv6SummaryGroup[] | null, running = false): IPv6Step {
  if (!context.readiness || !groups || context.dirty) return waiting('verify', context)
  if (running) return step('verify', '验证中', 'progress')
  if (groups.every(group => group.tone === 'success')) return step('verify', '已通过', 'success')
  if (groups.some(group => group.tone === 'danger')) return step('verify', '未通过', 'danger')
  if (groups.some(group => group.tone === 'warning')) return step('verify', '需处理', 'warning')
  if (groups.some(group => group.tone === 'progress')) return step('verify', '验证中', 'progress')
  return step('verify', '待验证', 'neutral')
}

/**
 * Steps of a mode, in order. In `nodes` the upstream step appears only while
 * the routers have work for this Mihomo (IPv6 rule-set routes, or Fake-IP
 * routes still to withdraw); it stays rendered so it can report that.
 */
export function stepOrder(mode: Exclude<MihomoIPv6Mode, 'off'>, hasOxiDNS: boolean): IPv6StepId[] {
  if (mode === 'nodes') return ['host', 'publish', 'upstream', 'verify']
  return ['host', 'publish', 'upstream', 'firewall', ...(hasOxiDNS ? ['oxidns' as const] : []), 'verify']
}

export function stepVisible(mode: Exclude<MihomoIPv6Mode, 'off'>, id: IPv6StepId, upstream: UpstreamPendingStatus | null): boolean {
  return mode === 'clients' || id !== 'upstream' || upstream?.state === 'pending'
}
