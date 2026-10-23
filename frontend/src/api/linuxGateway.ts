import api, { type PublishGuard } from '@/api'

export type LinuxGatewayScheme = 'router' | 'gateway'
export interface LinuxGatewaySettings { enabled: boolean; rule_set_ids: string[]; scheme?: LinuxGatewayScheme | null }
export interface LinuxGatewaySkippedRuleSet { id: string; name: string; reason: string }
export interface LinuxGatewayRouterTakeover { rule_set_ids: string[]; skipped: LinuxGatewaySkippedRuleSet[] }
/** 路由器保存后同步 IP 接管的结果；只有显式选择「主路由分流」的空间才会 synced。 */
export interface LinuxGatewaySyncResult { synced: boolean; changed: boolean; skipped: LinuxGatewaySkippedRuleSet[]; error?: string }
export interface LinuxGatewayCandidate {
  id: string; name: string; behavior: string; policy?: string; enabled: boolean
  library_enabled?: boolean; available: boolean; selected?: boolean; description?: string; reason?: string
}
export interface LinuxGatewayConfig {
  success?: boolean; settings: LinuxGatewaySettings; candidates: LinuxGatewayCandidate[]
  fake_ip_range: string; warnings: string[]; refresh_interval?: number; publish_guard?: PublishGuard
  config_revision: number; restore_point: LinuxGatewayRestorePoint | null
  scheme?: LinuxGatewayScheme | null; effective_scheme?: LinuxGatewayScheme; scheme_inferred?: boolean
  router_takeover?: LinuxGatewayRouterTakeover
}
export interface LinuxGatewayRestorePoint { id: string; created_at: string }
export interface LinuxGatewayDraftGuard { expected_config_revision: number; preview_token: string }
export interface LinuxGatewayDraftPreview {
  success: boolean; action: 'save' | 'restore'; config_revision: number; preview_token: string
  changes: Array<{ path: string; before: string; after: string }>; warnings: string[]
  restore_point: LinuxGatewayRestorePoint | null
}
export interface LinuxGatewaySpec { interface: string; address: string; lan_cidr: string; upstream_gateway: string }
export interface LinuxGatewayManaged { active: boolean; pending?: boolean; healthy?: boolean; error?: string; spec?: LinuxGatewaySpec; applied_at?: string }
export interface LinuxGatewayStatus {
  success: boolean; message?: string; enabled_in_config?: boolean; supported?: boolean
  detection_error?: string; config_error?: string; unsupported_reason?: string
  detection?: { interfaces?: Array<{ name: string; addresses?: string[] }>; default_routes?: Array<{ interface: string; gateway: string }>; suggested?: Partial<LinuxGatewaySpec> }
  managed: LinuxGatewayManaged
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
  remove: (agentId: string, profileId: string) =>
    api.post<LinuxGatewayStatus>(hostPath(agentId, 'remove'), {}, hostScope(profileId)),
}
