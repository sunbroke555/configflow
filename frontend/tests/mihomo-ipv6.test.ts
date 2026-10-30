import { describe, expect, it } from 'vitest'
import { parse } from 'yaml'
import { DEFAULT_MIHOMO_FAKE_IPV6_RANGE, readMihomoIPv6, setMihomoIPv6, setMihomoIPv6WithDetails } from '@/lib/mihomoIPv6'

describe('Mihomo IPv6 YAML controls', () => {
  it('shows manually written values independently without changing the document', () => {
    const source = '# core stays off\nipv6: false\ndns:\n  ipv6: true # AAAA\n'
    expect(readMihomoIPv6(source)).toMatchObject({ core: { value: false, source: 'explicit' }, dns: { value: true, source: 'explicit' }, error: '' })
  })

  it('distinguishes absent settings and core defaults from explicit false', () => {
    expect(readMihomoIPv6('mixed-port: 7890\n')).toMatchObject({ core: { value: true, source: 'default' }, dns: { value: false, source: 'default' } })
    expect(readMihomoIPv6('ipv6: false\ndns: { ipv6: false }\n')).toMatchObject({ core: { value: false, source: 'explicit' }, dns: { value: false, source: 'explicit' } })
  })

  it('changes only the chosen setting, retaining comments, quoted strings and anchors', () => {
    const source = '# manual core\nipv6: false # keep core note\nservers: &servers ["1.1.1.1"]\ndns:\n  ipv6: false # keep DNS note\n  nameserver: *servers\nsecret: "00123"\n'
    const next = setMihomoIPv6(source, 'core', true)
    expect(next).toContain('# manual core')
    expect(next).toContain('ipv6: true # keep core note')
    expect(next).toContain('ipv6: false # keep DNS note')
    expect(next).toContain('&servers')
    expect(next).toContain('*servers')
    expect(parse(next)).toEqual({ ...parse(source), ipv6: true })
    const dns = setMihomoIPv6(next, 'dns', true)
    expect(parse(dns)).toMatchObject({ ipv6: true, dns: { ipv6: true, nameserver: ['1.1.1.1'] }, secret: '00123' })
  })

  it('adds a nested DNS flag without losing existing DNS options', () => {
    const source = 'mixed-port: 7890\ndns:\n  listen: "0.0.0.0:1053"\n  nameserver: [223.5.5.5]\n'
    expect(parse(setMihomoIPv6(source, 'dns', true))).toEqual({ ...parse(source), dns: { ...parse(source).dns, ipv6: true } })
    expect(parse(setMihomoIPv6('mixed-port: 7890\n', 'dns', true))).toEqual({ 'mixed-port': 7890, dns: { ipv6: true } })
  })

  it('materializes the actual server default before editing an empty configuration', () => {
    const fallback = 'mixed-port: 7890\ndns:\n  enable: true\n  listen: "0.0.0.0:53"\n  nameserver: [223.5.5.5]\n'
    expect(readMihomoIPv6('', fallback).core).toMatchObject({ value: true, source: 'default' })
    expect(parse(setMihomoIPv6('  \n', 'core', false, fallback))).toEqual({ ...parse(fallback), ipv6: false })
    expect(() => setMihomoIPv6('', 'core', false)).toThrow()
    // A comment-only custom document is invalid in the backend, not an empty fallback.
    expect(readMihomoIPv6('# nothing\n', fallback).error).not.toBe('')
  })

  it.each(['ipv6: "true"\n', 'ipv6: ture\n', 'ipv6: 1\n', 'ipv6: null\n'])('does not silently repair malformed values: %s', source => {
    expect(readMihomoIPv6(source).core).toMatchObject({ value: null, source: 'invalid', editable: false })
    expect(() => setMihomoIPv6(source, 'core', true)).toThrow()
  })

  it.each(['[true, false]\n', 'dns: [\n', 'ipv6: true\nipv6: false\n'])('blocks automatic edits of invalid documents: %s', source => {
    expect(readMihomoIPv6(source).error).not.toBe('')
    expect(() => setMihomoIPv6(source, 'dns', true)).toThrow()
  })

  it('does not replace a non-mapping DNS configuration', () => {
    const source = 'ipv6: true\ndns: false\n'
    expect(readMihomoIPv6(source).dns.editable).toBe(false)
    expect(() => setMihomoIPv6(source, 'dns', true)).toThrow('dns 必须')
  })

  it('reads aliases without mutating their shared source', () => {
    const source = 'flag: &flag true\nipv6: *flag\nbase: &base\n  ipv6: true\ndns: *base\n'
    expect(readMihomoIPv6(source)).toMatchObject({ core: { value: true }, dns: { value: true, editable: false } })
    const next = parse(setMihomoIPv6(source, 'core', false))
    expect(next.flag).toBe(true)
    expect(next.ipv6).toBe(false)
    expect(() => setMihomoIPv6(source, 'dns', false)).toThrow('别名')
  })

  it('blocks changing an IPv6 scalar anchor referenced by another setting', () => {
    const source = 'ipv6: &flag false\nother: *flag\ndns: { ipv6: false }\n'
    expect(readMihomoIPv6(source).core).toMatchObject({ value: false, editable: false })
    expect(readMihomoIPv6(source).dns.editable).toBe(true)
    expect(() => setMihomoIPv6(source, 'core', true)).toThrow('锚点')
    const nested = 'ipv6: true\ndns:\n  ipv6: &flag false\nother: *flag\n'
    expect(readMihomoIPv6(nested).dns.editable).toBe(false)
    expect(() => setMihomoIPv6(nested, 'dns', true)).toThrow('锚点')
  })

  it('blocks changing a shared DNS mapping while allowing unreferenced anchors', () => {
    const source = 'ipv6: true\ndns: &dns\n  ipv6: false\nother: *dns\n'
    expect(readMihomoIPv6(source).dns).toMatchObject({ value: false, editable: false })
    expect(() => setMihomoIPv6(source, 'dns', true)).toThrow('锚点')
    const unused = 'ipv6: &flag false\ndns: &dns\n  ipv6: false\n'
    expect(parse(setMihomoIPv6(unused, 'core', true)).ipv6).toBe(true)
    expect(parse(setMihomoIPv6(unused, 'dns', true)).dns.ipv6).toBe(true)
  })

  it('does not replace a DNS mapping inherited from a merge with a partial override', () => {
    const source = 'base: &base\n  dns:\n    ipv6: false\n    nameserver: [223.5.5.5]\n<<: *base\n'
    expect(readMihomoIPv6(source).dns).toMatchObject({ value: false, editable: false })
    expect(() => setMihomoIPv6(source, 'dns', true)).toThrow('合并')
  })
})


