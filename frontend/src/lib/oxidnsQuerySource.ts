import type { OxiDNSQuery, OxiDNSQuerySource } from '@/types/oxidnsMonitor'

type Step = NonNullable<OxiDNSQuery['steps']>[number]
const forwards = new Map<string, OxiDNSQuerySource>([['forward_local', 'local'], ['forward_remote', 'remote'], ['forward_fallback', 'fallback']])
const completed = new Set(['next', 'stop', 'return'])
const sameCall = (a: Step, b: Step) => a.tag === b.tag && a.sequence_tag === b.sequence_tag && a.node_index === b.node_index
const quickPlugin = (tag: string | null, type: string) => tag?.startsWith('qs.') && tag.endsWith('.' + type)
const dnsToken = (value: unknown) => typeof value === 'string' || typeof value === 'number' ? String(value).trim().toLowerCase() : ''
const summaryLabels = new Map<OxiDNSQuerySource, string>([
  ['local', '本地 DNS'], ['remote', '远程 DNS'], ['fallback', '备用 DNS'], ['pinned', '指定上游'], ['cache', '缓存'],
  ['hosts', '本地 Hosts'], ['blocked', '规则拦截'], ['rejected', '规则拒绝'], ['unknown', '未识别'],
  ['unrecorded', '未记录'], ['error', '处理错误'], ['no_response', '无应答'],
  ['ipv4_preferred', 'IPv4 优先（抑制 AAAA）']
])
export const hasQuerySource = (record: OxiDNSQuery) => record.resolution_source !== undefined && summaryLabels.has(record.resolution_source)

/** Summarize ConfigFlow's standard routing tags, not the winning server inside a forward group. */
export function querySourceKind(record: OxiDNSQuery): OxiDNSQuerySource {
  if (record.error) return 'error'
  if (!record.has_response) return 'no_response'
  const steps = record.steps || []
  // An older Agent may not know this policy yet; a full trace can refine its
  // unknown/unrecorded summary without overriding an already identified source.
  if (hasQuerySource(record) && (!steps.length || !['unknown', 'unrecorded'].includes(record.resolution_source!))) {
    return record.resolution_source!
  }
  if (!steps.length) return 'unrecorded'
  let source: OxiDNSQuerySource = 'unknown'
  const question = record.questions_json[0]
  const qtype = dnsToken(question?.qtype)
  const rcode = dnsToken(record.rcode)
  const emptyAAAA = record.questions_json.length === 1 && ['aaaa', '28'].includes(qtype)
    && ['noerror', 'no error', '0'].includes(rcode) && record.answer_count === 0
  const calls: Array<{ step: Step; index: number }> = []
  for (const [index, step] of steps.entries()) {
    if (step.kind === 'builtin' && step.tag === 'reject' && step.outcome === 'stop') source = 'rejected'
    if (step.kind !== 'executor') continue
    if (step.outcome === 'entered') {
      calls.push({ step, index })
      continue
    }
    let callIndex = calls.length - 1
    while (callIndex >= 0 && !sameCall(calls[callIndex].step, step)) callIndex--
    if (callIndex < 0) continue
    const [call] = calls.splice(callIndex, 1)
    if (!completed.has(step.outcome)) continue
    const forward = forwards.get(step.tag || '')
    if (forward) {
      source = forward
    } else if (quickPlugin(step.tag, 'drop_resp') || step.tag === 'drop_resp') {
      source = 'unknown'
    } else if (step.tag === 'adguard_block' || step.tag === 'cf_dns_blackhole') {
      source = 'blocked'
    } else if (quickPlugin(step.tag, 'black_hole') && step.outcome === 'stop' && index === call.index + 1) {
      // Blocked query types answer with an inline black_hole; earlier drafts
      // used the builtin reject 0, labelled the same way.
      source = 'rejected'
    } else if (step.tag?.startsWith('cf_dns_fwd_')) {
      source = 'pinned'
    } else if (step.tag?.startsWith('cf_dns_up_') || step.tag?.startsWith('cf_dns_cache_')) {
      // A rule's pinned upstream chain and its own cache. Like lazy_cache, a
      // wrapping completion is not evidence: the inner forward or reject decides.
      if (index === call.index + 1) source = 'pinned'
    } else if (step.tag === 'custom_hosts') {
      const check = steps[index + 1]
      if (check?.kind === 'matcher' && quickPlugin(check.tag, 'has_resp') && check.outcome === 'matched') source = 'hosts'
    } else if (emptyAAAA && Number.isInteger(step.node_index) && step.node_index! >= 0 && step.sequence_tag
      && step.tag === `qs.exec.${step.sequence_tag}.${step.node_index}.prefer_ipv4`
      && step.outcome === 'stop' && index === call.index + 1) {
      // Suppression keeps only this call's entered/stop pair. Ordinary AAAA
      // results retain downstream steps and must keep their actual source.
      source = 'ipv4_preferred'
    } else if (step.tag === 'lazy_cache' && step.outcome === 'stop' && index === call.index + 1) {
      // A short-circuit hit has no downstream steps. A miss wraps the entire
      // remaining chain, so its trailing "stop" must not overwrite a forward.
      source = 'cache'
    } else if (step.tag === 'local_probe_cache' && step.sequence_tag === 'local_probe') {
      const inner = steps.slice(call.index + 1, index)
      // The generated probe skips !has_resp -> forward_local on a cache hit,
      // then returns. Later remote/fallback execution still replaces this label.
      if (inner.length === 2 && inner[0].kind === 'matcher' && quickPlugin(inner[0].tag, 'has_resp')
        && inner[0].outcome === 'not_matched' && inner[1].kind === 'builtin'
        && inner[1].tag === 'return' && inner[1].outcome === 'return' && step.outcome === 'return') source = 'cache'
    }
  }
  return source
}

export function querySourceLabel(record: OxiDNSQuery): string {
  return summaryLabels.get(querySourceKind(record))!
}
