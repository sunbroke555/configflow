import { oxidnsGlobalSchema, oxidnsPluginCatalog, type OxidnsSchema } from './oxidnsSchema'
import type { OxidnsMatchTarget } from './oxidnsMatchActions'

export interface OxidnsFlowPlugin {
  id: string; tag: string; type: string; kind: string; source: string; index: number
  value: Record<string, unknown>; editable: boolean; reason?: string; targets: OxidnsMatchTarget[]
}
export interface OxidnsFlowEntry {
  id: string; listener_id: string; listener_tag: string; listener_type: string
  listen: string; entry: string; path?: string; dynamic?: boolean
}
export interface OxidnsFlowEdge { from: string; to: string | null; target: string; path: Array<string | number> | string }
export interface OxidnsEntryFlow {
  entries: OxidnsFlowEntry[]; plugins: OxidnsFlowPlugin[]; edges: OxidnsFlowEdge[]
  globals: { source: string; value: Record<string, unknown> }
}
export type OxidnsFlowPatch = { source: string; index: number; before: Record<string, unknown>; value: Record<string, unknown> }
  | { kind: 'globals'; before: Record<string, unknown>; value: Record<string, unknown> }

export const flowClone = <T,>(value: T): T => JSON.parse(JSON.stringify(value))
export const flowObject = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value)
export const flowPluginSchema = (plugin: OxidnsFlowPlugin): OxidnsSchema => ({
  type: 'object', label: `${plugin.tag} 参数`, help: '编辑当前插件参数，保留未收录的扩展字段。名称与类型在完整参数中调整。',
  properties: { args: oxidnsPluginCatalog.find(item => item.type === plugin.type)?.args ?? { type: 'object', label: '参数', help: '按此插件的定义编辑参数。' } },
})
export const flowStepSchema = oxidnsPluginCatalog.find(item => item.type === 'sequence')!.args.items!
export { oxidnsGlobalSchema }
export const flowParameters = (plugin: OxidnsFlowPlugin) => Object.fromEntries(Object.entries(flowClone(plugin.value)).filter(([key]) => !['tag', 'type'].includes(key)))
export function flowEntryLabel(entry: OxidnsFlowEntry) {
  return `${entry.listener_tag} · ${entry.listener_type.replace('_server', '').toUpperCase()} ${entry.listen || '未设置地址'}${entry.path ? ` ${entry.path}` : ''}`
}
export function flowEntryGroups(flow: OxidnsEntryFlow) {
  const groups = new Map<string, OxidnsFlowEntry[]>()
  for (const entry of flow.entries) groups.set(entry.entry, [...(groups.get(entry.entry) ?? []), entry])
  return [...groups].map(([tag, entries]) => ({ tag, entries, plugin: flow.plugins.filter(plugin => plugin.tag === tag).length === 1 ? flow.plugins.find(plugin => plugin.tag === tag) : undefined }))
}
export function flowAffectedEntries(flow: OxidnsEntryFlow, pluginId: string) {
  const owners = new Set([pluginId])
  const queue = [pluginId]
  while (queue.length) {
    const current = queue.shift()!
    for (const edge of flow.edges) if (edge.to === current && !owners.has(edge.from)) { owners.add(edge.from); queue.push(edge.from) }
  }
  return flow.entries.filter(entry => entry.listener_id === pluginId || flow.plugins.some(plugin => plugin.tag === entry.entry && owners.has(plugin.id)))
}
export function flowStepSummary(value: unknown) {
  if (!flowObject(value)) return { condition: '原始步骤', action: JSON.stringify(value), reference: undefined }
  const conditions = Array.isArray(value.matches) ? value.matches : value.matches ? [value.matches] : []
  const action = typeof value.exec === 'string' ? value.exec : '尚未设置动作'
  const reference = /^(?:(goto|jump)\s+|\$)([^\s${}]+)$/.exec(action.trim())
  return {
    condition: conditions.length ? conditions.map(String).join(' 且 ') : '无条件执行', action,
    reference: reference ? { tag: reference[2]!, label: reference[1] === 'goto' ? '转入流程' : reference[1] === 'jump' ? '调用流程' : '调用插件' } : undefined,
  }
}
export function flowNextLabel(value: unknown) {
  if (!flowObject(value)) return '下一配置步骤'
  const exec = String(value.exec ?? '').trim()
  const ends = /^(goto\s|accept$|return$|reject(?:\s|$))/.test(exec)
  const conditional = Array.isArray(value.matches) ? value.matches.length > 0 : Boolean(value.matches)
  if (ends && conditional) return '条件不满足时继续'
  if (ends) return '当前动作结束；后续步骤不会执行'
  return '动作允许继续时进入下一步'
}
