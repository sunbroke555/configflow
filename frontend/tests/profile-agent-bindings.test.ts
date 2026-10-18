import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, shallowMount, type VueWrapper } from '@vue/test-utils'
import { profileApi } from '@/api'
import { useProfileStore } from '@/stores/profile'
import { confirmDanger, notify } from '@/lib/feedback'
import { Select } from '@/components/ui/select'
import ProfileAgentBindings from '@/components/ProfileAgentBindings.vue'
import Profiles from '@/views/Profiles.vue'

vi.mock('@/api', () => ({ profileApi: { list: vi.fn(), agentBindings: vi.fn(), rebindAgents: vi.fn(), delete: vi.fn() }, subscriptionApi: {} }))
vi.mock('@/lib/feedback', () => ({ confirmDanger: vi.fn(), prompt: vi.fn(), notify: { warning: vi.fn(), error: vi.fn(), success: vi.fn() } }))
const response = (data: any) => ({ data } as any)
const outside = { id: 'outside', name: '外面', is_default: false }
const home = { id: 'home', name: '家里', is_default: true }
const agents = [
  { id: 'proxy', name: '外面代理', host: '192.0.2.1', port: 8080, service_type: 'mihomo' },
  { id: 'dns', name: '外面 DNS', host: '2001:db8::1', port: 8081, service_type: 'oxidns' },
]
const wrappers: VueWrapper[] = []
const button = (wrapper: VueWrapper, text: string) => wrapper.findAll('button').find(item => item.text().includes(text))!
const store = useProfileStore()
async function render(deleting = false) {
  const wrapper = mount(ProfileAgentBindings, { props: { profile: outside, deleteRequested: deleting },
    global: { stubs: { DialogPortal: { template: '<div><slot /></div>' } } }, attachTo: document.body })
  wrappers.push(wrapper)
  await flushPromises()
  return wrapper
}
async function selectTarget(wrapper: VueWrapper) {
  wrapper.getComponent(Select).vm.$emit('update:modelValue', 'home')
  await flushPromises()
}
async function renderPage() {
  const wrapper = shallowMount(Profiles, { global: { renderStubDefaultSlot: true, stubs: { Button: false } } })
  wrappers.push(wrapper)
  await flushPromises()
  return wrapper
}

beforeEach(async () => {
  vi.resetAllMocks()
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: true, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} })))
  vi.mocked(profileApi.list).mockResolvedValue(response([{ ...outside, bound_agents: agents }, home]))
  vi.mocked(profileApi.agentBindings).mockResolvedValue(response({ profile: outside, agents }))
  vi.mocked(profileApi.rebindAgents).mockResolvedValue(response({ from_profile: outside, to_profile: home, agents }))
  vi.mocked(confirmDanger).mockResolvedValue(true)
  await store.refreshProfiles()
})
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  document.body.innerHTML = ''
  vi.unstubAllGlobals()
})

