import { describe, expect, it } from 'vitest'
import {
  DNS_COPY, chipClass, choiceLabel, defaultDnsChoice, dnsChip, dnsLabel, dnsWarnings, followRoute, parsePastedRules, ruleValueError, SUPPORTED_RULE_TYPES,
  pasteToRuleset, pinnedWithheld, publishNote, ruleSetPayload, rulePayload, splitPaste, stateValue, upstreamOptions
} from '@/lib/ruleDns'
import type { RuleDnsEngine } from '@/types'

const engine = (patch: Partial<RuleDnsEngine> = {}): RuleDnsEngine => ({
  mode: 'template', editable: true, reason: '', default_forward: 'forward_remote', prefer_ipv4: true,
  local_probe_active: false, oxidns_in_use: true, warnings: [], agents: [], ...patch
})

describe('followRoute', () => {
  it('maps DIRECT to direct DNS, groups to proxy DNS and REJECT-like policies to no special handling', () => {
    expect(followRoute({ itemType: 'rule', rule_type: 'DOMAIN-SUFFIX', policy: 'DIRECT' })).toBe('direct')
    expect(followRoute({ itemType: 'rule', rule_type: 'DOMAIN', policy: 'AI 服务' })).toBe('proxy')
    expect(followRoute({ itemType: 'ruleset', policy: 'Proxy' })).toBe('proxy')
    for (const policy of ['REJECT', 'REJECT-DROP', 'PASS', '']) expect(followRoute({ itemType: 'ruleset', policy })).toBeNull()
  })
  it('never routes single rules that cannot become DNS matches', () => {
    expect(followRoute({ itemType: 'rule', rule_type: 'GEOIP', policy: 'DIRECT' })).toBeNull()
    expect(followRoute({ itemType: 'rule', rule_type: 'IP-CIDR', policy: 'DIRECT' })).toBe('direct')
  })
})

describe('defaultDnsChoice', () => {
  it('follows the policy for domain sources', () => {
    expect(defaultDnsChoice({ domain: true, ip: false }, engine())).toBe('follow')
  })
  it('keeps the default upstream for pure IP sources and for IP parts while the local probe is off', () => {
    expect(defaultDnsChoice({ domain: false, ip: true }, engine({ local_probe_active: true }))).toBe('default')
    expect(defaultDnsChoice({ domain: true, ip: true }, engine())).toBe('default')
    expect(defaultDnsChoice({ domain: true, ip: true }, engine({ local_probe_active: true }))).toBe('follow')
  })
  it('never routes when the profile does not use OxiDNS or the state is unknown', () => {
    expect(defaultDnsChoice({ domain: true, ip: false }, engine({ oxidns_in_use: false }))).toBe('default')
    expect(defaultDnsChoice({ domain: true, ip: false }, null)).toBe('default')
  })
})

describe('rule value validation', () => {
  it.each([['DST-PORT', '80,443'], ['DOMAIN-KEYWORD', 'foo,bar']])('rejects comma-separated direct %s values: %s', (kind, value) => {
    expect(ruleValueError(kind, value)).toContain('值不能包含逗号')
  })
  it.each([
    ['DOMAIN-SHUFFIX', 'tmdb.org'], ['DOMAIN-SUFFIX', '+.tmdb.org'], ['DOMAIN', '999.1.1.1'],
    ['IP-CIDR', '192.0.2.0/024'], ['IP-CIDR6', '::/00'], ['IP-CIDR', '+.tmdb.org'], ['IP-CIDR6', ':::/64'], ['IP-CIDR', '192.0.2.1'],
    ['DST-PORT', '65536'], ['SRC-PORT', '90-80'], ['IN-PORT', 'https'], ['DSCP', '64'],
    ['NETWORK', 'icmp'], ['MATCH', 'tmdb.org'], ['DOMAIN', '']
  ])('rejects invalid %s values: %s', (kind, value) => { expect(ruleValueError(kind, value)).not.toBe('') })
  it.each([
    ['IP-CIDR', '2001:db8::/32'], ['IP-CIDR6', '192.0.2.0/24'], ['IP-CIDR6', '::ffff:192.0.2.1/128'],
    ['IN-PORT', '0/443/8000-9000/65535'], ['UID', '4294967295'], ['DSCP', '*'], ['MATCH', ''],
    ['DOMAIN', '中文.example'], ['DOMAIN-WILDCARD', '*.example.com'], ['NETWORK', 'TCP']
  ])('accepts valid %s values: %s', (kind, value) => { expect(ruleValueError(kind, value)).toBe('') })
  it('keeps the complete supported vocabulary separate from the short picker', () => {
    for (const kind of ['PROCESS-PATH-REGEX', 'SRC-GEOIP', 'REMATCH-NAME', 'SUB-RULE', 'DOMAIN-REGEX']) expect(SUPPORTED_RULE_TYPES.has(kind)).toBe(true)
  })
})

