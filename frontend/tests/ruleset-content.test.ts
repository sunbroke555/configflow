import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import { prepareRuleContent } from '@/lib/ruleset-content'
import { confirmState, notify, settleConfirm } from '@/lib/feedback'
import { detectRuleContentFormat, type RuleContentCheck } from '@/lib/ruleset-format'

const network = vi.hoisted(() => ({ post: vi.fn() }))
vi.mock('@/api', () => ({ default: { post: network.post } }))

beforeEach(() => {
  network.post.mockReset()
  vi.spyOn(notify, 'warning').mockImplementation(() => 0)
})
afterEach(() => {
  settleConfirm('cancel')
  vi.restoreAllMocks()
})

function respond(data: RuleContentCheck) {
  network.post.mockResolvedValueOnce({ data })
}

describe('content format detection', () => {
  it.each(['payload: []', '# keep\n"payload": ["example.org"]', "'payload': [example.org]", '\uFEFFpayload:\n  - example.org', '{"payload":["example.org"]}'])('detects payload YAML without rewriting: %s', content => {
    expect(detectRuleContentFormat(content)).toBe('yaml')
  })
  it.each(['DOMAIN,example.org', '// payload: is just a comment\nexample.org', '2001:db8::/32'])('detects line-based text: %s', content => {
    expect(detectRuleContentFormat(content)).toBe('text')
  })
})

describe('handwritten ruleset content checks', () => {
  it('saves classical bare domains and IPs unchanged without a conversion confirmation', async () => {
    const rule = { behavior: 'classical', content: '# keep\r\n  +.tmdb.org \r\n\r\napi.example.org\r\n192.0.2.1\r\n2001:db8::1\r\n', format: 'text' }
    const original = { ...rule }
    respond({ content: rule.content, changes: [], errors: [] })
    expect(await prepareRuleContent(rule)).toBe(rule.content)
    expect(network.post).toHaveBeenCalledWith('/rule-library/check-content', { ...rule, append_lines: [] })
    expect(rule).toEqual(original)
    expect(confirmState.open).toBe(false)
    expect(notify.warning).not.toHaveBeenCalled()
  })

  it.each(['+.tmdb.org\n*.example.org', '.example.org\nfull:example.net'])('keeps ambiguous classical lines without an extra save confirmation: %s', async content => {
    respond({ content, changes: [], errors: [], warnings: [{ line: 2, before: '*.example.org', message: '请手动调整' }] })
    expect(await prepareRuleContent({ behavior: 'classical', content })).toBe(content)
    expect(confirmState.open).toBe(false)
    expect(notify.warning).toHaveBeenCalledWith('规则内容有 1 条提醒', '第 2 行：请手动调整')
  })

  it.each(['confirm', 'cancel'] as const)('keeps the existing domain conversion confirmation: %s', async choice => {
    const rule = { behavior: 'domain', content: 'DOMAIN-SUFFIX,example.org' }
    respond({ content: '+.example.org', changes: [{ line: 1, before: 'DOMAIN-SUFFIX,example.org', after: '+.example.org' }], errors: [] })
    const result = prepareRuleContent(rule)
    await flushPromises()
    expect(confirmState.title).toBe('转换为 Domain 格式')
    expect(confirmState.description).toContain('转换后匹配范围不变')
    settleConfirm(choice)
    expect(await result).toBe(choice === 'confirm' ? '+.example.org' : null)
    expect(rule.content).toBe('DOMAIN-SUFFIX,example.org')
  })

  it('blocks malformed content while preserving the draft', async () => {
    const rule = { behavior: 'classical', format: 'yaml', content: 'payload: [' }
    respond({ content: rule.content, changes: [], errors: [{ line: 1, message: 'YAML 格式错误' }] })
    expect(await prepareRuleContent(rule)).toBeNull()
    expect(notify.warning).toHaveBeenCalledWith('第 1 行：YAML 格式错误')
    expect(rule.content).toBe('payload: [')
    expect(confirmState.open).toBe(false)
  })

  it.each(['ipcidr', 'unknown'])('returns every server issue for inline feedback and keeps %s input', async behavior => {
    const content = '+.tmdb.org\nDOMAIN-SHUFFIX,example.com'
    const checked = { content, changes: [], errors: [{ line: 1, message: '不是有效 CIDR' }, { line: 2, message: '未知规则类型' }], warnings: [] }
    respond(checked)
    const onCheck = vi.fn()
    expect(await prepareRuleContent({ behavior, content }, undefined, onCheck)).toBeNull()
    expect(onCheck).toHaveBeenCalledExactlyOnceWith(checked)
    expect(confirmState.open).toBe(false)
  })

  it('retains original bare yaml entries when adding rules, without a confirmation', async () => {
    const content = '# keep\npayload:\n  - +.tmdb.org\n  - 192.0.2.1\n'
    const appended = ['IP-CIDR6,2001:db8::/32,no-resolve']
    const checked = content + '  - "IP-CIDR6,2001:db8::/32,no-resolve"\n'
    respond({ content: checked, changes: [], errors: [] })
    expect(await prepareRuleContent({ behavior: 'classical', format: 'yaml', content }, appended)).toBe(checked)
    expect(network.post).toHaveBeenCalledWith('/rule-library/check-content', {
      behavior: 'classical', format: 'yaml', content, append_lines: appended
    })
    expect(confirmState.open).toBe(false)
  })

  it('checks ipcidr drafts through the same endpoint', async () => {
    const content = '192.0.2.0/24'
    respond({ content, changes: [], errors: [] })
    expect(await prepareRuleContent({ behavior: 'ipcidr', content })).toBe(content)
    expect(network.post).toHaveBeenCalledWith('/rule-library/check-content', { behavior: 'ipcidr', content, format: undefined, append_lines: [] })
    expect(confirmState.open).toBe(false)
  })
})
