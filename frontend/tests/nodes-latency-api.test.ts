import { afterEach, describe, expect, it, vi } from 'vitest'
import api, { nodeApi } from '@/api'

vi.mock('@/router', () => ({ default: { push: vi.fn() } }))
afterEach(() => { vi.restoreAllMocks() })

describe('node latency API identity', () => {
  it('posts opaque source keys without reducing them to display names', async () => {
    const result = { data: { results: {} } }
    const post = vi.spyOn(api, 'post').mockResolvedValue(result)
    const keys = [
      JSON.stringify(['node', 'manual-id']),
      JSON.stringify(['subscription', 'sub / ?#', '同名节点 / "香港"'])
    ]
    expect(await nodeApi.testLatency(keys)).toBe(result)
    expect(post).toHaveBeenCalledWith('/nodes/latency', { keys }, { timeout: 120000 })
  })

  it('keeps an empty selection explicit rather than requesting all nodes', async () => {
    const post = vi.spyOn(api, 'post').mockResolvedValue({ data: { results: {} } })
    await nodeApi.testLatency([])
    expect(post).toHaveBeenCalledWith('/nodes/latency', { keys: [] }, { timeout: 120000 })
  })
})
