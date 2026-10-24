import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import Rules from '@/views/Rules.vue'
import RuleEditorDialog from '@/components/rules/RuleEditorDialog.vue'
import { setActiveProfileId } from '@/profileContext'
import { confirmState, notify, settleConfirm } from '@/lib/feedback'

// Only HTTP boundaries are replaced; the page, cells, selector and dialogs stay real.
const network = vi.hoisted(() => {
  const state = { rules: [] as Record<string, any>[], dns: {} as Record<string, any> }
  const rules = {
    getAll: vi.fn(async (_profileId?: string) => ({ data: structuredClone(state.rules) })),
    update: vi.fn(async (_id: string, _body: Record<string, unknown>, _profileId?: string) => ({ data: { success: true } })),
    delete: vi.fn(async () => ({ data: {} })),
    renameGroup: vi.fn(async () => ({ data: { success: true } })),
    findDuplicates: vi.fn(async () => ({ data: {} })),
    getDns: vi.fn(async (_profileId?: string): Promise<{ data: any }> => ({ data: structuredClone(state.dns) })),
    setDns: vi.fn(async (routes: Record<string, string>, _options?: unknown, _profileId?: string) => {
      const rules = Object.fromEntries(Object.entries(routes).map(([id, route]) => [id, { state: 'explicit', route }]))
      Object.assign(state.dns.rules, rules)
      return { data: { success: true, skipped: [], warnings: [], rules } }
    }),
    compose: vi.fn()
  }
  const sets = { update: vi.fn(async () => ({ data: { success: true } })), delete: vi.fn(async () => ({ data: {} })) }
  const get = vi.fn(async (path: string) => ({ data: path === '/rule-library' ? [] : [] }))
  const post = vi.fn(async () => ({ data: { success: true } }))
  return { state, rules, sets, get, post }
})

vi.mock('@/api', () => ({
  default: { get: network.get, post: network.post, put: vi.fn(), delete: vi.fn() },
  ruleApi: network.rules,
  ruleSetApi: network.sets,
  proxyGroupApi: { getAll: vi.fn(async () => ({ data: [{ id: 'g', name: 'Proxy' }] })) }
}))

const engine = (patch: Record<string, unknown> = {}) => ({
  mode: 'template', editable: true, reason: '', default_forward: 'forward_remote', prefer_ipv4: true,
  local_probe_active: false, oxidns_in_use: true, warnings: [], agents: [], ...patch
})
const rule = (id: string, value: string, policy: string, patch: Record<string, unknown> = {}) =>
  ({ id, itemType: 'rule', rule_type: 'DOMAIN-SUFFIX', value, policy, enabled: true, ...patch })

const wrappers: VueWrapper[] = []
const scrollDescriptor = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollIntoView')

