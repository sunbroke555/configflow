import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DOMWrapper, flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import Rules from '@/views/Rules.vue'
import { setActiveProfileId } from '@/profileContext'
import { confirmState, notify, settleConfirm } from '@/lib/feedback'

// Exercise the real page, policy/DNS selectors, rename dialog and reorder controls.
// HTTP is the boundary: assert exactly which resource IDs each interaction submits.
const network = vi.hoisted(() => {
  const state = { rules: [] as Record<string, any>[], dns: {} as Record<string, any> }
  const rules = {
    getAll: vi.fn(async () => ({ data: structuredClone(state.rules) })),
    getDns: vi.fn(async () => ({ data: structuredClone(state.dns) })),
    renameGroup: vi.fn(async (ids: string[], name: string, _profileId?: string) => {
      state.rules.filter(item => ids.includes(item.id)).forEach(item => { item.group_name = name })
      return { data: { success: true } }
    }),
    setPolicy: vi.fn(async (ids: string[], policy: string, options: { groupName?: string } = {}, _profileId?: string) => {
      state.rules.filter(item => ids.includes(item.id)).forEach(item => {
        item.policy = policy
        if (options.groupName !== undefined) item.group_name = options.groupName
      })
      return { data: { success: true } }
    }),
    setDns: vi.fn(async (routes: Record<string, string>, _options?: unknown, _profileId?: string) => ({
      data: { rules: Object.fromEntries(Object.keys(routes).map(id => [id, state.dns.rules[id]])) }
    })),
    findDuplicates: vi.fn(), compose: vi.fn()
  }
  const post = vi.fn(async (_path: string, _body?: any, _config?: any): Promise<{ data: any }> => ({ data: { success: true } }))
  return { state, rules, post }
})
vi.mock('@/api', () => ({
  default: { get: vi.fn(async () => ({ data: [] })), post: network.post, put: vi.fn(), delete: vi.fn() },
  ruleApi: network.rules, ruleSetApi: { update: vi.fn(), delete: vi.fn() },
  proxyGroupApi: { getAll: vi.fn(async () => ({ data: [{ id: 'proxy', name: 'Proxy' }] })) }
}))
const rule = (id: string, groupName?: string, policy = 'DIRECT') => ({
  id, itemType: 'rule', rule_type: 'DOMAIN-SUFFIX', value: `${id}.test`, policy, enabled: true,
  ...(groupName === undefined ? {} : { group_name: groupName })
})
const wrappers: VueWrapper[] = []
beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  setActiveProfileId('default')
  network.state.rules = []
  network.post.mockImplementation(async () => ({ data: { success: true } }))
  vi.stubGlobal('matchMedia', vi.fn((query: string) => ({
    matches: false, media: query, onchange: null, addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn()
  })))
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  vi.stubGlobal('scrollTo', vi.fn())
  Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() })
  for (const kind of ['success', 'warning', 'error'] as const) vi.spyOn(notify, kind).mockImplementation(() => 0)
})
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  settleConfirm('cancel')
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})
async function render() {
  network.state.dns = {
    engine: { mode: 'template', editable: true, oxidns_in_use: true, default_forward: 'forward_remote', warnings: [], agents: [] },
    upstreams: [], mosdns: {},
    rules: Object.fromEntries(network.state.rules.map(item => [item.id, { state: 'follow', route: 'direct', follow_route: 'direct' }]))
  }
  const wrapper = mount(Rules, { attachTo: document.body })
  wrappers.push(wrapper)
  await flushPromises()
  return wrapper
}
const heads = (wrapper: VueWrapper) => wrapper.findAll('[data-group-head]')
const rows = (wrapper: VueWrapper) => wrapper.findAll('.rule-row').map(row => row.attributes('data-rule-key'))
async function clickText(text: string) {
  const button = Array.from(document.querySelectorAll<HTMLButtonElement>('button')).find(item => item.textContent?.trim() === text)
  expect(button, `button ${text}`).toBeTruthy()
  button!.click()
  await flushPromises()
}
async function policy(wrapper: VueWrapper, group: string, next: string) {
  await wrapper.get(`button[aria-label^="${group} 的策略"]`).trigger('click')
  await flushPromises()
  // Search in the actual selector, then select the exact policy option.
  const input = document.querySelector<HTMLInputElement>('[data-slot="command-input"]')!
  await new DOMWrapper(input).setValue(next)
  await flushPromises()
  const option = Array.from(document.querySelectorAll<HTMLElement>('[role="option"]')).find(el => el.textContent?.trim() === next)
  expect(option).toBeTruthy()
  option!.click()
  await flushPromises()
}

