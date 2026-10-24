import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import Rules from '@/views/Rules.vue'
import { setActiveProfileId } from '@/profileContext'
import { notify } from '@/lib/feedback'

// Only HTTP boundaries are replaced; the page, rows and reorder controls stay real.
const network = vi.hoisted(() => {
  const state = { rules: [] as Record<string, any>[] }
  const rules = {
    getAll: vi.fn(async (_profileId?: string) => ({ data: structuredClone(state.rules) })),
    renameGroup: vi.fn(async () => ({ data: { success: true } })),
    findDuplicates: vi.fn(async () => ({ data: {} })),
    getDns: vi.fn(async (_profileId?: string): Promise<{ data: any }> => ({ data: { engine: { oxidns_in_use: false }, rules: {} } })),
    setDns: vi.fn(),
    compose: vi.fn()
  }
  const get = vi.fn(async () => ({ data: [] }))
  const post = vi.fn(async (_path: string, _body?: any, _config?: any): Promise<{ data: any }> => ({ data: { success: true } }))
  return { state, rules, get, post }
})

vi.mock('@/api', () => ({
  default: { get: network.get, post: network.post, put: vi.fn(), delete: vi.fn() },
  ruleApi: network.rules,
  ruleSetApi: { update: vi.fn(), delete: vi.fn() },
  proxyGroupApi: { getAll: vi.fn(async () => ({ data: [{ id: 'g', name: 'Proxy' }] })), previewRegex: vi.fn() }
}))

const rule = (id: string, value: string, policy: string, patch: Record<string, unknown> = {}) =>
  ({ id, itemType: 'rule', rule_type: 'DOMAIN-SUFFIX', value, policy, enabled: true, ...patch })

const wrappers: VueWrapper[] = []

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  setActiveProfileId('default')
  network.state.rules = [
    rule('r-lan', 'lan', 'DIRECT', { rule_type: 'GEOIP' }),
    rule('r-a', 'a.test', 'Proxy', { group_name: 'DNS地址' }),
    rule('r-b', 'b.test', 'Proxy', { group_name: 'DNS地址' }),
    rule('r-c', 'c.test', 'Proxy', { group_name: 'DNS地址' }),
    rule('r-home', '192.168.0.0/24', 'Home', { rule_type: 'IP-CIDR' }),
    rule('r-x', 'x.test', 'DIRECT'),
    rule('r-y', 'y.test', 'DIRECT')
  ]
  vi.stubGlobal('matchMedia', vi.fn((query: string) => ({
    matches: false, media: query, onchange: null, addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn()
  })))
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  vi.stubGlobal('scrollTo', vi.fn())
  for (const kind of ['success', 'warning', 'error'] as const) vi.spyOn(notify, kind).mockImplementation(() => 0)
})

afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

async function render() {
  const wrapper = mount(Rules, { attachTo: document.body })
  wrappers.push(wrapper)
  await flushPromises()
  return wrapper
}
const heads = (wrapper: VueWrapper) => wrapper.findAll('[data-group-head]')
const rowValues = (wrapper: VueWrapper) => wrapper.findAll('.rule-row').map(row => row.find('.font-mono.text-\\[12\\.5px\\]').text())
const toggle = (wrapper: VueWrapper, name: string) => wrapper.find(`button[aria-label$="分组 ${name}"]`)
const clickText = async (text: string) => {
  const button = Array.from(document.querySelectorAll<HTMLButtonElement>('button')).find(item => item.textContent?.trim() === text)
  expect(button, `button ${text}`).toBeTruthy()
  button!.click()
  await flushPromises()
}