beforeEach(() => {
  vi.clearAllMocks()
  setActiveProfileId('default')
  network.state.rules = [
    rule('r-ads', 'doubleclick.net', 'REJECT'),
    rule('r-ai', 'openai.com', 'Proxy'),
    rule('r-cn', 'baidu.com', 'DIRECT'),
    rule('r-ip', '10.0.0.0/8', 'Proxy', { rule_type: 'IP-CIDR' }),
    rule('r-geo', 'CN', 'REJECT', { rule_type: 'GEOIP' }),
    rule('r-moved', 'moved.example', 'DIRECT'),
    rule('r-off', 'old.example', 'Proxy', { enabled: false }),
    rule('r-corp', 'corp.lan', 'REJECT-DROP')
  ]
  network.state.dns = {
    success: true, engine: engine(), mosdns: {},
    upstreams: [
      { key: 'b2e55ef47454', source: 'local', addr: 'udp://223.5.5.5', label: '直连 · 223.5.5.5', linked: false },
      { key: '6f01aa93c2d0', source: 'remote', addr: 'udp://127.0.0.1:1053', label: '代理 · 127.0.0.1:1053', linked: true }
    ],
    rules: {
      'r-ads': { state: 'explicit', route: 'nxdomain' },
      'r-ai': { state: 'follow', route: 'proxy', follow_route: 'proxy' },
      'r-cn': { state: 'follow', route: 'direct', follow_route: 'direct' },
      'r-geo': { state: 'unsupported', reason: 'GEOIP 规则不参与 DNS 分流' },
      'r-moved': { state: 'follow_drift', route: 'proxy', follow_route: 'direct' },
      'r-off': { state: 'disabled', route: 'blackhole' },
      'r-corp': { state: 'explicit', route: 'upstream:0123456789ab', upstream_label: '直连 · 10.0.0.53' }
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
  if (scrollDescriptor) Object.defineProperty(Element.prototype, 'scrollIntoView', scrollDescriptor)
  else Reflect.deleteProperty(Element.prototype, 'scrollIntoView')
  document.body.innerHTML = ''
})

async function render() {
  const wrapper = mount(Rules, { attachTo: document.body })
  wrappers.push(wrapper)
  await flushPromises()
  return wrapper
}
const row = (wrapper: VueWrapper, value: string) => wrapper.findAll('.rule-row').find(item => item.text().includes(value))!
const dnsTrigger = (wrapper: VueWrapper, value: string) => row(wrapper, value).find(`button[aria-label^="${value} 的 DNS："]`)
const options = () => Array.from(document.querySelectorAll<HTMLElement>('[role="option"]'))

describe('rule DNS column', () => {
  it('shows each state with its own wording, colour only for blocks and drift', async () => {
    // Neighbouring rules have different policies, so each stays on its own row.
    const wrapper = await render()
    const text = (value: string) => row(wrapper, value).text()
    expect(text('doubleclick.net')).toContain('不存在')
    expect(dnsTrigger(wrapper, 'doubleclick.net').classes()).toContain('chip-bad')
    expect(text('openai.com')).toContain('跟随 · 代理')
    expect(text('baidu.com')).toContain('跟随 · 直连')
    expect(text('10.0.0.0/8')).toContain('默认 · 代理 DNS')
    expect(row(wrapper, 'CN').get('[aria-label="GEOIP 规则不参与 DNS 分流"]').text()).toBe('不参与')
    expect(text('moved.example')).toContain('代理 · 与策略不一致')
    expect(dnsTrigger(wrapper, 'moved.example').classes()).toContain('chip-warn')
    expect(text('old.example')).toContain('已停用 · 启用后为黑洞')
    expect(dnsTrigger(wrapper, 'old.example').exists()).toBe(false)
    // Pinned upstreams show the bare address; the source is in the accessible name.
    expect(dnsTrigger(wrapper, 'corp.lan').text()).toBe('10.0.0.53')
    expect(dnsTrigger(wrapper, 'corp.lan').attributes('aria-label')).toBe('corp.lan 的 DNS：指定上游 · 直连 · 10.0.0.53，点击修改')
    expect(wrapper.html()).not.toContain('chip-sky')
  })

  it('labels the selector with the matched value and the current handling', async () => {
    const wrapper = await render()
    expect(dnsTrigger(wrapper, 'doubleclick.net').attributes('aria-label')).toBe('doubleclick.net 的 DNS：不存在，点击修改')
    expect(dnsTrigger(wrapper, 'openai.com').attributes('aria-label')).toBe('openai.com 的 DNS：跟随策略：代理 DNS，点击修改')
  })

  it('shows skeleton chips while loading and never the default upstream', async () => {
    let resolve!: (value: { data: any }) => void
    network.rules.getDns.mockImplementationOnce(() => new Promise(done => { resolve = done }))
    const wrapper = await render()
    expect(wrapper.findAll('[aria-label="DNS 状态加载中"]').length).toBeGreaterThan(0)
    expect(wrapper.text()).not.toContain('默认 · ')
    resolve({ data: structuredClone(network.state.dns) })
    await flushPromises()
    expect(wrapper.find('[aria-label="DNS 状态加载中"]').exists()).toBe(false)
  })

  it('switches DNS with PUT /rules/dns, showing progress and rolling back on failure', async () => {
    const wrapper = await render()
    await dnsTrigger(wrapper, 'openai.com').trigger('click')
    await flushPromises()
    let reject!: (error: unknown) => void
    network.rules.setDns.mockImplementationOnce(() => new Promise((_, fail) => { reject = fail }))
    options().find(item => item.textContent?.includes('黑洞'))!.click()
    await flushPromises()
    expect(network.rules.setDns).toHaveBeenCalledWith({ 'r-ai': 'blackhole' }, {}, 'default')
    const pending = dnsTrigger(wrapper, 'openai.com')
    expect(pending.find('.animate-spin').exists()).toBe(true)
    expect(pending.text()).toContain('黑洞')
    reject({ isAxiosError: true, response: { status: 409, data: { message: '规则仍在使用', usages: [{ code: 'oxidns_routing_custom', reason: 'sequence_main 含手写条目' }] } } })
    await flushPromises()
    expect(notify.error).toHaveBeenCalledWith('sequence_main 含手写条目')
    expect(dnsTrigger(wrapper, 'openai.com').text()).toContain('跟随 · 代理')
    expect(dnsTrigger(wrapper, 'openai.com').find('.animate-spin').exists()).toBe(false)
  })

  it('applies a successful switch to the row and reports how it reaches devices', async () => {
    network.state.dns.engine.agents = [{ id: 'a', name: 'OxiDNS', service_type: 'oxidns', auto_sync: { rules: true, library: false } }]
    const wrapper = await render()
    await dnsTrigger(wrapper, 'openai.com').trigger('click')
    await flushPromises()
    options().find(item => item.textContent?.includes('不存在'))!.click()
    await flushPromises()
    expect(dnsTrigger(wrapper, 'openai.com').text()).toContain('不存在')
    expect(notify.success).toHaveBeenCalledWith('DNS 已更新', '1 台已开启规则自动同步，将自动更新', undefined)
  })

  it('ignores reselecting the current value but lets a drifted follow rule resync to its policy', async () => {
    const wrapper = await render()
    await dnsTrigger(wrapper, 'openai.com').trigger('click')
    await flushPromises()
    options().find(item => item.querySelector('.block')?.textContent?.trim() === '跟随策略')!.click()
    await flushPromises()
    expect(network.rules.setDns).not.toHaveBeenCalled()
    await dnsTrigger(wrapper, 'moved.example').trigger('click')
    await flushPromises()
    options().find(item => item.querySelector('.block')?.textContent?.trim() === '跟随策略')!.click()
    await flushPromises()
    expect(network.rules.setDns).toHaveBeenCalledWith({ 'r-moved': 'follow' }, {}, 'default')
  })

  it('keeps linked upstreams disabled and the current pinned address that left the list', async () => {
    const wrapper = await render()
    await dnsTrigger(wrapper, 'corp.lan').trigger('click')
    await flushPromises()
    expect(options().find(item => item.textContent?.includes('127.0.0.1:1053'))!.hasAttribute('data-disabled')).toBe(true)
    expect(options().some(item => item.textContent?.includes('10.0.0.53（已不在上游列表）'))).toBe(true)
  })

  it('keeps the column after a failed read, explains it and retries', async () => {
    network.rules.getDns.mockRejectedValueOnce(new Error('offline'))
    const wrapper = await render()
    expect(wrapper.get('[data-dns-error]').text()).toContain('DNS 状态读取失败')
    expect(row(wrapper, 'openai.com').get('[aria-label="DNS 状态读取失败"]').text()).toBe('—')
    const retry = wrapper.get('[data-dns-error]').findAll('button').find(button => button.text() === '重试')!
    await retry.trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-dns-error]').exists()).toBe(false)
    expect(row(wrapper, 'openai.com').text()).toContain('跟随 · 代理')
  })

  it('hides the column for profiles that do not use OxiDNS', async () => {
    network.state.dns.engine.oxidns_in_use = false
    const wrapper = await render()
    expect(wrapper.text()).not.toContain('跟随 · ')
    expect(wrapper.find('[aria-label="策略规则列表"] > div').classes()).toContain('min-w-[680px]')
  })

  it('shows a locked native routing as read-only and links to the full parameters', async () => {
    network.state.dns.engine = engine({ mode: 'native', editable: false, reason: 'sequence_main 含无法识别的条目' })
    const wrapper = await render()
    expect(wrapper.get('[data-dns-locked]').text()).toContain('sequence_main 含无法识别的条目')
    expect(wrapper.get('[data-dns-locked] a').attributes()).toMatchObject({ href: '/generate?oxidns=config', target: '_blank' })
    expect(row(wrapper, 'openai.com').text()).toContain('完整参数')
    expect(dnsTrigger(wrapper, 'openai.com').exists()).toBe(false)
    const chip = row(wrapper, 'openai.com').get('[aria-label^="openai.com 的 DNS"]')
    expect(chip.attributes('aria-label')).toBe('openai.com 的 DNS：代理 DNS，需在完整参数修改')
  })

  it('summarizes groups read-only and confirms the impact before a batch change', async () => {
    network.state.rules = [rule('g1', 'a.test', 'Proxy'), rule('g2', 'b.test', 'Proxy'), rule('g3', 'c.test', 'Proxy'), rule('g4', 'CN', 'Proxy', { rule_type: 'GEOIP' })]
    network.state.dns.rules = {
      g1: { state: 'follow', route: 'proxy', follow_route: 'proxy' },
      g2: { state: 'explicit', route: 'blackhole' },
      g3: { state: 'explicit', route: 'upstream:b2e55ef47454', upstream_label: '直连 · 223.5.5.5' },
      g4: { state: 'unsupported', reason: 'GEOIP 规则不参与 DNS 分流' }
    }
    const wrapper = await render()
    const summary = wrapper.get('[data-group-dns]')
    expect(summary.text()).toBe('DNS 混合 1/4')
    expect(summary.find('button').exists()).toBe(false)
    await wrapper.get('button[aria-label="批量设置 4 条规则 的 DNS"]').trigger('click')
    await flushPromises()
    expect(network.rules.getAll).toHaveBeenCalledTimes(1) // the group did not expand or reload
    const apply = Array.from(document.querySelectorAll<HTMLButtonElement>('[role="dialog"] button')).find(button => button.textContent?.trim() === '应用')!
    expect(document.querySelector('[data-batch-impact]')?.textContent).toBe(
      '将把 4 项的 DNS 设为「跟随策略 · 代理 DNS」，将覆盖 2 条单独设置（黑洞 1 · 指定上游 1），跳过 1 条 GEOIP')
    apply.click()
    await flushPromises()
    expect(confirmState.open).toBe(true)
    expect(confirmState.description).toContain('将覆盖 2 条单独设置（黑洞 1 · 指定上游 1）')
    expect(network.rules.setDns).not.toHaveBeenCalled()
    settleConfirm('confirm')
    await flushPromises()
    expect(network.rules.setDns).toHaveBeenCalledWith({ g1: 'follow', g2: 'follow', g3: 'follow', g4: 'follow' }, { skipUnsupported: true }, 'default')
  })

  it('puts the card DNS on its own truncating line above the footer', async () => {
    const wrapper = await render()
    ;(wrapper.vm as any).$.setupState.viewMode = 'card'
    await flushPromises()
    const line = wrapper.findAll('[data-card-dns]')[0]!
    expect(line.classes()).toEqual(expect.arrayContaining(['w-full', 'min-w-0']))
    expect(line.element.nextElementSibling?.tagName).toBe('FOOTER')
    expect(line.find('.truncate').exists()).toBe(true)
  })

  it('makes DNS cells inert while reordering', async () => {
    const wrapper = await render()
    ;(wrapper.vm as any).$.setupState.enterReorder()
    await flushPromises()
    const trigger = dnsTrigger(wrapper, 'openai.com')
    expect(trigger.attributes('disabled')).toBeDefined()
    expect(trigger.element.closest('.cf-reorder-mute')).not.toBeNull()
  })

  it('enables a disabled duplicate from the editor notice', async () => {
    const wrapper = await render()
    wrapper.findComponent(RuleEditorDialog).vm.$emit('enable', 'r-off')
    await flushPromises()
    expect(network.rules.update).toHaveBeenCalledTimes(1)
    const [id, payload] = network.rules.update.mock.calls[0]!
    expect(id).toBe('r-off')
    expect(payload).toMatchObject({ enabled: true })
    expect(payload).not.toHaveProperty('dns')
    expect(notify.success).toHaveBeenCalledWith('状态已更新')
  })

  it('shows a skeleton for saved rows until DNS reloads, never the default upstream', async () => {
    const wrapper = await render()
    network.state.rules = [rule('r-new', 'new.example', 'Proxy'), ...network.state.rules]
    let resolve!: (value: { data: any }) => void
    network.rules.getDns.mockImplementationOnce(() => new Promise(done => { resolve = done }))
    wrapper.findComponent(RuleEditorDialog).vm.$emit('saved', { ids: ['r-new', 'r-ai'] })
    await flushPromises()
    expect(row(wrapper, 'new.example').find('[aria-label="DNS 状态加载中"]').exists()).toBe(true)
    expect(row(wrapper, 'new.example').text()).not.toContain('默认')
    // An edited row may have changed too; untouched rows keep their chip.
    expect(row(wrapper, 'openai.com').find('[aria-label="DNS 状态加载中"]').exists()).toBe(true)
    expect(row(wrapper, 'baidu.com').text()).toContain('跟随 · 直连')
    // The editor treats the unknown state as not loaded rather than as the default upstream.
    ;(wrapper.vm as any).$.setupState.editItem({ id: 'r-new', itemType: 'rule' })
    await flushPromises()
    expect(wrapper.findComponent(RuleEditorDialog).props('dns')).toBeNull()
    ;(wrapper.vm as any).$.setupState.editorOpen = false
    network.state.dns.rules['r-new'] = { state: 'explicit', route: 'blackhole' }
    resolve({ data: structuredClone(network.state.dns) })
    await flushPromises()
    expect(row(wrapper, 'new.example').text()).toContain('黑洞')
  })

  it('uses the DNS returned by compose right away and shows — when the reload fails', async () => {
    const wrapper = await render()
    network.state.rules = [rule('r-new', 'new.example', 'Proxy'), rule('r-plain', 'plain.example', 'DIRECT'), ...network.state.rules]
    let reject!: (error: unknown) => void
    network.rules.getDns.mockImplementationOnce(() => new Promise((_, fail) => { reject = fail }))
    wrapper.findComponent(RuleEditorDialog).vm.$emit('saved', { ids: ['r-new', 'r-plain'], dns: { 'r-new': { state: 'explicit', route: 'blackhole' } } })
    await flushPromises()
    expect(row(wrapper, 'new.example').text()).toContain('黑洞')
    expect(row(wrapper, 'plain.example').find('[aria-label="DNS 状态加载中"]').exists()).toBe(true)
    reject(new Error('offline'))
    await flushPromises()
    expect(row(wrapper, 'plain.example').get('[aria-label="DNS 状态读取失败"]').text()).toBe('—')
    expect(row(wrapper, 'new.example').text()).toContain('黑洞')
  })

  it('shows a skeleton for rows that appear while DNS reloads, e.g. when returning to the page', async () => {
    const wrapper = await render()
    network.state.rules = [rule('r-lib', 'lib.example', 'Proxy'), ...network.state.rules]
    let resolve!: (value: { data: any }) => void
    network.rules.getDns.mockImplementationOnce(() => new Promise(done => { resolve = done }))
    const setup = (wrapper.vm as any).$.setupState
    void setup.loadDns()
    await setup.loadAllRules()
    await flushPromises()
    expect(row(wrapper, 'lib.example').find('[aria-label="DNS 状态加载中"]').exists()).toBe(true)
    expect(row(wrapper, 'openai.com').text()).toContain('跟随 · 代理')
    resolve({ data: structuredClone(network.state.dns) })
    await flushPromises()
    expect(row(wrapper, 'lib.example').text()).toContain('默认 · 代理 DNS')
  })

  it('keeps rows read-only and the editor unloaded after a failed refresh, as the banner says', async () => {
    const wrapper = await render()
    expect(dnsTrigger(wrapper, 'openai.com').attributes('disabled')).toBeUndefined()
    network.rules.getDns.mockRejectedValueOnce(new Error('offline'))
    await (wrapper.vm as any).$.setupState.loadDns()
    await flushPromises()
    expect(wrapper.get('[data-dns-error]').text()).toContain('DNS 列暂不可修改')
    expect(dnsTrigger(wrapper, 'openai.com').attributes('disabled')).toBeDefined()
    expect(wrapper.findComponent(RuleEditorDialog).props('dns')).toBeNull()
    await wrapper.get('[data-dns-error]').findAll('button').find(button => button.text() === '重试')!.trigger('click')
    await flushPromises()
    expect(dnsTrigger(wrapper, 'openai.com').attributes('disabled')).toBeUndefined()
    expect(wrapper.findComponent(RuleEditorDialog).props('dns')).not.toBeNull()
  })

  it('never sends dns when toggling a rule', async () => {
    const wrapper = await render()
    await row(wrapper, 'openai.com').get('button[aria-label="停用该项"]').trigger('click')
    await flushPromises()
    const [id, payload] = network.rules.update.mock.calls[0]!
    expect(id).toBe('r-ai')
    expect(payload).not.toHaveProperty('dns')
    expect(payload).toMatchObject({ enabled: false })
  })

  it('surfaces warnings returned by enable and disable', async () => {
    network.rules.update.mockResolvedValueOnce({ data: { success: true, warnings: ['OxiDNS 分流含自定义结构，DNS 未修改：手写条目'] } } as any)
    const wrapper = await render()
    await row(wrapper, 'openai.com').get('button[aria-label="停用该项"]').trigger('click')
    await flushPromises()
    expect(notify.warning).toHaveBeenCalledWith('状态已更新', 'OxiDNS 分流含自定义结构，DNS 未修改：手写条目')
    expect(network.rules.getDns).toHaveBeenCalledTimes(2)
  })

  it('unifies rules whose DNS already matches their policy as follow, after confirming', async () => {
    network.state.dns.rules['r-cn'] = { state: 'explicit', route: 'direct' }
    network.state.dns.rules['r-ads'] = { state: 'explicit', route: 'nxdomain' }
    const wrapper = await render()
    const setup = (wrapper.vm as any).$.setupState
    // r-cn (DIRECT, direct DNS), r-corp is pinned and r-ads blocks, so only r-cn qualifies; GEOIP never does.
    expect(setup.followCandidates.map((item: any) => item.id)).toEqual(['r-cn'])
    const done = setup.unifyFollow()
    await flushPromises()
    expect(confirmState.description).toContain('将 1 条规则标记为跟随策略')
    settleConfirm('confirm')
    await done
    expect(network.rules.setDns).toHaveBeenCalledWith({ 'r-cn': 'follow' }, {}, 'default')
  })
})
