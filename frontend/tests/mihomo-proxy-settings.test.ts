import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import MihomoProxies from '@/components/mihomo/MihomoProxies.vue'
import { mihomoPanelApi } from '@/api'
import { notify } from '@/lib/feedback'
import { DEFAULT_PROXY_SETTINGS, finalNode, loadProxySettings, saveProxySettings } from '@/types/mihomo'

vi.mock('@/api', () => ({
  mihomoPanelApi: { groupDelay: vi.fn(), proxyDelay: vi.fn(), providerProxyDelay: vi.fn(), proxies: vi.fn(), proxyProviders: vi.fn(), selectProxy: vi.fn(), connections: vi.fn(), closeConnection: vi.fn() }
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
  vi.mocked(mihomoPanelApi.proxyDelay).mockResolvedValue({ data: { data: { delay: 123 } } } as any)
  vi.mocked(mihomoPanelApi.providerProxyDelay).mockResolvedValue({ data: { data: { delay: 123 } } } as any)
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
  it('shows text and progress while testing and restores the action after completion', async () => {
    const pending: Array<(value: any) => void> = []
    vi.mocked(mihomoPanelApi.groupDelay).mockImplementation(() => new Promise(resolve => pending.push(resolve)))
    const { wrapper } = await render()
    const button = wrapper.findAll('button').find(item => item.text() === '全部分组测速')!
    await button.trigger('click')
    await flushPromises()
    expect(button.text()).toBe('正在测速…')
    expect(button.attributes('aria-busy')).toBe('true')
    expect(button.attributes('disabled')).toBeDefined()
    expect(wrapper.get('[role="status"]').text()).toContain('已完成 0 / 4 组')
    pending.shift()!({ data: { data: {} } })
    await flushPromises()
    expect(wrapper.get('[role="status"]').text()).toContain('已完成 1 / 4 组')
    for (let i = 0; i < 3; i++) {
      pending.shift()!({ data: { data: {} } })
      await flushPromises()
    }
    expect(button.text()).toBe('全部分组测速')
    expect(button.attributes('aria-busy')).toBe('false')
    expect(button.attributes('disabled')).toBeUndefined()
    expect(wrapper.find('[role="status"]').exists()).toBe(false)
  })
  it('tests a provider node without selecting it and isolates the test URL', async () => {
    const { wrapper, state } = await render()
    await wrapper.get('button[aria-label="单节点测速 Fast（Proxy）"]').trigger('click')
    await flushPromises()
    expect(mihomoPanelApi.providerProxyDelay).toHaveBeenCalledWith('gw', 'sub', 'Fast', undefined)
    expect(mihomoPanelApi.proxyDelay).not.toHaveBeenCalled()
    expect(mihomoPanelApi.selectProxy).not.toHaveBeenCalled()
    expect(state.delayOf('Fast', PROXIES.Proxy)).toBe(123)
    expect(state.delayOf('Fast', { ...PROXIES.Proxy, testUrl: 'https://different.test' })).toBe(40)
    expect(wrapper.find('button[aria-label="单节点测速 Auto（Proxy）"]').exists()).toBe(false)
  })
  it('keeps ordinary node tests on the existing endpoint', async () => {
    const { wrapper, state } = await render()
    await wrapper.get('button[aria-label="单节点测速 Slow（Proxy）"]').trigger('click')
    await flushPromises()
    expect(mihomoPanelApi.proxyDelay).toHaveBeenCalledWith('gw', 'Slow', undefined)
    expect(mihomoPanelApi.providerProxyDelay).not.toHaveBeenCalled()
    expect(state.delayOf('Slow', PROXIES.Proxy)).toBe(123)
  })
  it('tests DIRECT normally and displays a zero delay as a timeout', async () => {
    vi.mocked(mihomoPanelApi.proxies).mockResolvedValue({ data: { data: { proxies: {
      ...PROXIES, DIRECT: { name: 'DIRECT', type: 'Direct' }
    } } } } as any)
    vi.mocked(mihomoPanelApi.proxyDelay).mockResolvedValue({ data: { data: { delay: 0 } } } as any)
    const { state } = await render()
    expect(state.canTestNode('DIRECT')).toBe(true)
    await state.testNode('DIRECT', PROXIES.Proxy)
    expect(mihomoPanelApi.proxyDelay).toHaveBeenCalledWith('gw', 'DIRECT', undefined)
    expect(state.delayOf('DIRECT', PROXIES.Proxy)).toBe(0)
    expect(notify.error).not.toHaveBeenCalled()
  })
  it('supports older cores exposing the same single-provider node through the normal endpoint', async () => {
    vi.mocked(mihomoPanelApi.proxies).mockResolvedValue({ data: { data: { proxies: {
      ...PROXIES, Fast: PROVIDERS.sub.proxies[0]
    } } } } as any)
    const { state } = await render()
    expect(state.proxies.Fast.type).toBe('Trojan')
    await state.testNode('Fast', PROXIES.Proxy)
    expect(mihomoPanelApi.proxyDelay).toHaveBeenCalledWith('gw', 'Fast', undefined)
    expect(mihomoPanelApi.providerProxyDelay).not.toHaveBeenCalled()
    expect(notify.error).not.toHaveBeenCalled()
  })
  it.each(['default', 'other'])('does not mistake a top-level node for a subscribed node with the same name in %s', async source => {
    const ordinaryNode = { name: 'Fast', type: 'Vless', history: history(15) }
    vi.mocked(mihomoPanelApi.proxies).mockResolvedValue({ data: { data: { proxies: {
      ...PROXIES, Fast: ordinaryNode
    } } } } as any)
    vi.mocked(mihomoPanelApi.proxyProviders).mockResolvedValue({ data: { data: { providers: {
      ...PROVIDERS,
      [source]: { name: source, type: 'Proxy', vehicleType: source === 'default' ? 'Compatible' : 'HTTP', proxies: [ordinaryNode] }
    } } } } as any)
    const { state } = await render()
    await state.testNode('Fast', PROXIES.Proxy)
    expect(mihomoPanelApi.proxyDelay).not.toHaveBeenCalled()
    expect(mihomoPanelApi.providerProxyDelay).not.toHaveBeenCalled()
    expect(notify.error).toHaveBeenCalledWith('Fast 测速失败', '多个来源中存在同名节点，请使用所属策略组的分组测速。')
    expect(state.nodeTesting.size).toBe(0)
    expect(state.delays.size).toBe(0)
    expect(mihomoPanelApi.selectProxy).not.toHaveBeenCalled()
  })
  it('does not guess the provider for duplicate node names and leaves group tests available', async () => {
    vi.mocked(mihomoPanelApi.proxyProviders).mockResolvedValue({ data: { data: { providers: {
      ...PROVIDERS, other: { ...PROVIDERS.sub, name: 'other' }
    } } } } as any)
    vi.mocked(mihomoPanelApi.groupDelay).mockResolvedValue({ data: { data: { Fast: 70 } } } as any)
    const { state } = await render()
    await state.testNode('Fast', PROXIES.Proxy)
    expect(mihomoPanelApi.proxyDelay).not.toHaveBeenCalled()
    expect(mihomoPanelApi.providerProxyDelay).not.toHaveBeenCalled()
    expect(notify.error).toHaveBeenCalledWith('Fast 测速失败', '多个来源中存在同名节点，请使用所属策略组的分组测速。')
    expect(state.nodeTesting.size).toBe(0)
    await state.testGroup(PROXIES.Proxy)
    expect(mihomoPanelApi.groupDelay).toHaveBeenCalledWith('gw', 'Proxy', undefined)
    expect(state.delayOf('Fast', PROXIES.Proxy)).toBe(70)
  })
  it('supports the default Compatible provider without treating repeated entries as ambiguity', async () => {
    vi.mocked(mihomoPanelApi.proxyProviders).mockResolvedValue({ data: { data: { providers: {
      default: { name: 'default', type: 'Proxy', vehicleType: 'Compatible', proxies: [
        { name: 'Fast', type: 'Trojan' }, { name: 'Fast', type: 'Trojan' }, PROXIES.Slow
      ] }
    } } } } as any)
    const { state, wrapper } = await render()
    await state.testNode('Fast', PROXIES.Proxy)
    expect(mihomoPanelApi.providerProxyDelay).toHaveBeenCalledWith('gw', 'default', 'Fast', undefined)
    await state.testNode('Slow', PROXIES.Proxy)
    expect(mihomoPanelApi.proxyDelay).toHaveBeenCalledWith('gw', 'Slow', undefined)
    expect(wrapper.text()).not.toContain('代理集合')
  })
  it('counts Compatible wrappers of multiple groups as the same ordinary configuration source', async () => {
    const node = { ...PROXIES.Slow, id: 'same-proxy-id', 'provider-name': '' }
    vi.mocked(mihomoPanelApi.proxyProviders).mockResolvedValue({ data: { data: { providers: {
      default: { name: 'default', type: 'Proxy', vehicleType: 'Compatible', proxies: [node] },
      TestGroup: { name: 'TestGroup', type: 'Proxy', vehicleType: 'Compatible', proxies: [node] },
      SecondGroup: { name: 'SecondGroup', type: 'Proxy', vehicleType: 'Compatible', proxies: [node] }
    } } } } as any)
    const { state, wrapper } = await render()
    state.applySettings({ ...state.settings, groupByProvider: true })
    await flushPromises()
    expect(wrapper.text()).toContain('配置节点')
    expect(wrapper.text()).not.toContain('多个代理集合')
    await state.testNode('Slow', PROXIES.Proxy)
    expect(mihomoPanelApi.proxyDelay).toHaveBeenCalledWith('gw', 'Slow', undefined)
    expect(mihomoPanelApi.providerProxyDelay).not.toHaveBeenCalled()
    expect(notify.error).not.toHaveBeenCalled()
  })
  it.each(['HTTP', 'File', 'Inline'])('still detects an external %s node conflicting with multiple Compatible wrappers', async vehicleType => {
    const node = { ...PROXIES.Slow, id: 'same-proxy-id', 'provider-name': '' }
    vi.mocked(mihomoPanelApi.proxyProviders).mockResolvedValue({ data: { data: { providers: {
      default: { name: 'default', type: 'Proxy', vehicleType: 'Compatible', proxies: [node] },
      TestGroup: { name: 'TestGroup', type: 'Proxy', vehicleType: 'Compatible', proxies: [node] },
      SecondGroup: { name: 'SecondGroup', type: 'Proxy', vehicleType: 'Compatible', proxies: [node] },
      sub: { name: 'sub', type: 'Proxy', vehicleType, proxies: [{ ...node, id: 'different-proxy-id', 'provider-name': 'sub' }] }
    } } } } as any)
    const { state } = await render()
    await state.testNode('Slow', PROXIES.Proxy)
    expect(mihomoPanelApi.proxyDelay).not.toHaveBeenCalled()
    expect(mihomoPanelApi.providerProxyDelay).not.toHaveBeenCalled()
    expect(notify.error).toHaveBeenCalledWith('Slow 测速失败', '多个来源中存在同名节点，请使用所属策略组的分组测速。')
    expect(state.nodeTesting.size).toBe(0)
  })
  it('preserves provider and node names and the group URL, without switching nodes or testing whole providers', async () => {
    const name = '🇯🇵 JP-05 · AWS2 / 中文 %?#'
    const provider = '订阅 / 🇯🇵 100%?#'
    vi.mocked(mihomoPanelApi.proxyProviders).mockResolvedValue({ data: { data: { providers: {
      [provider]: { name: provider, type: 'Proxy', vehicleType: 'HTTP', proxies: [{ name, type: 'Trojan' }] }
    } } } } as any)
    const { state } = await render()
    const testUrl = 'https://example.test/generate_204?key=a%20b&check=1'
    await state.testNode(name, { ...PROXIES.Proxy, testUrl })
    expect(mihomoPanelApi.providerProxyDelay).toHaveBeenCalledWith('gw', provider, name, testUrl)
    expect(mihomoPanelApi.proxyDelay).not.toHaveBeenCalled()
    expect(mihomoPanelApi.groupDelay).not.toHaveBeenCalled()
    expect(mihomoPanelApi.selectProxy).not.toHaveBeenCalled()
    expect(state.delayOf(name, { ...PROXIES.Proxy, testUrl })).toBe(123)
  })
  it('retains ordinary-node tests when the providers request fails', async () => {
    vi.mocked(mihomoPanelApi.proxyProviders).mockRejectedValue(new Error('unavailable'))
    const { state } = await render()
    await state.testNode('Slow', PROXIES.Proxy)
    expect(mihomoPanelApi.proxyDelay).toHaveBeenCalledWith('gw', 'Slow', undefined)
    expect(mihomoPanelApi.providerProxyDelay).not.toHaveBeenCalled()
  })
  it.each([404, 426, 504])('reports provider test HTTP %s errors and clears the pending state', async status => {
    vi.mocked(mihomoPanelApi.providerProxyDelay).mockRejectedValue({ response: { status, data: { message: `failure-${status}` } } })
    const { state } = await render()
    await state.testNode('Fast', PROXIES.Proxy)
    expect(notify.error).toHaveBeenCalledWith('Fast 测速失败', `failure-${status}`)
    expect(state.nodeTesting.size).toBe(0)
    expect(state.delayOf('Fast', PROXIES.Proxy)).toBe(40)
    expect(mihomoPanelApi.proxyDelay).not.toHaveBeenCalled()
    expect(mihomoPanelApi.groupDelay).not.toHaveBeenCalled()
  })
  it.each([undefined, -1, '20', Number.NaN, Number.POSITIVE_INFINITY])('rejects an invalid provider delay %s', async delay => {
    vi.mocked(mihomoPanelApi.providerProxyDelay).mockResolvedValue({ data: { data: { delay } } } as any)
    const { state } = await render()
    await state.testNode('Fast', PROXIES.Proxy)
    expect(notify.error).toHaveBeenCalledWith('Fast 测速失败', '测速结果无效，请重试。')
    expect(state.delayOf('Fast', PROXIES.Proxy)).toBe(40)
  })
  it.each(['reload', 'agent', 'unmount'])('discards provider results after %s', async change => {
    let resolve!: (value: any) => void
    vi.mocked(mihomoPanelApi.providerProxyDelay).mockImplementationOnce(() => new Promise(yes => { resolve = yes }))
    const { state, wrapper } = await render()
    const pending = state.testNode('Fast', PROXIES.Proxy)
    if (change === 'reload') await wrapper.setProps({ reloadToken: 1 })
    else if (change === 'agent') await wrapper.setProps({ agentId: 'other' })
    else wrapper.unmount()
    resolve({ data: { data: { delay: 999 } } }); await pending
    expect(state.delays.size).toBe(0)
    expect(notify.error).not.toHaveBeenCalled()
  })
  it('does not let an old result clear a newer pending node test after reload', async () => {
    let resolveOld!: (value: any) => void
    let resolveNew!: (value: any) => void
    vi.mocked(mihomoPanelApi.providerProxyDelay)
      .mockImplementationOnce(() => new Promise(yes => { resolveOld = yes }))
      .mockImplementationOnce(() => new Promise(yes => { resolveNew = yes }))
    const { state, wrapper } = await render()
    const oldTest = state.testNode('Fast', PROXIES.Proxy)
    await wrapper.setProps({ reloadToken: 1 }); await flushPromises()
    const newTest = state.testNode('Fast', PROXIES.Proxy)
    resolveOld({ data: { data: { delay: 999 } } }); await oldTest
    expect(state.nodeTesting.size).toBe(1)
    expect(state.delays.size).toBe(0)
    resolveNew({ data: { data: { delay: 25 } } }); await newTest
    expect(state.nodeTesting.size).toBe(0)
    expect(state.delayOf('Fast', PROXIES.Proxy)).toBe(25)
  })
  it('discards provider failures after reload without showing a stale toast', async () => {
    let reject!: (reason: any) => void
    vi.mocked(mihomoPanelApi.providerProxyDelay).mockImplementationOnce(() => new Promise((_yes, no) => { reject = no }))
    const { state, wrapper } = await render()
    const pending = state.testNode('Fast', PROXIES.Proxy)
    await wrapper.setProps({ reloadToken: 1 })
    reject(new Error('old failure')); await pending
    expect(notify.error).not.toHaveBeenCalled()
    expect(state.nodeTesting.size).toBe(0)
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
  it('stops old queued group tests after a panel reload and permits new tests', async () => {
    let resolve!: (value: any) => void
    vi.mocked(mihomoPanelApi.groupDelay).mockImplementationOnce(() => new Promise(yes => { resolve = yes }))
    const { state, wrapper } = await render()
    const batch = state.testAll()
    await wrapper.setProps({ reloadToken: 1 }); await flushPromises()
    resolve({ data: { data: { Fast: 999 } } }); await batch
    expect(mihomoPanelApi.groupDelay).toHaveBeenCalledTimes(1)
    expect(state.batchRunning).toBe(false)
    expect(state.testing.size).toBe(0)
    expect(state.delays.size).toBe(0)
    expect(notify.info).not.toHaveBeenCalled()
    await state.testNode('Fast', PROXIES.Proxy)
    expect(state.delayOf('Fast', PROXIES.Proxy)).toBe(123)
  })
})
