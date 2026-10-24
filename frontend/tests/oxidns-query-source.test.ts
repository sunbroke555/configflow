import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { defineComponent, reactive } from 'vue'
import { oxidnsMonitorApi } from '@/api'
import { useOxiDNSQuerySources } from '@/composables/useOxiDNSQuerySources'
import { querySourceLabel, type OxiDNSQueries, type OxiDNSQuery } from '@/types/oxidnsMonitor'

vi.mock('@/api', () => ({ oxidnsMonitorApi: { query: vi.fn() } }))
const row: OxiDNSQuery = { id: 1, created_at_ms: 1, elapsed_ms: 0, client_ip: '192.0.2.1',
  questions_json: [], error: null, has_response: true, rcode: 'NOERROR', answer_count: 0, answers_json: [] }
type Step = NonNullable<OxiDNSQuery['steps']>[number]
const step = (tag: string, outcome: string, kind = 'executor', sequence_tag = 'main'): Step =>
  ({ event_index: 0, sequence_tag, node_index: 0, kind, tag, outcome })
const run = (tag: string, outcome = 'next', sequence = 'main') => [step(tag, 'entered', 'executor', sequence), step(tag, outcome, 'executor', sequence)]
const label = (...steps: Step[]) => querySourceLabel({ ...row, steps })
const probeHit = [step('local_probe_cache', 'entered', 'executor', 'local_probe'),
  step('qs.match.local_probe.1.0.has_resp', 'not_matched', 'matcher', 'local_probe'),
  step('return', 'return', 'builtin', 'local_probe'), step('local_probe_cache', 'return', 'executor', 'local_probe')]

const suppressedAAAA: OxiDNSQuery = {
  ...row, id: 160909, client_ip: '192.168.0.105',
  questions_json: [{ name: 'api.telegram.org.', qtype: 'AAAA', qclass: 'IN' }],
  steps: ['entered', 'stop'].map((outcome, event_index) => ({
    event_index, sequence_tag: 'sequence_main', node_index: 2, kind: 'executor',
    tag: 'qs.exec.sequence_main.2.prefer_ipv4', outcome
  }))
}
const preferredLabel = 'IPv4 优先（抑制 AAAA）'

describe('query source from the standard ConfigFlow trace', () => {
  it.each([['forward_local', '本地 DNS'], ['forward_remote', '远程 DNS'], ['forward_fallback', '备用 DNS']])('summarizes %s', (tag, expected) => {
    expect(label(...run(tag))).toBe(expected)
  })
  it('uses the remote or fallback path after a local probe', () => {
    expect(label(...run('forward_local'), ...run('qs.exec.main.1.drop_resp'), ...run('forward_remote'))).toBe('远程 DNS')
    expect(label(...run('forward_remote', 'error'), ...run('forward_fallback'))).toBe('备用 DNS')
  })
  it('recognizes a short-circuit cache hit but not a miss wrapping a downstream stop', () => {
    expect(label(...run('lazy_cache', 'stop'))).toBe('缓存')
    expect(label(step('lazy_cache', 'entered'), ...run('forward_local'), step('accept', 'stop', 'builtin'), step('lazy_cache', 'stop'))).toBe('本地 DNS')
    expect(label(step('lazy_cache', 'entered'), step('lazy_cache', 'next'))).toBe('未识别')
  })
  it('recognizes a probe-cache hit and lets later remote forwarding replace it', () => {
    expect(label(...probeHit)).toBe('缓存')
    expect(label(...probeHit, ...run('qs.exec.main.1.drop_resp'), ...run('forward_remote'))).toBe('远程 DNS')
    expect(label(step('local_probe_cache', 'entered', 'executor', 'local_probe'),
      ...run('forward_local', 'next', 'local_probe'), step('local_probe_cache', 'return', 'executor', 'local_probe'))).toBe('本地 DNS')
  })
  it('does not claim success from an entry, a failed forward, a matcher or an unmatched completion', () => {
    expect(label(step('forward_remote', 'entered'))).toBe('未识别')
    expect(label(...run('forward_remote', 'error'))).toBe('未识别')
    expect(label(step('forward_remote', 'matched', 'matcher'))).toBe('未识别')
    expect(label(step('forward_remote', 'next'))).toBe('未识别')
    expect(label(...run('forward_custom'))).toBe('未识别')
  })
  it('does not confuse identical plugin tags in different sequences', () => {
    expect(label(step('lazy_cache', 'entered', 'executor', 'one'), step('lazy_cache', 'stop', 'executor', 'two'))).toBe('未识别')
  })
  it('clears discarded responses and distinguishes local handling from upstream resolution', () => {
    expect(label(...run('forward_local'), ...run('qs.exec.main.1.drop_resp'))).toBe('未识别')
    expect(label(...run('forward_local'), step('reject', 'stop', 'builtin'))).toBe('规则拒绝')
    // Blocked query types: the enclosing prefer_ipv4 stop follows the black_hole pair.
    expect(label(...run('qs.exec.main.4.black_hole', 'stop'), step('qs.exec.main.3.prefer_ipv4', 'stop'))).toBe('规则拒绝')
    expect(label(...run('qs.exec.main.4.black_hole'), ...run('forward_remote'))).toBe('远程 DNS')
    expect(label(...run('custom_hosts'), step('qs.match.main.1.0.has_resp', 'matched', 'matcher'))).toBe('本地 Hosts')
    expect(label(...run('custom_hosts'), step('qs.match.main.1.0.has_resp', 'not_matched', 'matcher'), ...run('forward_remote'))).toBe('远程 DNS')
    expect(querySourceLabel({ ...row, error: 'timeout', steps: run('forward_remote') })).toBe('处理错误')
  })
})

