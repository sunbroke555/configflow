import api from '@/api'

export interface AgentRuleSyncSettings { library: boolean; rules: boolean }
export interface AgentRuleSyncState {
  status: 'disabled' | 'idle' | 'pending' | 'running' | 'succeeded' | 'failed' | 'blocked'
  message: string
  updated_at?: string
}
export interface AgentRuleSyncResponse {
  success: boolean
  settings: AgentRuleSyncSettings
  state: AgentRuleSyncState
  baseline_ready: boolean
  message?: string
}

const path = (agentId: string) => `/agents/${encodeURIComponent(agentId)}/rule-sync`
export const agentRuleSyncApi = {
  get: (agentId: string, signal?: AbortSignal) => api.get<AgentRuleSyncResponse>(path(agentId), { signal }),
  save: (agentId: string, settings: AgentRuleSyncSettings, signal?: AbortSignal) =>
    api.put<AgentRuleSyncResponse>(path(agentId), settings, { signal }),
}
