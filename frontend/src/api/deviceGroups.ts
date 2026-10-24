import api from '@/api'
import type { DeviceGroupActivity, DeviceGroupDiagnostic, DeviceGroupLookup, DeviceGroupPublication, DeviceGroupsDraft, DeviceGroupsPreview, DeviceGroupsSnapshot } from '@/types/deviceGroups'

const path = (profile: string, suffix = '') => `/profiles/${encodeURIComponent(profile)}/device-groups${suffix}`
const options = (profile: string, signal?: AbortSignal) => ({ headers: { 'X-ConfigFlow-Profile': profile }, signal })
const baseURL = () => localStorage.getItem('serverDomain') || window.location.origin
export const deviceGroupsApi = {
  get: (profile: string, signal?: AbortSignal) => api.get<DeviceGroupsSnapshot>(path(profile), options(profile, signal)),
  save: (profile: string, draft: DeviceGroupsDraft, revision: string, previewToken: string) =>
    api.put<DeviceGroupsSnapshot>(path(profile), { ...draft, revision, preview_token: previewToken, base_url: baseURL() }, options(profile)),
  preview: (profile: string, draft: DeviceGroupsDraft, revision: string, signal?: AbortSignal) =>
    api.post<DeviceGroupsPreview>(path(profile, '/preview'), { ...draft, revision, base_url: baseURL() }, { ...options(profile, signal), timeout: 90000 }),
  lookup: (profile: string, ip: string, signal?: AbortSignal) => api.get<DeviceGroupLookup>(path(profile, '/lookup'), { ...options(profile, signal), params: { ip } }),
  activity: (profile: string, signal?: AbortSignal) => api.get<DeviceGroupActivity>(path(profile, '/activity'), { ...options(profile, signal), timeout: 60000 }),
  diagnostics: (profile: string, signal?: AbortSignal) => api.get<{ success: true; diagnostics: DeviceGroupDiagnostic[] }>(path(profile, '/diagnostics'), { ...options(profile, signal), timeout: 90000 }),
  migrationPreview: (profile: string, revision: string, signal?: AbortSignal) => api.post<DeviceGroupsPreview>(path(profile, '/migration/preview'), { revision, base_url: baseURL() }, { ...options(profile, signal), timeout: 90000 }),
  migrate: (profile: string, revision: string, previewToken: string) => api.post<DeviceGroupsSnapshot>(path(profile, '/migration'), { revision, preview_token: previewToken, base_url: baseURL() }, options(profile)),
  publish: (profile: string, batchId: string, previewToken: string, targetIds: string[]) => api.post<{ success: true; publication: DeviceGroupPublication }>(path(profile, '/publish'), { batch_id: batchId, preview_token: previewToken, target_ids: targetIds, base_url: baseURL() }, options(profile)),
  publication: (profile: string, batchId: string, signal?: AbortSignal) => api.get<{ success: true; publication: DeviceGroupPublication }>(path(profile, `/publish/${encodeURIComponent(batchId)}`), options(profile, signal)),
  retryPublication: (profile: string, batchId: string, action: 'reconcile' | 'retry' | 'activate', agentId?: string) => api.post<{ success: true; publication: DeviceGroupPublication }>(path(profile, `/publish/${encodeURIComponent(batchId)}/retry`), { action, ...(agentId ? { agent_id: agentId } : {}) }, options(profile)),
}
