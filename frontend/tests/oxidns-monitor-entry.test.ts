import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import OxiDNSMonitorEntry from '@/views/OxiDNSMonitorEntry.vue'
import { agentApi } from '@/api'
import { OXIDNS_DEVICE_STORAGE_KEY } from '@/composables/useOxiDNSDevices'
import { invalidateAgentSummaries } from '@/lib/agentSummaries'

const navigation = vi.hoisted(() => ({ replace: vi.fn(), push: vi.fn() }))
vi.mock('vue-router', () => ({ useRouter: () => navigation }))
vi.mock('@/api', () => ({ agentApi: { summaries: vi.fn() } }))
const wrappers: VueWrapper[] = []
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
const agent = (id: string, service_type = 'oxidns') => ({ id, name: 'Device ' + id, host: '192.0.2.13', status: 'online', service_type })
const response = (data: unknown) => ({ data } as any)
async function render() {
  const wrapper = mount(OxiDNSMonitorEntry)
  wrappers.push(wrapper)
  await flushPromises()
  return wrapper
}
beforeEach(() => { vi.clearAllMocks(); invalidateAgentSummaries(); stubStorage(); navigation.replace.mockResolvedValue(undefined) })
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); vi.unstubAllGlobals() })

describe('OxiDNS navigation entry', () => {
  it('opens the single OxiDNS Agent directly with an encoded ID', async () => {
    vi.mocked(agentApi.summaries).mockResolvedValue(response([agent('a/b'), agent('proxy', 'mihomo')]))
    await render()
    expect(navigation.replace).toHaveBeenCalledWith('/agents/a%2Fb/oxidns')
  })
  it('opens the first online OxiDNS Agent without an intermediate list', async () => {
    vi.mocked(agentApi.summaries).mockResolvedValue(response([{ ...agent('offline'), status: 'offline' }, agent('other', 'mosdns'), agent('online')]))
    const wrapper = await render()
    expect(navigation.replace).toHaveBeenCalledWith('/agents/online/oxidns')
    expect(wrapper.find('table').exists()).toBe(false)
  })
  it('restores a still available last selection even when it is offline', async () => {
    window.localStorage.setItem(OXIDNS_DEVICE_STORAGE_KEY, 'last')
    vi.mocked(agentApi.summaries).mockResolvedValue(response([agent('online'), { ...agent('last'), status: 'offline' }]))
    await render()
    expect(navigation.replace).toHaveBeenCalledWith('/agents/last/oxidns')
  })
  it('ignores a removed last selection and falls back to the first device when all are offline', async () => {
    window.localStorage.setItem(OXIDNS_DEVICE_STORAGE_KEY, 'removed')
    vi.mocked(agentApi.summaries).mockResolvedValue(response([{ ...agent('first'), status: 'offline' }, { ...agent('second'), status: 'offline' }]))
    await render()
    expect(navigation.replace).toHaveBeenCalledWith('/agents/first/oxidns')
  })
  it('does not restore a saved Agent that no longer runs OxiDNS', async () => {
    window.localStorage.setItem(OXIDNS_DEVICE_STORAGE_KEY, 'changed')
    vi.mocked(agentApi.summaries).mockResolvedValue(response([agent('changed', 'mihomo'), agent('dns')]))
    await render()
    expect(navigation.replace).toHaveBeenCalledWith('/agents/dns/oxidns')
  })
  it('shows connection guidance only after a successful empty result', async () => {
    vi.mocked(agentApi.summaries).mockResolvedValue(response([agent('proxy', 'mihomo')]))
    const wrapper = await render()
    expect(wrapper.text()).toContain('暂无 OxiDNS Agent')
    const button = wrapper.findAll('button').find(item => item.text().includes('前往 Agent 接入'))!
    await button.trigger('click')
    expect(navigation.push).toHaveBeenCalledWith('/agents')
  })
  it('shows a readable error and retries instead of reporting no Agents', async () => {
    vi.mocked(agentApi.summaries).mockRejectedValueOnce({ response: { data: { message: '服务暂不可用' } } })
    const wrapper = await render()
    expect(wrapper.text()).toContain('服务暂不可用')
    expect(wrapper.text()).not.toContain('暂无 OxiDNS Agent')
    vi.mocked(agentApi.summaries).mockResolvedValueOnce(response([agent('recovered')]))
    await wrapper.findAll('button').find(item => item.text() === '重试')!.trigger('click')
    await flushPromises()
    expect(navigation.replace).toHaveBeenCalledWith('/agents/recovered/oxidns')
  })
  it('continues when browser storage is unavailable', async () => {
    const storage = vi.spyOn(window.localStorage, 'getItem').mockImplementation(() => { throw new Error('blocked') })
    vi.mocked(agentApi.summaries).mockResolvedValue(response([agent('available')]))
    await render()
    expect(navigation.replace).toHaveBeenCalledWith('/agents/available/oxidns')
    storage.mockRestore()
  })
  it('ignores a late singleton response after leaving the page', async () => {
    let finish!: (value: any) => void
    vi.mocked(agentApi.summaries).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    const wrapper = await render()
    wrapper.unmount()
    finish(response([agent('late')]))
    await flushPromises()
    expect(navigation.replace).not.toHaveBeenCalled()
  })
})
