import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import type { Component } from 'vue'
import Rules from '@/views/Rules.vue'
import RuleLibrary from '@/views/RuleLibrary.vue'
import ConfirmHost from '@/components/feedback/ConfirmHost.vue'
import { Select } from '@/components/ui/select'
import { setActiveProfileId } from '@/profileContext'
import { notify, settleConfirm } from '@/lib/feedback'

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
    findDuplicates: vi.fn(async (_profileId?: string) => ({ data: {} }))
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

  it('requires selecting a shared source for new rulesets', async () => {
    network.state.rules = []
    const wrapper = await render(Rules)
    await menuAction('添加规则集')
    const saveButton = Array.from(document.querySelectorAll('button')).find(button => button.textContent?.trim() === '保存')
    expect(saveButton?.disabled).toBe(true)
    const sourceSelect = wrapper.findAllComponents(Select).find(select => select.props('modelValue') === '')
    expect(sourceSelect).toBeTruthy()
    sourceSelect!.vm.$emit('update:modelValue', 'library-one')
    await flushPromises()
    await clickText('保存')
    const [payload, profileId] = network.sets.create.mock.calls[0]!
    expectReferenceOnly(payload)
    expect(profileId).toBe('default')
    expect(payload.policy).toBe('DIRECT')
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

  it('shows correct examples and leaves mixed drafts untouched when conversion is cancelled', async () => {
    network.state.library[0]!.content = 'DOMAIN-SUFFIX,x.ai\napi.telegram.org'
    const wrapper = await render(RuleLibrary)
    await render(ConfirmHost)
    await wrapper.get('button[aria-label="编辑 Shared domains"]').trigger('click'); await flushPromises()
    expect(document.querySelector<HTMLTextAreaElement>('#lib-content')?.placeholder).toContain('+.example.com')
    expect(document.body.textContent).toContain('两种写法可混用')
    network.post.mockResolvedValueOnce({ data: { content: '+.x.ai\napi.telegram.org', errors: [], changes: [{ line: 1, before: 'DOMAIN-SUFFIX,x.ai', after: '+.x.ai' }] } })
    await clickText('保存')
    expect(network.put).not.toHaveBeenCalled()
    expect(document.body.textContent).toContain('DOMAIN-SUFFIX,x.ai → +.x.ai')
    await clickText('返回编辑')
    expect(network.put).not.toHaveBeenCalled()
    expect(document.querySelector<HTMLTextAreaElement>('#lib-content')?.value).toBe('DOMAIN-SUFFIX,x.ai\napi.telegram.org')
  })

  it('saves only after approving conversion and blocks unsafe formats', async () => {
    const wrapper = await render(RuleLibrary)
    await render(ConfirmHost)
    await wrapper.get('button[aria-label="编辑 Shared domains"]').trigger('click'); await flushPromises()
    const warning = vi.spyOn(notify, 'warning').mockImplementation(() => 0)
    network.post.mockResolvedValueOnce({ data: { content: 'bad', changes: [], errors: [{ line: 2, message: 'Domain 无法表达 DOMAIN-KEYWORD；请使用 Classical。' }] } })
    await clickText('保存')
    expect(warning).toHaveBeenCalledWith(expect.stringContaining('第 2 行'))
    expect(network.put).not.toHaveBeenCalled()
    network.post.mockResolvedValueOnce({ data: { content: '+.x.ai\napi.telegram.org', errors: [], changes: [{ line: 1, before: 'DOMAIN-SUFFIX,x.ai', after: '+.x.ai' }] } })
    await clickText('保存')
    expect(network.put).not.toHaveBeenCalled()
    await clickText('转换并保存')
    expect(network.put).toHaveBeenCalledExactlyOnceWith('/rule-library/library-one', expect.objectContaining({ content: '+.x.ai\napi.telegram.org' }))
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

  it.each([
    [2, 'success', '成功导入 2 条规则'],
    [1, 'warning', '批量导入完成：成功 1 条，失败 1 条'],
    [0, 'error', '批量导入失败，2 条规则均未导入']
  ] as const)('reports batch import feedback for %i successful rules', async (successCount, tone, message) => {
    for (const kind of ['success', 'warning', 'error'] as const) vi.spyOn(notify, kind).mockImplementation(() => 0)
    vi.spyOn(console, 'error').mockImplementation(() => undefined)
    for (let index = 0; index < 2; index++) {
      if (index < successCount) network.post.mockResolvedValueOnce({ data: { success: true } })
      else network.post.mockRejectedValueOnce(new Error('Import failed'))
    }
    await render(RuleLibrary)
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

  it('captures the destination profile when opening the reference dialog', async () => {
    network.state.rules = []
    const wrapper = await render(RuleLibrary)
    await wrapper.get('button[aria-label="将 Shared domains 加入当前配置"]').trigger('click')
    await flushPromises()
    setActiveProfileId('target')
    await clickText('加入配置')
    const call = network.post.mock.calls.find(([path]) => path === '/rule-sets')
    expect(call).toBeTruthy()
    expectReferenceOnly(call![1])
    expect(call![1]).toMatchObject({ policy: 'DIRECT', enabled: true })
    expect(call![2]).toEqual({ headers: { 'X-ConfigFlow-Profile': 'default' } })
    expect(network.get).toHaveBeenCalledWith('/rules', { headers: { 'X-ConfigFlow-Profile': 'default' } })
  })

  it('skips existing references rather than duplicating shared source data', async () => {
    const wrapper = await render(RuleLibrary)
    await wrapper.get('button[aria-label="将 Shared domains 加入当前配置"]').trigger('click')
    await flushPromises()
    await clickText('加入配置')
    expect(network.post).not.toHaveBeenCalled()
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
