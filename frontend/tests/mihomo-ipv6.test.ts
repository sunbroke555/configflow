import { describe, expect, it } from 'vitest'
import { parse } from 'yaml'
import { readMihomoIPv6, setMihomoIPv6 } from '@/lib/mihomoIPv6'

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