describe('parsePastedRules', () => {
  it('ignores both comment styles without losing source line numbers', () => {
    const result = parsePastedRules('// comment\n  # comment\nDOMAIN,example.com', 'DOMAIN')
    expect(result.invalid).toEqual([])
    expect(result.count).toBe(1)
    expect(result.rows).toEqual([{ line: 3, rule_type: 'DOMAIN', value: 'example.com', no_resolve: false }])
  })
  it('keeps MATCH payload empty when a pasted line includes a policy', () => {
    const result = parsePastedRules('MATCH,DIRECT', 'DOMAIN')
    expect(result.rows).toEqual([{ line: 1, rule_type: 'MATCH', value: '', no_resolve: false }])
    expect(result.ignored).toEqual([{ line: 1, value: 'DIRECT' }])
    expect(result.invalid).toEqual([])
  })
  it('rejects misspelled types while preserving full advanced rule values', () => {
    const result = parsePastedRules('DOMAIN-SHUFFIX,tmdb.org\nDOMAIN-REGEX,^(foo, bar)\nAND,((DOMAIN,x.test),(NETWORK,tcp))\nPROCESS-NAME,curl', 'DOMAIN')
    expect(result.invalid).toEqual([{ line: 1, text: 'DOMAIN-SHUFFIX,tmdb.org', message: '不支持的规则类型 DOMAIN-SHUFFIX' }])
    expect(result.rows.map(row => row.value)).toEqual(['^(foo, bar)', '((DOMAIN,x.test),(NETWORK,tcp))', 'curl'])
    expect(result.ignored).toEqual([])
  })
  it('reads prefixes, shorthand and comments, keeps no-resolve and ignores trailing policies', () => {
    const result = parsePastedRules([
      '# comment', '', 'openai.com', 'DOMAIN,api.example.org', 'IP-CIDR,1.2.3.0/24,no-resolve', '+.suffix.test',
      'full:exact.test', '*.wild.test', 'domain-keyword,ads,Proxy'
    ].join('\n'), 'DOMAIN-SUFFIX')
    expect(result.count).toBe(7)
    expect(result.rows.map(row => [row.line, row.rule_type, row.value, row.no_resolve])).toEqual([
      [3, 'DOMAIN-SUFFIX', 'openai.com', false], [4, 'DOMAIN', 'api.example.org', false], [5, 'IP-CIDR', '1.2.3.0/24', true],
      [6, 'DOMAIN-SUFFIX', 'suffix.test', false], [7, 'DOMAIN', 'exact.test', false], [8, 'DOMAIN-SUFFIX', 'wild.test', false],
      [9, 'DOMAIN-KEYWORD', 'ads', false]
    ])
    expect(result.ignored).toEqual([{ line: 9, value: 'Proxy' }])
    expect(result.notes).toEqual(['*.wild.test 将按 DOMAIN-SUFFIX 添加，也包含 wild.test 本身'])
  })
  it('reports invalid lines and adds repeated lines once', () => {
    const result = parsePastedRules('exa mple.net\nIP-CIDR,999.1.1.0/24\n10.0.0.1\nopenai.com\nopenai.com\nIP-CIDR6,2001:db8::/32', 'DOMAIN-SUFFIX')
    expect(result.invalid.map(row => [row.line, row.message])).toEqual([[1, '不是有效域名'], [2, '不是有效 IP 网段'], [3, '不是有效域名']])
    expect(result.repeated).toEqual([5])
    expect(result.rows.map(row => row.value)).toEqual(['openai.com', '2001:db8::/32'])
  })
  it('suggests a ruleset beyond 20 lines and blocks more than 200', () => {
    const lines = (count: number) => Array.from({ length: count }, (_, index) => `host${index}.example.com`).join('\n')
    expect(parsePastedRules(lines(21), 'DOMAIN').count).toBe(21)
    expect(parsePastedRules(lines(200), 'DOMAIN').overLimit).toBe(false)
    expect(parsePastedRules(lines(201), 'DOMAIN').overLimit).toBe(true)
  })
})

