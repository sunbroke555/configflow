import { ref, watch, type Ref } from 'vue'
import type { OxiDNSQueryFilters } from '@/types/oxidnsMonitor'

// Records and the overview sample use the DNS host's epoch clock. Advance a
// sample with elapsed time, so changing the browser's wall clock cannot move it.
export function useOxiDNSClock(collectedAt: Ref<string | undefined>) {
  let sampledAt: number | null = null
  let receivedAt = 0
  const skewMs = ref<number | null>(null)
  watch(collectedAt, value => {
    const sample = Date.parse(value || '')
    sampledAt = Number.isFinite(sample) ? sample : null
    receivedAt = performance.now()
    skewMs.value = sampledAt === null ? null : sampledAt - Date.now()
  }, { immediate: true, flush: 'sync' })
  const now = () => sampledAt === null ? Date.now() : sampledAt + Math.max(0, performance.now() - receivedAt)
  return { now, skewMs }
}

export function oxiDNSQueryTimeRange(range: OxiDNSQueryFilters['range'], now: number) {
  if (range === 'all') return { since_ms: undefined, until_ms: undefined }
  const until_ms = Math.floor(now)
  return { since_ms: Math.max(0, until_ms - (range === '1h' ? 3600000 : 86400000)), until_ms }
}
