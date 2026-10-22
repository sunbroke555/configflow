import api, { type PublishGuard } from '@/api'
import type { LinuxGatewayCandidate, LinuxGatewaySpec } from '@/api/linuxGateway'
import type { AgentDeployment } from '@/composables/useAgentDeployments'

export interface GatewayOnboardingSettings {
  rule_set_ids: string[]
  direct_rule_set_ids: string[]
  proxy_rule_set_ids: string[]
  local_dns: string
  default_forward?: 'forward_remote' | 'forward_local'
  confirm_template_conversion?: boolean
  expected_config_revision?: number
}
export interface GatewayOnboardingConfig {
  profile_id: string
  config_revision: number
  server_url?: string
  settings: GatewayOnboardingSettings
  ip_candidates: LinuxGatewayCandidate[]
  domain_candidates: LinuxGatewayCandidate[]
  fake_ip_range: string
  oxidns_mode?: string
  field_warnings?: { local_dns?: string }
  warnings: string[]
  blockers: string[]
  can_prepare: boolean
  proxy_prerequisites?: { ready: boolean; policies: string[]; warnings: string[] }
}
export interface GatewayConfigurationPreview {
  success: boolean; ready: boolean; blockers: string[]; warnings: string[]
  changes: string[]; validation?: unknown; config_revision?: number
  publish_guards?: Record<string, PublishGuard>; message?: string
}
export interface GatewayInstallationAgent {
  id: string; name: string; host: string; port: number; profile_id: string
  service_type: 'mihomo' | 'oxidns'; status: string; enabled?: boolean
}
export interface GatewayInstallation {
  id: string; profile_id: string; name: string; config_revision?: number; network: LinuxGatewaySpec
  server_url?: string; auto_network?: boolean; phase: 'waiting' | 'enrolled' | 'publishing' | 'publish_failed' | 'published'
  command?: string; download_url?: string; expires_at: string; message?: string; error?: string
  /** 规则因网络原因下载失败时，可在面板设置 GitHub 代理域名或规则下载代理后重试 */
  remedy?: { kind: 'rule_download'; github: boolean } | null
  agents: Partial<Record<'mihomo' | 'oxidns', GatewayInstallationAgent>>
  config_changed?: boolean; target_changed?: boolean; expired?: boolean
  deployments: Partial<Record<'mihomo' | 'oxidns', AgentDeployment>>
}
export interface GatewayInstallationInput {
  profile_id: string; name: string; server_url: string; network?: LinuxGatewaySpec; auto_network?: boolean; agent_ip?: string
  config_revision: number; mihomo_agent_port?: number; oxidns_agent_port?: number; use_as_server_domain?: boolean
}
const scope = (profileId: string) => ({ headers: { 'X-ConfigFlow-Profile': profileId } })
const configPath = (profileId: string) => `/profiles/${encodeURIComponent(profileId)}/gateway-onboarding`
const sessionPath = (sessionId: string) => `/agents/gateway-installations/${encodeURIComponent(sessionId)}`
export const gatewayOnboardingApi = {
  get: (profileId: string) => api.get<GatewayOnboardingConfig>(configPath(profileId), scope(profileId)),
  preview: (profileId: string, settings: GatewayOnboardingSettings) => api.post<GatewayConfigurationPreview>(`${configPath(profileId)}/preview`, settings, { ...scope(profileId), timeout: 120000 }),
  save: (profileId: string, settings: GatewayOnboardingSettings) => api.post<GatewayConfigurationPreview>(configPath(profileId), settings, { ...scope(profileId), timeout: 120000 }),
  create: (input: GatewayInstallationInput) => api.post<GatewayInstallation>('/agents/gateway-installations', input, scope(input.profile_id)),
  sessions: (profileId: string) => api.get<{ success: boolean; session: GatewayInstallation | null }>('/agents/gateway-installations', { ...scope(profileId), params: { profile_id: profileId } }),
  session: (sessionId: string, profileId: string) => api.get<GatewayInstallation>(sessionPath(sessionId), scope(profileId)),
  network: (sessionId: string, profileId: string, body: { auto_network: true } | { network: LinuxGatewaySpec }) => api.post<GatewayInstallation>(`${sessionPath(sessionId)}/network`, body, { ...scope(profileId), timeout: 60000 }),
  publish: (sessionId: string, profileId: string, retryFailed = false) => api.post<GatewayInstallation>(`${sessionPath(sessionId)}/publish`, { retry_failed: retryFailed }, { ...scope(profileId), timeout: 120000 }),
}
