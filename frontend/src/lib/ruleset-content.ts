import api from '@/api'
import { notify } from '@/lib/feedback'
import type { RuleContentCheck } from '@/lib/ruleset-format'
export type { RuleContentCheck } from '@/lib/ruleset-format'

export const CLASSICAL_CONTENT_HINT = 'Classical 中可识别的裸域名和 IP 会在下发给 Mihomo 时自动转换，原始内容保持不变。'
  + '.x、*.x、full:x、domain:x 等写法不会自动转换，请手动调整。'

/** Validate handwritten content without converting it; appended lines come from the checked response. */
export async function prepareRuleContent(rule: { behavior?: string; content?: string; format?: string }, appendLines?: string[], onCheck?: (check: RuleContentCheck) => void): Promise<string | null> {
  const content = rule.content || ''
  const { data } = await api.post<RuleContentCheck>('/rule-library/check-content', {
    behavior: rule.behavior, content, format: rule.format, append_lines: appendLines || []
  })
  onCheck?.(data)
  if (data.errors.length) {
    const error = data.errors[0]!
    notify.warning(`${error.line ? `第 ${error.line} 行：` : ''}${error.message}`)
    return null
  }
  if (data.warnings?.length) {
    const preview = data.warnings.slice(0, 3).map(warning => `${warning.line ? `第 ${warning.line} 行：` : ''}${warning.message}`).join('\n')
    notify.warning(`规则内容有 ${data.warnings.length} 条提醒`, preview)
  }
  // Source text stays unchanged; compatible Classical lines are normalized only at Mihomo delivery.
  return data.content
}
