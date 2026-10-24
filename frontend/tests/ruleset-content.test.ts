import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import { prepareRuleContent } from '@/lib/ruleset-content'
import { confirmState, notify, settleConfirm } from '@/lib/feedback'
import type { RuleContentCheck } from '@/lib/ruleset-format'

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

const conversion = {
  content: 'DOMAIN-SUFFIX,tmdb.org\nDOMAIN,api.example.org\nIP-CIDR,192.0.2.1/32,no-resolve\nIP-CIDR6,2001:db8::1/128,no-resolve',
  changes: [
    { line: 1, before: '+.tmdb.org', after: 'DOMAIN-SUFFIX,tmdb.org' },
    { line: 2, before: 'api.example.org', after: 'DOMAIN,api.example.org' },
    { line: 3, before: '192.0.2.1', after: 'IP-CIDR,192.0.2.1/32,no-resolve' },
    { line: 4, before: '2001:db8::1', after: 'IP-CIDR6,2001:db8::1/128,no-resolve' }
  ],
  errors: []
}

describe('handwritten ruleset content confirmation', () => {
  it('shows the classical conversion preview and waits for explicit confirmation without mutating the draft', async () => {
    const rule = { behavior: 'classical', content: '+.tmdb.org\napi.example.org\n192.0.2.1\n2001:db8::1', format: 'text' }
    const original = { ...rule }
    respond(conversion)
    const complete = vi.fn()
    const result = prepareRuleContent(rule).then(value => { complete(value); return value })
    await flushPromises()
    expect(network.post).toHaveBeenCalledWith('/rule-library/check-content', { ...rule, append_lines: [] })
    expect(complete).not.toHaveBeenCalled()
    expect(rule).toEqual(original)
    expect(confirmState.title).toBe('转换为 Classical 格式')
    expect(confirmState.confirmText).toBe('转换并保存')
    expect(confirmState.cancelText).toBe('返回编辑')
    expect(confirmState.description).toContain('裸域名精确匹配')
    for (const change of conversion.changes) expect(confirmState.description).toContain(`${change.before} → ${change.after}`)
    settleConfirm('confirm')
    expect(await result).toBe(conversion.content)
    expect(rule).toEqual(original)
  })

  it('returns to editing without adopting conversions on cancel', async () => {
    const rule = { behavior: 'classical', content: '+.tmdb.org' }
    respond(conversion)
    const result = prepareRuleContent(rule)
    await flushPromises()
    settleConfirm('cancel')
    expect(await result).toBeNull()
    expect(rule.content).toBe('+.tmdb.org')
  })

  it('preserves unsupported lines and shows their warning beside convertible lines', async () => {
    const warning = { line: 5, before: '*.example.org', message: '无法确定 Classical 语义，请手动调整。' }
    respond({ ...conversion, content: `${conversion.content}\n*.example.org`, warnings: [warning] })
    const result = prepareRuleContent({ behavior: 'classical', content: '+.tmdb.org\n*.example.org' })
    await flushPromises()
    expect(confirmState.description).toContain('内容将保留原样')
    expect(confirmState.description).toContain('第 5 行：*.example.org')
    expect(confirmState.description).toContain(warning.message)
    expect(confirmState.confirmText).toBe('转换并保存')
    settleConfirm('confirm')
    expect(await result).toBe(`${conversion.content}\n*.example.org`)
  })

  it.each(['confirm', 'cancel'] as const)('requires an explicit choice when only ambiguous lines remain: %s', async choice => {
    const content = '.example.org\nfull:example.net'
    respond({ content, changes: [], errors: [], warnings: [{ line: 1, before: '.example.org', message: '请手动调整' }] })
    const result = prepareRuleContent({ behavior: 'classical', content })
    await flushPromises()
    expect(confirmState.title).toBe('检查 Classical 写法')
    expect(confirmState.confirmText).toBe('按原样保存')
    expect(confirmState.description).toContain('Mihomo 可能跳过这些规则')
    expect(confirmState.description).not.toContain('发现 0 条')
    settleConfirm(choice)
    expect(await result).toBe(choice === 'confirm' ? content : null)
  })

  it('keeps the existing domain conversion confirmation', async () => {
    respond({ content: '+.example.org', changes: [{ line: 1, before: 'DOMAIN-SUFFIX,example.org', after: '+.example.org' }], errors: [] })
    const result = prepareRuleContent({ behavior: 'domain', content: 'DOMAIN-SUFFIX,example.org' })
    await flushPromises()
    expect(confirmState.title).toBe('转换为 Domain 格式')
    expect(confirmState.description).toContain('转换后匹配范围不变')
    settleConfirm('confirm')
    expect(await result).toBe('+.example.org')
  })

  it('returns unchanged valid classical content without a confirmation', async () => {
    const content = '# comment\nDOMAIN,example.org\n'
    respond({ content, changes: [], errors: [] })
    expect(await prepareRuleContent({ behavior: 'classical', content })).toBe(content)
    expect(confirmState.open).toBe(false)
  })

  it('blocks malformed content while preserving the draft', async () => {
    const rule = { behavior: 'classical', format: 'yaml', content: 'payload: [' }
    respond({ content: rule.content, changes: [], errors: [{ line: 1, message: 'YAML 格式错误' }] })
    expect(await prepareRuleContent(rule)).toBeNull()
    expect(notify.warning).toHaveBeenCalledWith('第 1 行：YAML 格式错误')
    expect(rule.content).toBe('payload: [')
    expect(confirmState.open).toBe(false)
  })

  it('passes yaml and appended rules to the server without rewriting them locally', async () => {
    const content = 'payload:\n  - DOMAIN,example.org\n'
    const appended = ['IP-CIDR6,2001:db8::/32,no-resolve']
    const checked = 'payload:\n  - DOMAIN,example.org\n  - IP-CIDR6,2001:db8::/32,no-resolve\n'
    respond({ content: checked, changes: [], errors: [] })
    expect(await prepareRuleContent({ behavior: 'classical', format: 'yaml', content }, appended)).toBe(checked)
    expect(network.post).toHaveBeenCalledWith('/rule-library/check-content', {
      behavior: 'classical', format: 'yaml', content, append_lines: appended
    })
  })

  it('leaves ipcidr drafts on their existing path', async () => {
    const content = '192.0.2.0/24'
    expect(await prepareRuleContent({ behavior: 'ipcidr', content })).toBe(content)
    expect(network.post).not.toHaveBeenCalled()
    expect(confirmState.open).toBe(false)
  })
})
