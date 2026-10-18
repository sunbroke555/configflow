import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import MihomoProxies from '@/components/mihomo/MihomoProxies.vue'
import { mihomoPanelApi } from '@/api'
import { notify } from '@/lib/feedback'
import { DEFAULT_PROXY_SETTINGS, finalNode, loadProxySettings, saveProxySettings } from '@/types/mihomo'

vi.mock('@/api', () => ({
  mihomoPanelApi: { groupDelay: vi.fn(), proxyDelay: vi.fn(), proxies: vi.fn(), proxyProviders: vi.fn(), selectProxy: vi.fn(), connections: vi.fn(), closeConnection: vi.fn() }
}))
vi.mock('@/lib/feedback', () => ({ notify: { info: vi.fn(), success: vi.fn(), error: vi.fn() } }))

function stubStorage() {
  const values = new Map<string, string>()
  vi.stubGlobal('localStorage', {
    get length() { return values.size },
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, String(value)) },
    removeItem: (key: string) => { values.delete(key) },
    clear: () => { values.clear() },
    key: (index: number) => Array.from(values.keys())[index] ?? null
  } satisfies Storage)
}

const history = (delay: number) => [{ time: '', delay }]
const PROXIES = {
  GLOBAL: { name: 'GLOBAL', type: 'Selector', all: ['Proxy', 'Hidden'], now: 'Proxy' },
  Proxy: { name: 'Proxy', type: 'Selector', all: ['Slow', 'Dead', 'Auto', 'Fast'], now: 'Auto' },
  Auto: { name: 'Auto', type: 'URLTest', all: ['Fast'], now: 'Fast' },
  Hidden: { name: 'Hidden', type: 'Selector', all: ['Fast'], now: 'Fast', hidden: true },
  Slow: { name: 'Slow', type: 'Vmess', history: history(600) }
}
const PROVIDERS = { sub: { name: 'sub', type: 'Proxy', vehicleType: 'HTTP', proxies: [
  { name: 'Fast', type: 'Trojan', history: history(40) }, { name: 'Dead', type: 'Trojan', history: history(0) }
] } }

const wrappers: VueWrapper[] = []
async function render() {
  const wrapper = mount(MihomoProxies, { props: { agentId: 'gw', reloadToken: 0 }, attachTo: document.body })
  wrappers.push(wrapper)
  await flushPromises()
  const state = (wrapper.vm as any).$.setupState
  state.expanded = new Set(['Proxy'])
  await flushPromises()
  return { wrapper, state }
}
const memberNames = (wrapper: VueWrapper) => wrapper.findAll('button[aria-pressed]').map(button => button.find('span').text())

beforeEach(() => {
  vi.clearAllMocks()
  stubStorage()
  vi.mocked(mihomoPanelApi.proxies).mockResolvedValue({ data: { data: { proxies: PROXIES } } } as any)
  vi.mocked(mihomoPanelApi.proxyProviders).mockResolvedValue({ data: { data: { providers: PROVIDERS } } } as any)
})
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); vi.unstubAllGlobals() })

describe('proxy panel settings', () => {
  it('validates stored settings and falls back to defaults', () => {
    localStorage.setItem('configflow:mihomo-panel:proxy-settings', JSON.stringify({ sort: 'bogus', hideUnavailable: 'yes', cardMinWidth: 9999, showFinalNode: true }))
    expect(loadProxySettings()).toEqual({ ...DEFAULT_PROXY_SETTINGS, showFinalNode: true, cardMinWidth: 320 })
    saveProxySettings({ ...DEFAULT_PROXY_SETTINGS, sort: 'delay' })
    expect(loadProxySettings().sort).toBe('delay')
  })

  it('resolves the final node through nested groups', () => {
    expect(finalNode(PROXIES as any, 'Proxy')).toBe('Fast')
    expect(finalNode(PROXIES as any, 'Slow')).toBe('Slow')
  })

  it('sorts by delay, hides unavailable nodes and shows hidden groups on request', async () => {
    const { wrapper, state } = await render()
    expect(memberNames(wrapper)).toEqual(['Slow', 'Dead', 'Auto', 'Fast'])
    expect(wrapper.text()).not.toContain('Hidden')
    state.applySettings({ ...state.settings, sort: 'delay', hideUnavailable: true, showHiddenGroups: true, showFinalNode: true })
    await flushPromises()
    expect(memberNames(wrapper)).toEqual(['Auto', 'Fast', 'Slow'])
    expect(wrapper.text()).toContain('Hidden')
    expect(wrapper.text()).toContain('Auto → Fast')
    expect(JSON.parse(localStorage.getItem('configflow:mihomo-panel:proxy-settings')!).sort).toBe('delay')
  })

  it('groups members by provider', async () => {
    const { wrapper, state } = await render()
    state.applySettings({ ...state.settings, groupByProvider: true })
    await flushPromises()
    expect(wrapper.text()).toContain('配置节点 · 1')
    expect(wrapper.text()).toContain('sub · 2')
    expect(wrapper.text()).toContain('策略组 · 1')
  })

  it('closes connections through the group after switching', async () => {
    vi.mocked(mihomoPanelApi.selectProxy).mockResolvedValue({} as any)
    vi.mocked(mihomoPanelApi.closeConnection).mockResolvedValue({} as any)
    vi.mocked(mihomoPanelApi.connections).mockResolvedValue({ data: { data: { connections: [
      { id: 'a', chains: ['Fast', 'Auto', 'Proxy'] }, { id: 'b', chains: ['DIRECT'] }
    ] } } } as any)
    const { state } = await render()
    await state.select(PROXIES.Proxy, 'Fast')
    expect(mihomoPanelApi.selectProxy).toHaveBeenCalledWith('gw', 'Proxy', 'Fast')
    expect(mihomoPanelApi.closeConnection).toHaveBeenCalledTimes(1)
    expect(mihomoPanelApi.closeConnection).toHaveBeenCalledWith('gw', 'a')
    expect(notify.success).toHaveBeenCalledWith('Proxy 已切换到 Fast', '已断开经过该策略组的 1 条连接')

    vi.clearAllMocks()
    state.applySettings({ ...state.settings, closeConnectionsOnSwitch: false })
    await state.select({ ...PROXIES.Proxy, now: 'Fast' }, 'Slow')
    expect(mihomoPanelApi.connections).not.toHaveBeenCalled()
  })
})


