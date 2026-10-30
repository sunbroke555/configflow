import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { agentApi } from '@/api'
import { Select } from '@/components/ui/select'
import { Input } from '@/components/ui/input'
import MihomoIPv6VerificationSettings from '@/components/generate/MihomoIPv6VerificationSettings.vue'

vi.mock('@/router', () => ({ default: { push: vi.fn() } }))
const response = (data: any) => ({ data } as any)
const settings = (extra = {}) => ({ success: true, agent_id: 'gw', profile_id: 'bound',
  settings: { client_agent_id: '', test_url: '' }, candidates: [{ id: 'client', name: '检测客户端', host: '192.0.2.30', status: 'online' }], ...extra })
const wrappers: VueWrapper[] = []
const button = (wrapper: VueWrapper, text: string) => wrapper.findAll('button').find(item => item.text().includes(text))!
async function render() {
  const wrapper = shallowMount(MihomoIPv6VerificationSettings, { props: { open: true, agentId: 'gw', profileId: 'bound' },
    global: { renderStubDefaultSlot: true, stubs: { Button: false, Primitive: false } } })
  wrappers.push(wrapper)
  await button(wrapper, '验收设置').trigger('click')
  await flushPromises()
  return wrapper
}
beforeEach(() => {
  vi.spyOn(agentApi, 'getIPv6VerificationSettings').mockResolvedValue(response(settings()))
  vi.spyOn(agentApi, 'setIPv6VerificationSettings').mockImplementation(async (_id, _profile, value) => response(settings({ settings: value })))
})
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); vi.restoreAllMocks() })

describe('independent IPv6 verification client settings', () => {
  it('keeps host-only mode until a client is explicitly selected and saved', async () => {
    const wrapper = await render()
    expect(wrapper.getComponent(Select).props('modelValue')).toBe('__none__')
    expect(agentApi.setIPv6VerificationSettings).not.toHaveBeenCalled()
    wrapper.getComponent(Select).vm.$emit('update:modelValue', 'client')
    await flushPromises()
    await button(wrapper, '保存验收设置').trigger('click')
    await flushPromises()
    expect(agentApi.setIPv6VerificationSettings).toHaveBeenCalledExactlyOnceWith('gw', 'bound', { client_agent_id: 'client', test_url: '' })
    expect(wrapper.emitted('saved')).toHaveLength(1)
  })

  it('does not replace a missing configured client with the remaining candidate', async () => {
    vi.mocked(agentApi.getIPv6VerificationSettings).mockResolvedValue(response(settings({ settings: { client_agent_id: 'old', test_url: '' } })))
    const wrapper = await render()
    expect(wrapper.getComponent(Select).props('modelValue')).toBe('old')
    expect(button(wrapper, '保存验收设置').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('原检测客户端已离线或不符合条件')
    expect(agentApi.setIPv6VerificationSettings).not.toHaveBeenCalled()
  })

  it('does not save a client whose Agent cannot support the checks', async () => {
    vi.mocked(agentApi.getIPv6VerificationSettings).mockResolvedValue(response(settings({ candidates: [
      { id: 'client', name: '旧版 Agent', host: '192.0.2.30', status: 'online', supported: false },
    ] })))
    const wrapper = await render()
    wrapper.getComponent(Select).vm.$emit('update:modelValue', 'client')
    await flushPromises()
    expect(button(wrapper, '保存验收设置').attributes('disabled')).toBeDefined()
    await button(wrapper, '保存验收设置').trigger('click')
    expect(agentApi.setIPv6VerificationSettings).not.toHaveBeenCalled()
  })

  it.each(['file:///etc/passwd', 'https://user:secret@example.test/check'])('rejects a detection URL that is not a plain HTTP(S) URL (%s)', async testURL => {
    const wrapper = await render()
    wrapper.getComponent(Input).vm.$emit('update:modelValue', testURL)
    await flushPromises()
    await button(wrapper, '保存验收设置').trigger('click')
    expect(agentApi.setIPv6VerificationSettings).not.toHaveBeenCalled()
    expect(wrapper.get('[role="alert"]').text()).toContain('不能包含账号密码')
  })
})
