import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import type { Component } from 'vue'
import Rules from '@/views/Rules.vue'
import RuleLibrary from '@/views/RuleLibrary.vue'
import RuleDnsField from '@/components/rules/RuleDnsField.vue'
import RuleDnsSelect from '@/components/rules/RuleDnsSelect.vue'
import ConfirmHost from '@/components/feedback/ConfirmHost.vue'
import { Select } from '@/components/ui/select'
import { setActiveProfileId } from '@/profileContext'
import { confirmState, notify, settleConfirm } from '@/lib/feedback'
import { CLASSICAL_CONTENT_HINT } from '@/lib/ruleset-content'

// Replace only HTTP boundaries; dialogs, selectors, sorting and stores stay real.
const network = vi.hoisted(() => {
  const state = {
    rules: [] as Record<string, unknown>[],
    library: [] as Record<string, unknown>[]
  }
  const get = vi.fn(async (path: string, _config?: unknown) => ({
    data: path === '/rule-library' ? structuredClone(state.library)
      : path === '/rules' ? structuredClone(state.rules)
      : path === '/proxy-groups' ? [{ id: 'group-one', name: 'Target policy' }]
      : path === '/rule-library/proxy-domains' ? { proxy_domains: '' }
      : []
  }))
  const post = vi.fn(async (_path: string, _body?: Record<string, unknown>, _config?: unknown): Promise<{ data: any }> => ({ data: _path === '/rule-library/check-content'
    ? { content: String(_body?.content || '') + ((_body?.append_lines as string[] || []).length ? '\n' + (_body!.append_lines as string[]).join('\n') + '\n' : ''), changes: [], errors: [] }
    : { success: true } }))
  const put = vi.fn(async (_path: string, _body?: Record<string, unknown>, _config?: unknown) => ({ data: { success: true } }))
  const remove = vi.fn(async (_path: string) => ({ data: { success: true } }))
  const rules = {
    getAll: vi.fn(async (_profileId?: string) => ({ data: structuredClone(state.rules) })),
    create: vi.fn(async (_body: Record<string, unknown>, _profileId?: string) => ({ data: {} })),
    update: vi.fn(async (_id: string, _body: Record<string, unknown>, _profileId?: string) => ({ data: {} })),
    delete: vi.fn(async (_id: string, _profileId?: string) => ({ data: {} })),
    renameGroup: vi.fn(async (_ids: string[], _name: string, _profileId?: string) => ({ data: { success: true } })),
    findDuplicates: vi.fn(async (_profileId?: string) => ({ data: {} })),
    getDns: vi.fn(async (_profileId?: string) => ({ data: {
      success: true, upstreams: [], rules: {}, mosdns: {},
      engine: { mode: 'template', editable: true, reason: '', default_forward: 'forward_remote', prefer_ipv4: true,
        local_probe_active: false, oxidns_in_use: true, warnings: [], agents: [] }
    } })),
    setDns: vi.fn(async (_routes: Record<string, string>, _options?: unknown, _profileId?: string) => ({ data: { success: true, rules: {}, skipped: [], warnings: [] } })),
    compose: vi.fn(async (_body: Record<string, any>, _profileId?: string) => ({ data: {
      success: true, data: { rules: [{ id: 'ruleset-new' }], library: null }, dns: {}, skipped: [], warnings: [], draft: true
    } }))
  }
  const sets = {
    create: vi.fn(async (_body: Record<string, unknown>, _profileId?: string) => ({ data: {} })),
    update: vi.fn(async (_id: string, _body: Record<string, unknown>, _profileId?: string) => ({ data: {} })),
    delete: vi.fn(async (_id: string, _profileId?: string) => ({ data: {} }))
  }
  return { state, get, post, put, remove, rules, sets }
})

vi.mock('@/api', () => ({
  default: { get: network.get, post: network.post, put: network.put, delete: network.remove },
  ruleApi: network.rules,
  ruleSetApi: network.sets,
  proxyGroupApi: { getAll: vi.fn(async () => ({ data: [{ id: 'group-one', name: 'Target policy' }] })) },
  profileApi: { list: vi.fn(async () => ({ data: [{ id: 'default', name: 'Default' }, { id: 'target', name: 'Target' }] })) }
}))

const wrappers: VueWrapper[] = []
const scrollDescriptor = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollIntoView')

beforeEach(() => {
  vi.clearAllMocks()
  setActiveProfileId('default')
  network.state.library = [{
    id: 'library-one', name: 'Shared domains', source_type: 'content',
    content: 'example.com', url: '', behavior: 'domain', format: 'text', enabled: true
  }]
  network.state.rules = [{
    id: 'ruleset-one', itemType: 'ruleset', library_rule_id: 'library-one',
    name: 'Shared domains', url: '/resolved-source', source_type: 'content',
    content: 'example.com', behavior: 'domain', policy: 'DIRECT', enabled: true,
    order: 0, remark: 'Profile remark', no_resolve: false,
    formats: ['mihomo', 'surge'], mosdns: { target: 'direct' }
  }]
  // jsdom lacks these browser primitives; no production component is stubbed.
  vi.stubGlobal('matchMedia', vi.fn((query: string) => ({
    matches: false, media: query, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn()
  })))
  Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() })
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
})

afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  settleConfirm('cancel')
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  if (scrollDescriptor) Object.defineProperty(Element.prototype, 'scrollIntoView', scrollDescriptor)
  else Reflect.deleteProperty(Element.prototype, 'scrollIntoView')
  document.body.innerHTML = ''
  sessionStorage.clear()
})

async function render(component: Component) {
  const wrapper = mount(component, { attachTo: document.body })
  wrappers.push(wrapper)
  await flushPromises()
  return wrapper
}

async function clickText(text: string) {
  const button = Array.from(document.querySelectorAll('button')).find(item => item.textContent?.trim() === text)
  expect(button, `button ${text}`).toBeTruthy()
  button!.click()
  await flushPromises()
}

async function menuAction(text: string) {
  const trigger = Array.from(document.querySelectorAll('button')).find(item => item.textContent?.trim() === '更多')
  expect(trigger).toBeTruthy()
  trigger!.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))
  await flushPromises()
  const item = Array.from(document.querySelectorAll<HTMLElement>('[role="menuitem"]')).find(element => element.textContent?.trim() === text)
  expect(item, `menu item ${text}`).toBeTruthy()
  item!.click()
  await flushPromises()
}

function expectReferenceOnly(payload: unknown) {
  expect(payload).toEqual(expect.objectContaining({ itemType: 'ruleset', library_rule_id: 'library-one' }))
  for (const field of ['name', 'url', 'content', 'behavior', 'source_type', 'uniqueId']) {
    expect(payload).not.toHaveProperty(field)
  }
}

describe('profile rule references', () => {
  it('renames a whole mixed group with IDs and a label only, never stale rule fields', async () => {
    network.state.rules.push({ id: 'inline-one', itemType: 'rule', rule_type: 'DOMAIN', value: 'local.test', policy: 'DIRECT', enabled: true })
    const wrapper = await render(Rules)
    await wrapper.get('button[aria-label="重命名分组"]').trigger('click')
    await flushPromises()
    const input = document.querySelector<HTMLInputElement>('#group-rename')!
    input.value = ' New label '
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await flushPromises()
    await clickText('保存')
    expect(network.rules.renameGroup).toHaveBeenCalledExactlyOnceWith(['ruleset-one', 'inline-one'], 'New label', 'default')
    expect(network.rules.update).not.toHaveBeenCalled()
    expect(network.sets.update).not.toHaveBeenCalled()
  })

  it('edits references without copying hydrated sources and keeps independent formats', async () => {
    const wrapper = await render(Rules)
    await wrapper.get('button[aria-label="编辑"]').trigger('click')
    await flushPromises()
    expect(document.querySelector('#ruleset-url')).toBeNull()
    expect(document.querySelector('[role="dialog"]')?.textContent).toContain('example.com')
    setActiveProfileId('target')
    await clickText('保存')
    expect(network.sets.update).toHaveBeenCalledTimes(1)
    const [id, payload, profileId] = network.sets.update.mock.calls[0]!
    expect(id).toBe('ruleset-one')
    expect(profileId).toBe('default')
    expectReferenceOnly(payload)
    expect(payload).toMatchObject({ policy: 'DIRECT', enabled: true, formats: ['mihomo', 'surge'], mosdns: { target: 'direct' } })
  })

  it('status changes only the profile reference, never the shared source', async () => {
    network.state.library[0]!.enabled = false
    network.state.rules[0]!.library_enabled = false
    const wrapper = await render(Rules)
    await wrapper.get('button[aria-label="停用该项"]').trigger('click')
    await flushPromises()
    const [, payload, profileId] = network.sets.update.mock.calls[0]!
    expectReferenceOnly(payload)
    expect(payload.enabled).toBe(false)
    expect(profileId).toBe('default')
    expect(network.put).not.toHaveBeenCalled()
    expect(network.post).not.toHaveBeenCalled()
  })

  it('adds rulesets from the library tab through compose and greys out references already in use', async () => {
    network.state.library.push({ id: 'library-two', name: 'Second domains', source_type: 'url', url: 'https://rules.test/two.list', behavior: 'domain', format: 'text', enabled: true })
    await render(Rules)
    await menuAction('添加规则集')
    // The menu restores focus to its trigger on a timer; a popover opened before that closes again.
    await new Promise(resolve => setTimeout(resolve, 20))
    expect(document.querySelector('[role="radio"][aria-checked="true"]')?.textContent?.trim()).toBe('规则库')
    const save = () => Array.from(document.querySelectorAll('button')).find(button => button.textContent?.trim().startsWith('添加') && button.textContent.includes('规则集'))
    expect(save()?.disabled).toBe(true)
    document.querySelector<HTMLButtonElement>('button[aria-label="选择规则集"]')!.click()
    await flushPromises()
    const options = Array.from(document.querySelectorAll<HTMLElement>('[role="option"]'))
    const used = options.find(option => option.textContent?.includes('Shared domains'))!
    expect(used.textContent).toContain('已添加 · #1')
    expect(used.hasAttribute('data-disabled')).toBe(true)
    options.find(option => option.textContent?.includes('Second domains'))!.click()
    await flushPromises()
    setActiveProfileId('target')
    expect(save()?.textContent?.trim()).toBe('添加 1 个规则集')
    save()!.click()
    await flushPromises()
    expect(network.rules.compose).toHaveBeenCalledTimes(1)
    const [payload, profileId] = network.rules.compose.mock.calls[0]!
    expect(profileId).toBe('default')
    expect(payload.source).toEqual({ kind: 'library', library_rule_ids: ['library-two'] })
    expect(payload.rule).toMatchObject({ policy: 'DIRECT', enabled: true, position: 'top' })
    // A domain ruleset follows its policy by default when OxiDNS is in use.
    expect(payload.dns).toBe('follow')
    expect(network.sets.create).not.toHaveBeenCalled()
  })

  it('reorders hydrated rulesets using only independent reference fields', async () => {
    network.state.rules.push({ id: 'inline-one', itemType: 'rule', rule_type: 'MATCH', value: '', policy: 'REJECT', enabled: true })
    const wrapper = await render(Rules)
    await menuAction('调整顺序')
    await wrapper.get('button[aria-label="Shared domains 下移"]').trigger('click')
    await clickText('保存顺序')
    const call = network.post.mock.calls.find(([path]) => path === '/rules/reorder')
    expect(call).toBeTruthy()
    const payload = call![1]?.rule_configs
    if (!Array.isArray(payload)) throw new Error('Expected ordered rule_configs array')
    expect(payload.map((item: Record<string, unknown>) => item.id)).toEqual(['inline-one', 'ruleset-one'])
    expectReferenceOnly(payload[1])
    expect(call![2]).toEqual({ headers: { 'X-ConfigFlow-Profile': 'default' } })
  })
})

