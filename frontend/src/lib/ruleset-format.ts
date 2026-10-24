/** Recognize payload YAML without parsing or changing its original text. Mirrors the backend detector. */
export function detectRuleContentFormat(content: string): 'text' | 'yaml' {
  const probe = content.replace(/^\uFEFF/, '')
  return /^\s*(?:payload|"payload"|'payload')\s*:/m.test(probe) || probe.trimStart().startsWith('{') ? 'yaml' : 'text'
}

/** Format a domain without changing exact versus suffix matching. */
export function domainRuleLine(behavior: string | undefined, kind: string, value: string): string {
  if (behavior !== 'domain') return `${kind},${value.trim()}`
  return kind === 'DOMAIN-SUFFIX' ? `+.${value.trim()}` : value.trim()
}

export interface RuleContentCheck {
  content: string
  changes: { line: number; before: string; after: string }[]
  errors: { line: number; message: string; before?: string }[]
  warnings?: { line: number; message: string; before?: string }[]
}
