import { compareSources, connectionMatcher, sourceLabel, type ConnectionRow } from '@/types/mihomo'

export const CONNECTION_COLUMNS = [
  { key: 'target', label: '主机' },
  { key: 'source', label: '来源' },
  { key: 'network', label: '协议' },
  { key: 'rule', label: '规则' },
  { key: 'chain', label: '链路' },
  { key: 'downSpeed', label: '下载速度' },
  { key: 'upSpeed', label: '上传速度' },
  { key: 'download', label: '下载量' },
  { key: 'upload', label: '上传量' },
  { key: 'duration', label: '时长' },
  { key: 'start', label: '开始时间' },
  { key: 'process', label: '进程' }
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
  'target', 'source', 'network', 'rule', 'chain', 'downSpeed', 'upSpeed', 'duration'
]

const LEGACY_DEFAULT_COLUMNS: ConnectionColumn[] = ['target', 'source', 'rule', 'chain', 'downSpeed', 'upSpeed', 'download', 'upload', 'duration']

/** Keep optional fields in the same compact columns instead of growing a horizontal table. */
export function connectionDisplayColumns(selected: ConnectionColumn[]) {
  const groups: Array<{ key: string; label: string; fields: ConnectionColumn[]; weight: number; numeric: boolean }> = [
    { key: 'target', label: '主机', fields: ['target', 'network', 'process'], weight: 28, numeric: false },
    { key: 'source', label: '来源设备', fields: ['source'], weight: 18, numeric: false },
    { key: 'routing', label: '规则 / 链路', fields: ['rule', 'chain'], weight: 26, numeric: false },
    { key: 'transfer', label: '实时速度', fields: ['downSpeed', 'upSpeed', 'download', 'upload'], weight: 20, numeric: true },
    { key: 'time', label: '时长', fields: ['duration', 'start'], weight: 11, numeric: true }
  ]
  return groups.map(group => {
    const fields = group.fields.filter(field => selected.includes(field))
    let label = group.label
    if (group.key === 'routing' && fields.length === 1) label = fields[0] === 'rule' ? '规则' : '链路'
    if (group.key === 'transfer') {
      const speed = fields.some(field => field === 'downSpeed' || field === 'upSpeed')
      const traffic = fields.some(field => field === 'download' || field === 'upload')
      label = speed && traffic ? '速度 / 流量' : traffic ? '流量' : '实时速度'
    }
    if (group.key === 'time') label = fields.includes('start') ? (fields.includes('duration') ? '时间' : '开始时间') : '时长'
    return { ...group, fields, label, weight: group.key === 'time' && fields.includes('start') ? 19 : group.weight }
  }).filter(group => group.fields.length)
}

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
      // Migrate the old default preset once; keep custom choices and future selections intact.
      if (stored.layoutVersion !== 2 && defaults.columns.length === LEGACY_DEFAULT_COLUMNS.length &&
        LEGACY_DEFAULT_COLUMNS.every(key => defaults.columns.includes(key))) defaults.columns = [...DEFAULT_CONNECTION_COLUMNS]
    }
  } catch { /* 禁用存储或旧数据损坏时使用默认表格。 */ }
  return defaults
}

export function saveConnectionTableSettings(settings: ConnectionTableSettings) {
  try { window.localStorage.setItem(CONNECTION_SETTINGS_KEY, JSON.stringify({ ...settings, layoutVersion: 2 })) } catch { /* 本次访问仍生效。 */ }
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