// Steps written as "sequence node kind tag outcome", as OxiDNS 1.6.1 records them.
const trace = (...lines: string[]): Step[] => lines.map((line, event_index) => {
  const [sequence_tag, node, kind, tag, outcome] = line.split(' ')
  return { event_index, sequence_tag, node_index: Number(node), kind, tag, outcome }
})
const chain = 'cf_dns_up_8c1812f0bae3'
const pinnedHead = [`sequence_main 11 matcher qs.match.sequence_main.11.0.qname matched`,
  `${chain} 0 executor qs.exec.${chain}.0.drop_resp entered`, `${chain} 0 executor qs.exec.${chain}.0.drop_resp next`,
  `${chain} 1 executor cf_dns_cache_8c1812f0bae3 entered`]
const pinnedForward = [`${chain} 2 executor cf_dns_fwd_8c1812f0bae3 entered`, `${chain} 2 executor cf_dns_fwd_8c1812f0bae3 next`]
const pinnedTail = [`${chain} 1 executor cf_dns_cache_8c1812f0bae3 stop`, 'sequence_main 11 builtin goto stop']
const probe = ['local_probe 0 executor local_probe_cache entered', 'local_probe 1 matcher qs.match.local_probe.1.0.has_resp matched',
  'local_probe 1 executor forward_local entered', 'local_probe 1 executor forward_local next',
  'local_probe 2 builtin return return', 'local_probe 0 executor local_probe_cache return']

describe('rule DNS actions in the query source', () => {
  it('labels the rule blackhole as a rule block for domain and IP rules', () => {
    expect(label(...trace('sequence_main 10 executor cf_dns_blackhole entered', 'sequence_main 10 executor cf_dns_blackhole stop'))).toBe('规则拦截')
    expect(label(...trace(...probe, 'sequence_main 18 executor cf_dns_blackhole entered', 'sequence_main 18 executor cf_dns_blackhole stop'))).toBe('规则拦截')
    expect(label(...trace('sequence_main 10 executor cf_dns_blackhole entered', 'sequence_main 10 executor cf_dns_blackhole error'))).toBe('未识别')
  })
  it.each([
    ['a cache miss', [...pinnedHead, ...pinnedForward, `${chain} 3 matcher qs.match.${chain}.3.0.has_resp not_matched`, `${chain} 4 builtin accept stop`, ...pinnedTail], '指定上游'],
    ['its own cache hit', [...pinnedHead, ...pinnedTail], '指定上游'],
    ['a chain without cache', [...pinnedHead.slice(0, 3), ...pinnedForward, 'sequence_main 11 builtin goto stop'], '指定上游'],
    ['the chain run as an executor', ['sequence_main 11 executor cf_dns_up_8c1812f0bae3 entered', 'sequence_main 11 executor cf_dns_up_8c1812f0bae3 stop'], '指定上游'],
    ['a reject on no answer', [...pinnedHead, ...pinnedForward, `${chain} 3 matcher qs.match.${chain}.3.0.has_resp matched`, `${chain} 3 builtin reject stop`, ...pinnedTail], '规则拒绝'],
    ['a failed forward', [...pinnedHead, `${chain} 2 executor cf_dns_fwd_8c1812f0bae3 entered`, `${chain} 2 executor cf_dns_fwd_8c1812f0bae3 error`, `${chain} 1 executor cf_dns_cache_8c1812f0bae3 error`], '未识别'],
    ['a similar tag', ['sequence_main 11 executor cf_dns_upstream entered', 'sequence_main 11 executor cf_dns_upstream next'], '未识别']
  ])('summarizes a pinned upstream with %s', (_name, lines, expected) => {
    expect(label(...trace(...lines))).toBe(expected)
  })
  it('lets an IP rule after the local probe show the pinned upstream, not the local DNS', () => {
    const ip = [...probe, 'sequence_main 19 matcher qs.match.sequence_main.19.0.resp_ip matched', ...pinnedHead.slice(1)]
    expect(label(...trace(...ip, ...pinnedForward, `${chain} 4 builtin accept stop`, ...pinnedTail))).toBe('指定上游')
    expect(label(...trace(...ip, ...pinnedTail))).toBe('指定上游')
  })
  it('uses an Agent pinned summary as is', () => {
    expect(querySourceLabel({ ...row, steps: [], resolution_source: 'pinned' })).toBe('指定上游')
  })
})

