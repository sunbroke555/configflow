import api, { type PublishGuard } from '@/api'
import type { ForwardFilter } from '@/lib/forwardFilter'

export interface LinuxGatewaySettings { enabled: boolean; rule_set_ids: string[] }
export interface LinuxGatewayCandidate {
  id: string; name: string; behavior: string; policy?: string; enabled: boolean
  library_enabled?: boolean; available: boolean; selected?: boolean; description?: string; reason?: string
}
export interface LinuxGatewayConfig {
  success?: boolean; settings: LinuxGatewaySettings; candidates: LinuxGatewayCandidate[]
  fake_ip_range: string; warnings: string[]; refresh_interval?: number; publish_guard?: PublishGuard
  config_revision: number; restore_point: LinuxGatewayRestorePoint | null
  /** 主路由分流的接管方式；null 表示普通 Linux 网关 */
  router_split?: LinuxGatewayRouterSplit
}
export type RouterSplitMode = 'full_tun' | 'invalid' | 'legacy' | null
export interface LinuxGatewayRouterSplit {
  mode: RouterSplitMode; source?: string | null; issues: string[]; route_rule_set_ids: string[]
}
export interface LinuxGatewayRestorePoint { id: string; created_at: string }
export interface LinuxGatewayDraftGuard { expected_config_revision: number; preview_token: string }
export interface LinuxGatewayDraftPreview {
  success: boolean; action: 'save' | 'restore'; config_revision: number; preview_token: string
  changes: Array<{ path: string; before: string; after: string }>; warnings: string[]
  restore_point: LinuxGatewayRestorePoint | null
}
export interface LinuxGatewaySpec { interface: string; address: string; lan_cidr: string; upstream_gateway: string }
export interface LinuxGatewayManaged {
  active: boolean; pending?: boolean; healthy?: boolean; error?: string; spec?: LinuxGatewaySpec; applied_at?: string
  /** AIO 转发持久化存在时，撤销网关会保留 IPv4 转发 */
  preserves_forwarding?: boolean
}
export interface LinuxGatewayForwarding {
  protocol?: number
  current: '0' | '1' | ''
  config: 'absent' | 'managed' | 'foreign' | 'symlink'
  hook_script: string
  startup_hook: string
  persisted: boolean
  /** 主机自己的 sysctl 配置已持久化转发（Agent 1.8.43-go 起）；不等于 ConfigFlow 的持久化 */
  system_persisted?: boolean
  system_source?: string
  supported?: boolean
  unsupported_reason?: string
  error?: string
}
export interface LinuxGatewayTun { configured: boolean; device: string; present: boolean; gateway_marker: boolean; error?: string }
export interface LinuxGatewayStatus {
  success: boolean; message?: string; enabled_in_config?: boolean; supported?: boolean
  detection_error?: string; config_error?: string; unsupported_reason?: string
  detection?: { interfaces?: Array<{ name: string; addresses?: string[] }>; default_routes?: Array<{ interface: string; gateway: string }>; suggested?: Partial<LinuxGatewaySpec> }
  managed: LinuxGatewayManaged
  /** Agent 1.8.40-go 起提供；缺少时表示 Agent 需要更新 */
  ipv4_forwarding?: LinuxGatewayForwarding
  tun?: LinuxGatewayTun
  /** Agent 1.8.42-go 起提供：完整 TUN 主机的转发过滤检查，只作提醒 */
  forward_filter?: ForwardFilter
}
export interface LinuxGatewayForwardingResult {
  success: boolean; message?: string; ipv4_forwarding?: LinuxGatewayForwarding
  tun_prepare?: { level: 'pass' | 'warn' | 'error'; message: string } | null; managed?: LinuxGatewayManaged
}
export interface LinuxGatewayPreview extends LinuxGatewayStatus {
  ready: boolean; blockers: string[]; warnings: string[]; commands: string[]; spec: LinuxGatewaySpec
}

const scope = (profileId: string) => ({ headers: { 'X-ConfigFlow-Profile': profileId } })
const hostPath = (id: string, action: string) => `/agents/${encodeURIComponent(id)}/linux-gateway/${action}`
const hostScope = (profileId: string) => ({ ...scope(profileId), params: { profile_id: profileId }, timeout: 60000 })

export const linuxGatewayApi = {
  get: (profileId: string) => api.get<LinuxGatewayConfig>('/custom-config/linux-gateway', scope(profileId)),
  save: (profileId: string, settings: LinuxGatewaySettings, guard?: LinuxGatewayDraftGuard) =>
    api.post<LinuxGatewayConfig>('/custom-config/linux-gateway', guard ? { settings, ...guard } : settings, scope(profileId)),
  previewDraft: (profileId: string, settings: LinuxGatewaySettings, revision: number) =>
    api.post<LinuxGatewayDraftPreview>('/custom-config/linux-gateway/preview', { settings, expected_config_revision: revision }, { ...scope(profileId), timeout: 120000 }),
  previewRestore: (profileId: string, revision: number) =>
    api.post<LinuxGatewayDraftPreview>('/custom-config/linux-gateway/restore/preview', { expected_config_revision: revision }, scope(profileId)),
  restoreDraft: (profileId: string, guard: LinuxGatewayDraftGuard) =>
    api.post<LinuxGatewayConfig>('/custom-config/linux-gateway/restore', guard, scope(profileId)),
  status: (agentId: string, profileId: string) => api.get<LinuxGatewayStatus>(hostPath(agentId, 'status'), hostScope(profileId)),
  preview: (agentId: string, profileId: string, spec: LinuxGatewaySpec) =>
    api.post<LinuxGatewayPreview>(hostPath(agentId, 'preview'), spec, hostScope(profileId)),
  apply: (agentId: string, profileId: string, spec: LinuxGatewaySpec) =>
    api.post<LinuxGatewayStatus>(hostPath(agentId, 'apply'), spec, hostScope(profileId)),
  /** 旧版主路由分流撤销会中断连接，需带上明确确认 */
  remove: (agentId: string, profileId: string, confirmTeardown = false) =>
    api.post<LinuxGatewayStatus>(hostPath(agentId, 'remove'), confirmTeardown ? { confirm_router_split_teardown: true } : {}, hostScope(profileId)),
  persistForwarding: (agentId: string, profileId: string) =>
    api.post<LinuxGatewayForwardingResult>(hostPath(agentId, 'forwarding'), {}, hostScope(profileId)),
}
