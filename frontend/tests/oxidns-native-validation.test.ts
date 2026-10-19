// @vitest-environment node
import { describe, expect, it } from 'vitest'
import { formatOxidnsIssuePath, readOxidnsValidationResult } from '@/lib/oxidnsNativeValidation'

const issue = { path: ['plugins', 0, 'args', 'size'], message: '容量不能为负数。', code: 'minimum', severity: 'error' as const }
describe('shared OxiDNS validation results', () => {
  it('keeps server paths, messages and source without reinterpretation', () => {
    const original = {valid: false, issues: [{...issue, source: 'plugins/cache.yaml'}]}
    expect(readOxidnsValidationResult(original)).toEqual(original)
    expect(formatOxidnsIssuePath(issue.path)).toBe('plugins[0].args.size')
    expect(formatOxidnsIssuePath([])).toBe('完整配置')
  })
  it('allows warnings and deduplicates the optional warnings list', () => {
    const warning = {...issue, severity: 'warning' as const, code: 'environment'}
    expect(readOxidnsValidationResult({valid: true, issues: [warning], warnings: [warning]})).toEqual({valid: true, issues: [warning]})
    expect(readOxidnsValidationResult({valid: true, issues: []})).toEqual({valid: true, issues: []})
  })
  it('fails closed for malformed or contradictory service responses', () => {
    for (const result of [null, {}, {valid: true}, {valid: true, issues: [{}]}, {valid: false, issues: []}, {valid: true, issues: [{...issue, path: [-1]}]}]) {
      expect(() => readOxidnsValidationResult(result)).toThrow()
    }
    expect(readOxidnsValidationResult({valid: true, issues: [issue]}).valid).toBe(false)
  })
})
