import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import Rules from '@/views/Rules.vue'
import { setActiveProfileId } from '@/profileContext'
import { confirmState, notify, settleConfirm } from '@/lib/feedback'

// Only HTTP boundaries are replaced; the page, selector and confirm dialog stay real.
const network = vi.hoisted(() => {
  const state = { rules: [] as Record<string, any>[], dns: {} as Record<string, any> }
  const rules = {
    getAll: vi.fn(async (_profileId?: string) => ({ data: structuredClone(state.rules) })),
    renameGroup: vi.fn(async () => ({ data: { success: true } })),
    findDuplicates: vi.fn(async () => ({ data: {} })),
    getDns: vi.fn(async (_profileId?: string): Promise<{ data: any }> => ({ data: structuredClone(state.dns) })),
    setDns: vi.fn(),
    setPolicy: vi.fn(async (ids: string[], policy: string, options: { groupName?: string } = {}, _profileId?: string): Promise<{ data: any }> => {
      for (const item of state.rules) {
        if (!ids.includes(item.id)) continue
        item.policy = policy
        if (options.groupName !== undefined) item.group_name = options.groupName
      }
      return { data: { success: true } }
    }),
    compose: vi.fn()
  }
  return { state, rules }
})

vi.mock('@/api', () => ({
  default: { get: vi.fn(async () => ({ data: [] })), post: vi.fn(async () => ({ data: { success: true } })), put: vi.fn(), delete: vi.fn() },
  ruleApi: network.rules,
  ruleSetApi: { update: vi.fn(), delete: vi.fn() },
  proxyGroupApi: { getAll: vi.fn(async () => ({ data: [{ id: 'g', name: 'Proxy' }, { id: 'j', name: 'Japan' }] })) }
}))

const rule = (id: string, value: string, policy: string, patch: Record<string, unknown> = {}) =>
  ({ id, itemType: 'rule', rule_type: 'DOMAIN-SUFFIX', value, policy, enabled: true, ...patch })
const engine = { mode: 'template', editable: true, reason: '', default_forward: 'forward_remote', prefer_ipv4: true,
  local_probe_active: false, oxidns_in_use: true, warnings: [], agents: [] }

const wrappers: VueWrapper[] = []

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  setActiveProfileId('default')
  network.state.rules = [
    rule('r-lan', 'lan.test', 'DIRECT'),
    rule('r-a', 'a.test', 'Proxy', { group_name: 'AI' }),
    rule('r-b', 'b.test', 'Proxy', { group_name: 'AI' }),
    rule('r-c', 'c.test', 'Proxy', { group_name: 'AI' }),
    rule('r-end', 'end.test', 'REJECT')
  ]
  network.state.dns = {
    success: true, engine, mosdns: {}, upstreams: [],
    rules: {
      'r-lan': { state: 'follow', route: 'direct', follow_route: 'direct' },
      'r-a': { state: 'follow', route: 'proxy', follow_route: 'proxy' },
      'r-b': { state: 'explicit', route: 'direct' },
      'r-c': { state: 'explicit', route: 'proxy' },
      'r-end': { state: 'default', route: null }
    }
  }
  vi.stubGlobal('matchMedia', vi.fn((query: string) => ({
    matches: false, media: query, onchange: null, addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn()
  })))
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
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
  const wrapper = mount(Rules, { attachTo: document.body })
  wrappers.push(wrapper)
  await flushPromises()
  return wrapper
}
const options = () => Array.from(document.querySelectorAll<HTMLElement>('[role="option"]'))
const pick = async (wrapper: VueWrapper, label: string, policy: string) => {
  await wrapper.get(`button[aria-label^="${label}"]`).trigger('click')
  await flushPromises()
  options().find(item => item.textContent?.trim() === policy)!.click()
  await flushPromises()
}

