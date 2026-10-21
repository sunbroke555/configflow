import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { agentApi } from '@/api'
import { invalidateAgentSummaries, loadAgentSummaries } from '@/lib/agentSummaries'

vi.mock('@/api', () => ({ agentApi: { summaries: vi.fn() } }))
const response = (data: unknown) => ({ data } as any)

beforeEach(() => {
  vi.useFakeTimers()
  vi.mocked(agentApi.summaries).mockReset()
  invalidateAgentSummaries()
})
afterEach(() => { vi.useRealTimers() })

describe('shared agent summaries', () => {
  it('shares one request between concurrent callers and reuses it briefly', async () => {
    vi.mocked(agentApi.summaries).mockResolvedValue(response([{ id: 'a' }]))
    const [first, second] = await Promise.all([loadAgentSummaries(), loadAgentSummaries()])
    expect(first).toEqual([{ id: 'a' }])
    expect(second).toBe(first)
    await loadAgentSummaries()
    expect(agentApi.summaries).toHaveBeenCalledTimes(1)

    vi.advanceTimersByTime(3001)
    await loadAgentSummaries()
    expect(agentApi.summaries).toHaveBeenCalledTimes(2)
  })

  it('does not keep a failed or malformed result', async () => {
    vi.mocked(agentApi.summaries).mockRejectedValueOnce(new Error('offline'))
    await expect(loadAgentSummaries()).rejects.toThrow('offline')
    vi.mocked(agentApi.summaries).mockResolvedValueOnce(response({ unexpected: true }))
    expect(await loadAgentSummaries()).toEqual({ unexpected: true })
    vi.mocked(agentApi.summaries).mockResolvedValueOnce(response([]))
    expect(await loadAgentSummaries()).toEqual([])
    expect(agentApi.summaries).toHaveBeenCalledTimes(3)
  })

  it('never caches a response that was in flight when invalidated', async () => {
    let finish!: (value: unknown) => void
    vi.mocked(agentApi.summaries)
      .mockReturnValueOnce(new Promise(resolve => { finish = resolve }) as any)
      .mockResolvedValueOnce(response([{ id: 'new' }]))
    const stale = loadAgentSummaries()
    invalidateAgentSummaries()
    finish(response([{ id: 'old' }]))
    expect(await stale).toEqual([{ id: 'old' }])
    expect(await loadAgentSummaries()).toEqual([{ id: 'new' }])
  })
})
