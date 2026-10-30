import type { IPv6ReadinessCheck, IPv6Verification, MihomoIPv6Readiness } from '@/types/mihomoIPv6'

type SummaryTone = 'success' | 'warning' | 'danger' | 'progress' | 'neutral'
type GroupId = 'ipv6-network' | 'restart' | 'dns-router' | 'node' | 'client'

export interface IPv6SummaryCheck extends IPv6ReadinessCheck {
  displayLabel?: string
  displayStatus?: string
  displayTone?: SummaryTone
}

export interface IPv6SummaryGroup {
  id: GroupId
  label: string
  status: string
  tone: SummaryTone
  issues: string[]
  checks: IPv6SummaryCheck[]
  note?: string
}

interface SummaryInput {
  readiness?: MihomoIPv6Readiness | null
  verification?: IPv6Verification | null
  preferVerificationSnapshot?: boolean
}

const groupDefinitions: Array<{ id: GroupId; label: string; note: string }> = [
  { id: 'ipv6-network', label: 'IPv6 网络', note: '检查 Mihomo 的 IPv6 设置、地址和默认路由；实际节点连接另行验证。' },
  { id: 'restart', label: '重启恢复', note: '只核对开机恢复配置，未实际重启设备。自动获得、带有效期的默认路由是正常的。' },
  { id: 'dns-router', label: 'DNS 与 ROS 配套', note: '核对 DNS、路由及防火墙等配置。规则检查不能代替实际连接验证。' },
  { id: 'node', label: '节点实际连接', note: '验证 Mihomo 能否通过实际代理节点访问检测网站，并确认 IPv6 连接证据。' },
  { id: 'client', label: '客户端分流', note: '使用独立检测客户端验证 DNS 和经过 ROS、Mihomo 的实际请求。' },
]

const invalidResultIds = new Set(['result_stale', 'target_changed', 'interrupted', 'verification_failed', 'runtime_config_stability'])
const rank = { error: 0, warning: 1, unknown: 2, ok: 3 }

function groupFor(check: IPv6ReadinessCheck): GroupId {
  const id = check.id
  if (id.startsWith('router_') || id === 'router') return 'dns-router'
  if (id === 'host_persistence' || id === 'host_tun_recovery' || id === 'prepare_tun_recovery' || id === 'persistence') return 'restart'
  if (id.startsWith('client_') || id === 'client') return 'client'
  if (id.startsWith('runtime_dns_')) return 'dns-router'
  if (id.startsWith('host_') || ['configuration', 'deployed_config', 'gateway_role', 'ra', 'runtime_deployed_config', 'runtime_runtime_core', 'runtime_runtime_ipv6', 'runtime_runtime_tun'].includes(id)) return 'ipv6-network'
  // Keep unfamiliar diagnostics visible and retain their severity. Do not silently
  // lose new backend checks when the backend and frontend are updated separately.
  return 'node'
}

function decorate(check: IPv6SummaryCheck): IPv6SummaryCheck {
  if (check.status === 'ok' && check.id.endsWith('_fake_ipv6') && check.message === '当前保存配置未启用 IPv6 Fake-IP，本次不新增相关路由') {
    return { ...check, displayStatus: '无需检查', displayTone: 'neutral' }
  }
  return check
}

