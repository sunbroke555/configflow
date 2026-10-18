import { compareSources, connectionMatcher, sourceLabel, type ConnectionRow } from '@/types/mihomo'

export const CONNECTION_COLUMNS = [
  { key: 'target', label: '主机', width: 240 },
  { key: 'source', label: '来源', width: 152 },
  { key: 'network', label: '协议', width: 96 },
  { key: 'rule', label: '规则', width: 176 },
  { key: 'chain', label: '链路', width: 192 },
  { key: 'downSpeed', label: '下载速度', width: 120, numeric: true },
  { key: 'upSpeed', label: '上传速度', width: 120, numeric: true },
  { key: 'download', label: '下载量', width: 112, numeric: true },
  { key: 'upload', label: '上传量', width: 112, numeric: true },
  { key: 'duration', label: '时长', width: 96, numeric: true },
  { key: 'start', label: '开始时间', width: 184, numeric: true },
  { key: 'process', label: '进程', width: 176 }
] as const

export type ConnectionColumn = typeof CONNECTION_COLUMNS[number]['key']
export type ConnectionSort = ConnectionColumn | 'recent' | 'speed' | 'total'
export type SortDirection = 'asc' | 'desc'
export const CONNECTION_SORT_OPTIONS = [
  { key: 'recent', label: '最新活动' },
  ...CONNECTION_COLUMNS,
  { key: 'speed', label: '总速度' },
  { key: 'total', label: '总流量' }
] as const

export const DEFAULT_CONNECTION_COLUMNS: ConnectionColumn[] = [
  'target', 'source', 'rule', 'chain', 'downSpeed', 'upSpeed', 'download', 'upload', 'duration'
]

export interface ConnectionTableSettings {
  sort: ConnectionSort
  direction: SortDirection
  columns: ConnectionColumn[]
}
export const CONNECTION_SETTINGS_KEY = 'configflow:mihomo-panel:connection-table'

/** 仅保存表格偏好；来源、搜索等临时条件不跨设备沿用。 */
export function loadConnectionTableSettings(): ConnectionTableSettings {
  const defaults: ConnectionTableSettings = { sort: 'recent', direction: 'desc', columns: [...DEFAULT_CONNECTION_COLUMNS] }
  try {
    const stored = JSON.parse(window.localStorage.getItem(CONNECTION_SETTINGS_KEY) || 'null')
    if (!stored || typeof stored !== 'object') return defaults
    if (CONNECTION_SORT_OPTIONS.some(option => option.key === stored.sort)) defaults.sort = stored.sort
    if (stored.direction === 'asc' || stored.direction === 'desc') defaults.direction = stored.direction
    if (Array.isArray(stored.columns)) {
      // 主机始终可见，保证连接详情的键盘入口不会被隐藏。
      defaults.columns = CONNECTION_COLUMNS.filter(column => column.key === 'target' || stored.columns.includes(column.key)).map(column => column.key)
    }
  } catch { /* 禁用存储或旧数据损坏时使用默认表格。 */ }
  return defaults
}

export function saveConnectionTableSettings(settings: ConnectionTableSettings) {
  try { window.localStorage.setItem(CONNECTION_SETTINGS_KEY, JSON.stringify(settings)) } catch { /* 本次访问仍生效。 */ }
}

export interface ConnectionFilters {
  sources: string[]
  networks: string[]
  rules: string[]
  chains: string[]
  exclude: string
}
export const emptyConnectionFilters = (): ConnectionFilters => ({ sources: [], networks: [], rules: [], chains: [], exclude: '' })
export const connectionRuleLabel = (row: ConnectionRow) => row.rulePayload ? `${row.rule}: ${row.rulePayload}` : row.rule
export const connectionNetworkLabel = (row: ConnectionRow) => row.metadata.network.toUpperCase()

function searchableValues(row: ConnectionRow) {
  const meta = row.metadata
  return [row.id, row.target, sourceLabel(row), meta.sourcePort, meta.host, meta.sniffHost, meta.destinationIP,
    meta.destinationPort, meta.process, meta.processPath, meta.network, meta.type, meta.inboundName, meta.inboundUser,
    meta.remoteDestination, row.rule, row.rulePayload, connectionRuleLabel(row), row.chain]
}

/** 同一项多选为「或」，不同项之间为「且」；批量断开也复用这份结果。 */
export function filterConnections(rows: ConnectionRow[], search: string, filters: ConnectionFilters, sourceName: (row: ConnectionRow) => string = () => ''): ConnectionRow[] {
  const includes = connectionMatcher(search)
  const excludes = filters.exclude.trim() ? connectionMatcher(filters.exclude) : null
  return rows.filter(row => {
    if (filters.sources.length && !filters.sources.includes(sourceLabel(row))) return false
    if (filters.networks.length && !filters.networks.includes(connectionNetworkLabel(row))) return false
    if (filters.rules.length && !filters.rules.includes(connectionRuleLabel(row))) return false
    if (filters.chains.length && !row.chains.some(chain => filters.chains.includes(chain))) return false
    const values = [...searchableValues(row), sourceName(row)]
    return values.some(includes) && (!excludes || !values.some(excludes))
  })
}

export function connectionDuration(row: ConnectionRow, now: number): number {
  const start = Date.parse(row.start)
  return Number.isFinite(start) ? Math.max(0, (row.closedAt ?? now) - start) : 0
}

export function defaultConnectionSortDirection(key: ConnectionSort): SortDirection {
  return ['target', 'source', 'network', 'rule', 'chain', 'process'].includes(key) ? 'asc' : 'desc'
}

const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' })
export function sortConnections(rows: ConnectionRow[], key: ConnectionSort, direction: SortDirection, now: number): ConnectionRow[] {
  const value = (row: ConnectionRow): string | number => {
    switch (key) {
      case 'recent': return row.closedAt ?? (Date.parse(row.start) || 0)
      case 'start': return Date.parse(row.start) || 0
      case 'duration': return connectionDuration(row, now)
      case 'source': return sourceLabel(row)
      case 'rule': return connectionRuleLabel(row)
      case 'network': return connectionNetworkLabel(row)
      case 'process': return row.metadata.process || row.metadata.processPath || ''
      case 'speed': return row.downSpeed + row.upSpeed
      case 'total': return row.download + row.upload
      default: return row[key]
    }
  }
  const sign = direction === 'asc' ? 1 : -1
  return [...rows].sort((a, b) => {
    const x = value(a), y = value(b)
    const diff = key === 'source' ? compareSources(String(x), String(y))
      : typeof x === 'number' && typeof y === 'number' ? x - y : collator.compare(String(x), String(y))
    // 同值按 ID 固定顺序，避免核心返回顺序变化导致表格跳动。
    return diff * sign || a.id.localeCompare(b.id)
  })
}
