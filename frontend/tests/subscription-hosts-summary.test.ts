import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { computed, defineComponent, markRaw } from 'vue'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import GenerateStudio from '@/components/generate/GenerateStudio.vue'
import SubscriptionHostsSummary from '@/components/generate/SubscriptionHostsSummary.vue'
import { generateApi, ruleSetApi, statsApi } from '@/api'
import type { SubscriptionHostsReport } from '@/types'

vi.mock('@/api', () => ({
  agentApi: { pushConfig: vi.fn(), getDeployment: vi.fn() },
  generateApi: { previewMihomo: vi.fn(), previewMosdns: vi.fn(), previewOxidns: vi.fn(), previewSurge: vi.fn(), previewLoon: vi.fn() },
  statsApi: { getOverview: vi.fn() }, ruleSetApi: { getAll: vi.fn() },
}))
vi.mock('@/stores/live', () => ({ useLive: () => ({ boundAgents: computed(() => []), isOnline: () => true, start: vi.fn(), stop: vi.fn(), refresh: vi.fn() }) }))
vi.mock('@/lib/feedback', () => ({ notify: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() } }))

const report = (overrides: Partial<SubscriptionHostsReport> = {}): SubscriptionHostsReport => ({
  merged: 2,
  ignored: 2,
  domain_mapped: 1,
  subscriptions: [{ id: 'air', name: '机场', total: 4, merged: 2, ignored: 2 }],
  items: [
    { subscription: '机场', domain: 'hk.example.net', value: 'hk.example.org', status: 'merged', kind: 'domain' },
    { subscription: '机场', domain: 'jp.example.net', value: ['198.51.100.12', '2001:db8::12'], status: 'merged', kind: 'address' },
    { subscription: '机场', domain: 'google.com', value: '203.0.113.66', status: 'ignored', reason: 'not_node_domain', detail: '不是这个订阅里被使用节点的 server 域名' },
  ],
  ...overrides,
})

const wrappers: VueWrapper[] = []
afterEach(() => { wrappers.splice(0).forEach(w => w.unmount()); document.body.innerHTML = ''; vi.useRealTimers(); vi.unstubAllGlobals() })

describe('subscription hosts summary', () => {
  it('states merged and ignored counts and explains domain mappings in plain sight', () => {
    const wrapper = mount(SubscriptionHostsSummary, { props: { report: report() } })
    wrappers.push(wrapper)
    expect(wrapper.get('[data-subscription-hosts-counts]').text().replace(/\s+/g, '')).toBe('已合并2条，忽略2条')
    expect(wrapper.get('[data-subscription-hosts-domain-note]').text())
      .toBe('其中 1 条是「域名 → 域名」：Mihomo 连接节点时不采用这类映射，节点仍连接原域名解析到的地址。')
    expect(wrapper.find('#subscription-hosts-items').exists()).toBe(false)
  })

  it('lists each entry with its subscription, value and the reason it was ignored', async () => {
    const wrapper = mount(SubscriptionHostsSummary, { props: { report: report() }, attachTo: document.body })
    wrappers.push(wrapper)
    const toggle = wrapper.get('button[aria-controls="subscription-hosts-items"]')
    expect(toggle.attributes('aria-expanded')).toBe('false')
    await toggle.trigger('click')
    expect(toggle.attributes('aria-expanded')).toBe('true')
    const rows = wrapper.findAll('#subscription-hosts-items li')
    // Separate spans keep domain, value and subscription apart for the reader.
    expect(rows.map(row => row.findAll(':scope > span').map(span => span.text()).filter(text => text !== '→'))).toEqual([
      ['已合并', 'hk.example.net', 'hk.example.org', '· 机场'],
      ['已合并', 'jp.example.net', '198.51.100.12, 2001:db8::12', '· 机场'],
      ['已忽略', 'google.com', '203.0.113.66', '· 机场', '不是这个订阅里被使用节点的 server 域名'],
      [],
    ])
    expect(rows[3]!.text()).toBe('另有 1 条未列出，原因见服务日志。')
    expect(rows[0]!.get('.chip').classes()).toContain('chip-ok')
    expect(rows[2]!.get('.chip').classes()).not.toContain('chip-ok')
  })

  it('omits the domain note when every merged entry is an address', () => {
    const wrapper = mount(SubscriptionHostsSummary, { props: { report: report({ domain_mapped: 0 }) } })
    wrappers.push(wrapper)
    expect(wrapper.find('[data-subscription-hosts-domain-note]').exists()).toBe(false)
  })
})

describe('generation studio', () => {
  const icon = markRaw(defineComponent({ template: '<i />' }))
  beforeEach(() => {
    vi.resetAllMocks()
    vi.useFakeTimers()
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => setTimeout(() => cb(1), 1))
    vi.mocked(statsApi.getOverview).mockResolvedValue({ data: { success: true, data: {} } } as any)
    vi.mocked(ruleSetApi.getAll).mockResolvedValue({ data: [] } as any)
    vi.mocked(generateApi.previewSurge).mockResolvedValue({ data: { content: '[General]' } } as any)
  })

  async function generate(key: 'mihomo' | 'surge', hosts: SubscriptionHostsReport | null) {
    vi.mocked(generateApi.previewMihomo).mockResolvedValue({ data: { content: 'hosts: {}', subscription_hosts: hosts } } as any)
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: icon }] })
    await router.push('/')
    const targets = [{ key: 'mihomo', title: 'Mihomo' }, { key: 'surge', title: 'Surge' }]
      .map(item => ({ ...item, icon, desc: '', url: '', urlDisplay: '', actions: [] })) as any
    const wrapper = mount(GenerateStudio, { props: { targets }, global: { plugins: [router] }, attachTo: document.body })
    wrappers.push(wrapper)
    if (key === 'surge') await wrapper.findAll('button').find(b => b.text() === 'Surge')!.trigger('click')
    await wrapper.findAll('button').find(b => b.text() === '生成')!.trigger('click')
    await vi.advanceTimersByTimeAsync(3000)
    await flushPromises()
    return wrapper
  }

  it('shows the summary after a Mihomo generation that used subscription hosts', async () => {
    const wrapper = await generate('mihomo', report())
    expect(wrapper.get('[data-subscription-hosts]').text()).toContain('订阅 hosts')
  })

  it('stays silent when no subscription carries hosts or for other targets', async () => {
    expect((await generate('mihomo', report({ merged: 0, ignored: 0, domain_mapped: 0, items: [] }))).find('[data-subscription-hosts]').exists()).toBe(false)
    expect((await generate('mihomo', null)).find('[data-subscription-hosts]').exists()).toBe(false)
    expect((await generate('surge', report())).find('[data-subscription-hosts]').exists()).toBe(false)
  })
})
