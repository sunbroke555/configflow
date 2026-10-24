/** Format a domain without changing exact versus suffix matching. */
export function domainRuleLine(behavior: string | undefined, kind: string, value: string): string {
  if (behavior !== 'domain') return `${kind},${value.trim()}`
  return kind === 'DOMAIN-SUFFIX' ? `+.${value.trim()}` : value.trim()
}

export interface RuleContentCheck {
  content: string
  changes: { line: number; before: string; after: string }[]
  errors: { line: number; message: string }[]
  warnings?: { line: number; message: string; before?: string }[]
}