function issueFor(check: IPv6SummaryCheck): string {
  const id = check.id
  if (id === 'runtime_deployed_config') return '尚未确认 Mihomo 使用的是当前保存的配置，请发布后再验证。'
  if (id === 'runtime_expected_config') return '尚未找到这版配置的发布记录，请发布后再验证。'
  if (id === 'host_default_route') return '尚未确认 Mihomo 的 IPv6 出口，请检查上级路由。'
  if (id === 'host_address') return '尚未确认可用的 IPv6 地址，请检查设备的网络设置。'
  if (id === 'host_forwarding_ra') return '转发和自动获取网关的设置需要核对，请展开查看。'
  if (id === 'host_persistence' || id === 'host_tun_recovery' || id === 'prepare_tun_recovery') return '开机恢复配置尚未确认齐全，请展开查看缺项或冲突。'
  if (id.startsWith('router_')) {
    if (id.endsWith('_wan')) return '尚未确认路由器的 IPv6 外网出口，请检查上网设置。'
    if (id.endsWith('_dns')) return '需要核对路由器和客户端是否使用预期的 DNS。'
    if (id.endsWith('_ra')) return '尚未确认路由器向内网提供 IPv6 网关信息，请核对 LAN 设置。'
    if (/_read_/.test(id)) return '部分路由器配置读取失败，请检查连接和账号权限。'
    if (/(?:_firewall|_nat|_node_(?:\d|policy)|_fake[46]_)/.test(id)) {
      if (check.status === 'error') return '已检查的路径中发现阻断规则，请展开核对对应路径。'
      if (check.status === 'unknown') return '部分防火墙、NAT 或回程路径尚未确认，可运行连接验证并查看规则详情。'
    }
    if (/_input$/.test(id) && check.status === 'unknown') return '部分 IPv6 基础通信规则尚未确认，请展开核对。'
  }
  return check.message || `${check.displayLabel || check.label}尚未确认，请展开查看。`
}

function reportChecks(readiness: MihomoIPv6Readiness | null | undefined, verification: IPv6Verification | null | undefined, preferVerificationSnapshot = false): IPv6SummaryCheck[] {
  const checks = new Map<string, IPv6SummaryCheck>()
  const mismatched = readiness?.config_revision !== undefined && verification && readiness.config_revision !== verification.config_revision
  const invalid = verification?.checks.filter(check => invalidResultIds.has(check.id) && check.status !== 'ok') || []
  const stale = verification && 'stale' in verification && verification.stale === true
  if (verification) {
    if (mismatched || stale || invalid.length) {
      // Once a run is invalidated, its successful socket/HTTP observations must not
      // appear as current evidence in any group. Keep the invalidation reason.
      const reasons = invalid.length ? invalid : [{ id: 'result_stale', label: '验收结果需更新', status: 'unknown' as const,
        message: '当前配置或设备已变化，请重新验证连接与分流。' }]
      for (const check of reasons) checks.set(check.id, check)
      // An interrupted/failed current run may already contain a useful failure.
      // Keep it visible while discarding the earlier passing runtime observations.
      if (!mismatched && !stale && !invalid.some(check => ['result_stale', 'target_changed'].includes(check.id))) {
        for (const check of verification.checks.filter(check => check.status !== 'ok')) checks.set(check.id, decorate(check))
      }
    } else {
      for (const check of verification.checks) checks.set(check.id, decorate(check))
    }
    if (verification.status === 'error' && ![...checks.values()].some(check => check.status === 'error') && !mismatched && !stale) {
      for (const [id, check] of checks) if ((id.startsWith('runtime_') || id.startsWith('client_')) && check.status === 'ok') checks.delete(id)
      checks.set('verification_failed', { id: 'verification_failed', label: '验收未完成', status: 'error', message: verification.message || '验收未完成，请重新验证。' })
    }
    if (verification.status === 'warning' && ![...checks.values()].some(check => check.status !== 'ok' && check.id !== 'client_not_configured')) {
      checks.set('verification_incomplete', { id: 'verification_incomplete', label: '验收结论', status: 'unknown', message: '验收尚未全部通过，但未返回具体问题，请重新验证。' })
    }
  }
  const useVerificationSnapshot = preferVerificationSnapshot && verification && verification.status !== 'running'
    && !mismatched && !stale && !invalid.some(check => ['result_stale', 'target_changed'].includes(check.id))
  if (readiness && !useVerificationSnapshot) {
    // A fresh inspection replaces the complete static snapshot, including routers
    // that have since been removed; it never borrows their old passing checks.
    for (const [id] of checks) {
      if (id.startsWith('host_') || id.startsWith('router_') || ['configuration', 'deployed_config', 'router', 'gateway_role', 'prepare_tun_recovery'].includes(id)) checks.delete(id)
    }
    for (const check of readiness.checks) checks.set(check.id, decorate(check))
    for (const router of readiness.routers) {
      for (const check of router.checks) {
        const row = decorate({ ...check, id: `router_${router.id}_${check.id}`, displayLabel: `${router.name} · ${check.label}` })
        checks.set(row.id, row)
      }
    }
    if (readiness.connectivity.status !== 'not_checked') {
      checks.set('node_port_probe', { id: 'node_port_probe', label: '节点端口测试', ...readiness.connectivity, status: readiness.connectivity.status })
    }
  }
  return [...checks.values()]
}

