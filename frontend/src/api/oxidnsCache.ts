import api from '@/api'

export interface OxiDNSCacheListResponse { success: boolean; caches: string[]; code?: string; message?: string }
export interface OxiDNSCacheFlushResponse { success: boolean; flushed: string[]; code?: string; message?: string }

const path = (agentId: string) => `/agents/${encodeURIComponent(agentId)}/oxidns/cache`
export const oxidnsCacheApi = {
  list: (agentId: string) => api.get<OxiDNSCacheListResponse>(path(agentId)),
  flush: (agentId: string, tags: string[]) => api.post<OxiDNSCacheFlushResponse>(path(agentId) + '/flush', { tags }),
}
