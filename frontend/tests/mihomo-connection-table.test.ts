import { beforeEach, describe, expect, it } from 'vitest'
import {
  CONNECTION_COLUMNS, CONNECTION_SETTINGS_KEY, DEFAULT_CONNECTION_COLUMNS, connectionDisplayColumns, emptyConnectionFilters, filterConnections,
  loadConnectionTableSettings, saveConnectionTableSettings, sortConnections
} from '@/lib/mihomo-connections'
import type { ConnectionRow } from '@/types/mihomo'

const row = (id: string, overrides: Partial<ConnectionRow> = {}): ConnectionRow => {
  const connection = {
    id, upload: 100, download: 1000, start: '2026-10-18T00:00:00Z', chains: ['HK', 'Proxy'], rule: 'RuleSet', rulePayload: 'test',
    metadata: { network: 'tcp', type: 'TUN', sourceIP: '10.0.0.3', sourcePort: '80', destinationIP: '1.1.1.1', destinationPort: '443', host: 'example.com', processPath: '/bin/browser' }
  }
  return { ...connection, raw: connection, upSpeed: 2, downSpeed: 10, chain: 'Proxy → HK', target: 'example.com:443', ...overrides }
}
const ids = (rows: ConnectionRow[]) => rows.map(row => row.id)

beforeEach(() => localStorage.clear())

describe('connection table filters', () => {
  it('combines dimensions with AND and choices within each dimension with OR', () => {
    const a = row('a'), b = row('b', { chains: ['US', 'Proxy'] }), c = row('c', { rulePayload: 'other' })
    const d = row('d'); d.metadata = { ...d.metadata, sourceIP: '10.0.0.50', network: 'udp' }
    const filters = { ...emptyConnectionFilters(), sources: ['10.0.0.3', '10.0.0.50'], networks: ['TCP'], rules: ['RuleSet: test'], chains: ['HK', 'US'] }
    expect(ids(filterConnections([a, b, c, d], '', filters))).toEqual(['a', 'b'])
    expect(ids(filterConnections([a, b, c, d], '', { ...filters, networks: ['TCP', 'UDP'] }))).toEqual(['a', 'b', 'd'])
  })

  it('searches hidden metadata and excludes matching connections without treating empty hide text as a match', () => {
    const a = row('a'), b = row('b'); b.metadata = { ...b.metadata, processPath: '/bin/updater' }
    expect(ids(filterConnections([a, b], 'browser$', emptyConnectionFilters()))).toEqual(['a'])
    expect(ids(filterConnections([a, b], '', { ...emptyConnectionFilters(), exclude: 'updater$' }))).toEqual(['a'])
    expect(filterConnections([a, b], '', emptyConnectionFilters())).toHaveLength(2)
  })
})

describe('connection table sorting', () => {
  const now = Date.parse('2026-10-18T00:02:00Z')
  it('compares raw bytes, independent speeds, and source IPs rather than formatted strings', () => {
    const a = row('a', { download: 900, upSpeed: 1000 }), b = row('b', { download: 12_000, downSpeed: 200 })
    a.metadata = { ...a.metadata, sourceIP: '10.0.0.50' }
    expect(ids(sortConnections([a, b], 'download', 'desc', now))).toEqual(['b', 'a'])
    expect(ids(sortConnections([a, b], 'upSpeed', 'desc', now))).toEqual(['a', 'b'])
    expect(ids(sortConnections([a, b], 'downSpeed', 'desc', now))).toEqual(['b', 'a'])
    expect(ids(sortConnections([a, b], 'source', 'asc', now))).toEqual(['b', 'a'])
  })

  it('uses the closed duration instead of current time and keeps equal values stable', () => {
    const a = row('a', { closedAt: now - 110_000 }), b = row('b')
    expect(ids(sortConnections([a, b], 'duration', 'desc', now))).toEqual(['b', 'a'])
    expect(ids(sortConnections([b, a], 'upload', 'desc', now))).toEqual(['a', 'b'])
    expect(ids(sortConnections([b, a], 'upload', 'asc', now))).toEqual(['a', 'b'])
    expect(ids(sortConnections([a, b], 'recent', 'desc', now))).toEqual(['a', 'b'])
    expect(ids([a, b])).toEqual(['a', 'b'])
  })
})

describe('connection table preferences', () => {
  it('migrates the old default layout but preserves deliberate and custom selections', () => {
    const old = ['target', 'source', 'rule', 'chain', 'downSpeed', 'upSpeed', 'download', 'upload', 'duration']
    localStorage.setItem(CONNECTION_SETTINGS_KEY, JSON.stringify({ sort: 'upload', direction: 'asc', columns: old }))
    expect(loadConnectionTableSettings()).toEqual({ sort: 'upload', direction: 'asc', columns: DEFAULT_CONNECTION_COLUMNS })
    saveConnectionTableSettings({ sort: 'upload', direction: 'asc', columns: old as any })
    expect(loadConnectionTableSettings().columns).toEqual(old)
    localStorage.setItem(CONNECTION_SETTINGS_KEY, JSON.stringify({ columns: ['target', 'download', 'start'] }))
    expect(loadConnectionTableSettings().columns).toEqual(['target', 'download', 'start'])
  })

  it('keeps independent columns on wide screens and folds secondary fields without changing preferences', () => {
    const selected = CONNECTION_COLUMNS.map(column => column.key)
    expect(connectionDisplayColumns(selected, 2000).map(column => column.key)).toEqual(selected)
    const narrow = connectionDisplayColumns(selected, 800)
    expect(narrow.map(column => column.key)).toContain('target')
    expect(narrow.map(column => column.key)).not.toContain('process')
    expect(narrow.reduce((sum, column) => sum + column.width, 76)).toBeLessThanOrEqual(800)
    expect(connectionDisplayColumns(selected, 375).map(column => column.key)).toEqual(selected)
    expect(selected).toHaveLength(CONNECTION_COLUMNS.length)
  })

  it('upgrades the previous compact preset and validates grouping preferences', () => {
    localStorage.setItem(CONNECTION_SETTINGS_KEY, JSON.stringify({ layoutVersion: 2, columns: ['target', 'source', 'network', 'rule', 'chain', 'downSpeed', 'upSpeed', 'duration'], groupBy: 'source' }))
    expect(loadConnectionTableSettings().columns).toEqual(DEFAULT_CONNECTION_COLUMNS)
    expect(loadConnectionTableSettings().groupBy).toBe('source')
    localStorage.setItem(CONNECTION_SETTINGS_KEY, JSON.stringify({ groupBy: '__proto__' }))
    expect(loadConnectionTableSettings().groupBy).toBeUndefined()
  })

  it('validates stored fields and preserves the detail entry column', () => {
    localStorage.setItem(CONNECTION_SETTINGS_KEY, JSON.stringify({ sort: 'bad', direction: false, columns: ['upload', 'upload', 'bad'] }))
    expect(loadConnectionTableSettings()).toEqual({ sort: 'recent', direction: 'desc', columns: ['target', 'upload'] })
    localStorage.setItem(CONNECTION_SETTINGS_KEY, '{broken')
    expect(loadConnectionTableSettings().columns).toEqual(DEFAULT_CONNECTION_COLUMNS)
    localStorage.setItem(CONNECTION_SETTINGS_KEY, 'null')
    expect(loadConnectionTableSettings().sort).toBe('recent')
  })
})
