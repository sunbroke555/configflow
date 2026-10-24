import type { AgentDeployment } from '@/composables/useAgentDeployments'

export type DeviceGroupMode = 'follow' | 'direct' | 'proxy'
export interface DeviceGroup {
  id: string
  name: string
  note?: string
  members: string[]
  mode?: DeviceGroupMode
  policy?: string
  keep_blocking?: boolean
  dns?: 'follow' | 'direct_full'
}
export interface PolicyRoutingSources { include: string[]; exclude_groups: string[] }
export interface DeviceGroupsDraft {
  device_groups: { version: 1; groups: DeviceGroup[] }
  policy_routing_sources: PolicyRoutingSources
  device_notes: Record<string, string>
}
export interface DeviceGroupWarning {
  code: string; message: string; group_id?: string; member?: string; agent_id?: string; router_id?: string
}
export interface DeviceGroupTarget {
  id: string; name: string; host: string; service_type: 'mihomo' | 'oxidns'
  status: string; enabled?: boolean; target_fingerprint: string
  core_version?: string | null; version_state?: 'supported' | 'unsupported' | 'unknown'
}
export interface EffectiveDeviceGroup {
  id: string; ownership_networks: string[]; effective_networks: string[]; blocking_count: number
  member_overlaps?: Array<{ member: string; group_id: string; group_name: string; count?: number }>
}
export interface DeviceGroupsSnapshot extends DeviceGroupsDraft {
  success: true; profile_id: string; profile_name: string; revision: string; config_revision: number
  migrated: boolean
  migration: { available: boolean; reason?: string }
  capabilities: { oxidns_mode: 'template' | 'native'; keep_blocking_editable: boolean; keep_blocking_reason?: string }
  policy_groups: Array<{ id: string; name: string }>
  effective_groups: EffectiveDeviceGroup[]
  infrastructure: { addresses: Array<{ ip: string; kind: string; name?: string }>; unresolved: string[] }
  affected_agents: DeviceGroupTarget[]; warnings: DeviceGroupWarning[]
  active_publication?: DeviceGroupPublication | null
}
export interface DeviceGroupBlockingRule { id: string; name: string; dns_action?: string; policy?: string }
export interface DeviceGroupsPreview {
  success: true; profile_id: string; revision: string; config_revision: number; preview_token: string
  draft: DeviceGroupsDraft
  generated_changes: Array<{ service_type: 'oxidns' | 'mihomo'; before: string; after: string }>
  copied_blocking_rules: DeviceGroupBlockingRule[]; excluded_after_match_rules: DeviceGroupBlockingRule[]
  routing_changes: { added: string[]; removed: string[]; affected_clients?: string[] }
  affected_agents: DeviceGroupTarget[]; blockers: string[]; warnings: DeviceGroupWarning[]
  infrastructure_samples?: Array<{ ip: string; count: number; share: number }>
  migration?: { sources_before: Record<string, string[]>; sources_after: Record<string, string[]> }
}
export interface DeviceGroupLookup {
  success: true; ip: string; group_id: string; group_name: string; mode: DeviceGroupMode
  matched_member: string | null
  overridden_members: Array<{ member: string; group_name: string }>
  infrastructure_exclusion: string | null; policy_routing_included: boolean
}
export interface DeviceGroupActivity {
  success: true; partial: boolean; sample_size: number; sample_limited: boolean
  clients: Array<{ ip: string; count: number; agent_ids: string[] }>
  agents: Array<{ id: string; name: string; state: 'available' | 'offline' | 'upgrade_required' | 'recorder_disabled' | 'unavailable'; message?: string; recorder?: string; sample_size?: number; sample_limited?: boolean }>
  infrastructure_samples: Array<{ ip: string; kind: string; count: number; share: number }>
}
export interface DeviceGroupDiagnostic {
  router_id?: string; agent_id?: string; checked_at: string | null
  state: 'not_checked' | 'checking' | 'confirmed' | 'uncertain' | 'failed'
  preserves_source_ip: boolean | null
  deployment: 'router_split' | 'selective_gateway' | 'full_tun' | 'mihomo_only' | 'unknown'
  message: string; warnings: DeviceGroupWarning[]
}
export interface DeviceGroupPublication {
  batch_id: string; profile_id: string; config_revision: number; revision: string
  state: 'preparing' | 'running' | 'paused' | 'succeeded' | 'failed'
  phase: 'mihomo' | 'oxidns' | 'completed'; message: string
  targets: Array<{ agent_id: string; name: string; service_type: 'mihomo' | 'oxidns'; state: 'queued' | 'running' | 'paused' | 'succeeded' | 'failed'; deployment_id?: string; deployment?: AgentDeployment; message?: string }>
  actions: { can_retry: boolean; can_reconcile: boolean; activate_agent_ids: string[]; requires_new_preview: boolean }
}
export const deviceGroupModeLabel = (mode?: DeviceGroupMode) => ({ follow: '跟随全局', direct: '全部直连', proxy: '全部代理' })[mode || 'follow']
