import { onUnmounted, ref } from 'vue'
import { loadAgentSummaries } from '@/lib/agentSummaries'
import type { Agent } from '@/types'

export type OxiDNSDevice = Pick<Agent, 'id' | 'name' | 'host' | 'status'>
export const OXIDNS_DEVICE_STORAGE_KEY = 'configflow:oxidns-monitor:agent'
export const oxidnsMonitorPath = (id: string) => '/agents/' + encodeURIComponent(id) + '/oxidns'

export function rememberOxiDNSDevice(id: string) {
  try { window.localStorage.setItem(OXIDNS_DEVICE_STORAGE_KEY, id) } catch { /* Storage can be unavailable in private browsers. */ }
}

export function preferredOxiDNSDevice(devices: OxiDNSDevice[]) {
  let saved: string | null = null
  try { saved = window.localStorage.getItem(OXIDNS_DEVICE_STORAGE_KEY) } catch { /* Selection still works without storage. */ }
  return devices.find(device => device.id === saved) || devices.find(device => device.status === 'online') || devices[0]
}

export function useOxiDNSDevices() {
  const devices = ref<OxiDNSDevice[] | null>(null)
  const loading = ref(false)
  const error = ref('')
  let epoch = 0
  let disposed = false

  async function load(): Promise<OxiDNSDevice[] | null> {
    const requestEpoch = ++epoch
    loading.value = true
    error.value = ''
    try {
      const data = await loadAgentSummaries()
      if (disposed || requestEpoch !== epoch) return null
      if (!Array.isArray(data)) throw new Error('设备列表格式异常，请稍后重试。')
      devices.value = data.filter((agent: Agent) => agent && agent.service_type === 'oxidns')
      return devices.value
    } catch (failure: any) {
      if (!disposed && requestEpoch === epoch) error.value = failure?.response?.data?.message || failure?.message || '暂时无法读取设备列表。'
      return null
    } finally {
      if (!disposed && requestEpoch === epoch) loading.value = false
    }
  }
  onUnmounted(() => { disposed = true; epoch++ })
  return { devices, loading, error, load }
}