export function buildMihomoIPv6Summary({ readiness, verification, preferVerificationSnapshot }: SummaryInput): IPv6SummaryGroup[] {
  const checks = reportChecks(readiness, verification, preferVerificationSnapshot)
  const invalidated = checks.some(check => invalidResultIds.has(check.id) && check.status !== 'ok')
  return groupDefinitions.map(definition => {
    const rows = checks.filter(check => groupFor(check) === definition.id)
    const group: IPv6SummaryGroup = { ...definition, status: '待检查', tone: 'neutral', issues: [], checks: rows }
    const notConfigured = definition.id === 'client' && rows.some(check => check.id === 'client_not_configured' && check.status === 'unknown')
    const relevant = rows.filter(check => !(check.id === 'client_not_configured' && check.status === 'unknown') && check.displayStatus !== '无需检查')
    const problems = relevant.filter(check => check.status !== 'ok').sort((left, right) => rank[left.status] - rank[right.status])
    group.issues = [...new Set(problems.map(issueFor))]

    if (problems.some(check => check.status === 'error')) {
      group.status = '有项目未通过'; group.tone = 'danger'
    } else if (problems.some(check => check.status === 'warning')) {
      group.status = '需留意'; group.tone = 'warning'
    } else if (problems.length) {
      group.status = '尚未全部验证'; group.tone = 'neutral'
    } else if (notConfigured && !relevant.length) {
      group.status = '未配置检测客户端'
      group.note = '未配置检测客户端，本次只检查 Mihomo 主机。'
    } else if (notConfigured) {
      group.status = '尚未全部验证'
      group.issues = ['检测客户端的设置与验收结果不一致，请重新验证。']
    } else if (definition.id === 'node' || definition.id === 'client') {
      if (verification?.status === 'running' && !invalidated && (definition.id === 'node' || verification.scope === 'client_path')) {
        group.status = '验证中'; group.tone = 'progress'
      } else if (!relevant.length) {
        group.status = '待验证'
      } else {
        const required = definition.id === 'node' ? ['runtime_proxy_http', 'runtime_node_ipv6_transport']
          : ['client_http_ipv4', 'client_mihomo_path', 'client_ros_path', 'client_ipv6_transport']
        if (required.every(id => relevant.some(check => check.id === id && check.status === 'ok'))) {
          group.status = '已通过'; group.tone = 'success'
        } else {
          group.status = '尚未全部验证'
          group.issues = [definition.id === 'node' ? '尚未取得完整的节点访问与 IPv6 连接证据，请运行连接验证。'
            : '尚未取得客户端整条链路的完整证据，请重新验证。']
        }
      }
    } else if (relevant.length) {
      group.status = definition.id === 'restart' ? '恢复配置已就绪' : '已通过'
      group.tone = 'success'
    } else if (rows.length && rows.every(check => check.displayStatus === '无需检查')) {
      group.status = '无需检查'
    }
    if ((definition.id === 'node' || definition.id === 'client') && relevant.length && !notConfigured && !invalidated && verification?.status !== 'running' && verification?.checked_at) {
      const checkedAt = new Date(verification.checked_at)
      if (!Number.isNaN(checkedAt.getTime())) group.note += ` 上次验证：${checkedAt.toLocaleString('zh-CN')}。`
    }
    return group
  })
}