describe('IPv4 preference source', () => {
  it.each(['NOERROR', 'No Error', 'noerror', '0', 0])('recognizes the reported suppressed AAAA with rcode %s', rcode => {
    expect(querySourceLabel({ ...suppressedAAAA, rcode })).toBe(preferredLabel)
  })
  it.each(['AAAA', 'aaaa', '28', 28])('accepts DNS question type %s', qtype => {
    expect(querySourceLabel({ ...suppressedAAAA, questions_json: [{ ...suppressedAAAA.questions_json[0], qtype }] })).toBe(preferredLabel)
  })
  it.each(['unknown', 'unrecorded'] as const)('refines an older %s summary only when the full trace is available', resolution_source => {
    expect(querySourceLabel({ ...suppressedAAAA, resolution_source })).toBe(preferredLabel)
    expect(querySourceLabel({ ...suppressedAAAA, resolution_source, steps: [] })).toBe(resolution_source === 'unknown' ? '未识别' : '未记录')
  })
  it('uses the Agent summary without requiring the trace', () => {
    expect(querySourceLabel({ ...suppressedAAAA, steps: undefined, resolution_source: 'ipv4_preferred' })).toBe(preferredLabel)
    expect(querySourceLabel({ ...suppressedAAAA, resolution_source: 'remote' })).toBe('远程 DNS')
  })
  it.each([
    { questions_json: [{ name: 'example.test', qtype: 'A', qclass: 'IN' }] },
    { questions_json: [] }, { questions_json: [...suppressedAAAA.questions_json, ...suppressedAAAA.questions_json] },
    { questions_json: [{ name: 'example.test', qtype: [28], qclass: 'IN' }] },
    { rcode: 'NXDOMAIN' }, { rcode: null }, { rcode: [0] },
    { answer_count: 1 }, { answer_count: undefined }, { answer_count: null }, { answer_count: '0' },
    { steps: suppressedAAAA.steps!.slice(1) }, { steps: suppressedAAAA.steps!.slice(0, 1) },
    { steps: suppressedAAAA.steps!.map(s => ({ ...s, outcome: s.outcome === 'stop' ? 'next' : s.outcome })) },
    { steps: suppressedAAAA.steps!.map(s => ({ ...s, outcome: s.outcome === 'stop' ? 'error' : s.outcome })) },
    { steps: suppressedAAAA.steps!.map(s => ({ ...s, tag: 'custom.prefer_ipv4' })) },
    { steps: suppressedAAAA.steps!.map(s => ({ ...s, tag: 'qs.custom.sequence_main.2.prefer_ipv4' })) },
    { steps: suppressedAAAA.steps!.map(s => ({ ...s, node_index: null })) },
    { steps: suppressedAAAA.steps!.map(s => ({ ...s, node_index: 3 })) },
    { steps: [suppressedAAAA.steps![0], { ...suppressedAAAA.steps![1], sequence_tag: 'other' }] }
  ])('does not infer suppression from incomplete or contradictory evidence: %j', patch => {
    expect(querySourceLabel({ ...suppressedAAAA, ...patch } as OxiDNSQuery)).toBe('未识别')
  })
  it('keeps actual forwarding when prefer_ipv4 wraps downstream execution', () => {
    expect(querySourceLabel({ ...suppressedAAAA, steps: [suppressedAAAA.steps![0], ...run('forward_remote'), suppressedAAAA.steps![1]] })).toBe('远程 DNS')
  })
  it('does not confuse a cache miss wrapping suppression with a cache hit', () => {
    expect(querySourceLabel({ ...suppressedAAAA, steps: [step('lazy_cache', 'entered'), ...suppressedAAAA.steps!, step('lazy_cache', 'stop')] })).toBe(preferredLabel)
    expect(querySourceLabel({ ...suppressedAAAA, steps: run('lazy_cache', 'stop') })).toBe('缓存')
  })
  it('still reports errors, missing responses, discarded responses and later forwards accurately', () => {
    expect(querySourceLabel({ ...suppressedAAAA, error: 'timeout' })).toBe('处理错误')
    expect(querySourceLabel({ ...suppressedAAAA, has_response: false })).toBe('无应答')
    expect(querySourceLabel({ ...suppressedAAAA, steps: [...suppressedAAAA.steps!, ...run('qs.exec.main.1.drop_resp')] })).toBe('未识别')
    expect(querySourceLabel({ ...suppressedAAAA, steps: [...suppressedAAAA.steps!, ...run('forward_local')] })).toBe('本地 DNS')
  })
})

