import { FileCode2, Globe, LayoutGrid, Monitor, MoreHorizontal } from '@lucide/vue'
import type { FlowColumn, FlowEdge, FlowNode, FlowTone } from '@/components/dashboard/FlowMap.vue'
import { formatRate } from '@/stores/live'

/**
 * Mihomo 实时流向：Agent 对 /connections 做字节差分后，按「来源 → 规则 → 策略 → 出口」聚合的路径。
 * 速率单位为字节/秒；interval 为 0 表示 Agent 刚建立基线，还没有速率。
 */
export interface LiveFlowPath {
  source: string
  rule: string
  policy: string
  outlet: string
  up: number
  down: number
  connections: number
}

export interface LiveFlowSnapshot {
  interval: number
  connections: number
  active: number
  up: number
  down: number
  paths: LiveFlowPath[]
  omitted: number
}

/** 每列最多展示的节点数，其余按列合并为「其余 N 项」 */
export const LIVE_FLOW_LIMIT = 6

const COLUMNS = [
  { key: 'source', title: '来源设备', icon: Monitor },
  { key: 'rule', title: '命中规则', icon: FileCode2 },
  { key: 'policy', title: '策略组', icon: LayoutGrid },
  { key: 'outlet', title: '出口节点', icon: Globe }
] as const
type ColumnKey = (typeof COLUMNS)[number]['key']
const EDGE_TONES: FlowTone[] = ['primary', 'kraft', 'success']

export const rateText = (bytes: number) => formatRate(bytes).join(' ')

const total = (p: { up: number; down: number }) => p.up + p.down

export interface LiveFlowGraph {
  columns: FlowColumn[]
  edges: FlowEdge[]
}

/**
 * 只用真实速率建图：没有速率的路径不会出现；连线粗细按本次最大速率取对数，避免一条大流量淹没其他链路。
 * labelOf 把来源 IP 换成设备备注；to 为节点点击后的跳转地址。
 */
export function buildLiveFlow(
  paths: LiveFlowPath[],
  { labelOf = (ip: string) => ip, to, limit = LIVE_FLOW_LIMIT }: { labelOf?: (ip: string) => string; to?: string; limit?: number } = {}
): LiveFlowGraph {
  const live = paths.filter(p => total(p) > 0)
  const kept = new Map<ColumnKey, Set<string>>()
  const columns: FlowColumn[] = COLUMNS.map(column => {
    const stats = new Map<string, { up: number; down: number; connections: number }>()
    for (const path of live) {
      const s = stats.get(path[column.key]) || { up: 0, down: 0, connections: 0 }
      s.up += path.up
      s.down += path.down
      s.connections += path.connections
      stats.set(path[column.key], s)
    }
    const ranked = [...stats.entries()].sort((a, b) => total(b[1]) - total(a[1]) || a[0].localeCompare(b[0]))
    const top = ranked.slice(0, limit)
    kept.set(column.key, new Set(top.map(([value]) => value)))
    const nodes: FlowNode[] = top.map(([value, s]) => ({
      id: `${column.key}:${value}`,
      title: (column.key === 'source' ? labelOf(value) : value) || (column.key === 'source' ? 'Inner' : '未知'),
      meta: `↓ ${rateText(s.down)} · ↑ ${rateText(s.up)} · ${s.connections} 连接`,
      icon: column.icon,
      to
    }))
    const rest = ranked.slice(limit)
    if (rest.length) {
      const sum = rest.reduce((acc, [, s]) => acc + total(s), 0)
      nodes.push({ id: `${column.key}:*`, title: `其余 ${rest.length} 项`, meta: `合计 ${rateText(sum)}`, icon: MoreHorizontal, to, muted: true })
    }
    return { key: column.key, title: column.title, nodes }
  })

  const nodeOf = (key: ColumnKey, value: string) => (kept.get(key)!.has(value) ? `${key}:${value}` : `${key}:*`)
  const sums = new Map<string, { from: string; to: string; tone: FlowTone; rate: number }>()
  for (const path of live) {
    for (let i = 0; i < COLUMNS.length - 1; i++) {
      const from = nodeOf(COLUMNS[i].key, path[COLUMNS[i].key])
      const next = nodeOf(COLUMNS[i + 1].key, path[COLUMNS[i + 1].key])
      const id = `${from}>${next}`
      const edge = sums.get(id) || { from, to: next, tone: EDGE_TONES[i], rate: 0 }
      edge.rate += total(path)
      sums.set(id, edge)
    }
  }
  const max = Math.max(1, ...[...sums.values()].map(e => e.rate))
  const edges: FlowEdge[] = [...sums.values()].map(({ from, to: target, tone, rate }) => ({
    from,
    to: target,
    tone,
    weight: 0.2 + 1.6 * (Math.log1p(rate) / Math.log1p(max))
  }))
  return { columns, edges }
}
