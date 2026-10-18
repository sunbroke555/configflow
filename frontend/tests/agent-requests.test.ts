import { expect, it } from 'vitest'
import { submitAgentRequests } from '@/lib/agentRequests'

it('limits publication submissions to two and preserves result order', async () => {
  let active = 0, peak = 0
  const result = await submitAgentRequests([1, 2, 3, 4, 5], async item => {
    active++; peak = Math.max(peak, active)
    await Promise.resolve()
    active--
    return item * 10
  })
  expect(peak).toBe(2)
  expect(result).toEqual([10, 20, 30, 40, 50])
})
