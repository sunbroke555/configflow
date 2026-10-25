export interface OxidnsMatchTarget {
  tag: string
  type: string
  exec: string
  source?: string
}

export const oxidnsMatchBuiltinActions = [
  { value: 'reject', label: '拒绝（REFUSED）' },
  { value: 'reject 3', label: '不存在（NXDOMAIN）' },
  { value: 'reject 0', label: '空应答（NOERROR）' },
  { value: 'accept', label: '接受响应并结束' },
  { value: 'return', label: '返回上级序列' },
]

// Classify only exact expressions. Whitespace, jump and $sequence retain their
// original spelling and call/return semantics in the custom action editor.
export function oxidnsMatchAction(exec: unknown, targets: OxidnsMatchTarget[]): string {
  if (oxidnsMatchBuiltinActions.some(action => action.value === exec)) return String(exec)
  if (!exec || (typeof exec === 'string' && /^goto [^\s${}]+$/.test(exec))) return 'sequence'
  if (targets.some(target => target.type !== 'sequence' && target.exec === exec)) return 'plugin'
  return 'custom'
}

export function oxidnsMatchTargetWarning(exec: unknown, targets: OxidnsMatchTarget[], tags?: string[]): string {
  if (typeof exec !== 'string' || tags === undefined) return ''
  const reference = /^(?:(goto|jump)\s+|\$)([^\s${}]+)$/.exec(exec.trim())
  if (!reference) return ''
  const tag = reference[2]!
  if (!tags.includes(tag)) return `目标「${tag}」不在当前配置中，请重新选择或在完整参数中恢复。`
  const target = targets.find(target => target.tag === tag)
  if (!target || (reference[1] && target.type !== 'sequence')) {
    return `目标「${tag}」不在当前可选目标中，原动作已保留，请在完整参数中检查类型和引用。`
  }
  return ''
}
