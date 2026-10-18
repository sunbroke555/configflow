import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import AgentDNSFollow from '@/components/agents/AgentDNSFollow.vue'
import { Select } from '@/components/ui/select'
import { agentApi } from '@/api'
import { notify } from '@/lib/feedback'

vi.mock('@/api', () => ({ agentApi: {
  getDNSFollow: vi.fn(), setDNSFollow: vi.fn(), removeDNSFollow: vi.fn(), retryDNSFollow: vi.fn(),
} }))
vi.mock('@/lib/feedback', () => ({ notify: { success: vi.fn(), error: vi.fn() } }))
const wrappers: ReturnType<typeof mount>[] = []
const binding = {
  id: 'binding-1', source_agent_id: 'mihomo-1', upstream_tag: 'forward_remote', upstream_index: 1,
  connection: 'auto', source_profile_id: 'default', target_profile_id: 'dns', after_deployment_id: 'older',
}
const sources = [{ id: 'mihomo-1', name: '家庭 Mihomo' }]
const upstreams = [
  { tag: 'forward_remote', index: 0, address: 'udp://9.9.9.9:53' },
  { tag: 'forward_remote', index: 1, address: 'tcp://127.0.0.1:1053' },
]
function options(overrides: Record<string, unknown> = {}) {
  return { success: true, binding: null, state: null, sources, upstreams, ...overrides }
}
function agent(overrides: Record<string, unknown> = {}) {
  return { id: 'oxi-1', name: '家庭 OxiDNS', service_type: 'oxidns', ...overrides } as any
}
function render(overrides: Record<string, unknown> = {}) {
  const wrapper = mount(AgentDNSFollow, { props: { agent: agent(overrides), sourceName: '家庭 Mihomo' }, attachTo: document.body })
  wrappers.push(wrapper)
  return wrapper
}
function button(text: string) {
  const found = Array.from(document.querySelectorAll('button')).find(item => item.textContent?.trim() === text)
  expect(found, `button ${text}`).toBeTruthy()
  return found!
}
async function click(text: string) {
  button(text).click()
  await flushPromises()
}
async function choose(wrapper: ReturnType<typeof mount>, index: number, value: string) {
  wrapper.findAllComponents(Select)[index].vm.$emit('update:modelValue', value)
  await flushPromises()
}
async function submit() {
  document.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
  await flushPromises()
}
beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(agentApi.getDNSFollow).mockResolvedValue({ data: options() } as any)
  vi.mocked(agentApi.setDNSFollow).mockResolvedValue({ data: { success: true, binding, state: { status: 'waiting' } } } as any)
  vi.mocked(agentApi.removeDNSFollow).mockResolvedValue({ data: { success: true, binding: null, state: null } } as any)
  vi.mocked(agentApi.retryDNSFollow).mockResolvedValue({ data: { success: true, binding, state: { status: 'preparing', message: '正在确认已生效的 Mihomo DNS' } } } as any)
})
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  document.body.innerHTML = ''
})

describe('explicit Mihomo DNS follow binding', () => {
  it('requires an explicit source and exact upstream entry, then enables follow without publishing in the browser', async () => {
    const wrapper = render()
    expect(agentApi.getDNSFollow).not.toHaveBeenCalled()
    await click('跟随 Mihomo DNS')
    expect(agentApi.getDNSFollow).toHaveBeenCalledWith('oxi-1')
    expect(button('开启自动同步并发布').disabled).toBe(true)
    await choose(wrapper, 0, 'mihomo-1')
    expect(button('开启自动同步并发布').disabled).toBe(true)
    await choose(wrapper, 1, JSON.stringify(['forward_remote', 1]))
    expect(button('开启自动同步并发布').disabled).toBe(false)
    await submit()
    expect(agentApi.setDNSFollow).toHaveBeenCalledWith('oxi-1', {
      source_agent_id: 'mihomo-1', upstream_tag: 'forward_remote', upstream_index: 1, connection: 'auto',
    })
    expect(wrapper.emitted('changed')).toHaveLength(1)
    expect(wrapper.text()).toContain('等待 Mihomo 发布')
    expect(agentApi.retryDNSFollow).not.toHaveBeenCalled()
    expect(wrapper.text()).not.toContain('同步失败')
  })

  it('requires an explicit network address and never fills it from the management host', async () => {
    const wrapper = render({ host: 'agent-proxy.example', port: 8080 })
    await click('跟随 Mihomo DNS')
    await choose(wrapper, 0, 'mihomo-1')
    await choose(wrapper, 1, JSON.stringify(['forward_remote', 1]))
    await choose(wrapper, 2, 'network')
    const field = document.getElementById('dns-follow-address-oxi-1') as HTMLInputElement
    expect(field.value).toBe('')
    expect(button('开启自动同步并发布').disabled).toBe(true)
    field.value = ' 2001:db8::123 '
    field.dispatchEvent(new Event('input', { bubbles: true }))
    await flushPromises()
    await submit()
    expect(agentApi.setDNSFollow).toHaveBeenCalledWith('oxi-1', expect.objectContaining({ connection: 'network', address: '2001:db8::123' }))
  })

  it('shows backend validation failures and retains the form for correction', async () => {
    vi.mocked(agentApi.setDNSFollow).mockRejectedValue({ response: { data: { message: '请先成功发布一次 OxiDNS 配置。' } } })
    const wrapper = render()
    await click('跟随 Mihomo DNS')
    await choose(wrapper, 0, 'mihomo-1')
    await choose(wrapper, 1, JSON.stringify(['forward_remote', 0]))
    await submit()
    expect(document.querySelector('[role="dialog"]')).not.toBeNull()
    expect(document.querySelector('[role="alert"]')?.textContent).toContain('请先成功发布一次 OxiDNS 配置。')
    expect(wrapper.emitted('changed')).toBeUndefined()
    expect(notify.success).not.toHaveBeenCalled()
    expect(button('开启自动同步并发布').disabled).toBe(false)
  })

  it('shows read errors with retry and lets users stop an existing binding without modifying configs', async () => {
    vi.mocked(agentApi.getDNSFollow).mockRejectedValueOnce({ response: { data: { message: '当前无法读取配置空间。' } } })
      .mockResolvedValueOnce({ data: options({ binding, state: { status: 'waiting' } }) } as any)
    const wrapper = render({ dns_follow: binding })
    await click('设置')
    expect(document.querySelector('[role="alert"]')?.textContent).toContain('当前无法读取配置空间。')
    await click('重新读取')
    await click('停止跟随')
    expect(agentApi.removeDNSFollow).toHaveBeenCalledWith('oxi-1')
    expect(agentApi.setDNSFollow).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('跟随 Mihomo DNS')
    expect(wrapper.text()).not.toContain('等待 Mihomo 发布')
  })

  it('explains empty upstreams instead of selecting another DNS service or making up an endpoint', async () => {
    vi.mocked(agentApi.getDNSFollow).mockResolvedValue({ data: options({ sources: [], upstreams: [] }) } as any)
    render()
    await click('跟随 Mihomo DNS')
    expect(document.body.textContent).toContain('请先接入一个 Mihomo Agent')
    expect(document.body.textContent).toContain('当前配置没有可绑定的 UDP / TCP 上游')
    expect(button('开启自动同步并发布').disabled).toBe(true)
    expect(agentApi.setDNSFollow).not.toHaveBeenCalled()
  })
})

