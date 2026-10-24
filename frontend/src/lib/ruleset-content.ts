import api from '@/api'
import { confirm, notify } from '@/lib/feedback'
import type { RuleContentCheck } from '@/lib/ruleset-format'

export const CLASSICAL_CONTENT_HINT = 'Classical 中可识别的裸域名和 IP 会在下发给 Mihomo 时自动转换，原始内容保持不变。'
  + '.x、*.x、full:x、domain:x 等写法不会自动转换，请手动调整。'

/** Validate handwritten content; confirm only Domain format repairs. */
export async function prepareRuleContent(rule: { behavior?: string; content?: string; format?: string }, appendLines?: string[]): Promise<string | null> {
  const content = rule.content || ''
  if (rule.behavior !== 'domain' && rule.behavior !== 'classical' && !appendLines) return content
  const { data } = await api.post<RuleContentCheck>('/rule-library/check-content', {
    behavior: rule.behavior, content, format: rule.format, append_lines: appendLines || []
  })
  if (data.errors.length) {
    const error = data.errors[0]!
    notify.warning(`${error.line ? `第 ${error.line} 行：` : ''}${error.message}`)
    return null
  }
  // Classical repairs belong to Mihomo delivery. The check response retains the
  // source text, including any appended rules, and never requires confirmation.
  if (rule.behavior === 'classical' || !data.changes.length) return data.content
  const preview = data.changes.slice(0, 6).map(change => `第 ${change.line} 行：${change.before} → ${change.after}`).join('\n')
  const remaining = data.changes.length > 6 ? `\n另有 ${data.changes.length - 6} 条同类转换。` : ''
  const accepted = await confirm(`发现 ${data.changes.length} 条 Classical 写法。转换后匹配范围不变，已有裸域名和通配符保留。\n\n${preview}${remaining}`, {
    title: '转换为 Domain 格式', confirmText: '转换并保存', cancelText: '返回编辑'
  })
  return accepted ? data.content : null
}
