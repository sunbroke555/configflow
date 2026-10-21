import { agentApi } from '@/api'
import type { Agent } from '@/types'

/**
 * Agent list without content sync status, for device pickers and live polling.
 *
 * Opening a panel requested the full list next to the live poll, and the full
 * list loads and hashes every profile. Concurrent callers now share one cheap
 * request and a result stays usable for a moment; callers still check the shape.
 */
const FRESH_MS = 3000

let cached: { at: number; data: Agent[] } | undefined
let pending: Promise<unknown> | undefined
let generation = 0

export async function loadAgentSummaries(): Promise<unknown> {
  if (cached && Date.now() - cached.at < FRESH_MS) return cached.data
  if (!pending) {
    const requestGeneration = generation
    const request = agentApi.summaries()
      .then(({ data }) => {
        if (requestGeneration === generation && Array.isArray(data)) cached = { at: Date.now(), data }
        return data
      })
      .finally(() => { if (pending === request) pending = undefined })
    pending = request
  }
  return pending
}

/** Forget the shared result, including a request still in flight. */
export function invalidateAgentSummaries() {
  generation++
  cached = undefined
  pending = undefined
}
