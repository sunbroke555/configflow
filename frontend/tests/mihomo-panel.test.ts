import { beforeEach, describe, expect, it } from 'vitest'
import { delayText, delayTone, latestDelay, orderedGroups, type MihomoProxy } from '@/types/mihomo'

const proxy = (name: string, type: string, extra: Partial<MihomoProxy> = {}): MihomoProxy => ({ name, type, ...extra })

describe('mihomo panel helpers', () => {
  it('orders groups like the config file and keeps GLOBAL last', () => {
    const proxies = {
      GLOBAL: proxy('GLOBAL', 'Selector', { all: ['DIRECT', 'Media', 'Proxy'] }),
      Proxy: proxy('Proxy', 'Selector'),
      Media: proxy('Media', 'URLTest'),
      Hidden: proxy('Hidden', 'Selector', { hidden: true }),
      HK: proxy('HK', 'Shadowsocks'),
      DIRECT: proxy('DIRECT', 'Direct')
    }
    expect(orderedGroups(proxies).map(group => group.name)).toEqual(['Media', 'Proxy', 'GLOBAL'])
  })

  it('prefers the history of the group test URL', () => {
    const node = proxy('HK', 'Shadowsocks', {
      history: [{ time: '', delay: 120 }],
      extra: { 'https://example.test': { history: [{ time: '', delay: 0 }] } }
    })
    expect(latestDelay(node)).toBe(120)
    expect(latestDelay(node, 'https://example.test')).toBe(0)
    expect(latestDelay(node, 'https://other.test')).toBe(120)
  })

  it('labels delays by speed and treats zero as a timeout', () => {
    expect([undefined, 0, 100, 500, 1200].map(delayTone)).toEqual(['muted', 'danger', 'success', 'warning', 'danger'])
    expect(delayText(0)).toBe('超时')
    expect(delayText(88)).toBe('88 ms')
  })
})

import { flushPromises, mount } from '@vue/test-utils'
import { reactive } from 'vue'
import { vi } from 'vitest'
import MihomoPanel from '@/views/MihomoPanel.vue'
import MihomoConnections from '@/components/mihomo/MihomoConnections.vue'
import MihomoProxies from '@/components/mihomo/MihomoProxies.vue'
import MihomoRules from '@/components/mihomo/MihomoRules.vue'
import MihomoLogs from '@/components/mihomo/MihomoLogs.vue'
import { agentApi, mihomoPanelApi } from '@/api'
import { invalidateAgentSummaries } from '@/lib/agentSummaries'

const routeState = vi.hoisted(() => ({ route: null as any, replace: vi.fn() }))
vi.mock('vue-router', () => ({ useRoute: () => routeState.route, useRouter: () => ({ replace: routeState.replace, push: vi.fn() }) }))
vi.mock('@/api', () => {
  const ok = (data: unknown) => vi.fn().mockResolvedValue({ data: { success: true, data } })
  return {
    clientToolsApi: { notes: vi.fn().mockResolvedValue({ data: { success: true, notes: {} } }) },
    agentApi: {
      getAll: vi.fn(), summaries: vi.fn(),
      getLogSources: vi.fn().mockResolvedValue({ data: { success: true, sources: [{ id: 'core', label: 'Mihomo 日志', kind: 'journal', unit: 'mihomo' }] } }),
      getLogs: vi.fn().mockResolvedValue({ data: { success: true, logs: 'Mihomo log entry' } })
    },
    mihomoPanelApi: {
      version: ok({ version: 'v1.19.0' }),
      configs: ok({ mode: 'Rule' }),
      proxies: ok({ proxies: {
        GLOBAL: { name: 'GLOBAL', type: 'Selector', all: ['Proxy'], now: 'Proxy' },
        Proxy: { name: 'Proxy', type: 'Selector', all: ['HK', 'DIRECT', 'SH'], now: 'HK' },
        HK: { name: 'HK', type: 'Shadowsocks', history: [{ time: '', delay: 88 }] },
        DIRECT: { name: 'DIRECT', type: 'Direct' }
      } }),
      proxyProviders: ok({ providers: { sh: { name: 'sh', type: 'Proxy', vehicleType: 'HTTP', proxies: [{ name: 'SH', type: 'Trojan', history: [{ time: '', delay: 42 }] }] } } }),
      connections: ok({ uploadTotal: 2048, downloadTotal: 4096, connections: [{
        id: 'c1', upload: 10, download: 20, start: new Date().toISOString(), chains: ['HK', 'Proxy'], rule: 'DomainSuffix', rulePayload: 'example.com',
        metadata: { network: 'tcp', type: 'HTTP', sourceIP: '192.0.2.10', sourcePort: '5000', destinationIP: '', destinationPort: '443', host: 'www.example.com' }
      }] }),
      rules: ok({ rules: [{ type: 'DomainSuffix', payload: 'example.com', proxy: 'Proxy' }, { type: 'Match', payload: '', proxy: 'DIRECT' }] }),
      ruleProviders: ok({ providers: { ads: { name: 'ads', type: 'Rule', vehicleType: 'HTTP', behavior: 'Domain', format: 'MrsRule', ruleCount: 12 } } })
    }
  }
})

vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })

describe('mihomo panel view', () => {
  beforeEach(() => invalidateAgentSummaries())
  it('picks a mihomo agent and renders groups with the current node', async () => {
    routeState.route = reactive({ query: {} })
    vi.mocked(agentApi.summaries).mockResolvedValue({ data: [
      { id: 'dns', name: 'DNS', service_type: 'oxidns', status: 'online' },
      { id: 'gw', name: 'Gateway', service_type: 'mihomo', status: 'online', host: '192.0.2.1' }
    ] } as any)
    routeState.replace.mockImplementation(({ query }) => { routeState.route.query = query })
    const wrapper = mount(MihomoPanel)
    await flushPromises()
    expect(routeState.replace).toHaveBeenCalledWith({ query: { agent: 'gw' } })
    await flushPromises()
    expect(wrapper.text()).toContain('Mihomo v1.19.0')
    expect(wrapper.text()).toContain('Proxy')
    expect(wrapper.text()).toContain('88 ms')
    // provider 节点不在 /proxies 里，展开后也要有类型和延迟
    await wrapper.findAll('button[aria-expanded]').find(button => button.text().includes('手动选择'))!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Trojan')
    expect(wrapper.text()).toContain('42 ms')
    expect(vi.mocked(mihomoPanelApi.proxies)).toHaveBeenCalledWith('gw')
    wrapper.unmount()
  })

  it('places logs after rules and opens a directly linked logs tab', async () => {
    routeState.route = reactive({ query: { agent: 'gw', tab: 'logs' } })
    vi.mocked(agentApi.summaries).mockResolvedValue({ data: [
      { id: 'gw', name: 'Gateway', service_type: 'mihomo', status: 'online' }
    ] } as any)
    routeState.replace.mockImplementation(({ query }) => { routeState.route.query = query })
    const wrapper = mount(MihomoPanel)
    await flushPromises()
    expect(wrapper.findAll('[role="tab"]').map(tab => tab.text())).toEqual(['策略', '连接', '规则', '日志'])
    expect(wrapper.get('[role="tab"][data-state="active"]').text()).toBe('日志')
    expect(wrapper.getComponent(MihomoLogs).props()).toMatchObject({ agentId: 'gw', active: true })
    expect(wrapper.text()).toContain('Mihomo log entry')
    wrapper.unmount()
  })

  it('keeps the logs tab available when the core controller cannot be reached', async () => {
    routeState.route = reactive({ query: { agent: 'gw', tab: 'logs' } })
    vi.mocked(agentApi.summaries).mockResolvedValue({ data: [
      { id: 'gw', name: 'Gateway', service_type: 'mihomo', status: 'online' }
    ] } as any)
    vi.mocked(mihomoPanelApi.version).mockRejectedValueOnce(new Error('core unavailable'))
    const wrapper = mount(MihomoPanel)
    await flushPromises()
    expect(wrapper.text()).toContain('无法连接核心')
    expect(wrapper.findComponent(MihomoLogs).exists()).toBe(true)
    expect(wrapper.text()).toContain('Mihomo log entry')
    wrapper.unmount()
  })

  it('keeps visited tabs mounted, reloads hidden ones only when shown and rebuilds on device change', async () => {
    routeState.route = reactive({ query: { agent: 'gw' } })
    vi.mocked(agentApi.summaries).mockResolvedValue({ data: [
      { id: 'gw', name: 'Gateway', service_type: 'mihomo', status: 'online' },
      { id: 'gw2', name: 'Gateway 2', service_type: 'mihomo', status: 'online' }
    ] } as any)
    routeState.replace.mockImplementation(({ query }) => { routeState.route.query = query })
    const { proxies, rules, connections } = vi.mocked(mihomoPanelApi)
    proxies.mockClear(); rules.mockClear(); connections.mockClear()
    const wrapper = mount(MihomoPanel)
    await flushPromises()
    const show = async (tab: string) => { routeState.route.query = { ...routeState.route.query, tab }; await flushPromises() }
    expect(wrapper.findComponent(MihomoRules).exists()).toBe(false)
    expect(wrapper.findComponent(MihomoConnections).exists()).toBe(false)
    await show('rules')
    await show('connections')
    await show('proxies')
    await show('rules')
    expect(proxies).toHaveBeenCalledTimes(1)
    expect(rules).toHaveBeenCalledTimes(1)
    expect(connections).toHaveBeenCalledTimes(1)
    expect(wrapper.getComponent(MihomoProxies).props('active')).toBe(false)
    expect(wrapper.getComponent(MihomoConnections).props('active')).toBe(false)
    expect(wrapper.get('[role="tabpanel"][data-state="inactive"]').classes()).toContain('data-[state=inactive]:hidden')
    // 刷新只读取当前标签，隐藏的「策略」切回时再读
    await wrapper.get('button[aria-label="刷新面板"]').trigger('click')
    await flushPromises()
    expect(rules).toHaveBeenCalledTimes(2)
    expect(proxies).toHaveBeenCalledTimes(1)
    await show('proxies')
    expect(proxies).toHaveBeenCalledTimes(2)
    await show('rules')
    expect(rules).toHaveBeenCalledTimes(2)
    // 换设备后重建，只挂载当前标签
    routeState.route.query = { agent: 'gw2', tab: 'rules' }
    await flushPromises()
    expect(rules).toHaveBeenLastCalledWith('gw2')
    expect(wrapper.findComponent(MihomoProxies).exists()).toBe(false)
    expect(wrapper.findComponent(MihomoConnections).exists()).toBe(false)
    expect(proxies).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('lists connections with the outbound chain', async () => {
    const wrapper = mount(MihomoConnections, { props: { agentId: 'gw', active: true } })
    await flushPromises()
    expect(wrapper.text()).toContain('www.example.com:443')
    expect(wrapper.text()).toContain('Proxy → HK')
    wrapper.unmount()
  })

  it('lists rules and rule providers', async () => {
    const wrapper = mount(MihomoRules, { props: { agentId: 'gw', active: true, reloadToken: 0 } })
    await flushPromises()
    expect(wrapper.text()).toContain('2 / 2 条')
    expect(wrapper.text()).toContain('ads')
    wrapper.unmount()
  })
})
