import { isAlias, isMap, isScalar, parseDocument, visit } from 'yaml'

export type MihomoIPv6Field = 'core' | 'dns'
export const DEFAULT_MIHOMO_FAKE_IPV6_RANGE = 'fc00::1/64'
export interface MihomoIPv6Edit {
  content: string
  addedFakeIPv6Range: boolean
  notice: string
}
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

/** Edit the selected flag and fill a missing Fake-IP IPv6 range when it becomes usable. */
export function setMihomoIPv6WithDetails(source: string, field: MihomoIPv6Field, enabled: boolean, defaultSource?: string): MihomoIPv6Edit {
  const state = readMihomoIPv6(source, defaultSource)
  if (!state[field].editable) throw new Error(state[field].message)
  const document = parse(source.trim() ? source : defaultSource || '')
  const path = pathFor(field)
  const previous = document.getIn(path, true)
  if (isScalar(previous) && !isAlias(previous)) previous.value = enabled
  else document.setIn(path, enabled)

  let addedFakeIPv6Range = false
  let notice = ''
  // Read the effective values so inherited or aliased custom ranges remain intact.
  // Auto-fill never turns on DNS/TUN, changes DNS mode, or writes shared mappings.
  const values = document.toJS({ maxAliasCount: 100 }) as Record<string, unknown>
  const dns = values.dns
  if (enabled && (values.ipv6 === undefined || values.ipv6 === true)
    && dns !== null && typeof dns === 'object' && !Array.isArray(dns)) {
    const settings = dns as Record<string, unknown>
    if (settings.enable === true && settings.ipv6 === true && settings['enhanced-mode'] === 'fake-ip') {
      if (Object.prototype.hasOwnProperty.call(settings, 'fake-ip-range6')) {
        if (typeof settings['fake-ip-range6'] !== 'string' || !settings['fake-ip-range6'].trim()) {
          notice = '已有 fake-ip-range6 的值为空或不是文本，未自动覆盖；请在 YAML 中填写有效的 IPv6 网段。'
        }
      } else if (!state.dns.editable) {
        notice = 'IPv6 开关已更新，但 DNS 使用共享或继承配置，未自动补充 IPv6 Fake-IP 网段；请在 YAML 中填写 fake-ip-range6。'
      } else {
        document.setIn(['dns', 'fake-ip-range6'], DEFAULT_MIHOMO_FAKE_IPV6_RANGE)
        addedFakeIPv6Range = true
        notice = `已补充 IPv6 Fake-IP 网段 ${DEFAULT_MIHOMO_FAKE_IPV6_RANGE}；保存后到「上级路由」查看差异并发布路由。`
      }
    }
  }
  return { content: document.toString({ lineWidth: 0 }), addedFakeIPv6Range, notice }
}

/** String-only compatibility API for callers that do not display edit notices. */
export function setMihomoIPv6(source: string, field: MihomoIPv6Field, enabled: boolean, defaultSource?: string): string {
  return setMihomoIPv6WithDetails(source, field, enabled, defaultSource).content
}
