export interface OxidnsNativeChangeResult { native_config: string; notices?: string[] }
export interface OxidnsNativeReference { path: Array<string | number>; tag?: string; reason?: string }

// This only decides when to consult the server, not how dependencies are wired.
// Ordinary scalar typing stays local; structural edits use the same lifecycle
// implementation as the common controls.
export function needsOxidnsReconciliation(before: any, after: any): boolean {
  const empty = (value: any) => value == null || (typeof value === 'string' && !value.trim())
  const changed = (left: any, right: any, key = ''): boolean => {
    if (Array.isArray(left) || Array.isArray(right)) {
      return !Array.isArray(left) || !Array.isArray(right) || left.length !== right.length
        || left.some((item, index) => changed(item, right[index], key))
    }
    if (left && right && typeof left === 'object' && typeof right === 'object') {
      return Object.keys(left).some(key => !(key in right) || changed(left[key], right[key], key))
        || Object.keys(right).some(key => !(key in left))
    }
    return left !== right && (key === 'name' || empty(right) || empty(left) || typeof left === 'boolean' || typeof left !== typeof right)
  }
  const left = before?.plugins, right = after?.plugins
  if (!Array.isArray(left) || !Array.isArray(right)) return changed(left, right)
  if (left.length !== right.length) return true
  return left.some((plugin: any, index: number) => {
    const next = right[index]
    return plugin?.tag !== next?.tag || plugin?.type !== next?.type || changed(plugin?.args, next?.args)
  })
}
