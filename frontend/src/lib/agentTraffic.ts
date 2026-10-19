import type { AgentSystemMetrics } from '@/types'

export const TRAFFIC_WINDOW_MS = 5 * 60 * 1000
export const TRAFFIC_STALE_MS = 2 * 60 * 1000

export interface TrafficSample {
  at: number
  upload: number | null
  download: number | null
}

const validSpeed = (value: unknown): number | null =>
  typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null

export function trafficSample(metrics?: AgentSystemMetrics): TrafficSample | null {
  // Agent timestamps include a timezone. Do not turn a missing timestamp into a new sample.
  if (!metrics?.collected_at || !/(?:Z|[+-]\d{2}:\d{2})$/i.test(metrics.collected_at)) return null
  const at = Date.parse(metrics.collected_at)
  if (!Number.isFinite(at)) return null
  return {
    at,
    upload: validSpeed(metrics.network?.speed_sent),
    download: validSpeed(metrics.network?.speed_recv),
  }
}

export function recordTraffic(history: TrafficSample[], sample: TrafficSample | null, now: number): TrafficSample[] {
  const samples = new Map(history.map(point => [point.at, point]))
  if (sample && !samples.has(sample.at)) samples.set(sample.at, sample)
  return [...samples.values()]
    .filter(point => point.at >= now - TRAFFIC_WINDOW_MS && point.at <= now)
    .sort((a, b) => a.at - b.at)
    .slice(-301)
}

export function isTrafficFresh(sample: TrafficSample | null, now: number): boolean {
  return !!sample && sample.at <= now && now - sample.at < TRAFFIC_STALE_MS
}
