// @vitest-environment node
import { expect, it } from 'vitest'
import { submitAgentRequests, uniquePublicationTargets } from '@/lib/agentRequests'

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

it('submits each HA pair once without dropping independent devices or an individually selected backup', () => {
  const a = { id: 'a', oxidns_ha: { enabled: true, group_id: 'g', peer_id: 'b' } }
  const b = { id: 'b', oxidns_ha: { enabled: true, group_id: 'g', peer_id: 'a' } }
  const c = { id: 'c' }
  const disabled = { id: 'd', oxidns_ha: { enabled: false, group_id: 'g', peer_id: 'a' } }
  expect(uniquePublicationTargets([b, c, a, disabled])).toEqual([b, c, disabled])
  expect(uniquePublicationTargets([b])).toEqual([b])
  expect(uniquePublicationTargets([a, c])).toEqual([a, c])
})
