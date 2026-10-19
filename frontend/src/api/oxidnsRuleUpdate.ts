import api from '@/api'

export interface OxiDNSRuleUpdateTask {
  id: string
  status: 'idle' | 'running' | 'succeeded' | 'failed'
  phase: 'idle' | 'downloading' | 'reloading' | 'done'
  code?: string
  message?: string
  downloaded: number
  download_total: number
  reloaded: number
  reload_total: number
}

export interface OxiDNSRuleUpdateResponse {
  success: boolean
  message?: string
  code?: string
  task?: OxiDNSRuleUpdateTask
}

const path = (agentId: string) => `/agents/${encodeURIComponent(agentId)}/oxidns/rules/update`
export const oxidnsRuleUpdateApi = {
  status: (agentId: string, signal?: AbortSignal) => api.get<OxiDNSRuleUpdateResponse>(path(agentId), { signal }),
  start: (agentId: string, signal?: AbortSignal) => api.post<OxiDNSRuleUpdateResponse>(path(agentId), {}, { signal }),
}