let wrapper: VueWrapper | undefined
beforeEach(() => { vi.resetAllMocks(); Object.defineProperty(document, 'hidden', { configurable: true, value: false }) })
afterEach(() => { wrapper?.unmount(); wrapper = undefined })
function harness(count = 1) {
  const input = reactive({ agentId: 'one', recorder: 'queries', instanceId: 'core', data: {
    success: true, recorder: 'queries', next_cursor: null,
    records: Array.from({ length: count }, (_, i) => ({ ...row, id: i + 1 }))
  } as OxiDNSQueries | null })
  let model!: ReturnType<typeof useOxiDNSQuerySources>
  wrapper = mount(defineComponent({ setup() { model = useOxiDNSQuerySources(input); return () => null } }))
  return { input, model }
}
const detail = (id: string | number, steps = run('forward_remote'), created_at_ms = 1) => ({ data: { success: true, record: { ...row, id, created_at_ms, steps } } } as any)
function deferred() {
  let resolve!: (value: any) => void
  const promise = new Promise<any>(yes => { resolve = yes })
  return { promise, resolve }
}
describe('query list source loading', () => {
  it('displays an inline IPv4 preference summary without requesting details', async () => {
    const { input, model } = harness(0)
    const record: OxiDNSQuery = { ...suppressedAAAA, steps: undefined, resolution_source: 'ipv4_preferred' }
    input.data = { ...input.data!, records: [record] }
    await flushPromises()
    expect(model.sourceOf(record).label).toBe(preferredLabel)
    expect(oxidnsMonitorApi.query).not.toHaveBeenCalled()
  })
  it('classifies suppressed AAAA through the historical detail fallback', async () => {
    vi.mocked(oxidnsMonitorApi.query).mockResolvedValue({ data: { success: true, record: { ...suppressedAAAA, id: 1 } } } as any)
    const { input, model } = harness()
    await flushPromises()
    expect(model.sourceOf(input.data!.records[0]).label).toBe(preferredLabel)
    expect(oxidnsMonitorApi.query).toHaveBeenCalledTimes(1)
  })

  it('uses fifty Agent stream summaries without requesting any details', async () => {
    const { input, model } = harness(0)
    input.data = { ...input.data!, records: Array.from({ length: 50 }, (_, i) => ({
      ...row, id: i + 1, resolution_source: i % 2 ? 'cache' as const : 'remote' as const
    })) }
    await flushPromises()
    expect(model.sourceOf(input.data.records[0]).label).toBe('远程 DNS')
    expect(model.sourceOf(input.data.records[1]).label).toBe('缓存')
    expect(oxidnsMonitorApi.query).not.toHaveBeenCalled()
  })
  it('fetches only rows not covered by the Agent stream cache', async () => {
    vi.mocked(oxidnsMonitorApi.query).mockImplementation(async (_agent, id) => detail(id))
    const { input, model } = harness(0)
    input.data = { ...input.data!, records: [{ ...row, resolution_source: 'cache' }, { ...row, id: 2 }] }
    await flushPromises()
    expect(oxidnsMonitorApi.query).toHaveBeenCalledTimes(1)
    expect(oxidnsMonitorApi.query).toHaveBeenCalledWith('one', 2, 'queries', expect.anything())
    expect(model.sourceOf(input.data.records[0]).label).toBe('缓存')
    expect(model.sourceOf(input.data.records[1]).label).toBe('远程 DNS')
  })
  it('lets a fresh Agent summary replace an earlier detail failure', async () => {
    vi.mocked(oxidnsMonitorApi.query).mockRejectedValue(new Error('unavailable'))
    const { input, model } = harness()
    await flushPromises()
    expect(model.sourceOf(row).failed).toBe(true)
    input.data = { ...input.data!, records: [{ ...row, resolution_source: 'local' }] }
    await flushPromises()
    expect(model.sourceOf(input.data.records[0])).toEqual({ label: '本地 DNS' })
    expect(oxidnsMonitorApi.query).toHaveBeenCalledTimes(1)
  })
  it('loads at most four details concurrently and reuses summaries on list refresh', async () => {
    const pending = deferred()
    vi.mocked(oxidnsMonitorApi.query).mockImplementation(async (_agent, id) => { await pending.promise; return detail(id) })
    const { input, model } = harness(6)
    expect(oxidnsMonitorApi.query).toHaveBeenCalledTimes(4)
    expect(model.sourceOf(row).label).toBe('读取中…')
    pending.resolve(null)
    await flushPromises()
    expect(oxidnsMonitorApi.query).toHaveBeenCalledTimes(6)
    expect(model.sourceOf(row).label).toBe('远程 DNS')
    input.data = { ...input.data!, records: [...input.data!.records] }
    await flushPromises()
    expect(oxidnsMonitorApi.query).toHaveBeenCalledTimes(6)
  })
  it.each(['agentId', 'recorder', 'instanceId'] as const)('aborts and discards stale results when %s changes', async field => {
    const pending = deferred()
    vi.mocked(oxidnsMonitorApi.query).mockReturnValueOnce(pending.promise).mockResolvedValue(detail(1, run('lazy_cache', 'stop')))
    const { input, model } = harness()
    const signal = vi.mocked(oxidnsMonitorApi.query).mock.calls[0][3]?.signal
    input[field] = 'second'
    await flushPromises()
    expect(signal?.aborted).toBe(true)
    pending.resolve(detail(1))
    await flushPromises()
    expect(model.sourceOf(row).label).toBe('缓存')
  })
  it('keeps list rows on a detail failure, offers a retry, and does not repeatedly retry on refresh', async () => {
    vi.mocked(oxidnsMonitorApi.query).mockRejectedValueOnce(new Error('unreachable')).mockResolvedValue(detail(1))
    const { input, model } = harness()
    await flushPromises()
    expect(model.sourceOf(row)).toEqual({ label: '读取失败', failed: true })
    input.data = { ...input.data! }
    await flushPromises()
    expect(oxidnsMonitorApi.query).toHaveBeenCalledTimes(1)
    model.retry(row)
    await flushPromises()
    expect(model.sourceOf(row).label).toBe('远程 DNS')
  })
  it('uses inline steps without requesting details and skips failed or unanswered records', async () => {
    const { input, model } = harness(0)
    const answered = { ...row, steps: run('forward_local') }
    input.data!.records = [answered, { ...row, id: 2, has_response: false }, { ...row, id: 3, error: 'timeout' }]
    input.data = { ...input.data! }
    await flushPromises()
    expect(model.sourceOf(answered).label).toBe('本地 DNS')
    expect(oxidnsMonitorApi.query).not.toHaveBeenCalled()
  })
  it('rejects a detail whose record identity changed, and fetches a new timestamp for a reused ID', async () => {
    vi.mocked(oxidnsMonitorApi.query).mockResolvedValueOnce(detail(1, run('forward_remote'), 99)).mockResolvedValue(detail(1, run('forward_local'), 2))
    const { input, model } = harness()
    await flushPromises()
    expect(model.sourceOf(row).failed).toBe(true)
    const newRow = { ...row, created_at_ms: 2 }
    input.data = { ...input.data!, records: [newRow] }
    await flushPromises()
    expect(model.sourceOf(newRow).label).toBe('本地 DNS')
  })
  it('aborts on unmount without starting remaining queued requests', async () => {
    const pending = deferred()
    vi.mocked(oxidnsMonitorApi.query).mockReturnValue(pending.promise)
    harness(6)
    const signals = vi.mocked(oxidnsMonitorApi.query).mock.calls.map(call => call[3]?.signal)
    wrapper!.unmount(); wrapper = undefined
    pending.resolve(detail(1))
    await flushPromises()
    expect(signals.every(signal => signal?.aborted)).toBe(true)
    expect(oxidnsMonitorApi.query).toHaveBeenCalledTimes(4)
  })
})