describe('profile Agent binding editor', () => {
  it('lists device names, types and addresses with the exact impact before a target is chosen', async () => {
    const wrapper = await render()
    for (const text of ['外面代理', '外面 DNS', '192.0.2.1:8080', '[2001:db8::1]:8081', 'Mihomo', 'OxiDNS', '发布记录保留', '不会复制到新空间']) {
      expect(wrapper.text()).toContain(text)
    }
    expect(button(wrapper, '解除绑定并改绑').attributes('disabled')).toBeDefined()
    expect(profileApi.rebindAgents).not.toHaveBeenCalled()
  })

  it('moves only the reviewed IDs and shows a persistent result before allowing deletion', async () => {
    const wrapper = await render(true)
    await selectTarget(wrapper)
    await button(wrapper, '解除绑定并改绑').trigger('click')
    await flushPromises()
    expect(profileApi.rebindAgents).toHaveBeenCalledWith('outside', 'home', ['proxy', 'dns'])
    expect(wrapper.emitted('changed')).toHaveLength(1)
    expect(wrapper.text()).toContain('以下 2 个 Agent 已解除与「外面」的绑定，并改绑到「家里」')
    expect(wrapper.text()).toContain('本次未推送配置或重启服务')
    expect(wrapper.text()).toContain('外面 DNS')
    expect(profileApi.delete).not.toHaveBeenCalled()
    await button(wrapper, '继续删除配置空间').trigger('click')
    expect(wrapper.emitted('continue-delete')).toHaveLength(1)
  })

  it('requires refreshing and reviewing after a conflict without showing a success', async () => {
    vi.mocked(profileApi.rebindAgents).mockRejectedValue({ response: { status: 409, data: { message: '外面 DNS 正在发布，请完成后再改绑' } } })
    const wrapper = await render()
    await selectTarget(wrapper)
    await button(wrapper, '解除绑定并改绑').trigger('click')
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('外面 DNS 正在发布')
    expect(button(wrapper, '解除绑定并改绑').attributes('disabled')).toBeDefined()
    expect(wrapper.emitted('changed')).toBeUndefined()
    vi.mocked(profileApi.agentBindings).mockResolvedValue(response({ profile: outside, agents: [agents[0]] }))
    await button(wrapper, '刷新绑定列表').trigger('click')
    await flushPromises()
    vi.mocked(profileApi.rebindAgents).mockResolvedValue(response({ from_profile: outside, to_profile: home, agents: [agents[0]] }))
    await button(wrapper, '解除绑定并改绑').trigger('click')
    await flushPromises()
    expect(profileApi.rebindAgents).toHaveBeenLastCalledWith('outside', 'home', ['proxy'])
  })

  it('makes read errors retryable and never treats failure as no bindings', async () => {
    vi.mocked(profileApi.agentBindings).mockRejectedValueOnce(new Error('offline'))
    const wrapper = await render(true)
    expect(wrapper.get('[role="alert"]').text()).toContain('读取 Agent 绑定失败')
    expect(button(wrapper, '继续删除')).toBeUndefined()
    await button(wrapper, '刷新绑定列表').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('外面 DNS')
  })

  it('does not duplicate submission or close while a rebind is pending', async () => {
    let finish!: (value: any) => void
    vi.mocked(profileApi.rebindAgents).mockReturnValue(new Promise(resolve => { finish = resolve }))
    const wrapper = await render()
    await selectTarget(wrapper)
    await button(wrapper, '解除绑定并改绑').trigger('click')
    await button(wrapper, '正在调整绑定').trigger('click')
    expect(profileApi.rebindAgents).toHaveBeenCalledTimes(1)
    expect(button(wrapper, '关闭').attributes('disabled')).toBeDefined()
    finish(response({ from_profile: outside, to_profile: home, agents }))
    await flushPromises()
    expect(button(wrapper, '完成').attributes('disabled')).toBeUndefined()
  })
})

describe('profile deletion binding checks', () => {
  it('opens binding details before any deletion confirmation and can be canceled', async () => {
    const wrapper = await renderPage()
    expect(wrapper.text()).toContain('2 个 Agent 绑定：外面代理、外面 DNS')
    await wrapper.findAll('[aria-label="删除"]')[0]!.trigger('click')
    await flushPromises()
    expect(profileApi.agentBindings).toHaveBeenCalledWith('outside')
    expect(wrapper.getComponent(ProfileAgentBindings).props('deleteRequested')).toBe(true)
    expect(confirmDanger).not.toHaveBeenCalled()
    expect(profileApi.delete).not.toHaveBeenCalled()
    wrapper.getComponent(ProfileAgentBindings).vm.$emit('close')
    await flushPromises()
    expect(wrapper.findComponent(ProfileAgentBindings).exists()).toBe(false)
  })

  it('checks bindings again before continuing and still requires deletion confirmation', async () => {
    const wrapper = await renderPage()
    await wrapper.findAll('[aria-label="删除"]')[0]!.trigger('click')
    await flushPromises()
    vi.mocked(profileApi.agentBindings).mockResolvedValue(response({ profile: outside, agents: [] }))
    vi.mocked(confirmDanger).mockResolvedValueOnce(false)
    wrapper.getComponent(ProfileAgentBindings).vm.$emit('continue-delete')
    await flushPromises()
    expect(profileApi.agentBindings).toHaveBeenCalledTimes(2)
    expect(confirmDanger).toHaveBeenCalledWith(expect.stringContaining('配置链接将失效'), { title: '删除配置空间' })
    expect(profileApi.delete).not.toHaveBeenCalled()
  })

  it('opens fresh binding details if an Agent binds after the initial check', async () => {
    vi.mocked(profileApi.agentBindings).mockResolvedValue(response({ profile: outside, agents: [] }))
    vi.mocked(profileApi.delete).mockRejectedValue({ response: { data: { usages: [{ kind: 'agent', id: 'new' }] } } })
    const wrapper = await renderPage()
    await wrapper.findAll('[aria-label="删除"]')[0]!.trigger('click')
    await flushPromises()
    expect(profileApi.delete).toHaveBeenCalledWith('outside')
    expect(wrapper.getComponent(ProfileAgentBindings).props('profile').id).toBe('outside')
    expect(notify.success).not.toHaveBeenCalled()
  })
})
