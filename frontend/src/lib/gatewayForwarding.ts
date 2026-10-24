/**
 * 代理网关的 IPv4 转发：路由器把要分流的流量转给这台 Mihomo 主机，它需要开启 IPv4 转发。
 * 状态与「开启并持久化」在 Agent 页的设备卡片上（/api/agents/gateway-forwarding）；
 * 上游路由页只在转发不可用时提示（/api/routers/gateway-forwarding）。
 */
import type { ForwardFilter } from '@/lib/forwardFilter'
import { forwardFilterWarning } from '@/lib/forwardFilter'

/** 单独安装的 Mihomo Agent 从此版本起支持在面板开启并持久化 IPv4 转发（同机安装为 1.8.40-go）。 */
export const STANDALONE_FORWARDING_AGENT_VERSION = '1.8.41-go'
export const FORWARDING_CONFIG_FILE = '/etc/sysctl.d/99-configflow-aio-forward.conf'

export type GatewayForwardingState =
  | 'persisted' | 'needs_persist' | 'blocked' | 'unsupported' | 'upgrade_required'
  | 'offline' | 'docker' | 'not_mihomo' | 'missing' | 'error'

/** 把流量转给这台 Agent 的路由器；Agent 页的读取跨配置空间，会带上空间信息。 */
export interface GatewayForwardingRouter {
  id: string
  name: string
  profile_id?: string
  profile_name?: string
}

/** 与后端 linux_gateway.gateway_forwarding_entry 的返回结构一一对应。 */
export interface GatewayForwardingEntry {
  agent_id: string
  agent_name: string
  agent_version: string
  routers: GatewayForwardingRouter[]
  state: GatewayForwardingState
  can_persist: boolean
  message: string
  minimum_version: string | null
  current: string | null
  persisted: boolean | null
  config: string | null
  hook_script: string | null
  startup_hook: string | null
  layout: string | null
  gateway_record: boolean
  preserves_forwarding: boolean
  /** Agent 1.8.42-go 起提供；只作提醒，不影响 state、label 与 action */
  forward_filter?: ForwardFilter | null
}

export interface GatewayForwardingView {
  label: string
  tone: 'success' | 'warning' | 'danger' | 'neutral'
  note?: string
  action: 'persist' | null
}

const withNote = (view: Omit<GatewayForwardingView, 'note'>, note?: string | null): GatewayForwardingView =>
  note ? { ...view, note } : view

/** Agent 卡片「IPv4 转发」一行的状态；tone 与 AgentStatusRow 一致。 */
export function gatewayForwardingView(entry: GatewayForwardingEntry): GatewayForwardingView {
  const message = entry.message || ''
  switch (entry.state) {
    case 'persisted':
      return { label: '已开启并持久化', tone: 'success', action: null }
    case 'needs_persist': {
      const action = entry.can_persist ? 'persist' : null
      if (entry.current === '1') return withNote({ label: '已开启，未持久化', tone: 'warning', action }, message)
      if (entry.current === '0') return withNote({ label: '未开启', tone: 'danger', action }, message)
      return withNote({ label: '状态未知', tone: 'warning', action }, message)
    }
    case 'upgrade_required':
      return { label: '请先更新 Agent', tone: 'warning', action: null,
        note: `需要 ${entry.minimum_version || STANDALONE_FORWARDING_AGENT_VERSION} 或更新版本才能在面板开启并持久化。` }
    case 'unsupported':
      return withNote({ label: '不支持自动持久化', tone: 'neutral', action: null }, message)
    case 'blocked':
      return withNote({ label: '持久化文件已被修改', tone: 'danger', action: null }, message)
    case 'offline':
      return { label: 'Agent 离线', tone: 'neutral', action: null }
    case 'docker':
      return withNote({ label: 'Docker 部署', tone: 'neutral', action: null }, message)
    case 'missing':
      return withNote({ label: 'Agent 已删除', tone: 'danger', action: null }, message)
    case 'not_mihomo':
      return withNote({ label: '非 Mihomo', tone: 'warning', action: null }, message)
    default:
      return withNote({ label: '无法读取', tone: 'warning', action: null }, message)
  }
}

/** 「路由器「ROS」」；涉及多个配置空间时在名称后注明空间。 */
export function gatewayRoutersText(entry: GatewayForwardingEntry): string {
  const routers = Array.isArray(entry.routers) ? entry.routers : []
  const profiles = new Set(routers.map(router => router.profile_id).filter(Boolean))
  const names = routers.map(router => {
    const name = `「${router.name || router.id}」`
    return profiles.size > 1 && router.profile_name ? `${name}（${router.profile_name}）` : name
  })
  return names.length ? `路由器${names.join('、')}` : '路由器'
}

/** 展开说明：为什么这台设备需要 IPv4 转发。 */
export function gatewayForwardingReason(entry: GatewayForwardingEntry): string {
  return `${gatewayRoutersText(entry)}把要分流的流量转给这台设备，需要开启 IPv4 转发；未开启时转来的 UDP（如 QUIC、游戏、语音）和 ping 会被丢弃。持久化后重启设备也保持开启。`
}

/** 路由器状态下方的提示：「代理网关「agent」problem」；Agent 已删除时没有名称。 */
export interface RouterForwardingWarning {
  agent: string | null
  problem: string
}

/**
 * 上游路由页在路由器状态下方的提示：只在代理网关的转发确定不可用、或防火墙可能拦截时出现，
 * 其他状态（含未持久化、离线、需要更新、读取失败）不提示，处理入口在 Agent 页。
 */
export function routerForwardingWarning(entry: GatewayForwardingEntry | undefined): RouterForwardingWarning | null {
  if (!entry) return null
  const agent = entry.agent_name || entry.agent_id
  if (entry.state === 'missing') return { agent: null, problem: '代理网关 Agent 已删除' }
  if (entry.state === 'not_mihomo') return { agent, problem: '不是 Mihomo' }
  if (entry.state === 'needs_persist' && entry.current === '0') return { agent, problem: '未开启 IPv4 转发' }
  const filter = forwardFilterWarning(entry.forward_filter)
  return filter ? { agent, problem: filter.short } : null
}

/** 一键开启前的确认文案；设备有 Linux 网关记录时说明撤销网关后转发保持开启。 */
export function persistConfirmText(entry: GatewayForwardingEntry): { title: string; description: string } {
  const name = entry.agent_name || entry.agent_id
  let description = `将在「${name}」写入 ${FORWARDING_CONFIG_FILE}，并在 Mihomo Agent 启动前注册转发检查，随后立即开启 IPv4 转发；不修改核心配置，也不重启 Mihomo 或 Agent。`
  if (entry.gateway_record) description += '这台设备有 Linux 网关记录：之后撤销网关时会保留 IPv4 转发开启。'
  return { title: `开启「${name}」的 IPv4 转发？`, description }
}