describe('Mihomo IPv6 Fake-IP range auto-fill', () => {
  const fakeIP = 'ipv6: true\ndns:\n  enable: true\n  ipv6: false # keep DNS note\n  enhanced-mode: fake-ip\n  fake-ip-range: 198.18.0.1/16\n  nameserver: [223.5.5.5]\ntun:\n  enable: false\n'

  it('adds the missing range only when DNS IPv6 is enabled and leaves other settings intact', () => {
    const edit = setMihomoIPv6WithDetails(fakeIP, 'dns', true)
    expect(edit.addedFakeIPv6Range).toBe(true)
    expect(edit.notice).toContain('上级路由')
    expect(edit.notice).toContain('查看差异并发布路由')
    expect(edit.content).toContain('ipv6: true # keep DNS note')
    expect(parse(edit.content)).toEqual({ ...parse(fakeIP), dns: { ...parse(fakeIP).dns,
      ipv6: true, 'fake-ip-range6': DEFAULT_MIHOMO_FAKE_IPV6_RANGE } })
    expect(parse(edit.content).tun.enable).toBe(false)
    const repeated = setMihomoIPv6WithDetails(edit.content, 'dns', true)
    expect(repeated.addedFakeIPv6Range).toBe(false)
    expect(repeated.notice).toBe('')
    expect(repeated.content).toBe(edit.content)
  })

  it('fills the range when enabling the core activates previously configured DNS IPv6', () => {
    const source = fakeIP.replace('ipv6: true', 'ipv6: false').replace('  ipv6: false', '  ipv6: true')
    const edit = setMihomoIPv6WithDetails(source, 'core', true)
    expect(edit.addedFakeIPv6Range).toBe(true)
    expect(parse(edit.content)).toMatchObject({ ipv6: true, dns: { ipv6: true, 'fake-ip-range6': DEFAULT_MIHOMO_FAKE_IPV6_RANGE } })
  })

  it('uses the actual default template for a new empty draft', () => {
    const template = fakeIP.replace('ipv6: true\n', '')
    const edit = setMihomoIPv6WithDetails('', 'dns', true, template)
    expect(edit.addedFakeIPv6Range).toBe(true)
    expect(parse(edit.content)).toEqual({ ...parse(template), dns: { ...parse(template).dns,
      ipv6: true, 'fake-ip-range6': DEFAULT_MIHOMO_FAKE_IPV6_RANGE } })
  })

  it.each([
    ['DNS off', fakeIP.replace('enable: true', 'enable: false'), 'dns'],
    ['DNS not explicitly enabled', fakeIP.replace('  enable: true\n', ''), 'dns'],
    ['redir-host mode', fakeIP.replace('enhanced-mode: fake-ip', 'enhanced-mode: redir-host'), 'dns'],
    ['mode omitted', fakeIP.replace('  enhanced-mode: fake-ip\n', ''), 'dns'],
    ['core disabled', fakeIP.replace('ipv6: true', 'ipv6: false'), 'dns'],
    ['only IPv6 node support requested', fakeIP, 'core'],
  ] as const)('does not change the setup for %s', (_name, source, field) => {
    const edit = setMihomoIPv6WithDetails(source, field, true)
    expect(edit.addedFakeIPv6Range).toBe(false)
    expect(parse(edit.content).dns['fake-ip-range6']).toBeUndefined()
    const expected = parse(source)
    if (field === 'core') expected.ipv6 = true
    else expected.dns.ipv6 = true
    expect(parse(edit.content)).toEqual(expected)
  })

  it('preserves an existing custom range and leaves it in place when either switch is disabled', () => {
    const source = fakeIP.replace('  fake-ip-range:', '  fake-ip-range6: fd12:3456:789a::1/64 # custom subnet\n  fake-ip-range:')
    const enabled = setMihomoIPv6WithDetails(source, 'dns', true)
    expect(enabled.addedFakeIPv6Range).toBe(false)
    expect(enabled.content).toContain('fake-ip-range6: fd12:3456:789a::1/64 # custom subnet')
    for (const field of ['core', 'dns'] as const) {
      const edit = setMihomoIPv6WithDetails(enabled.content, field, false)
      expect(parse(edit.content).dns['fake-ip-range6']).toBe('fd12:3456:789a::1/64')
      expect(edit.addedFakeIPv6Range).toBe(false)
      expect(edit.notice).toBe('')
    }
  })

  it.each(['null', '""', 'false', '123'])('does not overwrite an explicitly malformed range (%s)', value => {
    const source = fakeIP.replace('  fake-ip-range:', `  fake-ip-range6: ${value}\n  fake-ip-range:`)
    const edit = setMihomoIPv6WithDetails(source, 'dns', true)
    expect(edit.addedFakeIPv6Range).toBe(false)
    expect(edit.notice).toContain('未自动覆盖')
    expect(parse(edit.content).dns['fake-ip-range6']).toEqual(parse(source).dns['fake-ip-range6'])
  })

  it('keeps inherited custom ranges and aliases rather than masking them with defaults', () => {
    const source = 'dns-base: &base\n  fake-ip-range6: fd12:3456::1/64\nrange: &range fd99::1/64\nipv6: true\ndns:\n  <<: *base\n  enable: true\n  ipv6: false\n  enhanced-mode: fake-ip\n'
    const inherited = setMihomoIPv6WithDetails(source, 'dns', true)
    expect(inherited.addedFakeIPv6Range).toBe(false)
    expect(inherited.content).toContain('<<: *base')
    expect(inherited.content.match(/fake-ip-range6:/g)).toHaveLength(1)
    const aliased = setMihomoIPv6WithDetails(source.replace('  <<: *base', '  fake-ip-range6: *range'), 'dns', true)
    expect(aliased.addedFakeIPv6Range).toBe(false)
    expect(aliased.content).toContain('fake-ip-range6: *range')
  })

  it.each([
    'base: &base\n  enable: true\n  ipv6: true\n  enhanced-mode: fake-ip\ndns: *base\nipv6: false\n',
    'ipv6: false\ndns: &dns\n  enable: true\n  ipv6: true\n  enhanced-mode: fake-ip\nother: *dns\n',
    'base: &base\n  dns:\n    enable: true\n    ipv6: true\n    enhanced-mode: fake-ip\n<<: *base\nipv6: false\n',
  ])('changes only the core when the missing range belongs to shared or inherited DNS', source => {
    const edit = setMihomoIPv6WithDetails(source, 'core', true)
    expect(edit.addedFakeIPv6Range).toBe(false)
    expect(edit.notice).toContain('未自动补充')
    expect(edit.notice).toContain('fake-ip-range6')
    expect(edit.content).not.toContain('fake-ip-range6:')
    expect(edit.content).toContain('ipv6: true')
  })

  it('can add a local range next to inherited DNS options without modifying the source anchor', () => {
    const source = 'base: &base\n  enable: true\n  enhanced-mode: fake-ip\ndns:\n  <<: *base\n  ipv6: false\n'
    const edit = setMihomoIPv6WithDetails(source, 'dns', true)
    expect(edit.addedFakeIPv6Range).toBe(true)
    expect(edit.content).toContain('<<: *base')
    expect(parse(edit.content).base).toEqual(parse(source).base)
    expect(parse(edit.content).dns['fake-ip-range6']).toBe(DEFAULT_MIHOMO_FAKE_IPV6_RANGE)
  })
})