describe('inline policy selection', () => {
  it('lists built-in policies and groups, marking a deleted current group', async () => {
    network.state.rules[0]!.policy = 'Gone'
    const wrapper = await render()
    await wrapper.get('button[aria-label^="lan.test 的策略"]').trigger('click')
    await flushPromises()
    expect(options().map(item => item.textContent?.trim())).toEqual(['DIRECT', 'REJECT', 'Gone已不存在', 'Proxy', 'Japan'])
  })

  it('moves a single row out of its group and clears its group name', async () => {
    const wrapper = await render()
    expect(wrapper.get('button[aria-label="b.test 的策略：Proxy，点击修改"]').classes()).toContain('text-muted-foreground')
    await pick(wrapper, 'b.test 的策略', 'Japan')
    expect(network.rules.setPolicy).toHaveBeenCalledExactlyOnceWith(['r-b'], 'Japan', { groupName: '' }, 'default')
    expect(confirmState.open).toBe(false)
    expect(network.rules.getAll).toHaveBeenCalledTimes(2)
    // The explicit DNS stays and the toast says so.
    expect(notify.warning).toHaveBeenCalledWith('策略已改为「Japan」', expect.stringContaining('1 项 DNS 为单独设置，不随策略变化'), expect.anything())
    expect(wrapper.get('button[aria-label^="b.test 的策略"]').text()).toBe('Japan')
  })

  it('does nothing when the current policy is picked again', async () => {
    const wrapper = await render()
    await pick(wrapper, 'lan.test 的策略', 'DIRECT')
    expect(network.rules.setPolicy).not.toHaveBeenCalled()
  })

  it('changes a whole group after confirming the impact and keeps its name', async () => {
    const wrapper = await render()
    await pick(wrapper, 'AI 的策略', 'DIRECT')
    expect(confirmState.open).toBe(true)
    expect(confirmState.description).toBe('将把「AI」的 3 项策略从「Proxy」改为「DIRECT」，组名保留。跟随策略的 DNS 会一起变化；1 项 DNS 为单独设置，不随策略变化。')
    settleConfirm('cancel')
    await flushPromises()
    expect(network.rules.setPolicy).not.toHaveBeenCalled()

    await pick(wrapper, 'AI 的策略', 'DIRECT')
    settleConfirm('confirm')
    await flushPromises()
    expect(network.rules.setPolicy).toHaveBeenCalledExactlyOnceWith(['r-a', 'r-b', 'r-c'], 'DIRECT', {}, 'default')
    expect(notify.warning).toHaveBeenCalledWith('已将 3 项的策略改为「DIRECT」', expect.stringContaining('1 项 DNS 为单独设置'), expect.anything())
    // DIRECT now spans lan.test and the group; the merged group keeps the AI name.
    expect(wrapper.find('[data-group-head]').text()).toContain('AI')
    expect(wrapper.find('[data-group-head]').text()).toContain('01–04')
  })

  it('reports a failed change and stays usable', async () => {
    network.rules.setPolicy.mockRejectedValueOnce(Object.assign(new Error('boom'), { isAxiosError: true, response: { data: { message: '策略不存在：Japan' } } }))
    const wrapper = await render()
    await pick(wrapper, 'lan.test 的策略', 'Japan')
    expect(notify.error).toHaveBeenCalledWith('策略不存在：Japan')
    expect(wrapper.get('button[aria-label^="lan.test 的策略"]').attributes('disabled')).toBeUndefined()
  })

  it('locks the selectors while reordering', async () => {
    const wrapper = await render()
    ;(wrapper.vm as any).$.setupState.enterReorder()
    await flushPromises()
    expect(wrapper.get('button[aria-label^="lan.test 的策略"]').attributes('disabled')).toBeDefined()
  })

  it('offers the selector on card footers and card section headers', async () => {
    const wrapper = await render()
    ;(wrapper.vm as any).$.setupState.viewMode = 'card'
    await flushPromises()
    expect(wrapper.find('button[aria-label="AI 的策略：Proxy，点击修改整组"]').exists()).toBe(true)
    await pick(wrapper, 'end.test 的策略', 'DIRECT')
    expect(network.rules.setPolicy).toHaveBeenCalledWith(['r-end'], 'DIRECT', { groupName: '' }, 'default')
  })
})
