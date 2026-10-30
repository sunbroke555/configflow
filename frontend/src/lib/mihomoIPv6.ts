import { isAlias, isMap, isScalar, parseDocument, visit } from 'yaml'

export type MihomoIPv6Field = 'core' | 'dns'
export interface MihomoIPv6Value {
  value: boolean | null
  source: 'explicit' | 'default' | 'invalid'
  editable: boolean
  message: string
}
export interface MihomoIPv6Config {
  core: MihomoIPv6Value
  dns: MihomoIPv6Value
  error: string
}

const invalid = (message: string): MihomoIPv6Value => ({ value: null, source: 'invalid', editable: false, message })
const pathFor = (field: MihomoIPv6Field) => field === 'core' ? ['ipv6'] : ['dns', 'ipv6']

function parse(source: string) {
  const document = parseDocument(source, { merge: true, uniqueKeys: true })
  if (document.errors.length) throw new Error(`YAML 无法解析：${document.errors[0].message}`)
  if (document.contents === null) throw new Error('当前没有可编辑的基础配置，请重新读取默认模板或在 YAML 中填写配置。')
  if (!isMap(document.contents)) throw new Error('基础配置必须是 YAML 对象，请在下方修正。')
  return document
}

export function readMihomoIPv6(source: string, defaultSource?: string): MihomoIPv6Config {
  try {
    const document = parse(source.trim() ? source : defaultSource || '')
    // Resolve aliases for display without ever writing through them.
    const values = document.toJS({ maxAliasCount: 100 }) as Record<string, unknown>
    const shared = new Set<unknown>()
    visit(document, { Alias(_key, alias) { shared.add(alias.resolve(document)) } })
    const read = (field: MihomoIPv6Field): MihomoIPv6Value => {
      const parent = field === 'core' ? values : values.dns
      if (field === 'dns' && parent !== undefined && (parent === null || typeof parent !== 'object' || Array.isArray(parent))) {
        return invalid('dns 必须是 YAML 对象，请先修正 DNS 配置。')
      }
      const value = (parent as Record<string, unknown> | undefined)?.ipv6
      if (value !== undefined && typeof value !== 'boolean') return invalid(`${field === 'dns' ? 'dns 下的 ' : ''}ipv6 只能填写 true 或 false，不要加引号。`)
      const node = field === 'core' ? document.contents : document.getIn(['dns'], true)
      const target = document.getIn(pathFor(field), true)
      const sharedAnchor = shared.has(document.contents) || (node !== undefined && shared.has(node))
        || (target !== undefined && shared.has(target))
      const inheritedDNS = field === 'dns' && node === undefined && parent !== undefined
      const editable = !sharedAnchor && !inheritedDNS && (field === 'core' || node === undefined || isMap(node))
      return {
        value: value === undefined ? field === 'core' : value,
        source: value === undefined ? 'default' : 'explicit',
        editable,
        message: sharedAnchor ? '此设置的 YAML 锚点被其他配置引用，请在下方 YAML 中修改。'
          : inheritedDNS ? 'DNS 来自 YAML 合并配置，请在下方 YAML 中修改。'
          : !editable ? 'DNS 使用 YAML 别名，请在下方 YAML 中修改。'
          : value === undefined ? `未填写，沿用 Mihomo 默认${field === 'core' ? '开启' : '关闭'}` : `YAML 已${value ? '开启' : '关闭'}`,
      }
    }
    return { core: read('core'), dns: read('dns'), error: '' }
  } catch (failure) {
    const message = failure instanceof Error ? failure.message : '无法读取 IPv6 设置，请检查 YAML。'
    return { core: invalid(message), dns: invalid(message), error: message }
  }
}

/** Modify only the chosen YAML node; preserve comments, anchors and other fields. */
export function setMihomoIPv6(source: string, field: MihomoIPv6Field, enabled: boolean, defaultSource?: string): string {
  const state = readMihomoIPv6(source, defaultSource)
  if (!state[field].editable) throw new Error(state[field].message)
  const document = parse(source.trim() ? source : defaultSource || '')
  const path = pathFor(field)
  const previous = document.getIn(path, true)
  if (isScalar(previous) && !isAlias(previous)) previous.value = enabled
  else document.setIn(path, enabled)
  return document.toString({ lineWidth: 0 })
}
