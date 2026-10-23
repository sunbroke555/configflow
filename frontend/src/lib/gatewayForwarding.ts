/** 上游路由页「IPv4 转发」列：代理网关 Agent 的转发状态与一键开启（数据来自 /api/routers/gateway-forwarding）。 */
import type { ForwardFilter } from '@/lib/forwardFilter'

/** 单独安装的 Mihomo Agent 从此版本起支持在面板开启并持久化 IPv4 转发（同机安装为 1.8.40-go）。 */
export const STANDALONE_FORWARDING_AGENT_VERSION = '1.8.41-go'
export const FORWARDING_CONFIG_FILE = '/etc/sysctl.d/99-configflow-aio-forward.conf'

export type GatewayForwardingState =
  | 'persisted' | 'needs_persist' | 'blocked' | 'unsupported' | 'upgrade_required'
  | 'offline' | 'docker' | 'not_mihomo' | 'missing' | 'error'

/** 与后端 linux_gateway.gateway_forwarding_entry 的返回结构一一对应。 */
export interface GatewayForwardingEntry {
  agent_id: string
  agent_name: string
  agent_version: string
  routers: { id: string; name: string }[]
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
  tone: 'success' | 'warning' | 'danger' | 'info' | 'muted'
  note?: string
  action: 'persist' | 'upgrade' | null
}

const withNote = (view: Omit<GatewayForwardingView, 'note'>, note?: string | null): GatewayForwardingView =>
  note ? { ...view, note } : view

/**
 * 一行路由器的转发状态。`failed` 表示整个列表读取失败：每个关联行显示「无法读取」。
 * 未关联代理网关的路由器没有条目，显示「—」。
 */
export function gatewayForwardingView(entry: GatewayForwardingEntry | undefined, loading: boolean, failed = false): GatewayForwardingView {
  if (!entry) {
    if (loading) return { label: '读取中…', tone: 'muted', action: null }
    if (failed) return { label: '无法读取', tone: 'warning', note: '读取 IPv4 转发状态失败，请稍后刷新页面。', action: null }
    return { label: '—', tone: 'muted', action: null }
  }
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
      return { label: '请先更新 Agent', tone: 'warning', action: 'upgrade',
        note: `需要 ${entry.minimum_version || STANDALONE_FORWARDING_AGENT_VERSION} 或更新版本` }
    case 'unsupported':
      return withNote({ label: '不支持自动持久化', tone: 'muted', action: null }, message)
    case 'blocked':
      return withNote({ label: '持久化文件已被修改', tone: 'danger', action: null }, message)
    case 'offline':
      return { label: 'Agent 离线', tone: 'muted', action: null }
    case 'docker':
      return withNote({ label: 'Docker 部署', tone: 'muted', action: null }, message)
    case 'missing':
      return withNote({ label: 'Agent 已删除', tone: 'danger', action: null }, message)
    case 'not_mihomo':
      return withNote({ label: '非 Mihomo', tone: 'warning', action: null }, message)
    default:
      return withNote({ label: '无法读取', tone: 'warning', action: null }, message)
  }
}

/** 一键开启前的确认文案；设备有 Linux 网关记录时说明撤销网关后转发保持开启。 */
export function persistConfirmText(entry: GatewayForwardingEntry): { title: string; description: string } {
  const name = entry.agent_name || entry.agent_id
  let description = `将在「${name}」写入 ${FORWARDING_CONFIG_FILE}，并在 Mihomo Agent 启动前注册转发检查，随后立即开启 IPv4 转发；不修改核心配置，也不重启 Mihomo 或 Agent。`
  if (entry.gateway_record) description += '这台设备有 Linux 网关记录：之后撤销网关时会保留 IPv4 转发开启。'
  return { title: `开启「${name}」的 IPv4 转发？`, description }
}