describe('splitPaste and pasteToRuleset', () => {
  const rows = parsePastedRules('openai.com\nIP-CIDR,1.2.3.0/24\nGEOIP,CN\nDOMAIN-KEYWORD,ads', 'DOMAIN-SUFFIX').rows
  it('splits domain, IP and unsupported lines', () => {
    const parts = splitPaste(rows)
    expect(parts.domain.map(row => row.value)).toEqual(['openai.com', 'ads'])
    expect(parts.ip.map(row => row.value)).toEqual(['1.2.3.0/24'])
    expect(parts.unsupported.map(row => row.rule_type)).toEqual(['GEOIP'])
  })
  it('keeps exact and suffix domains as a domain ruleset and anything else as classical', () => {
    expect(pasteToRuleset(parsePastedRules('openai.com\nDOMAIN,api.example.org', 'DOMAIN-SUFFIX').rows))
      .toEqual({ behavior: 'domain', content: '+.openai.com\napi.example.org' })
    expect(pasteToRuleset(parsePastedRules('openai.com\nIP-CIDR,1.2.3.0/24,no-resolve', 'DOMAIN-SUFFIX').rows))
      .toEqual({ behavior: 'classical', content: 'DOMAIN-SUFFIX,openai.com\nIP-CIDR,1.2.3.0/24,no-resolve' })
  })
})

describe('labels and chips', () => {
  it('names choices with the route a follow rule resolves to', () => {
    expect(choiceLabel('follow', { policyRoute: 'proxy' })).toBe('跟随策略 · 代理 DNS')
    expect(choiceLabel('follow', { policyRoute: null })).toBe('跟随策略 · 默认上游')
    expect(choiceLabel('default')).toBe('默认上游')
    expect(choiceLabel('upstream:b2e55ef47454', { upstreamLabel: '直连 · 223.5.5.5' })).toBe('指定上游 · 直连 · 223.5.5.5')
    expect(choiceLabel('nodata')).toBe('空应答')
  })
  it.each([
    [{ state: 'follow', route: 'direct', follow_route: 'direct' }, '跟随策略 · 直连', 'muted', undefined],
    [{ state: 'follow', route: null, follow_route: null }, '跟随策略 · 默认', 'muted', undefined],
    [{ state: 'follow_drift', route: 'proxy', follow_route: 'direct' }, '代理 · 与策略不一致', 'warn', 'drift'],
    [undefined, '默认上游 · 代理', 'muted', undefined],
    [{ state: 'explicit', route: 'direct' }, '指定 · 直连', 'neutral', undefined],
    // The 148px column shows only the address; the 直连 / 代理 source stays in the title.
    [{ state: 'explicit', route: 'upstream:b2e55ef47454', upstream_label: '直连 · 223.5.5.5' }, '223.5.5.5', 'neutral', 'upstream'],
    [{ state: 'follow_drift', route: 'upstream:b2e55ef47454', upstream_label: '代理 · tls://dns.example', follow_route: 'direct' }, 'tls://dns.example · 与策略不一致', 'warn', 'drift'],
    [{ state: 'explicit', route: 'blackhole' }, '黑洞', 'bad', undefined],
    [{ state: 'unsupported', reason: 'GEOIP 规则不参与 DNS 分流' }, '不参与', 'muted', undefined],
    [{ state: 'disabled', route: 'blackhole' }, '已停用 · 启用后为黑洞', 'muted', undefined],
    [{ state: 'disabled', route: 'direct', follow: true }, '已停用 · 启用后跟随策略', 'muted', undefined]
  ] as const)('shows %j as %s', (entry, text, tone, icon) => {
    const chip = dnsChip(entry as any, engine())
    expect(chip).toMatchObject({ text, tone })
    expect(chip.icon).toBe(icon)
  })
  it('shows the default upstream destination and the locked state', () => {
    expect(dnsChip(undefined, engine({ default_forward: 'forward_local' })).text).toBe('默认上游 · 直连')
    expect(dnsChip({ state: 'explicit', route: 'nxdomain' }, engine({ editable: false })))
      .toEqual({ text: '完整参数', tone: 'muted', icon: 'lock', title: '不存在，需在完整参数修改' })
    expect(dnsChip({ state: 'explicit', route: 'upstream:b2e55ef47454', upstream_label: '直连 · 223.5.5.5' }, engine()).title)
      .toBe('指定上游 · 直连 · 223.5.5.5')
    expect(dnsLabel({ state: 'follow', route: 'proxy', follow_route: 'proxy' }, engine())).toBe('跟随 · 代理 DNS')
  })
  it('colours only blocking actions and policy drift, never the info colour', () => {
    expect(chipClass('bad')).toBe('chip-bad')
    expect(chipClass('warn')).toBe('chip-warn')
    expect([chipClass('neutral'), chipClass('muted')].join(' ')).not.toMatch(/chip-(sky|acc|ok)/)
  })
  it('reads the selector value from a state entry', () => {
    expect(stateValue(undefined)).toBe('default')
    expect(stateValue({ state: 'follow_drift', route: 'proxy' })).toBe('follow')
    expect(stateValue({ state: 'disabled', route: 'refused' })).toBe('refused')
    expect(stateValue({ state: 'disabled', route: 'direct', follow: true })).toBe('follow')
    expect(stateValue({ state: 'unsupported' })).toBe('default')
  })
  it('keeps a pinned upstream that left the upstream list as a read-only option', () => {
    const options = upstreamOptions([{ key: 'aaaaaaaaaaaa', source: 'local', addr: 'udp://223.5.5.5', label: '直连 · 223.5.5.5', linked: false }],
      { value: 'upstream:bbbbbbbbbbbb', label: '直连 · 10.0.0.53' })
    expect(options.map(option => [option.label, option.stale])).toEqual([['直连 · 223.5.5.5', false], ['10.0.0.53（已不在上游列表）', true]])
  })
})

