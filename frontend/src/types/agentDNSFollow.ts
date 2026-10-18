import type { AgentOperationEvent } from './agentOperation'

export interface AgentDNSFollowInput {
  source_agent_id: string
  upstream_tag: string
  upstream_index: number
  connection: 'auto' | 'network'
  address?: string
}

export interface AgentDNSFollowBinding extends AgentDNSFollowInput {
  id: string
  source_profile_id: string
  target_profile_id: string
  after_deployment_id?: string
}

export interface AgentDNSFollowState {
  status: 'waiting' | 'preparing' | 'publishing' | 'succeeded' | 'failed'
  source_deployment_id?: string
  target_deployment_id?: string
  address?: string
  message?: string
  events?: AgentOperationEvent[]
  updated_at?: string
}

export interface AgentDNSFollowOptions {
  success: boolean
  binding: AgentDNSFollowBinding | null
  state: AgentDNSFollowState | null
  sources: { id: string; name: string }[]
  upstreams: { tag: string; index: number; address: string }[]
  message?: string
}

export const dnsFollowLabels: Record<string, string> = {
  waiting: '等待 Mihomo 发布',
  preparing: '准备同步 OxiDNS',
  publishing: '发布 OxiDNS',
  succeeded: 'OxiDNS DNS 同步完成',
  failed: 'OxiDNS 同步失败',
}