describe('individual and all-group delay tests', () => {
  it('tests a node without selecting it and isolates the test URL', async () => {
    vi.mocked(mihomoPanelApi.proxyDelay).mockResolvedValue({ data: { data: { delay: 123 } } } as any)
    const { wrapper, state } = await render()
    await wrapper.get('button[aria-label="单节点测速 Fast（Proxy）"]').trigger('click')
    await flushPromises()
    expect(mihomoPanelApi.proxyDelay).toHaveBeenCalledWith('gw', 'Fast', undefined)
    expect(mihomoPanelApi.selectProxy).not.toHaveBeenCalled()
    expect(state.delayOf('Fast', PROXIES.Proxy)).toBe(123)
    expect(state.delayOf('Fast', { ...PROXIES.Proxy, testUrl: 'https://different.test' })).toBe(40)
    expect(wrapper.find('button[aria-label="单节点测速 Auto（Proxy）"]').exists()).toBe(false)
  })
  it('tests hidden and visible groups sequentially and continues after failure', async () => {
    let running = 0, peak = 0
    vi.mocked(mihomoPanelApi.groupDelay).mockImplementation(async (_id, name) => {
      peak = Math.max(peak, ++running); await Promise.resolve(); running--
      if (name === 'Auto') throw new Error('failed')
      return { data: { data: { Fast: 10 } } } as any
    })
    const { state } = await render()
    state.search = 'Slow'
    await state.testAll()
    expect(vi.mocked(mihomoPanelApi.groupDelay).mock.calls.map(call => call[1]).sort()).toEqual(['Auto', 'GLOBAL', 'Hidden', 'Proxy'])
    expect(peak).toBe(1); expect(state.batchDone).toBe(4); expect(state.batchRunning).toBe(false)
    expect(notify.info).toHaveBeenCalledWith('全部分组测速完成', '已测 4 组，1 组失败')
  })
  it('stops queued tests after stop and unmount', async () => {
    let resolve!: (value: any) => void
    vi.mocked(mihomoPanelApi.groupDelay).mockImplementation(() => new Promise(yes => { resolve = yes }))
    const { state, wrapper } = await render()
    const batch = state.testAll(); await flushPromises(); state.batchStop = true
    resolve({ data: { data: {} } }); await batch
    expect(mihomoPanelApi.groupDelay).toHaveBeenCalledTimes(1)
    const second = state.testAll(); wrapper.unmount()
    resolve({ data: { data: {} } }); await second
    expect(mihomoPanelApi.groupDelay).toHaveBeenCalledTimes(2)
  })
  it('discards old results when changing Agent', async () => {
    let resolve!: (value: any) => void
    vi.mocked(mihomoPanelApi.groupDelay).mockImplementationOnce(() => new Promise(yes => { resolve = yes }))
    const { state, wrapper } = await render()
    const batch = state.testAll()
    await wrapper.setProps({ agentId: 'other' })
    resolve({ data: { data: { Fast: 999 } } }); await batch
    expect(mihomoPanelApi.groupDelay).toHaveBeenCalledTimes(1)
    expect(state.batchRunning).toBe(false); expect(state.delays.size).toBe(0)
  })
})
