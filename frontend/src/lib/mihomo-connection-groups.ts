import { deviceAddress } from './device-notes'
import { compareSources, sourceLabel, type ConnectionRow } from '@/types/mihomo'
import { connectionDuration, connectionRuleLabel, connectionTypeLabel, type ConnectionColumn, type ConnectionGroupField, type ConnectionSort, type SortDirection } from './mihomo-connections'

export interface ConnectionGroup {
  id: string
  label: string
  members: ConnectionRow[]
  values: Partial<Record<ConnectionColumn, string>>
  download: number
  upload: number
  downSpeed: number
  upSpeed: number
  duration: number
  start: number
  recent: number
}

export function connectionText(row: ConnectionRow, key: ConnectionColumn, sourceName: (row: ConnectionRow) => string): string {
  switch (key) {
    case 'target': return row.target
    case 'source': return sourceName(row) || sourceLabel(row)
    case 'network': return connectionTypeLabel(row)
    case 'rule': return connectionRuleLabel(row) || '—'
    case 'chain': return row.chain || '—'
    case 'process': return row.metadata.process || row.metadata.processPath || '—'
    default: return ''
  }
}

const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' })
const textKeys = ['target', 'source', 'network', 'rule', 'chain', 'process'] as const
const sourceIdentity = (row: ConnectionRow) => {
  const source = sourceLabel(row)
  return source.includes(':') ? deviceAddress(source) || source : source
}

/** Group the complete filtered result, before pagination. Never use a device note as identity. */
export function groupConnections(rows: ConnectionRow[], field: ConnectionGroupField, sort: ConnectionSort, direction: SortDirection, now: number, sourceName: (row: ConnectionRow) => string = () => ''): ConnectionGroup[] {
  const buckets = new Map<string, ConnectionRow[]>()
  for (const row of rows) {
    const identity = field === 'source' ? sourceIdentity(row) : field === 'chain' ? JSON.stringify(row.chains) : connectionText(row, field, sourceName)
    const id = JSON.stringify([field, identity])
    const members = buckets.get(id)
    if (members) members.push(row)
    else buckets.set(id, [row])
  }
  const groups: ConnectionGroup[] = []
  for (const [id, members] of buckets) {
    const first = members[0]
    const group: ConnectionGroup = {
      id, members, label: connectionText(first, field, sourceName), values: {},
      download: 0, upload: 0, downSpeed: 0, upSpeed: 0, duration: 0, start: Infinity, recent: 0
    }
    if (field === 'source') group.label = sourceName(first) ? `${sourceName(first)} · ${sourceIdentity(first)}` : sourceIdentity(first)
    const unique = new Map(textKeys.map(key => [key, new Set<string>()]))
    for (const row of members) {
      for (const key of textKeys) unique.get(key)!.add(connectionText(row, key, sourceName))
      group.download += row.download
      group.upload += row.upload
      if (!row.closedAt) { group.downSpeed += row.downSpeed; group.upSpeed += row.upSpeed }
      group.duration = Math.max(group.duration, connectionDuration(row, now))
      const start = Date.parse(row.start)
      if (Number.isFinite(start)) group.start = Math.min(group.start, start)
      group.recent = Math.max(group.recent, row.closedAt ?? (start || 0))
    }
    for (const [key, values] of unique) group.values[key] = values.size === 1 ? [...values][0] : `${values.size} 种`
    // Identical notes on different devices must not imply one source in the summary.
    const sources = new Set(members.map(sourceIdentity))
    group.values.source = sources.size > 1 ? `${sources.size} 台设备` : sourceName(first) || sourceIdentity(first)
    groups.push(group)
  }
  const value = (group: ConnectionGroup): string | number => {
    if (sort === 'speed') return group.downSpeed + group.upSpeed
    if (sort === 'total') return group.download + group.upload
    if (sort === field) return field === 'source' ? sourceIdentity(group.members[0]) : group.label
    if (textKeys.includes(sort as typeof textKeys[number])) return group.values[sort as ConnectionColumn] || ''
    return group[sort as 'download' | 'upload' | 'downSpeed' | 'upSpeed' | 'duration' | 'start' | 'recent']
  }
  const sign = direction === 'asc' ? 1 : -1
  return groups.sort((a, b) => {
    const x = value(a), y = value(b)
    const diff = sort === 'source' && field === 'source' ? compareSources(String(x), String(y))
      : typeof x === 'number' && typeof y === 'number' ? x - y : collator.compare(String(x), String(y))
    return diff * sign || a.id.localeCompare(b.id)
  })
}
