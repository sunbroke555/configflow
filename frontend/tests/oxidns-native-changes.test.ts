import { describe, expect, it } from 'vitest'
import { needsOxidnsReconciliation } from '@/lib/oxidnsNativeChanges'

const forward = (addr: unknown) => ({plugins: [{tag: 'forward_fallback', type: 'forward', args: {upstreams: [{addr}]}}]})
describe('shared native lifecycle trigger', () => {
  it.each(['', '  ', '\t', null])('synchronizes both sides of the empty upstream boundary: %s', empty => {
    expect(needsOxidnsReconciliation(forward('192.0.2.53'), forward(empty))).toBe(true)
    expect(needsOxidnsReconciliation(forward(empty), forward('192.0.2.53'))).toBe(true)
  })
  it('lets ordinary scalar typing stay local without implementing dependency policy', () => {
    expect(needsOxidnsReconciliation(forward('192.0.2.53'), forward('192.0.2.54'))).toBe(false)
  })
  it('synchronizes renamed jobs, removal and plugin identity changes', () => {
    const before = {plugins: [{tag: 'rules_cron', type: 'cron', args: {jobs: [{name: 'refresh_rules', executors: ['$rules_refresh']}]}}]}
    const after = structuredClone(before)
    after.plugins[0].args.jobs[0].name = 'custom'
    expect(needsOxidnsReconciliation(before, after)).toBe(true)
    after.plugins[0].args.jobs = []
    expect(needsOxidnsReconciliation(before, after)).toBe(true)
    expect(needsOxidnsReconciliation(before, {plugins: []})).toBe(true)
    const renamed = structuredClone(before)
    renamed.plugins[0].tag = 'renamed'
    expect(needsOxidnsReconciliation(before, renamed)).toBe(true)
  })
})