describe('shared rule library', () => {
  it('explains classical delivery for content and URL sources and saves the original draft without confirmation', async () => {
    const content = '# keep\n  +.tmdb.org \n\n192.0.2.1\n*.manual.test\n'
    Object.assign(network.state.library[0]!, { behavior: 'classical', content })
    const wrapper = await render(RuleLibrary)
    await wrapper.get('button[aria-label="编辑 Shared domains"]').trigger('click'); await flushPromises()
    expect(document.body.textContent).toContain(CLASSICAL_CONTENT_HINT)
    const state = (wrapper.vm as any).$.setupState
    state.form.source_type = 'url'; await flushPromises()
    expect(document.body.textContent).toContain(CLASSICAL_CONTENT_HINT)
    state.form.source_type = 'content'; await flushPromises()
    network.post.mockResolvedValueOnce({ data: { content, changes: [], errors: [], warnings: [
      { line: 5, before: '*.manual.test', message: '请手动调整' }
    ] } })
    await clickText('保存')
    expect(confirmState.open).toBe(false)
    expect(network.put).toHaveBeenCalledExactlyOnceWith('/rule-library/library-one', expect.objectContaining({ content, behavior: 'classical' }))
  })

  it('lets content sources fix an explicit format mismatch without overriding their choice', async () => {
    const content = 'payload:\n  - +.tmdb.org\n'
    Object.assign(network.state.library[0]!, { behavior: 'classical', format: 'text', content })
    const wrapper = await render(RuleLibrary)
    await wrapper.get('button[aria-label="编辑 Shared domains"]').trigger('click'); await flushPromises()
    const format = wrapper.findAllComponents(Select).find(item => item.find('[aria-label="内容格式"]').exists())!
    expect(format.props('modelValue')).toBe('text')
    network.post.mockResolvedValueOnce({ data: { content, changes: [], errors: [{ line: 0, message: '请选择 YAML 内容格式' }] } })
    await clickText('保存')
    expect(network.post).toHaveBeenCalledWith('/rule-library/check-content', expect.objectContaining({ format: 'text', content }))
    expect(network.put).not.toHaveBeenCalled()
    expect(document.querySelector('[data-rule-content-issues]')?.textContent).toContain('请选择 YAML 内容格式')
    format.vm.$emit('update:modelValue', 'yaml'); await flushPromises()
    await clickText('保存')
    expect(network.post).toHaveBeenLastCalledWith('/rule-library/check-content', expect.objectContaining({ format: 'yaml', content }))
    expect(network.put).toHaveBeenCalledWith('/rule-library/library-one', expect.objectContaining({ format: 'yaml', content }))
  })

  it('records detected YAML for content sources with automatic format', async () => {
    const content = 'payload: ["+.tmdb.org"]\n'
    Object.assign(network.state.library[0]!, { behavior: 'classical', format: undefined, content })
    const wrapper = await render(RuleLibrary)
    await wrapper.get('button[aria-label="编辑 Shared domains"]').trigger('click'); await flushPromises()
    expect(wrapper.findAllComponents(Select).find(item => item.find('[aria-label="内容格式"]').exists())!.props('modelValue')).toBe('auto')
    await clickText('保存')
    expect(network.post).toHaveBeenCalledWith('/rule-library/check-content', expect.objectContaining({ format: 'yaml', content }))
    expect(network.put).toHaveBeenCalledWith('/rule-library/library-one', expect.objectContaining({ format: 'yaml', content }))
  })

  it('checks full IPCIDR drafts and keeps all line errors beside the content', async () => {
    Object.assign(network.state.library[0]!, { behavior: 'ipcidr', content: '+.tmdb.org\nDOMAIN-SHUFFIX,example.com' })
    const wrapper = await render(RuleLibrary)
    await wrapper.get('button[aria-label="编辑 Shared domains"]').trigger('click'); await flushPromises()
    network.post.mockResolvedValueOnce({ data: { content: '+.tmdb.org', changes: [], errors: [
      { line: 1, message: '只接受 CIDR' }, { line: 2, message: '不是有效 IP 网段' }
    ] } })
    await clickText('保存')
    expect(network.post).toHaveBeenCalledWith('/rule-library/check-content', expect.objectContaining({ behavior: 'ipcidr' }))
    expect(document.querySelector('[data-rule-content-issues]')?.textContent).toContain('第 2 行：不是有效 IP 网段')
    expect(network.put).not.toHaveBeenCalled()
    expect(document.querySelector<HTMLTextAreaElement>('#lib-content')?.value).toBe('+.tmdb.org\nDOMAIN-SHUFFIX,example.com')
  })

  it('validates an appended classical type and value on the server, including IPv6', async () => {
    Object.assign(network.state.library[0]!, { behavior: 'classical', content: '+.tmdb.org' })
    const wrapper = await render(RuleLibrary)
    const state = (wrapper.vm as any).$.setupState
    state.showAddRuleToSetDialog(network.state.library[0]); await flushPromises()
    state.addRuleToSetForm.rule_type = 'IP-CIDR6'
    state.addRuleToSetForm.value = '+.tmdb.org'; await flushPromises()
    network.post.mockResolvedValueOnce({ data: { content: '+.tmdb.org', changes: [], errors: [{ line: 2, message: '不是有效 IP 网段' }] } })
    await clickText('添加')
    expect(network.post).toHaveBeenCalledWith('/rule-library/check-content', expect.objectContaining({ append_lines: ['IP-CIDR6,+.tmdb.org'] }))
    expect(network.put).not.toHaveBeenCalled()
    expect(document.querySelector('[data-rule-content-issues]')?.textContent).toContain('第 2 行：不是有效 IP 网段')
    state.addRuleToSetForm.value = '::ffff:192.0.2.1/128'; await flushPromises()
    await clickText('添加')
    expect(network.put).toHaveBeenCalledWith('/rule-library/library-one', expect.objectContaining({ content: '+.tmdb.org\nIP-CIDR6,::ffff:192.0.2.1/128\n' }))
  })

  it('keeps existing bare classical entries when appending a typed rule', async () => {
    const content = '# keep\n  +.tmdb.org \n\n192.0.2.1'
    Object.assign(network.state.library[0]!, { behavior: 'classical', content })
    const wrapper = await render(RuleLibrary)
    const state = (wrapper.vm as any).$.setupState
    state.showAddRuleToSetDialog(network.state.library[0]); await flushPromises()
    expect(document.body.textContent).toContain(CLASSICAL_CONTENT_HINT)
    state.addRuleToSetForm.value = 'api.example.com'; await flushPromises()
    await clickText('添加')
    expect(confirmState.open).toBe(false)
    expect(network.put).toHaveBeenCalledWith('/rule-library/library-one', expect.objectContaining({
      content: content + '\nDOMAIN-SUFFIX,api.example.com\n', behavior: 'classical'
    }))
  })

  it('offers exact, suffix and explicit wildcard input with the stored format preview', async () => {
    const wrapper = await render(RuleLibrary)
    const state = (wrapper.vm as any).$.setupState
    state.showAddRuleToSetDialog(network.state.library[0]); await flushPromises()
    state.addRuleToSetForm.value = 'api.example.com'; await flushPromises()
    expect(state.addRulePreview).toBe('api.example.com')
    expect(document.body.textContent).toContain('匹配范围')
    state.addRuleToSetForm.rule_type = 'DOMAIN-SUFFIX'; await flushPromises()
    expect(state.addRulePreview).toBe('+.api.example.com')
    state.addRuleToSetForm.rule_type = 'PATTERN'
    state.addRuleToSetForm.value = '+.x.ai\napi.telegram.org'; await flushPromises()
    expect(state.addRulePreview).toBe('+.x.ai\napi.telegram.org')
    await clickText('添加')
    expect(network.put).toHaveBeenCalledWith('/rule-library/library-one', expect.objectContaining({
      content: 'example.com\n+.x.ai\napi.telegram.org\n', behavior: 'domain'
    }))
  })

  it.each(['DOMAIN', 'DOMAIN-SUFFIX'])('rejects a %s prefix in Domain content without offering conversion', async kind => {
    const content = `${kind},x.ai\napi.telegram.org`
    network.state.library[0]!.content = content
    const wrapper = await render(RuleLibrary)
    await render(ConfirmHost)
    await wrapper.get('button[aria-label="编辑 Shared domains"]').trigger('click'); await flushPromises()
    expect(document.querySelector<HTMLTextAreaElement>('#lib-content')?.placeholder).toContain('+.example.com')
    expect(document.body.textContent).toContain('不加 DOMAIN 等前缀')
    network.post.mockResolvedValueOnce({ data: { content, changes: [], errors: [{ line: 1, message: `Domain 不接受 ${kind} 前缀，请使用域名写法。` }] } })
    await clickText('保存')
    expect(network.put).not.toHaveBeenCalled()
    expect(confirmState.open).toBe(false)
    expect(document.body.textContent).not.toContain('转换并保存')
    expect(document.querySelector('[data-rule-content-issues]')?.textContent).toContain(`第 1 行：Domain 不接受 ${kind} 前缀`)
    expect(document.querySelector<HTMLTextAreaElement>('#lib-content')?.value).toBe(content)
  })

  it('saves after manually fixing Domain content and preserves all supported wildcard forms', async () => {
    const content = 'DOMAIN-SUFFIX,x.ai\nDOMAIN,api.telegram.org'
    network.state.library[0]!.content = content
    const wrapper = await render(RuleLibrary)
    await render(ConfirmHost)
    await wrapper.get('button[aria-label="编辑 Shared domains"]').trigger('click'); await flushPromises()
    network.post.mockResolvedValueOnce({ data: { content, changes: [], errors: [
      { line: 1, message: 'Domain 不接受 DOMAIN-SUFFIX 前缀' }, { line: 2, message: 'Domain 不接受 DOMAIN 前缀' }
    ] } })
    await clickText('保存')
    expect(network.put).not.toHaveBeenCalled()
    expect(confirmState.open).toBe(false)
    const corrected = '+.x.ai\napi.telegram.org\n.example.org\n*.example.org\n*.*.example.org\n*'
    const input = document.querySelector<HTMLTextAreaElement>('#lib-content')!
    input.value = corrected; input.dispatchEvent(new Event('input', { bubbles: true })); await flushPromises()
    expect(document.querySelector('[data-rule-content-issues]')).toBeNull()
    network.post.mockResolvedValueOnce({ data: { content: corrected, errors: [], changes: [] } })
    await clickText('保存')
    expect(confirmState.open).toBe(false)
    expect(network.put).toHaveBeenCalledExactlyOnceWith('/rule-library/library-one', expect.objectContaining({ content: corrected, behavior: 'domain' }))
  })

  it('keeps a draft and releases the save lock when format checking fails', async () => {
    const wrapper = await render(RuleLibrary)
    await wrapper.get('button[aria-label="编辑 Shared domains"]').trigger('click'); await flushPromises()
    network.post.mockRejectedValueOnce(new Error('offline'))
    const error = vi.spyOn(notify, 'error').mockImplementation(() => 0)
    await clickText('保存')
    expect(network.put).not.toHaveBeenCalled()
    expect(error).toHaveBeenCalled()
    expect(document.querySelector<HTMLTextAreaElement>('#lib-content')?.value).toBe('example.com')
    expect((wrapper.vm as any).$.setupState.savingRule).toBe(false)
  })

  it('parses quoted YAML behaviors and formats under rule-providers', async () => {
    await render(RuleLibrary); await menuAction('批量导入')
    const input = document.querySelector<HTMLTextAreaElement>('[role="dialog"] textarea')!
    input.value = 'rule-providers:\n  networks:\n    type: http\n    behavior: "ipcidr"\n    format: "yaml"\n    url: https://example.test/rules\n'
    input.dispatchEvent(new Event('input', { bubbles: true })); await flushPromises()
    await clickText('导入')
    expect(network.post).toHaveBeenCalledWith('/rule-library', expect.objectContaining({ name: 'networks', behavior: 'ipcidr', format: 'yaml' }))
  })

  it.each(['classcial', 'DOMAIN-SUFFIX'])('retains unknown behavior %s and explains the rejected source', async behavior => {
    await render(RuleLibrary); await menuAction('批量导入')
    const input = document.querySelector<HTMLTextAreaElement>('[role="dialog"] textarea')!
    const original = `source: { type: http, behavior: "${behavior}", url: "https://example.test/rules" }`
    input.value = original; input.dispatchEvent(new Event('input', { bubbles: true })); await flushPromises()
    await clickText('导入')
    expect(network.post).not.toHaveBeenCalled()
    expect(input.isConnected).toBe(true)
    expect(input.value).toBe(original)
    expect(document.querySelector('[role="dialog"] ul[role="alert"]')?.textContent).toContain(`source：不支持的类型 ${behavior}`)
  })

  it.each([
    ['source: { url: https://example.test/rules, format: typo }', '不支持的格式 typo'],
    ['source: [', 'unexpected end'],
    ['source: { url: https://example.test/rules, format: yaml, behavior: ipcidr }\nsource: { url: https://example.test/other }', 'duplicated mapping key']
  ])('preserves invalid YAML imports with actionable feedback: %s', async (original, reason) => {
    await render(RuleLibrary); await menuAction('批量导入')
    const input = document.querySelector<HTMLTextAreaElement>('[role="dialog"] textarea')!
    input.value = original; input.dispatchEvent(new Event('input', { bubbles: true })); await flushPromises()
    await clickText('导入')
    expect(network.post).not.toHaveBeenCalled()
    expect(input.isConnected).toBe(true)
    expect(input.value).toBe(original)
    expect(document.querySelector('[role="dialog"] ul[role="alert"]')?.textContent).toContain(reason)
  })

  it('cannot close or restart an active import and retains completed-source bookkeeping', async () => {
    const wrapper = await render(RuleLibrary); await menuAction('批量导入')
    const state = (wrapper.vm as any).$.setupState
    const input = document.querySelector<HTMLTextAreaElement>('[role="dialog"] textarea')!
    const original = 'first: { url: https://first.test/rules }\nsecond: { url: https://second.test/rules }'
    input.value = original; input.dispatchEvent(new Event('input', { bubbles: true })); await flushPromises()
    let reject!: (reason: unknown) => void
    network.post.mockResolvedValueOnce({ data: { success: true } })
    network.post.mockImplementationOnce(() => new Promise((_, fail) => { reject = fail }))
    await clickText('导入')
    const popup = document.querySelector<HTMLElement>('[role="dialog"]')!
    popup.querySelector<HTMLButtonElement>('[data-slot="dialog-close"]')!.click(); await flushPromises()
    popup.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); await flushPromises()
    state.showBatchImportDialog(); await flushPromises()
    expect(state.batchImportDialogVisible).toBe(true)
    expect(input.isConnected).toBe(true)
    expect(input.value).toBe(original)
    reject(new Error('下载失败')); await flushPromises()
    await clickText('导入')
    expect(network.post.mock.calls.filter(([path]) => path === '/rule-library')).toHaveLength(3)
    expect(network.post.mock.calls.at(-1)?.[1]).toMatchObject({ name: 'second' })
  })

  it('keeps failed input and retries only failed sources with a specific reason', async () => {
    await render(RuleLibrary); await menuAction('批量导入')
    const input = document.querySelector<HTMLTextAreaElement>('[role="dialog"] textarea')!
    const original = 'first: { url: https://first.test/rules }\nsecond: { url: https://second.test/rules }'
    input.value = original; input.dispatchEvent(new Event('input', { bubbles: true })); await flushPromises()
    network.post.mockResolvedValueOnce({ data: { success: true } })
    network.post.mockRejectedValueOnce({ isAxiosError: true, response: { data: { message: '远端内容不是有效 CIDR' } } })
    await clickText('导入')
    expect(input.value).toBe(original)
    expect(input.isConnected).toBe(true)
    expect(document.querySelector('[role="dialog"] ul[role="alert"]')?.textContent).toContain('second：远端内容不是有效 CIDR')
    await clickText('导入')
    expect(network.post.mock.calls.filter(([path]) => path === '/rule-library')).toHaveLength(3)
    expect(network.post.mock.calls.at(-1)?.[1]).toMatchObject({ name: 'second' })
  })

  it.each([
    [2, 'success', '成功导入 2 条规则'],
    [1, 'warning', '批量导入完成：成功 1 条，失败 1 条'],
    [0, 'error', '批量导入失败，2 条规则均未导入']
  ] as const)('reports batch import feedback for %i successful rules', async (successCount, tone, message) => {
    for (const kind of ['success', 'warning', 'error'] as const) vi.spyOn(notify, kind).mockImplementation(() => 0)
    vi.spyOn(console, 'error').mockImplementation(() => undefined)
    for (let index = 0; index < 2; index++) {
      network.post.mockImplementationOnce(async (_path, body) => {
        if (index >= successCount) {
          throw { response: { status: 502, data: { success: false, message: '规则源下载失败，未添加规则，请检查来源后重试' } } }
        }
        const created = structuredClone(body!)
        network.state.library.push(created)
        return { data: { success: true, data: created } }
      })
    }
    const wrapper = await render(RuleLibrary)
    await menuAction('批量导入')
    const input = document.querySelector<HTMLTextAreaElement>('[role="dialog"] textarea')!
    input.value = 'first: { type: http, behavior: domain, url: "https://first.test/rules" }\nsecond: { type: http, behavior: domain, url: "https://second.test/rules" }'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await flushPromises()
    await clickText('导入')
    expect(network.post.mock.calls.filter(([path]) => path === '/rule-library')).toHaveLength(2)
    expect(notify[tone]).toHaveBeenCalledWith(message)
    for (const other of ['success', 'warning', 'error'] as const) {
      if (other !== tone) expect(notify[other]).not.toHaveBeenCalled()
    }
    // The refreshed catalog must agree with the feedback, including a batch
    // where no source was committed. Existing custom sources remain visible.
    expect(network.state.library.map(rule => rule.name)).toEqual(['Shared domains', ...['first', 'second'].slice(0, successCount)])
    expect(wrapper.text()).toContain('Shared domains')
    for (const [index, name] of ['first', 'second'].entries()) {
      const labels = wrapper.findAll(`button[aria-label="编辑 ${name}"]`)
      expect(labels).toHaveLength(index < successCount ? 1 : 0)
    }
  })

  it.each([
    [['pending', 'pending'], '批量导入：成功 0 条，失败 0 条，保存结果待确认 2 条；请核对列表，勿重复导入'],
    [['success', 'failed', 'pending'], '批量导入：成功 1 条，失败 1 条，保存结果待确认 1 条；请核对列表，勿重复导入']
  ] as const)('keeps uncertain creations separate from failed imports: %j', async (outcomes, message) => {
    for (const tone of ['success', 'warning', 'error'] as const) vi.spyOn(notify, tone).mockImplementation(() => 0)
    vi.spyOn(console, 'error').mockImplementation(() => undefined)
    for (const outcome of outcomes) {
      network.post.mockImplementationOnce(async (_path, body) => {
        if (outcome !== 'failed') network.state.library.push(structuredClone(body!))
        if (outcome !== 'success') {
          throw { isAxiosError: true, response: { status: outcome === 'pending' ? 503 : 502,
            data: { success: false, confirmation_required: outcome === 'pending' } } }
        }
        return { data: { success: true } }
      })
    }
    const wrapper = await render(RuleLibrary)
    await menuAction('批量导入')
    const input = document.querySelector<HTMLTextAreaElement>('[role="dialog"] textarea')!
    input.value = outcomes.map((_, index) => `source${index}: { type: http, behavior: domain, url: "https://source${index}.test/rules" }`).join('\n')
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await flushPromises()
    await clickText('导入')

    expect(notify.warning).toHaveBeenCalledWith(message)
    expect(notify.success).not.toHaveBeenCalled()
    expect(notify.error).not.toHaveBeenCalled()
    for (const [index, outcome] of outcomes.entries()) {
      expect(wrapper.findAll(`button[aria-label="编辑 source${index}"]`)).toHaveLength(outcome === 'failed' ? 0 : 1)
    }
  })

  it('keeps a shared source draft when switching profiles and writes shared fields only', async () => {
    network.state.library[0]!.policy = 'legacy-profile-policy'
    const wrapper = await render(RuleLibrary)
    await wrapper.get('button[aria-label="编辑 Shared domains"]').trigger('click')
    await flushPromises()
    const input = document.querySelector<HTMLInputElement>('#lib-name')!
    input.value = 'Updated shared source'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    setActiveProfileId('target')
    await flushPromises()
    expect(document.querySelector<HTMLInputElement>('#lib-name')?.value).toBe('Updated shared source')
    await clickText('保存')
    expect(network.put).toHaveBeenCalledTimes(1)
    const [path, payload] = network.put.mock.calls[0]!
    expect(path).toBe('/rule-library/library-one')
    expect(payload).toMatchObject({ name: 'Updated shared source', content: 'example.com', behavior: 'domain', format: 'text' })
    expect(payload).not.toHaveProperty('policy')
    expect(network.sets.update).not.toHaveBeenCalled()
  })

  it('captures the destination profile and adds through compose with the DNS choice', async () => {
    network.state.rules = []
    const wrapper = await render(RuleLibrary)
    await wrapper.get('button[aria-label="将 Shared domains 加入当前配置"]').trigger('click')
    await flushPromises()
    expect(network.rules.getDns).toHaveBeenCalledWith('default')
    // A domain ruleset follows its policy by default when OxiDNS is in use.
    expect(wrapper.getComponent(RuleDnsSelect).props('modelValue')).toBe('follow')
    setActiveProfileId('target')
    await clickText('加入配置')
    expect(network.rules.compose).toHaveBeenCalledExactlyOnceWith({
      source: { kind: 'library', library_rule_ids: ['library-one'] },
      rule: { policy: 'DIRECT', remark: '', group_name: '', no_resolve: false, enabled: true, position: 'top' },
      dns: 'follow'
    }, 'default')
    expect(network.post.mock.calls.some(([path]) => path === '/rule-sets')).toBe(false)
    expect(network.get).toHaveBeenCalledWith('/rules', { headers: { 'X-ConfigFlow-Profile': 'default' } })
  })

  it('skips existing references rather than duplicating shared source data', async () => {
    const wrapper = await render(RuleLibrary)
    await wrapper.get('button[aria-label="将 Shared domains 加入当前配置"]').trigger('click')
    await flushPromises()
    await clickText('加入配置')
    expect(network.post).not.toHaveBeenCalled()
    expect(network.rules.compose).not.toHaveBeenCalled()
  })

  it('keeps IP rulesets on the default upstream until chosen and sends no-resolve per batch', async () => {
    network.state.rules = []
    network.state.library.push({ id: 'library-ip', name: 'Shared networks', source_type: 'content', content: '10.0.0.0/8', url: '', behavior: 'ipcidr', format: 'text', enabled: true, has_ip_part: true })
    const wrapper = await render(RuleLibrary)
    ;(wrapper.vm as any).$.setupState.showReferenceDialog(['library-ip'])
    await flushPromises()
    const field = wrapper.getComponent(RuleDnsField)
    expect(field.props('modelValue')).toBe('default')
    field.vm.$emit('update:modelValue', 'blackhole')
    await flushPromises()
    // Routing an IP ruleset in DNS turns on local probing for every query.
    expect(field.props('warnings').map((item: any) => item.text).join('\n')).toContain('这是第一条参与 DNS 的 IP 规则')
    await clickText('加入配置')
    expect(network.rules.compose).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({
      source: { kind: 'library', library_rule_ids: ['library-ip'] }, dns: 'blackhole',
      rule: expect.objectContaining({ no_resolve: true })
    }), 'default')

    network.rules.compose.mockClear()
    ;(wrapper.vm as any).$.setupState.showReferenceDialog(['library-one', 'library-ip'])
    await flushPromises()
    expect(wrapper.getComponent(RuleDnsField).props('modelValue')).toBe('default')
    await clickText('加入配置')
    expect(network.rules.compose.mock.calls.map(([payload]) => [payload.source.library_rule_ids, payload.rule.no_resolve, payload.dns]))
      .toEqual([[['library-ip'], true, undefined], [['library-one'], false, undefined]])
  })

  it.each([
    ['locked native routing', { editable: false, reason: '主执行链含手写条目' }, 'DNS 处理需在完整参数修改：主执行链含手写条目'],
    ['unreadable DNS state', null, 'DNS 状态未加载，本次不修改 DNS']
  ])('disables the DNS choice and sends none for %s', async (_, engine, warning) => {
    network.state.rules = []
    if (engine) {
      network.rules.getDns.mockResolvedValueOnce({ data: { success: true, upstreams: [], rules: {}, mosdns: {}, engine: {
        mode: 'native', default_forward: 'forward_remote', prefer_ipv4: true, local_probe_active: false, oxidns_in_use: true,
        warnings: [], agents: [], ...engine } } })
    } else network.rules.getDns.mockRejectedValueOnce(new Error('offline'))
    const wrapper = await render(RuleLibrary)
    await wrapper.get('button[aria-label="将 Shared domains 加入当前配置"]').trigger('click')
    await flushPromises()
    const field = wrapper.getComponent(RuleDnsField)
    expect(field.props()).toMatchObject({ modelValue: 'default', disabled: true })
    expect(field.props('warnings')[0].text).toBe(warning)
    await clickText('加入配置')
    expect(network.rules.compose.mock.calls[0]![0]).not.toHaveProperty('dns')
  })

  it('hides the DNS choice for a profile that does not use OxiDNS', async () => {
    network.state.rules = []
    network.rules.getDns.mockResolvedValueOnce({ data: { success: true, upstreams: [], rules: {}, mosdns: {}, engine: {
      mode: 'template', editable: true, reason: '', default_forward: 'forward_remote', prefer_ipv4: true, local_probe_active: false,
      oxidns_in_use: false, warnings: [], agents: [] } } })
    const wrapper = await render(RuleLibrary)
    await wrapper.get('button[aria-label="将 Shared domains 加入当前配置"]').trigger('click')
    await flushPromises()
    expect(wrapper.findComponent(RuleDnsField).exists()).toBe(false)
    await clickText('加入配置')
    expect(network.rules.compose.mock.calls[0]![0]).not.toHaveProperty('dns')
  })

  it('offers to add without DNS when native routing became custom while saving', async () => {
    network.state.rules = []
    network.rules.compose.mockRejectedValueOnce({ isAxiosError: true, response: { status: 409, data: {
      success: false, message: '配置仍在使用', usages: [{ kind: 'oxidns', code: 'oxidns_routing_custom', reason: '主执行链含手写条目' }] } } })
    const wrapper = await render(RuleLibrary)
    await wrapper.get('button[aria-label="将 Shared domains 加入当前配置"]').trigger('click')
    await flushPromises()
    await clickText('加入配置')
    expect(confirmState).toMatchObject({ open: true, description: '主执行链含手写条目', confirmText: '仍保存（DNS 默认上游）', altText: '复制原因' })
    settleConfirm('confirm')
    await flushPromises()
    expect(network.rules.compose.mock.calls.map(([payload]) => payload.dns)).toEqual(['follow', undefined])
    expect(document.querySelector('[role="dialog"]')).toBeNull()
  })

  it('reports a cancelled second batch as not added and keeps only it selected', async () => {
    network.state.rules = []
    network.state.library.push({ id: 'library-ip', name: 'Shared networks', source_type: 'content', content: '10.0.0.0/8', url: '', behavior: 'ipcidr', format: 'text', enabled: true, has_ip_part: true })
    const success = vi.spyOn(notify, 'success').mockImplementation(() => 0)
    const warning = vi.spyOn(notify, 'warning').mockImplementation(() => 0)
    network.rules.compose
      .mockResolvedValueOnce({ data: { success: true, data: { rules: [{ id: 'ruleset-ip' }], library: null }, dns: {}, skipped: [], warnings: [], draft: true } })
      .mockRejectedValueOnce({ isAxiosError: true, response: { status: 409, data: { usages: [{ code: 'oxidns_routing_custom', reason: '主执行链含手写条目' }] } } })
    const wrapper = await render(RuleLibrary)
    const setup = (wrapper.vm as any).$.setupState
    setup.showReferenceDialog(['library-one', 'library-ip'])
    await flushPromises()
    wrapper.getComponent(RuleDnsField).vm.$emit('update:modelValue', 'blackhole')
    await flushPromises()
    await clickText('加入配置')
    expect(confirmState.open).toBe(true)
    settleConfirm('cancel')
    await flushPromises()
    expect(success).not.toHaveBeenCalled()
    expect(warning).toHaveBeenCalledTimes(1)
    expect(warning.mock.calls[0]![0]).toMatch(/添加 1 个规则集，未添加 1 个（已取消）$/)
    expect(setup.referenceRuleIds).toEqual(['library-one'])
    expect(document.querySelector('[role="dialog"]')).not.toBeNull()
  })

  it('does not claim zero added rulesets when the first request fails', async () => {
    network.state.rules = []
    const error = vi.spyOn(notify, 'error').mockImplementation(() => 0)
    network.rules.compose.mockRejectedValueOnce({ isAxiosError: true, response: { status: 400, data: { message: '规则集不存在' } } })
    const wrapper = await render(RuleLibrary)
    await wrapper.get('button[aria-label="将 Shared domains 加入当前配置"]').trigger('click')
    await flushPromises()
    await clickText('加入配置')
    expect(error).toHaveBeenCalledWith('规则集不存在')
  })

  it('surfaces all-profile deletion conflicts without deleting configuration references', async () => {
    const wrapper = await render(RuleLibrary)
    await render(ConfirmHost)
    const error = vi.spyOn(notify, 'error').mockImplementation(() => 0)
    network.remove.mockRejectedValueOnce({
      isAxiosError: true,
      response: { status: 409, data: { message: 'Used by profile Target: ruleset-one' } }
    })
    await wrapper.get('button[aria-label="删除 Shared domains"]').trigger('click')
    await flushPromises()
    await clickText('删除')
    expect(network.remove).toHaveBeenCalledExactlyOnceWith('/rule-library/library-one')
    expect(error).toHaveBeenCalledWith('Used by profile Target: ruleset-one')
    expect(network.get.mock.calls.some(([path]) => path === '/rules')).toBe(false)
    expect(network.sets.delete).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('Shared domains')
  })
})
