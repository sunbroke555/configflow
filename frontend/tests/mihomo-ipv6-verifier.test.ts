import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { agentApi } from '@/api'
import MihomoIPv6Verification from '@/components/generate/MihomoIPv6Verification.vue'
import MihomoIPv6VerificationResult from '@/components/generate/MihomoIPv6VerificationResult.vue'

vi.mock('@/router', () => ({ default: { push: vi.fn() } }))
const response = (data: any) => ({ data } as any)
const report = (extra = {}) => ({ success: true, agent_id: 'gw', profile_id: 'bound', verification: {
  status: 'running', scope: 'mihomo_runtime', config_revision: 7, deployment_id: 'current', checks: [], ...extra,
} })
const wrappers: VueWrapper[] = []
async function render(extra = {}) {
  const wrapper = shallowMount(MihomoIPv6Verification, { props: {
    open: true, agentId: 'gw', profileId: 'bound', configRevision: 7, ...extra,
  }, global: { stubs: { Button: false, Primitive: false } } })
  wrappers.push(wrapper)
  await flushPromises()
  return wrapper
}
beforeEach(() => {
  vi.useFakeTimers()
  vi.spyOn(agentApi, 'verifyIPv6').mockResolvedValue(response(report()))
  vi.spyOn(agentApi, 'getIPv6Verification').mockResolvedValue(response({ success: true, agent_id: 'gw', profile_id: 'bound', verification: null }))
})
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); vi.restoreAllMocks(); vi.useRealTimers() })

describe('Mihomo verification lifecycle', () => {
  it('reads saved results without starting a verification until the user asks', async () => {
    const wrapper = await render()
    expect(agentApi.getIPv6Verification).toHaveBeenCalledExactlyOnceWith('gw', 'bound', undefined)
    expect(agentApi.verifyIPv6).not.toHaveBeenCalled()
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(agentApi.verifyIPv6).toHaveBeenCalledExactlyOnceWith('gw', { profile_id: 'bound', expected_config_revision: 7 })
    vi.mocked(agentApi.getIPv6Verification).mockResolvedValue(response(report({ status: 'warning', checks: [
      { id: 'client', label: '客户端全链路', status: 'unknown', message: '尚未配置独立检测客户端。' },
    ] })))
    await vi.advanceTimersByTimeAsync(2000)
    await flushPromises()
    expect(wrapper.getComponent(MihomoIPv6VerificationResult).props('verification').status).toBe('warning')
    const count = vi.mocked(agentApi.getIPv6Verification).mock.calls.length
    await vi.advanceTimersByTimeAsync(4000)
    expect(agentApi.getIPv6Verification).toHaveBeenCalledTimes(count)
  })

  it('ensures automatic verification for the exact successful publication and polls its saved result', async () => {
    const wrapper = await render({ auto: true, deploymentId: 'current' })
    expect(agentApi.verifyIPv6).toHaveBeenCalledExactlyOnceWith('gw', { profile_id: 'bound', expected_config_revision: 7, deployment_id: 'current' })
    vi.mocked(agentApi.getIPv6Verification).mockResolvedValue(response(report()))
    await vi.advanceTimersByTimeAsync(2000)
    expect(agentApi.getIPv6Verification).toHaveBeenCalledWith('gw', 'bound', 'current')
    expect(wrapper.text()).toContain('可以离开此窗口')
    await wrapper.setProps({ open: false })
    const count = vi.mocked(agentApi.getIPv6Verification).mock.calls.length
    await vi.advanceTimersByTimeAsync(10000)
    expect(agentApi.getIPv6Verification).toHaveBeenCalledTimes(count)
  })

  it.each([{ config_revision: 8 }, { deployment_id: 'old' }])('rejects saved evidence from another revision or deployment (%j)', async changes => {
    vi.mocked(agentApi.verifyIPv6).mockResolvedValue(response(report({ ...changes, status: 'ok' })))
    const wrapper = await render({ auto: true, deploymentId: 'current' })
    expect(wrapper.get('[role="alert"]').text()).toContain('其他配置版本')
    expect(wrapper.findComponent(MihomoIPv6VerificationResult).exists()).toBe(false)
  })

  it('ignores delayed verification results after the target changes', async () => {
    let resolve!: (value: any) => void
    vi.mocked(agentApi.verifyIPv6).mockReturnValueOnce(new Promise(done => { resolve = done }))
    const wrapper = await render({ auto: true, deploymentId: 'current' })
    await wrapper.setProps({ open: false })
    resolve(response(report({ status: 'ok', message: 'old target result' })))
    await flushPromises()
    expect(wrapper.findComponent(MihomoIPv6VerificationResult).exists()).toBe(false)
    expect(wrapper.text()).not.toContain('old target result')
  })

  it('stops prolonged UI polling without claiming the backend task was canceled or passed', async () => {
    vi.mocked(agentApi.getIPv6Verification).mockResolvedValue(response(report()))
    const wrapper = await render({ auto: true, deploymentId: 'current' })
    await vi.advanceTimersByTimeAsync(152000)
    await flushPromises()
    expect(wrapper.text()).toContain('验收仍在后台执行')
    expect(wrapper.findComponent(MihomoIPv6VerificationResult).exists()).toBe(false)
    const count = vi.mocked(agentApi.getIPv6Verification).mock.calls.length
    await vi.advanceTimersByTimeAsync(10000)
    expect(agentApi.getIPv6Verification).toHaveBeenCalledTimes(count)
  })
})