describe('option copy', () => {
  // Hints for refused, nxdomain, nodata and blackhole were confirmed by the user and stay as written.
  const confirmed = ['refused', 'nxdomain', 'nodata', 'blackhole']
  it.each(Object.entries(DNS_COPY))('%s stays within the copy limits', (key, copy) => {
    expect(copy.hint.length).toBeLessThanOrEqual(25)
    if (!confirmed.includes(key)) expect(copy.hint.length).toBeGreaterThanOrEqual(15)
    const [conditions, behavior] = copy.help.replace('【适用条件】\n', '').split('\n\n【工作原理】\n')
    expect(conditions!.length + behavior!.length).toBeLessThanOrEqual(100)
    expect(copy.help).toContain('【适用条件】')
    expect(copy.help).toContain('【工作原理】')
  })
  it('keeps the user-confirmed hints verbatim', () => {
    expect(DNS_COPY.refused.hint).toBe('直接拒绝查询，客户端立即失败')
    expect(DNS_COPY.nxdomain.hint).toBe('告诉客户端这个域名不存在')
    expect(DNS_COPY.nodata.hint).toBe('域名存在但没有地址，常用于屏蔽 IPv6')
    expect(DNS_COPY.blackhole.hint).toBe('返回 0.0.0.0 / ::，连接立刻失败')
  })
})