describe('named rule group boundaries', () => {
  it('keeps an unnamed GEOIP outside 205 adjacent E2E rules and changes only those 205', async () => {
    const ids = Array.from({ length: 205 }, (_, i) => `e2e-${i + 1}`)
    network.state.rules = [{ ...rule('geoip'), rule_type: 'GEOIP', value: 'lan' }, ...ids.map(id => rule(id, 'E2E'))]
    // A saved collapsed group still submits every member, without mounting 205 row menus.
    localStorage.setItem('cf-rules-collapsed:default', JSON.stringify(['group_rule-e2e-1']))
    const wrapper = await render()
    expect(heads(wrapper)).toHaveLength(1)
    expect(heads(wrapper)[0]!.text()).toContain('02–206 · 205 项')
    expect(wrapper.get('[data-rule-key="rule-geoip"]').classes()).not.toContain('in-group')
    await policy(wrapper, 'E2E', 'Proxy')
    expect(confirmState.description).toContain('205 项策略')
    settleConfirm('confirm')
    await flushPromises()
    expect(network.rules.setPolicy).toHaveBeenCalledExactlyOnceWith(ids, 'Proxy', {}, 'default')
    expect(network.state.rules[0]!.policy).toBe('DIRECT')
    expect(network.state.rules.slice(1).every(item => item.group_name === 'E2E')).toBe(true)
  }, 15000)

  it('keeps E2E and DNS地址 separate under the same policy, including batch DNS and cards', async () => {
    network.state.rules = [rule('e1', 'E2E'), rule('e2', 'E2E'), rule('d1', 'DNS地址'), rule('d2', 'DNS地址')]
    const wrapper = await render()
    expect(heads(wrapper)).toHaveLength(2)
    expect(heads(wrapper).map(head => head.text())).toEqual([expect.stringContaining('01–02 · 2 项'), expect.stringContaining('03–04 · 2 项')])
    await wrapper.get('button[aria-label="批量设置 E2E 的 DNS"]').trigger('click')
    await flushPromises()
    expect(document.body.textContent).toContain('E2E · 共 2 项')
    await clickText('应用')
    expect(confirmState.description).toContain('2 项的 DNS')
    settleConfirm('confirm')
    await flushPromises()
    expect(network.rules.setDns).toHaveBeenCalledExactlyOnceWith({ e1: 'follow', e2: 'follow' }, { skipUnsupported: true }, 'default')
    await wrapper.get('button[aria-label="卡片视图"]').trigger('click')
    expect(wrapper.findAll('button[aria-label^="收起分组"]')).toHaveLength(2)
    expect(wrapper.get('button[aria-label="批量设置 DNS地址 的 DNS"]').exists()).toBe(true)
  })

  it('uses the default name for three adjacent missing, empty and whitespace names', async () => {
    network.state.rules = [rule('a'), rule('b', ''), rule('c', ' \t ')]
    const wrapper = await render()
    expect(heads(wrapper)).toHaveLength(1)
    expect(heads(wrapper)[0]!.text()).toContain('3 条规则')
    expect(heads(wrapper)[0]!.text()).toContain('01–03')
    expect(heads(wrapper)[0]!.text()).not.toContain('3 项')
    expect(wrapper.get('button[aria-label="收起分组 3 条规则"]').exists()).toBe(true)
  })

  it('keeps three segments when unnamed rules sit between equal named groups, without sharing collapse IDs', async () => {
    network.state.rules = [rule('a1', 'E2E'), rule('a2', 'E2E'), rule('u1'), rule('u2'), rule('b1', 'E2E'), rule('b2', 'E2E')]
    const wrapper = await render()
    expect(heads(wrapper)).toHaveLength(3)
    expect(heads(wrapper).map(head => head.text())).toEqual([
      expect.stringContaining('01–02 · 2 项'), expect.stringContaining('2 条规则03–04'), expect.stringContaining('05–06 · 2 项')
    ])
    await wrapper.findAll('button[aria-label="收起分组 E2E"]')[0]!.trigger('click')
    expect(rows(wrapper)).toEqual(['rule-u1', 'rule-u2', 'rule-b1', 'rule-b2'])
    expect(JSON.parse(localStorage.getItem('cf-rules-collapsed:default')!)).toEqual(['group_rule-a1'])
    ;(wrapper.vm as any).$.setupState.enterReorder()
    await flushPromises()
    await wrapper.get('button[aria-label="E2E 下移"]').trigger('click')
    await clickText('保存顺序')
    const saved = network.post.mock.calls.find(([path]) => path === '/rules/reorder')![1].rule_configs
    expect(saved.map((item: any) => item.id)).toEqual(['u1', 'a1', 'a2', 'u2', 'b1', 'b2'])
  })

  it('keeps a single unnamed rule between two equal named segments outside both groups', async () => {
    network.state.rules = [rule('a1', 'E2E'), rule('a2', 'E2E'), rule('u1'), rule('b1', 'E2E'), rule('b2', 'E2E')]
    const wrapper = await render()
    expect(heads(wrapper)).toHaveLength(2)
    expect(heads(wrapper).map(head => head.text())).toEqual([
      expect.stringContaining('01–02 · 2 项'), expect.stringContaining('04–05 · 2 项')
    ])
    expect(wrapper.get('[data-rule-key="rule-u1"]').classes()).not.toContain('in-group')
    await wrapper.findAll('button[aria-label="收起分组 E2E"]')[0]!.trigger('click')
    expect(rows(wrapper)).toEqual(['rule-u1', 'rule-b1', 'rule-b2'])
  })

  it('renames only the selected segment and preserves both neighboring names', async () => {
    network.state.rules = [rule('left1', 'DNS地址'), rule('left2', 'DNS地址'), rule('e1', 'E2E'), rule('e2', 'E2E'), rule('right1', 'DNS地址'), rule('right2', 'DNS地址')]
    const wrapper = await render()
    expect(heads(wrapper)).toHaveLength(3)
    await heads(wrapper)[1]!.get('button[aria-label="重命名分组"]').trigger('click')
    await flushPromises()
    const input = document.querySelector<HTMLInputElement>('#group-rename')!
    expect(input).toBeTruthy()
    await new DOMWrapper(input).setValue('  Renamed  ')
    await flushPromises()
    await clickText('保存')
    expect(network.rules.renameGroup).toHaveBeenCalledExactlyOnceWith(['e1', 'e2'], 'Renamed', 'default')
    expect(network.state.rules.map(item => item.group_name)).toEqual(['DNS地址', 'DNS地址', 'Renamed', 'Renamed', 'DNS地址', 'DNS地址'])
    expect(heads(wrapper)).toHaveLength(3)
  })

  it('groups trimmed equal names without mutating raw names, but still respects policy and adjacency', async () => {
    network.state.rules = [rule('a', ' E2E '), rule('b', '\tE2E\n'), rule('c', 'E2E', 'Proxy'), rule('d', 'E2E')]
    const wrapper = await render()
    expect(heads(wrapper)).toHaveLength(1)
    expect(wrapper.get('button[aria-label="收起分组 E2E"]').exists()).toBe(true)
    expect(heads(wrapper)[0]!.text()).toContain('01–02 · 2 项')
    expect(network.state.rules.map(item => item.group_name)).toEqual([' E2E ', '\tE2E\n', 'E2E', 'E2E'])
    expect(network.rules.renameGroup).not.toHaveBeenCalled()
  })

  it('keeps collapse only on the segment containing the old first item after a group splits', async () => {
    network.state.rules = [rule('e1', 'E2E'), rule('e2', 'E2E'), rule('d1', 'E2E'), rule('d2', 'E2E')]
    const wrapper = await render()
    await wrapper.get('button[aria-label="收起分组 E2E"]').trigger('click')
    network.state.rules.slice(2).forEach(item => { item.group_name = 'DNS地址' })
    await (wrapper.vm as any).$.setupState.loadAllRules()
    await flushPromises()
    expect(heads(wrapper)).toHaveLength(2)
    expect(wrapper.get('button[aria-label="展开分组 E2E"]').attributes('aria-expanded')).toBe('false')
    expect(wrapper.get('button[aria-label="收起分组 DNS地址"]').attributes('aria-expanded')).toBe('true')
    expect(rows(wrapper)).toEqual(['rule-d1', 'rule-d2'])
    expect(JSON.parse(localStorage.getItem('cf-rules-collapsed:default')!)).toEqual(['group_rule-e1'])
  })

  it('reveals only the matching named segment and keeps the other group collapsed', async () => {
    network.state.rules = [rule('e1', 'E2E'), rule('e2', 'E2E'), rule('d1', 'DNS地址'), rule('d2', 'DNS地址')]
    network.post.mockImplementation(async path => ({ data: path === '/rules/match-test'
      ? { success: true, matched: true, rule_id: 'd2', item_type: 'rule', policy: 'DIRECT', priority: 4, elapsed_time: 1 }
      : { success: true } }))
    vi.useFakeTimers()
    try {
      const wrapper = await render()
      await wrapper.get('button[aria-label="收起分组 E2E"]').trigger('click')
      await wrapper.get('button[aria-label="收起分组 DNS地址"]').trigger('click')
      ;(wrapper.vm as any).$.setupState.simQuery = 'd2.test'
      const done = (wrapper.vm as any).$.setupState.simulate()
      await vi.runAllTimersAsync()
      await done
      await flushPromises()
      expect(rows(wrapper)).toEqual(['rule-d1', 'rule-d2'])
      expect(wrapper.get('.rule-row.is-match').attributes('data-rule-key')).toBe('rule-d2')
      expect(JSON.parse(localStorage.getItem('cf-rules-collapsed:default')!)).toEqual(['group_rule-e1', 'group_rule-d1'])
    } finally { vi.useRealTimers() }
  })
})
