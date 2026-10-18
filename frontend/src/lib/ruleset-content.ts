import api from '@/api'
import { confirm, notify } from '@/lib/feedback'
import type { RuleContentCheck } from '@/lib/ruleset-format'

/** Validate a draft and ask before replacing classical domain prefixes. */
export async function prepareRuleContent(rule: { behavior?: string; content?: string; format?: string }, appendLines?: string[]): Promise<string | null> {
  const content = rule.content || ''
  if (rule.behavior !== 'domain' && !appendLines) return content
  const { data } = await api.post<RuleContentCheck>('/rule-library/check-content', {
    behavior: rule.behavior, content, format: rule.format, append_lines: appendLines || []
  })
  if (data.errors.length) {
    const error = data.errors[0]!
    notify.warning(`${error.line ? `第 ${error.line} 行：` : ''}${error.message}`)
    return null
  }
  if (!data.changes.length) return data.content
  const preview = data.changes.slice(0, 6).map(change => `第 ${change.line} 行：${change.before} → ${change.after}`).join('\n')
  const remaining = data.changes.length > 6 ? `\n另有 ${data.changes.length - 6} 条同类转换。` : ''
  const accepted = await confirm(`发现 ${data.changes.length} 条 Classical 写法。转换后匹配范围不变，已有裸域名和通配符保留。\n\n${preview}${remaining}`, {
    title: '转换为 Domain 格式', confirmText: '转换并保存', cancelText: '返回编辑'
  })
  return accepted ? data.content : null
}
