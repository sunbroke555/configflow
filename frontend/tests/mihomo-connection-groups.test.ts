// @vitest-environment node
import { describe, expect, it } from 'vitest'
import { groupConnections } from '@/lib/mihomo-connection-groups'
import type { ConnectionRow } from '@/types/mihomo'

const now = Date.parse('2026-10-18T12:00:00Z')
function row(id: string, patch: Partial<ConnectionRow> = {}): ConnectionRow {
  const raw = { id, start: new Date(now - 60_000).toISOString(), download: 100, upload: 200, chains: ['HK', 'Proxy'], rule: 'RuleSet', rulePayload: 'test', metadata: { sourceIP: '192.168.0.101', sourcePort: '1234', destinationIP: '1.1.1.1', destinationPort: '443', host: 'example.com', network: 'tcp', type: 'Redir' } }
  return { ...raw, raw, downSpeed: 10, upSpeed: 20, target: 'example.com:443', chain: 'Proxy → HK', ...patch }
}

describe('connection grouping', () => {
  it('aggregates the entire result, including members beyond one page', () => {
    const rows = Array.from({ length: 205 }, (_, i) => row(String(i)))
    const [group] = groupConnections(rows, 'target', 'download', 'desc', now)
    expect(group.members).toHaveLength(205)
    expect(group).toMatchObject({ download: 20_500, upload: 41_000, downSpeed: 2050, upSpeed: 4100, duration: 60_000 })
  })

  it('uses source identities even when device notes match', () => {
    const a = row('a'), b = row('b')
    b.metadata = { ...b.metadata, sourceIP: '192.168.0.102' }
    const groups = groupConnections([a, b], 'source', 'source', 'asc', now, () => '电脑')
    expect(groups.map(group => group.label)).toEqual(['电脑 · 192.168.0.101', '电脑 · 192.168.0.102'])
    expect(groupConnections([a, b], 'target', 'target', 'asc', now, () => '电脑')[0].values.source).toBe('2 台设备')
  })

  it('combines equivalent IPv4-mapped source addresses', () => {
    const a = row('a'), b = row('b')
    b.metadata = { ...b.metadata, sourceIP: '::ffff:192.168.0.101' }
    const groups = groupConnections([a, b], 'source', 'source', 'asc', now)
    expect(groups).toHaveLength(1)
    expect(groups[0].label).toBe('192.168.0.101')
    expect(groups[0].values.source).toBe('192.168.0.101')
  })

  it('separates ports, inbound types, full chains and rule payloads', () => {
    const a = row('a'), b = row('b', { target: 'example.com:80', chains: ['HK', 'Other'], chain: 'Other → HK', rulePayload: 'other' })
    b.metadata = { ...b.metadata, type: 'TUN' }
    for (const field of ['target', 'network', 'chain', 'rule'] as const) expect(groupConnections([a, b], field, field, 'asc', now)).toHaveLength(2)
  })

  it('sorts by aggregate totals, zeroes closed speeds and retains closed durations', () => {
    const a = row('a', { download: 1000, downSpeed: 100 }), b = row('b', { download: 1000, downSpeed: 900, closedAt: now - 50_000 })
    const c = row('c', { target: 'other:443', download: 1500, downSpeed: 20 })
    const groups = groupConnections([a, b, c], 'target', 'download', 'desc', now)
    expect(groups.map(group => group.download)).toEqual([2000, 1500])
    expect(groups[0]).toMatchObject({ downSpeed: 100, duration: 60_000 })
    expect(groupConnections([b], 'target', 'duration', 'desc', now)[0]).toMatchObject({ downSpeed: 0, upSpeed: 0, duration: 10_000 })
    expect(groupConnections([a, b, c], 'target', 'download', 'asc', now)[0].label).toBe('other:443')
  })
})