describe('DNS follow results and retry', () => {
  it('does not claim Mihomo was updated when binding validation fails before a new publish', () => {
    const wrapper = render({ dns_follow: binding, dns_follow_state: {
      status: 'failed', message: 'DNS 联动暂不可用：Mihomo 来源已删除，请重新绑定。',
    } })
    expect(wrapper.text()).toContain('OxiDNS 同步失败')
    expect(wrapper.text()).toContain('Mihomo 来源已删除')
    expect(wrapper.text()).not.toContain('Mihomo 已更新')
  })

  it('keeps source success distinct from destination failure and retries once with real new progress', async () => {
    const wrapper = render({ dns_follow: binding, dns_follow_state: {
      status: 'failed', source_deployment_id: 'mihomo-publish-2', message: 'Mihomo 已更新，OxiDNS 同步失败：OxiDNS 有其他未发布修改，请先发布或还原。',
      events: [{ at: '2026-10-19T01:00:00Z', status: 'failed', message: 'OxiDNS 有其他未发布修改，请先发布或还原。' }],
    } })
    expect(wrapper.text()).toContain('Mihomo 已更新，OxiDNS 同步失败')
    expect(wrapper.text()).toContain('OxiDNS 有其他未发布修改')
    expect(wrapper.get('details').attributes('open')).toBeUndefined()
    let resolve!: (value: any) => void
    vi.mocked(agentApi.retryDNSFollow).mockImplementationOnce(() => new Promise(done => { resolve = done }))
    button('重试同步').click()
    await flushPromises()
    expect(button('正在提交…').disabled).toBe(true)
    resolve({ data: { success: true, binding, state: { status: 'preparing', source_deployment_id: 'mihomo-publish-2', message: '正在确认已生效的 Mihomo DNS' } } })
    await flushPromises()
    expect(agentApi.retryDNSFollow).toHaveBeenCalledExactlyOnceWith('oxi-1')
    expect(wrapper.text()).toContain('Mihomo 已更新')
    expect(wrapper.text()).toContain('正在确认已生效的 Mihomo DNS')
    expect(wrapper.text()).not.toContain('重试同步')
    expect(wrapper.text()).not.toContain('OxiDNS DNS 同步完成')
  })

  it('retains failure details when retry is rejected and updates when Agent list polling confirms success', async () => {
    const failed = { status: 'failed', source_deployment_id: 'published', message: 'OxiDNS 暂时离线。' }
    const wrapper = render({ dns_follow: binding, dns_follow_state: failed })
    vi.mocked(agentApi.retryDNSFollow).mockRejectedValue({ response: { data: { message: '另一个发布任务仍在运行。' } } })
    await click('重试同步')
    expect(wrapper.text()).toContain('OxiDNS 暂时离线')
    expect(wrapper.get('[role="alert"]').text()).toContain('另一个发布任务仍在运行')
    expect(notify.success).not.toHaveBeenCalled()
    await wrapper.setProps({ agent: agent({ dns_follow: binding, dns_follow_state: {
      status: 'succeeded', source_deployment_id: 'published', address: 'tcp://127.0.0.1:2053', message: 'OxiDNS 已通过健康检查。',
    } }) })
    expect(wrapper.text()).toContain('OxiDNS DNS 同步完成')
    expect(wrapper.text()).toContain('tcp://127.0.0.1:2053')
    expect(wrapper.text()).not.toContain('重试同步')
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
  })

  it('waits for a publishing result without exposing retry or claiming success', () => {
    const wrapper = render({ dns_follow: binding, dns_follow_state: {
      status: 'publishing', source_deployment_id: 'published', message: '暂时无法确认 OxiDNS 发布结果，正在继续查询。',
    } })
    expect(wrapper.text()).toContain('Mihomo 已更新')
    expect(wrapper.text()).toContain('正在继续查询')
    expect(wrapper.text()).not.toContain('OxiDNS DNS 同步完成')
    expect(wrapper.text()).not.toContain('重试同步')
    expect(agentApi.retryDNSFollow).not.toHaveBeenCalled()
  })
})
