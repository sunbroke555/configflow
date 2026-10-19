import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import MihomoCoreActions from '@/components/mihomo/MihomoCoreActions.vue'
import { agentApi, mihomoPanelApi } from '@/api'
import { confirm, confirmDanger, notify } from '@/lib/feedback'

vi.mock('@/api', () => ({
  agentApi: { restart: vi.fn() },
  mihomoPanelApi: {
    version: vi.fn(), reloadConfig: vi.fn(), upgradeCore: vi.fn(), flushFakeIP: vi.fn(), flushDNS: vi.fn()
  }
}))
vi.mock('@/lib/feedback', () => ({
  confirm: vi.fn(), confirmDanger: vi.fn(),
  notify: { success: vi.fn(), error: vi.fn(), warning: vi.fn() }
}))

type Actions = {
  reloadConfig: () => Promise<void>
  flush: (kind: 'fakeip' | 'dns') => Promise<void>
  upgradeCore: () => Promise<void>
  restartCore: () => Promise<void>
}
const render = () => {
  const wrapper = mount(MihomoCoreActions, { props: { agentId: 'gw' } })
  return { wrapper, actions: (wrapper.vm as any).$.setupState as Actions }
}

beforeEach(() => { vi.clearAllMocks(); vi.useFakeTimers() })
afterEach(() => { vi.useRealTimers() })

describe('mihomo core actions', () => {
  it('reloads only after confirmation and refreshes the panel', async () => {
    const { wrapper, actions } = render()
    vi.mocked(confirm).mockResolvedValueOnce(false)
    await actions.reloadConfig()
    expect(mihomoPanelApi.reloadConfig).not.toHaveBeenCalled()
    vi.mocked(confirm).mockResolvedValueOnce(true)
    vi.mocked(mihomoPanelApi.reloadConfig).mockResolvedValue({} as any)
    await actions.reloadConfig()
    expect(mihomoPanelApi.reloadConfig).toHaveBeenCalledWith('gw')
    expect(wrapper.emitted('changed')).toHaveLength(1)
  })

  it('flushes caches without confirmation', async () => {
    const { actions } = render()
    vi.mocked(mihomoPanelApi.flushFakeIP).mockResolvedValue({} as any)
    await actions.flush('fakeip')
    expect(mihomoPanelApi.flushFakeIP).toHaveBeenCalledWith('gw')
    expect(notify.success).toHaveBeenCalledWith('已清空 FakeIP 缓存')
  })

  it('restarts through the Agent and waits for the core to answer again', async () => {
    const { wrapper, actions } = render()
    vi.mocked(confirmDanger).mockResolvedValue(true)
    vi.mocked(agentApi.restart).mockResolvedValue({ data: { success: true } } as any)
    vi.mocked(mihomoPanelApi.version)
      .mockRejectedValueOnce(new Error('down'))
      .mockResolvedValue({ data: { data: { version: 'v1.19.32' } } } as any)
    const pending = actions.restartCore()
    await vi.advanceTimersByTimeAsync(5000)
    await pending
    await flushPromises()
    expect(agentApi.restart).toHaveBeenCalledWith('gw')
    expect(mihomoPanelApi.version).toHaveBeenCalledTimes(2)
    expect(notify.success).toHaveBeenCalledWith('内核已重启')
    expect(wrapper.emitted('changed')).toHaveLength(1)
  })

  it('reports an upgrade refused by the core', async () => {
    const { wrapper, actions } = render()
    vi.mocked(confirm).mockResolvedValue(true)
    vi.mocked(mihomoPanelApi.upgradeCore).mockRejectedValue({ response: { data: { message: 'already using latest version' } } })
    await actions.upgradeCore()
    expect(notify.error).toHaveBeenCalledWith('更新内核失败', 'already using latest version')
    expect(wrapper.emitted('changed')).toBeUndefined()
  })

  it('distinguishes an in-flight core update request from waiting for the core to return', async () => {
    const { wrapper, actions } = render()
    vi.mocked(confirm).mockResolvedValue(true)
    let accept!: (value: any) => void
    vi.mocked(mihomoPanelApi.upgradeCore).mockReturnValue(new Promise(resolve => { accept = resolve }))
    vi.mocked(mihomoPanelApi.version).mockResolvedValue({ data: { data: { version: 'v1.19.32' } } } as any)
    const pending = actions.upgradeCore()
    await flushPromises()
    expect(wrapper.text()).toContain('请求 Mihomo 更新')
    expect(wrapper.get('button').attributes('aria-label')).toBe('请求 Mihomo 更新…')
    expect(wrapper.get('button').attributes('aria-busy')).toBe('true')
    expect(wrapper.emitted('status-change')?.at(-1)).toEqual(['请求 Mihomo 更新…'])
    expect(mihomoPanelApi.version).not.toHaveBeenCalled()
    accept({})
    await flushPromises()
    expect(wrapper.text()).toContain('等待 Mihomo 重新上线')
    expect(wrapper.emitted('status-change')?.at(-1)).toEqual(['等待 Mihomo 重新上线…'])
    expect(notify.success).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(2000)
    await pending
    await flushPromises()
    expect(wrapper.emitted('status-change')?.at(-1)).toEqual([''])
    expect(wrapper.get('button').attributes('aria-busy')).toBe('false')
    expect(notify.success).toHaveBeenCalledWith('内核已更新', '当前版本 v1.19.32')
    wrapper.unmount()
  })
})
