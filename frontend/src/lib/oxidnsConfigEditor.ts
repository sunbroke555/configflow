import { dump, JSON_SCHEMA, load } from 'js-yaml'

export type ConfigValueType = 'string' | 'number' | 'boolean' | 'null' | 'array' | 'object'
export interface ConfigEntry { id: string; key: string; value: ConfigValueNode }
export interface ConfigValueNode {
  id: string
  type: ConfigValueType
  scalar: string
  entries: ConfigEntry[]
  items: ConfigValueNode[]
}

export const configValueTypes: Array<{ value: ConfigValueType; label: string }> = [
  { value: 'string', label: '文本' }, { value: 'number', label: '数字' },
  { value: 'boolean', label: '布尔值' }, { value: 'null', label: '空值' },
  { value: 'array', label: '列表' }, { value: 'object', label: '对象' }
]
let sequence = 0
const nextId = () => `oxi-config-${++sequence}`
const forbiddenKeys = new Set(['__proto__', 'prototype', 'constructor'])
const maxDepth = 24
const maxNodes = 5000

function checkKey(key: string) {
  if (forbiddenKeys.has(key)) throw new Error(`不支持字段名「${key}」，请修改后保存。`)
}

export function valueToConfigNode(value: unknown): ConfigValueNode {
  let count = 0
  const ancestors = new Set<object>()
  const visit = (item: any, depth: number): ConfigValueNode => {
    if (depth > maxDepth || ++count > maxNodes) throw new Error('配置过于复杂，请将嵌套控制在 24 层、条目控制在 5000 项以内。')
    const node: ConfigValueNode = { id: nextId(), type: 'null', scalar: '', entries: [], items: [] }
    if (item === null) return node
    if (typeof item === 'string' || typeof item === 'boolean' || typeof item === 'number') {
      if (typeof item === 'number' && (!Number.isFinite(item) || (Number.isInteger(item) && !Number.isSafeInteger(item)))) {
        throw new Error('数字必须有限且整数不得超出安全范围，请用文本保留超大整数。')
      }
      node.type = typeof item as ConfigValueType
      node.scalar = String(item)
      return node
    }
    if (!item || typeof item !== 'object' || (!Array.isArray(item) && Object.getPrototypeOf(item) !== Object.prototype && Object.getPrototypeOf(item) !== null)) {
      throw new Error('仅支持文本、数字、布尔值、空值、列表和对象。')
    }
    if (ancestors.has(item)) throw new Error('配置含循环 YAML 引用，请展开循环引用后编辑。')
    ancestors.add(item)
    if (Array.isArray(item)) {
      node.type = 'array'
      node.items = item.map(child => visit(child, depth + 1))
    } else {
      node.type = 'object'
      node.entries = Object.entries(item).map(([key, child]) => {
        checkKey(key)
        return { id: nextId(), key, value: visit(child, depth + 1) }
      })
    }
    ancestors.delete(item)
    return node
  }
  return visit(value, 0)
}

export function defaultConfigNode(type: ConfigValueType = 'string') {
  return valueToConfigNode(({ string: '', number: 0, boolean: false, null: null, array: [], object: {} })[type])
}

export function configNodeToValue(node: ConfigValueNode): any {
  let count = 0
  const visit = (current: ConfigValueNode, path: string, depth: number): any => {
    if (depth > maxDepth || ++count > maxNodes) throw new Error('配置过于复杂，请减少嵌套或条目。')
    if (current.type === 'null') return null
    if (current.type === 'string') return current.scalar
    if (current.type === 'boolean') return current.scalar === 'true'
    if (current.type === 'number') {
      const text = current.scalar.trim()
      const number = Number(text)
      if (!/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(text) || !Number.isFinite(number)
        || (Number.isInteger(number) && !Number.isSafeInteger(number))) throw new Error(`${path}：请输入有效数字；超大整数请使用文本类型。`)
      return number
    }
    if (current.type === 'array') return current.items.map((item, index) => visit(item, `${path}[${index + 1}]`, depth + 1))
    const seen = new Set<string>()
    return Object.fromEntries(current.entries.map(entry => {
      checkKey(entry.key)
      if (seen.has(entry.key)) throw new Error(`${path}：字段名「${entry.key}」重复，请修改后保存。`)
      seen.add(entry.key)
      return [entry.key, visit(entry.value, `${path}.${entry.key}`, depth + 1)]
    }))
  }
  return visit(node, '基础配置', 0)
}

export function parseConfigDocument(text: string): ConfigValueNode {
  if (text.split('\n').every(line => !line.trim() || line.trim().startsWith('#'))) return valueToConfigNode({})
  const value = text.trim() ? load(text, { schema: JSON_SCHEMA }) : {}
  if (value === undefined) return valueToConfigNode({})
  if (value === null || !value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('基础配置扩展必须是 YAML 对象；可清空文本表示不扩展。')
  }
  return valueToConfigNode(value)
}

export function serializeConfigDocument(node: ConfigValueNode) {
  return dump(configNodeToValue(node), { schema: JSON_SCHEMA, noRefs: true, lineWidth: -1, sortKeys: false })
}

/** Apply server-side linked edits without replacing unchanged input identities. */
export function reconcileConfigNode(node: ConfigValueNode, value: any) {
  const next = valueToConfigNode(value)
  const merge = (current: ConfigValueNode, proposed: ConfigValueNode, key = '') => {
    if (current.type !== proposed.type) {
      Object.assign(current, proposed, { id: current.id })
      return
    }
    current.scalar = proposed.scalar
    if (current.type === 'object') {
      current.entries = proposed.entries.map(entry => {
        const existing = current.entries.find(item => item.key === entry.key)
        if (!existing) return entry
        merge(existing.value, entry.value, entry.key)
        return existing
      })
    } else if (current.type === 'array') {
      const tag = (item: ConfigValueNode) => item.entries.find(entry => entry.key === 'tag')?.value.scalar
      const used = new Set<ConfigValueNode>()
      current.items = proposed.items.map((item, index) => {
        const existing = key === 'plugins' && tag(item)
          ? current.items.find(candidate => !used.has(candidate) && tag(candidate) === tag(item)) : current.items[index]
        if (!existing) return item
        used.add(existing)
        merge(existing, item)
        return existing
      })
    }
  }
  merge(node, next)
}

export function appendConfigEntry(node: ConfigValueNode, prefix = 'parameter', value: unknown = '') {
  const used = new Set(node.entries.map(entry => entry.key))
  let key = prefix
  for (let index = 1; used.has(key); index++) key = `${prefix}_${index}`
  const entry = { id: nextId(), key, value: valueToConfigNode(value) }
  node.entries.push(entry)
  return entry
}