describe('dnsWarnings', () => {
  const base = { value: 'follow' as const, policy: 'Proxy', policyRoute: 'proxy' as const }
  it('orders lock, unloaded, IP, MosDNS and policy notes', () => {
    const warnings = dnsWarnings({ ...base, engine: engine({ editable: false, reason: '自定义匹配' }), unloaded: true })
    expect(warnings.map(item => item.kind)).toEqual(['locked', 'unloaded'])
    expect(warnings[0]).toMatchObject({ action: 'open-config', text: 'DNS 处理需在完整参数修改：自定义匹配' })
    const rest = dnsWarnings({ ...base, value: 'blackhole', engine: engine(), ipRouted: true, mosdns: { route: 'direct' }, explainExplicit: true })
    expect(rest.map(item => item.kind)).toEqual(['ip', 'ip', 'mosdns', 'policy'])
    expect(rest[0]!.text).toContain('保存后会开启本地探测')
    expect(rest[2]!.text).toBe('MosDNS 仍按直连 DNS处理此规则，与这里的「黑洞」不同；本字段只对 OxiDNS 生效。')
    expect(rest[3]).toMatchObject({ text: 'DNS 为单独设置的黑洞，不随策略变化', action: 'follow', actionLabel: '改为跟随策略' })
  })
  it('skips the first-IP warning once the local probe is already active', () => {
    const warnings = dnsWarnings({ ...base, engine: engine({ local_probe_active: true }), ipRouted: true })
    expect(warnings.map(item => item.text)).toEqual(['IP 规则按直连 DNS 的解析结果匹配：查询已先发给直连 DNS，命中后再替换为所选结果；IPv4 网段不影响 IPv6 查询。'])
  })
  it('does not warn about probing when the IP route equals the unmatched route', () => {
    const same = dnsWarnings({ ...base, engine: engine(), ipRouted: true })
    expect(same.map(item => [item.tone, item.text])).toEqual([['info', 'IP 规则和未匹配时的去向（代理 DNS）相同，不需要开启本地探测。']])
    const local = dnsWarnings({ ...base, value: 'direct', engine: engine({ default_forward: 'forward_local' }), ipRouted: true })
    expect(local.map(item => item.text)).toEqual(['IP 规则和未匹配时的去向（直连 DNS）相同，不需要开启本地探测。'])
    const differs = dnsWarnings({ ...base, value: 'direct', engine: engine(), ipRouted: true })
    expect(differs[0]).toMatchObject({ tone: 'warning', text: '保存后会开启本地探测：没命中域名规则的域名会先发给直连 DNS 解析一次，再用解析出的 IP 对照 IP 规则。' })
  })
  it('explains REJECT under follow and offers to resolve a drift', () => {
    expect(dnsWarnings({ ...base, engine: engine(), policy: 'REJECT', policyRoute: null }).map(item => item.text))
      .toEqual(['需要同时拦截解析请选「拒绝」或「黑洞」'])
    const drift = dnsWarnings({ ...base, engine: engine(), policy: 'DIRECT', policyRoute: 'direct', drift: { route: 'proxy' } })
    expect(drift[0]).toMatchObject({ text: '策略已是 DIRECT，DNS 仍为代理 DNS（与策略不一致）', action: 'resync', actionLabel: '按策略更新 DNS' })
  })
  it('stays quiet when MosDNS behaves the same', () => {
    expect(dnsWarnings({ ...base, engine: engine(), mosdns: { route: 'proxy' } })).toEqual([])
    expect(dnsWarnings({ ...base, value: 'default', engine: engine(), mosdns: { route: null } })).toEqual([])
  })
})

describe('publish notes and payloads', () => {
  const agent = (rules: boolean, library = false) => ({ id: 'a', name: 'a', service_type: 'oxidns', auto_sync: { rules, library } })
  it('separates auto-synced devices from those that need publishing and withholds new pinned upstreams', () => {
    expect(publishNote([agent(true), agent(false)], ['rules'])).toEqual({ text: '1 台已开启规则自动同步，将自动更新；1 台需发布后生效', publish: true })
    expect(publishNote([agent(true)], ['rules'], true)).toEqual({ text: '1 台已开启规则自动同步，将自动更新；指定上游需手动发布', publish: true })
    expect(publishNote([agent(true)], ['library'])).toEqual({ text: '1 台需发布后生效', publish: true })
    expect(publishNote([agent(false, true)], ['library'])).toEqual({ text: '1 台已开启规则自动同步，将自动更新', publish: false })
    expect(publishNote([], ['rules']).text).toBe('发布后生效')
  })
  it('asks to publish a pinned upstream only when the server withheld it from an OxiDNS auto-sync', () => {
    const mosdns = { id: 'm', name: 'm', service_type: 'mosdns', auto_sync: { rules: true, library: true } }
    // MosDNS never withholds a pin, so its auto-sync needs no manual publish.
    expect(publishNote([mosdns], ['rules'], true)).toEqual({ text: '1 台已开启规则自动同步，将自动更新', publish: false })
    // The server reports pinned_withheld; when it is false the pin is published by the auto-sync.
    expect(publishNote([agent(true)], ['rules'], pinnedWithheld({ pinned_withheld: false }, true)))
      .toEqual({ text: '1 台已开启规则自动同步，将自动更新', publish: false })
    expect(pinnedWithheld({ pinned_withheld: true }, false)).toBe(true)
    expect(pinnedWithheld({}, true)).toBe(true)
    expect(pinnedWithheld(undefined, false)).toBe(false)
  })
  it('never carries DNS or display fields in rule payloads', () => {
    const item = { id: 'r', itemType: 'ruleset', library_rule_id: 'l', name: 'N', url: 'u', policy: 'DIRECT', enabled: true, dns: 'blackhole', uniqueId: 'ruleset-r' }
    expect(ruleSetPayload(item)).not.toHaveProperty('dns')
    expect(ruleSetPayload(item)).not.toHaveProperty('name')
    expect(rulePayload({ id: 'r', rule_type: 'DOMAIN', value: 'a.test', dns: 'follow', uniqueId: 'rule-r', isExpandedGroupItem: true }))
      .toEqual({ id: 'r', rule_type: 'DOMAIN', value: 'a.test' })
  })
})