describe('rule group sections', () => {
  it('expands groups by default with a header above members that keep their own rows', async () => {
    const wrapper = await render()
    expect(heads(wrapper)).toHaveLength(2)
    const named = heads(wrapper)[0]!
    expect(named.text()).toContain('DNS地址')
    expect(named.text()).toContain('02–04 · 3 项')
    expect(toggle(wrapper, 'DNS地址').attributes('aria-expanded')).toBe('true')
    // The unnamed group falls back to its default name and repeats no count.
    expect(heads(wrapper)[1]!.text()).toContain('2 条规则')
    expect(heads(wrapper)[1]!.text()).toContain('06–07')
    expect(heads(wrapper)[1]!.text()).not.toContain('项')

    expect(rowValues(wrapper)).toEqual(['lan', 'a.test', 'b.test', 'c.test', '192.168.0.0/24', 'x.test', 'y.test'])
    const member = wrapper.findAll('.rule-row')[1]!
    expect(member.classes()).toContain('in-group')
    expect(member.find('.chip').exists()).toBe(false) // the header carries the policy chip
    expect(wrapper.findAll('.rule-row')[3]!.classes()).toContain('border-border')
    expect(wrapper.findAll('.rule-row')[0]!.find('.chip').text()).toBe('DIRECT')
    expect(wrapper.text()).toContain('共 7 项')
  })

  it('remembers collapsed groups per profile and previews their members', async () => {
    const wrapper = await render()
    await toggle(wrapper, 'DNS地址').trigger('click')
    expect(toggle(wrapper, 'DNS地址').attributes('aria-expanded')).toBe('false')
    expect(rowValues(wrapper)).toEqual(['lan', '192.168.0.0/24', 'x.test', 'y.test'])
    expect(heads(wrapper)[0]!.text()).toContain('a.test、b.test、c.test')

    wrappers.splice(0).forEach(item => item.unmount())
    const again = await render()
    expect(rowValues(again)).toEqual(['lan', '192.168.0.0/24', 'x.test', 'y.test'])
    await toggle(again, 'DNS地址').trigger('click')
    expect(rowValues(again)).toHaveLength(7)
    expect(JSON.parse(localStorage.getItem('cf-rules-collapsed:default')!)).toEqual([])
  })

  it('still renders when local storage cannot be read', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('blocked') })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('blocked') })
    const wrapper = await render()
    expect(rowValues(wrapper)).toHaveLength(7)
    await toggle(wrapper, 'DNS地址').trigger('click')
    expect(rowValues(wrapper)).toHaveLength(4)
  })

  it('collapses and expands every group from the menu', async () => {
    const wrapper = await render()
    ;(wrapper.vm as any).$.setupState.toggleAllGroups()
    await flushPromises()
    expect(rowValues(wrapper)).toEqual(['lan', '192.168.0.0/24'])
    ;(wrapper.vm as any).$.setupState.toggleAllGroups()
    await flushPromises()
    expect(rowValues(wrapper)).toHaveLength(7)
  })

  it('reorders expanded members one by one and collapsed groups as a unit', async () => {
    const wrapper = await render()
    await toggle(wrapper, '2 条规则').trigger('click')
    ;(wrapper.vm as any).$.setupState.enterReorder()
    await flushPromises()
    const units = (wrapper.vm as any).$.setupState.reorderDisplayItems
    // Headers of expanded groups are not sortable; collapsed groups move whole.
    expect(units.map((item: any) => item.uniqueId)).toEqual(['rule-r-lan', 'rule-r-a', 'rule-r-b', 'rule-r-c', 'rule-r-home', 'group_rule-r-x_DIRECT'])

    await wrapper.get('button[aria-label="2 条规则 上移"]').trigger('click')
    await clickText('保存顺序')
    const call = network.post.mock.calls.find(([path]) => path === '/rules/reorder')!
    expect(call[1].rule_configs.map((item: any) => item.id)).toEqual(['r-lan', 'r-a', 'r-b', 'r-c', 'r-x', 'r-y', 'r-home'])
    // Saving keeps the remembered state instead of collapsing everything.
    expect(toggle(wrapper, 'DNS地址').attributes('aria-expanded')).toBe('true')
  })

  it('reveals a collapsed group for a simulation hit without forgetting it was collapsed', async () => {
    network.post.mockImplementation(async (path: string) => ({
      data: path === '/rules/match-test'
        ? { success: true, matched: true, rule_id: 'r-b', item_type: 'rule', policy: 'Proxy', priority: 3, matched_line: 'DOMAIN-SUFFIX,b.test,Proxy', elapsed_time: 1 }
        : { success: true }
    }))
    vi.useFakeTimers()
    try {
      const wrapper = await render()
      await toggle(wrapper, 'DNS地址').trigger('click')
      ;(wrapper.vm as any).$.setupState.simQuery = 'b.test'
      const done = (wrapper.vm as any).$.setupState.simulate()
      await vi.runAllTimersAsync()
      await done
      await flushPromises()
      expect(rowValues(wrapper)).toContain('b.test')
      expect(wrapper.find('.rule-row.is-match').text()).toContain('b.test')
      expect(JSON.parse(localStorage.getItem('cf-rules-collapsed:default')!)).toEqual(['group_rule-r-a_Proxy'])
    } finally {
      vi.useRealTimers()
    }
  })

  it('keeps a collapsed group collapsed when a new rule becomes its first member', async () => {
    const wrapper = await render()
    await toggle(wrapper, 'DNS地址').trigger('click')
    network.state.rules.splice(1, 0, rule('r-new', 'new.test', 'Proxy'))
    await (wrapper.vm as any).$.setupState.loadAllRules()
    await flushPromises()
    expect(toggle(wrapper, 'DNS地址').attributes('aria-expanded')).toBe('false')
    expect(JSON.parse(localStorage.getItem('cf-rules-collapsed:default')!)).toEqual(['group_rule-r-new_Proxy'])
  })

  it('drops group styling from members while reordering', async () => {
    const wrapper = await render()
    ;(wrapper.vm as any).$.setupState.enterReorder()
    await flushPromises()
    expect(wrapper.findAll('.rule-row.in-group')).toHaveLength(0)
    expect(wrapper.findAll('.rule-row')[1]!.find('.chip').text()).toBe('Proxy')
  })

  it('ends each card section with a full-width divider outside reorder mode only', async () => {
    const wrapper = await render()
    ;(wrapper.vm as any).$.setupState.viewMode = 'card'
    await flushPromises()
    const children = () => Array.from(document.querySelector('#sortable-rules')!.children)
    expect(children().filter(el => el.getAttribute('aria-hidden') === 'true' && el.classList.contains('col-span-full'))).toHaveLength(2)
    ;(wrapper.vm as any).$.setupState.enterReorder()
    await flushPromises()
    // Every child stays a single draggable card for Sortable.
    expect(children().every(el => el.hasAttribute('data-reorder-item') && el.getAttribute('aria-hidden') !== 'true')).toBe(true)
  })
})
