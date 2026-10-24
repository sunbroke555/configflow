import api from '@/api'
import { confirm, notify } from '@/lib/feedback'
import type { RuleContentCheck } from '@/lib/ruleset-format'

/** Validate handwritten content and ask before applying format conversions. */
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
  const warnings = data.warnings || []
  if (!data.changes.length && !warnings.length) return data.content
  const preview = data.changes.slice(0, 6).map(change => `第 ${change.line} 行：${change.before} → ${change.after}`).join('\n')
  const remaining = data.changes.length > 6 ? `\n另有 ${data.changes.length - 6} 条同类转换。` : ''
  const warningPreview = warnings.slice(0, 6).map(warning => `${warning.line ? `第 ${warning.line} 行：` : ''}${warning.before ? `${warning.before} — ` : ''}${warning.message}`).join('\n')
  const remainingWarnings = warnings.length > 6 ? `\n另有 ${warnings.length - 6} 条需检查。` : ''
  const warningMessage = warnings.length
    ? `以下 ${warnings.length} 条内容将保留原样，Mihomo 可能跳过这些规则，请检查并手动调整：\n${warningPreview}${remainingWarnings}`
    : ''
  const classical = rule.behavior === 'classical'
  const description = classical
    ? `发现 ${data.changes.length} 条未带规则类型的域名或 IP。将补充 Classical 类型，裸域名精确匹配，+. 域名包含子域名，IP 不触发域名解析。`
    : `发现 ${data.changes.length} 条 Classical 写法。转换后匹配范围不变，已有裸域名和通配符保留。`
  const message = [data.changes.length ? `${description}\n\n${preview}${remaining}` : '', warningMessage].filter(Boolean).join('\n\n')
  const accepted = await confirm(message, {
    title: data.changes.length ? `转换为 ${classical ? 'Classical' : 'Domain'} 格式` : '检查 Classical 写法',
    confirmText: data.changes.length ? '转换并保存' : '按原样保存', cancelText: '返回编辑'
  })
  return accepted ? data.content : null
}
