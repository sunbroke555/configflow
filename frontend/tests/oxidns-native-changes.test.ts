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
  it.each(['', '  ', '\t', null, undefined])('consults the server when a dump path is cleared or removed: %s', empty => {
    const before = {plugins: [{tag: 'lazy_cache', type: 'cache', args: {dump_file: './state/cache.dump', dump_interval: 180}}]}
    const after: any = structuredClone(before)
    if (empty === undefined) delete after.plugins[0].args.dump_file
    else after.plugins[0].args.dump_file = empty
    expect(needsOxidnsReconciliation(before, after)).toBe(true)
    expect(needsOxidnsReconciliation(after, before)).toBe(true)
  })
  it.each(['files', 'entries'])('consults the server when the last Hosts %s source is removed', field => {
    const before = {plugins: [{tag: 'custom_hosts', type: 'hosts', args: {[field]: ['one-source']}}]}
    for (const args of [{[field]: []}, {[field]: null}, {}]) {
      const after = {plugins: [{tag: 'custom_hosts', type: 'hosts', args}]}
      expect(needsOxidnsReconciliation(before, after)).toBe(true)
    }
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
