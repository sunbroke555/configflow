import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ruleLocationDetails } from './fixtures/agent-failure'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { agentApi, oxidnsDevicePolicyApi, oxidnsMonitorApi } from '@/api'
import { confirm, notify } from '@/lib/feedback'
import { canReloadPage } from '@/lib/pageReloadGuard'
import OxiDNSDevicePolicies from '@/components/agents/OxiDNSDevicePolicies.vue'
import OxiDNSMonitor from '@/views/OxiDNSMonitor.vue'
import OxiDNSClientSelect from '@/components/agents/OxiDNSClientSelect.vue'
import { Select } from '@/components/ui/select'

vi.mock('@/api', () => ({
  oxidnsDevicePolicyApi: { get: vi.fn(), save: vi.fn() },
  oxidnsMonitorApi: { overview: vi.fn(), clients: vi.fn(), queries: vi.fn() },
  agentApi: { getAll: vi.fn(), pushConfig: vi.fn(), getDeployment: vi.fn(), activateDeployment: vi.fn() }
}))
vi.mock('@/lib/feedback', () => ({ confirm: vi.fn(), notify: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() } }))
const wrappers: VueWrapper[] = []
let scrollDescriptor: PropertyDescriptor | undefined
const response = (data: any) => ({ data } as any)
const state = (policies: any[] = []) => ({ success: true, draft: true, profile_id: 'office', profile_name: '办公室', revision: 'v1', notes_revision: 'n1',
  policies, editable: true, reason: '', agents: [{ id: 'one', name: 'DNS 1' }, { id: 'two', name: 'DNS 2' }] })
const button = (wrapper: VueWrapper, text: string) => wrapper.findAll('button').find(item => item.text() === text)!
async function render(props: any = {}) {
  const wrapper = mount(OxiDNSDevicePolicies, { props: { agentId: 'one', recorder: '', ...props }, attachTo: document.body })
  wrappers.push(wrapper)
  await flushPromises()
  return wrapper
}
async function add(wrapper: VueWrapper, ip = '192.168.50.51') {
  await wrapper.get('#policy-client-ip').setValue(ip)
  await button(wrapper, '添加策略').trigger('click')
}
beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: true, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} })))
  vi.stubGlobal('localStorage', { getItem: () => null, setItem: vi.fn() })
  scrollDescriptor = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollIntoView')
  Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, writable: true, value: vi.fn() })
  vi.mocked(oxidnsDevicePolicyApi.get).mockResolvedValue(response(state()))
  vi.mocked(oxidnsDevicePolicyApi.save).mockImplementation(async (_id, data) => response({ ...state(data.policies), revision: 'v2' }))
  vi.mocked(agentApi.pushConfig).mockImplementation(async (_id, id) => response({ deployment_id: id, status: 'succeeded' }))
  vi.mocked(confirm).mockResolvedValue(true)
})
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  document.body.innerHTML = ''
  vi.useRealTimers()
  vi.unstubAllGlobals()
  if (scrollDescriptor) Object.defineProperty(Element.prototype, 'scrollIntoView', scrollDescriptor)
  else Reflect.deleteProperty(Element.prototype, 'scrollIntoView')
})

describe('OxiDNS device policy editing', () => {

  it('protects an IP still being entered and only the unacknowledged publish request', async () => {
    let resolvePublish!: (value: any) => void
    vi.mocked(agentApi.pushConfig).mockImplementationOnce(() => new Promise(resolve => { resolvePublish = resolve }))
    const wrapper = await render()
    expect(canReloadPage()).toBe(true)
    const input = wrapper.get<HTMLInputElement>('#policy-client-ip')
    await input.setValue('192.168.50.')
    expect(canReloadPage()).toBe(false)
    expect(input.element.value).toBe('192.168.50.')
    await input.setValue('')
    expect(canReloadPage()).toBe(true)
    await button(wrapper, '发布到当前设备').trigger('click')
    await flushPromises()
    expect(canReloadPage()).toBe(false)
    const deploymentId = vi.mocked(agentApi.pushConfig).mock.lastCall![1]
    resolvePublish(response({ deployment_id: deploymentId, status: 'queued' }))
    await flushPromises()
    expect(canReloadPage()).toBe(true)
  })


  it('shows progress while a slow publish request is pending and ignores its late accepted snapshot', async () => {
    vi.useFakeTimers()
    let resolvePublish!: (value: any) => void
    vi.mocked(agentApi.pushConfig).mockImplementationOnce(() => new Promise(resolve => { resolvePublish = resolve }))
    vi.mocked(agentApi.getDeployment)
      .mockRejectedValueOnce({ response: { status: 404 } })
      .mockImplementationOnce(async (_id, deploymentId) => response({ deployment_id: deploymentId, status: 'uploading' }))
      .mockImplementationOnce(async (_id, deploymentId) => response({ deployment_id: deploymentId, status: 'checking' }))
      .mockImplementationOnce(async (_id, deploymentId) => response({ deployment_id: deploymentId, status: 'succeeded' }))
    const wrapper = await render()
    await button(wrapper, '发布到当前设备').trigger('click')
    await flushPromises()
    const deploymentId = vi.mocked(agentApi.pushConfig).mock.lastCall![1]
    expect(button(wrapper, '发布中…').attributes('disabled')).toBeDefined()
    await button(wrapper, '发布中…').trigger('click')
    expect(agentApi.pushConfig).toHaveBeenCalledOnce()
    await vi.advanceTimersByTimeAsync(0)
    expect(wrapper.get('[data-agent-status-row="配置推送"] button[aria-expanded]').attributes('aria-label')).toBe('配置推送：提交中')
    expect(wrapper.text()).not.toContain('发布结果暂未确认')
    await vi.advanceTimersByTimeAsync(2000)
    expect(wrapper.text()).toContain('上传中')
    await vi.advanceTimersByTimeAsync(2000)
    expect(wrapper.text()).toContain('启动检查')
    resolvePublish(response({ deployment_id: deploymentId, status: 'verifying' }))
    await flushPromises()
    expect(wrapper.text()).toContain('启动检查')
    expect(wrapper.text()).not.toContain('校验中')
    await vi.advanceTimersByTimeAsync(2000)
    expect(wrapper.get('[data-agent-status-row="配置推送"] button[aria-expanded]').attributes('aria-label')).toBe('配置推送：成功')
    expect(button(wrapper, '发布到当前设备').attributes('disabled')).toBeUndefined()
    expect(wrapper.emitted('published')).toHaveLength(1)
  })

  it('keeps a polled success when the original request later times out', async () => {
    vi.useFakeTimers()
    let rejectPublish!: (error: any) => void
    vi.mocked(agentApi.pushConfig).mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectPublish = reject }))
    vi.mocked(agentApi.getDeployment).mockImplementationOnce(async (_id, deploymentId) => response({ deployment_id: deploymentId, status: 'succeeded' }))
    const wrapper = await render()
    await button(wrapper, '发布到当前设备').trigger('click')
    await flushPromises()
    await vi.advanceTimersByTimeAsync(0)
    rejectPublish(new Error('request timed out'))
    await flushPromises()
    expect(wrapper.get('[data-agent-status-row="配置推送"] button[aria-expanded]').attributes('aria-label')).toBe('配置推送：成功')
    expect(wrapper.text()).not.toContain('结果待确认')
    expect(button(wrapper, '发布到当前设备').attributes('disabled')).toBeUndefined()
    expect(wrapper.emitted('published')).toHaveLength(1)
  })

  it('reports a definitive rejection and allows another publication', async () => {
    vi.mocked(agentApi.pushConfig).mockRejectedValueOnce({ response: { status: 409, data: { message: '配置已更新，请重新载入' } } })
    const wrapper = await render()
    await button(wrapper, '发布到当前设备').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('配置已更新，请重新载入')
    expect(button(wrapper, '发布到当前设备').attributes('disabled')).toBeUndefined()
  })

  it('adds an offline client, saves to the bound profile and publishes the saved revision', async () => {
    const wrapper = await render()
    expect(wrapper.text()).toContain('DNS 1、DNS 2')
    expect(wrapper.text()).toContain('可手动添加 IP')
    expect(button(wrapper, '保存草稿')).toBeUndefined()
    await add(wrapper)
    await flushPromises()
    expect(oxidnsDevicePolicyApi.save).toHaveBeenCalledWith('one', { profile_id: 'office', revision: 'v1', notes_revision: 'n1', policies: [{ ip: '192.168.50.51', name: '', mode: 'smart' }] })
    wrapper.getComponent(Select).vm.$emit('update:modelValue', 'direct')
    await wrapper.get('input[aria-label="192.168.50.51 备注"]').setValue('电视')
    // Publishing saves the pending note first, then publishes the saved revision.
    await button(wrapper, '发布到当前设备').trigger('click')
    await flushPromises()
    expect(agentApi.pushConfig).toHaveBeenCalledWith('one', expect.any(String), { expected_profile_id: 'office', expected_oxidns_revision: 'v2' })
    expect(wrapper.get('[data-agent-status-row="配置推送"] button[aria-expanded]').attributes('aria-label')).toBe('配置推送：成功')
  })
  it('selects existing clients without an all-clients option and prevents duplicates', async () => {
    vi.mocked(oxidnsMonitorApi.clients).mockResolvedValue(response({ success: true, clients: [{ ip: '192.168.50.50', count: 4 }], sample_limited: false }))
    const wrapper = await render({ recorder: 'queries' })
    await wrapper.getComponent(OxiDNSClientSelect).get('button').trigger('click')
    await flushPromises()
    expect(document.body.textContent).not.toContain('全部客户端')
    const option = [...document.querySelectorAll<HTMLElement>('[role="option"]')].find(node => node.textContent?.includes('192.168.50.50'))!
    option.click()
    await flushPromises()
    expect(wrapper.findAll('tbody tr')).toHaveLength(1)
    await add(wrapper, '192.168.50.50')
    expect(wrapper.findAll('tbody tr')).toHaveLength(1)
    expect(notify.info).toHaveBeenCalledWith('此客户端 IP 或网段已在列表中。')
  })
  it('rejects malformed addresses and canonicalizes IPv6', async () => {
    const wrapper = await render()
    for (const ip of ['999.168.1.1', '192.168.1.1/33', 'dns.example', '2001::::1']) await add(wrapper, ip)
    expect(wrapper.findAll('tbody tr')).toHaveLength(0)
    await add(wrapper, '2001:0db8:0:0:0:0:0:1')
    await add(wrapper, '2001:db8::1')
    expect(wrapper.findAll('tbody tr')).toHaveLength(1)
    expect(wrapper.text()).toContain('2001:db8::1')
  })
  it('adds canonical IPv4/IPv6 networks with host exceptions and prevents equivalent duplicates', async () => {
    const wrapper = await render()
    await add(wrapper, '192.168.50.51/24')
    wrapper.getComponent(Select).vm.$emit('update:modelValue', 'direct')
    await add(wrapper, '192.168.50.0/24')
    await add(wrapper, '::ffff:192.168.50.99/120')
    await add(wrapper, '192.168.50.50')
    await add(wrapper, '192.168.50.50/32')
    await add(wrapper, '2001:0DB8:abcd::1234/48')
    await add(wrapper, '2001:db8:abcd::/48')
    expect(wrapper.findAll('tbody tr')).toHaveLength(3)
    await wrapper.get('[aria-label="手动添加 IP / 网段说明"]').trigger('focus')
    await flushPromises()
    expect(document.querySelector('[data-field-help-content]')?.textContent).toContain('单个 IP 优先')
    await wrapper.get('[aria-label="手动添加 IP / 网段说明"]').trigger('blur')
    expect(wrapper.text()).toContain('192.168.50.0/24')
    expect(wrapper.text()).toContain('2001:db8:abcd::/48')
    await flushPromises()
    expect(vi.mocked(oxidnsDevicePolicyApi.save).mock.lastCall?.[1].policies).toEqual([
      { ip: '192.168.50.0/24', name: '', mode: 'direct' },
      { ip: '192.168.50.50', name: '', mode: 'smart' },
      { ip: '2001:db8:abcd::/48', name: '', mode: 'smart' }
    ])
    await wrapper.get('[aria-label="移除 192.168.50.0/24"]').trigger('click')
    expect(wrapper.findAll('tbody tr')).toHaveLength(2)
    await flushPromises()
    expect(vi.mocked(oxidnsDevicePolicyApi.save).mock.lastCall?.[1].policies.map(row => row.ip)).toEqual(['192.168.50.50', '2001:db8:abcd::/48'])
  })
  it('keeps edits after a conflicting save and requires confirmation to discard them', async () => {
    vi.mocked(oxidnsDevicePolicyApi.save).mockRejectedValue({ response: { status: 409, data: { message: '配置已更新，请重新载入' } } })
    const wrapper = await render()
    await add(wrapper)
    await flushPromises()
    expect(wrapper.text()).toContain('配置已更新，请重新载入')
    expect(wrapper.text()).toContain('保存失败')
    expect(button(wrapper, '发布到当前设备').attributes('disabled')).toBeDefined()
    expect(wrapper.findAll('tbody tr')).toHaveLength(1)
    vi.mocked(confirm).mockResolvedValueOnce(false)
    await button(wrapper, '重新载入').trigger('click')
    await flushPromises()
    expect(wrapper.findAll('tbody tr')).toHaveLength(1)
    await button(wrapper, '重新载入').trigger('click')
    await flushPromises()
    expect(wrapper.findAll('tbody tr')).toHaveLength(0)
  })
  it('saves an empty list when removing the last device', async () => {
    vi.mocked(oxidnsDevicePolicyApi.get).mockResolvedValue(response(state([{ ip: '192.168.50.51', name: '', mode: 'direct' }])))
    const wrapper = await render()
    await wrapper.get('[aria-label="移除 192.168.50.51"]').trigger('click')
    await flushPromises()
    expect(vi.mocked(oxidnsDevicePolicyApi.save).mock.lastCall?.[1].policies).toEqual([])
  })
  it('makes unrecognized native configuration read-only with an explanation', async () => {
    vi.mocked(oxidnsDevicePolicyApi.get).mockResolvedValue(response({ ...state(), editable: false, reason: '处理链已手动修改' }))
    const wrapper = await render()
    expect(wrapper.text()).toContain('处理链已手动修改')
    expect(wrapper.get('fieldset').attributes('disabled')).toBeDefined()
    expect(button(wrapper, '发布到当前设备').attributes('disabled')).toBeDefined()
  })
  it('ignores a late policy response when the selected Agent changes', async () => {
    let resolve!: (value: any) => void
    vi.mocked(oxidnsDevicePolicyApi.get).mockImplementationOnce(() => new Promise(done => { resolve = done }))
    const wrapper = await render()
    await wrapper.setProps({ agentId: 'two' })
    await flushPromises()
    resolve(response({ ...state(), profile_name: '旧设备' }))
    await flushPromises()
    expect(wrapper.text()).not.toContain('旧设备')
  })
  it('keeps the policy tab accessible without monitor data and guards device switches', async () => {
    vi.mocked(agentApi.getAll).mockResolvedValue(response([{ id: 'one', name: 'DNS 1', service_type: 'oxidns' }, { id: 'two', name: 'DNS 2', service_type: 'oxidns' }]))
    vi.mocked(oxidnsMonitorApi.overview).mockRejectedValue(new Error('核心离线'))
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/agents/:agentId/oxidns', component: OxiDNSMonitor }] })
    await router.push('/agents/one/oxidns?tab=devices')
    const wrapper = mount({ template: '<RouterView />' }, { global: { plugins: [router] } })
    wrappers.push(wrapper)
    await flushPromises()
    expect(wrapper.findComponent(OxiDNSDevicePolicies).exists()).toBe(true)
    vi.mocked(oxidnsDevicePolicyApi.save).mockRejectedValue(new Error('网络中断'))
    await add(wrapper)
    await flushPromises()
    vi.mocked(confirm).mockResolvedValueOnce(false)
    await router.push('/agents/two/oxidns?tab=devices')
    expect(router.currentRoute.value.params.agentId).toBe('one')
    await router.push('/agents/two/oxidns?tab=devices')
    await flushPromises()
    expect(router.currentRoute.value.params.agentId).toBe('two')
    expect(wrapper.findAll('tbody tr')).toHaveLength(0)
  })
})

it('reuses a shared device note when adding a DNS policy', async () => {
  vi.mocked(oxidnsDevicePolicyApi.get).mockResolvedValue(response({ ...state(), device_notes: { '192.168.50.51': '工作电脑' } }))
  // Like the server, unnamed new rows take the current shared name (renamed elsewhere here).
  vi.mocked(oxidnsDevicePolicyApi.save).mockImplementation(async (_id, data) => response({ ...state(
    data.policies.map(row => ({ ...row, name: row.name || '客厅电脑' }))), revision: 'v2' }))
  const wrapper = await render({ recorder: 'query_recorder' })
  expect(wrapper.findComponent(OxiDNSClientSelect).props('notes')).toEqual({ '192.168.50.51': '工作电脑' })
  await add(wrapper)
  expect(vi.mocked(oxidnsDevicePolicyApi.save).mock.lastCall?.[1].policies).toEqual([{ ip: '192.168.50.51', name: '', mode: 'smart' }])
  await flushPromises()
  // The loaded name is not sent back, so a rename made elsewhere is kept and shown.
  expect(wrapper.get('input[aria-label="192.168.50.51 备注"]').element.value).toBe('客厅电脑')
  expect(agentApi.pushConfig).not.toHaveBeenCalled()
})

it('keeps a rename made elsewhere when another client is added while the first save is running', async () => {
  vi.mocked(oxidnsDevicePolicyApi.get).mockResolvedValue(response({ ...state(), device_notes: { '192.168.50.51': '旧名', '192.168.50.52': '音箱' } }))
  // The server fills unnamed new rows with the current shared name, renamed elsewhere to 新名.
  const current: Record<string, string> = { '192.168.50.51': '新名', '192.168.50.52': '音箱' }
  let release!: () => void
  const gate = new Promise<void>(resolve => { release = resolve })
  vi.mocked(oxidnsDevicePolicyApi.save).mockImplementation(async (_id, data) => {
    const first = vi.mocked(oxidnsDevicePolicyApi.save).mock.calls.length === 1
    if (first) await gate
    const policies = data.policies.map(row => ({ ...row, name: (current[row.ip] = row.name || current[row.ip] || '') }))
    return response({ ...state(policies), device_notes: { ...current }, revision: first ? 'v2' : 'v3', notes_revision: first ? 'n2' : 'n3' })
  })
  const wrapper = await render()
  await add(wrapper)
  await add(wrapper, '192.168.50.52')
  release(); await flushPromises()
  expect(vi.mocked(oxidnsDevicePolicyApi.save).mock.calls.map(call => call[1].policies.map(row => row.name))).toEqual([[''], ['新名', '']])
  expect(current['192.168.50.51']).toBe('新名')
  expect(wrapper.get('input[aria-label="192.168.50.51 备注"]').element.value).toBe('新名')
})

describe('OxiDNS device policy autosave', () => {
  const existing = () => response(state([{ ip: '192.168.50.51', name: '', mode: 'smart' }]))
  it('saves a note once after typing pauses and reports the saved draft', async () => {
    vi.mocked(oxidnsDevicePolicyApi.get).mockResolvedValue(existing())
    const wrapper = await render()
    vi.useFakeTimers()
    const note = wrapper.get('input[aria-label="192.168.50.51 备注"]')
    for (const value of ['客', '客厅', '客厅电视']) { await note.setValue(value); vi.advanceTimersByTime(300) }
    expect(oxidnsDevicePolicyApi.save).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('正在保存草稿')
    vi.advanceTimersByTime(800)
    await flushPromises()
    expect(oxidnsDevicePolicyApi.save).toHaveBeenCalledTimes(1)
    expect(vi.mocked(oxidnsDevicePolicyApi.save).mock.lastCall?.[1].policies).toEqual([{ ip: '192.168.50.51', name: '客厅电视', mode: 'smart' }])
    expect(wrapper.text()).toContain('草稿已自动保存')
  })
  it('saves immediately when a note field loses focus', async () => {
    vi.mocked(oxidnsDevicePolicyApi.get).mockResolvedValue(existing())
    const wrapper = await render()
    const note = wrapper.get('input[aria-label="192.168.50.51 备注"]')
    await note.setValue('电视')
    await note.trigger('blur')
    await flushPromises()
    expect(vi.mocked(oxidnsDevicePolicyApi.save).mock.lastCall?.[1].policies[0].name).toBe('电视')
  })
  it('keeps edits made while a save is in flight and saves them with the new revision', async () => {
    vi.mocked(oxidnsDevicePolicyApi.get).mockResolvedValue(existing())
    let release!: () => void
    vi.mocked(oxidnsDevicePolicyApi.save)
      .mockImplementationOnce((_id, data) => new Promise(done => { release = () => done(response({ ...state(data.policies), revision: 'v2' })) }))
      .mockImplementationOnce(async (_id, data) => response({ ...state(data.policies), revision: 'v3' }))
    const wrapper = await render()
    const note = wrapper.get('input[aria-label="192.168.50.51 备注"]')
    await note.setValue('电视'); await note.trigger('blur')
    await note.setValue('电视机')
    release(); await flushPromises()
    expect(note.element.value).toBe('电视机')
    expect(oxidnsDevicePolicyApi.save).toHaveBeenCalledTimes(2)
    expect(vi.mocked(oxidnsDevicePolicyApi.save).mock.calls[1][1]).toMatchObject({ revision: 'v2', policies: [{ ip: '192.168.50.51', name: '电视机', mode: 'smart' }] })
    await button(wrapper, '发布到当前设备').trigger('click'); await flushPromises()
    expect(agentApi.pushConfig).toHaveBeenCalledWith('one', expect.any(String), { expected_profile_id: 'office', expected_oxidns_revision: 'v3' })
  })
  it('keeps a trailing space while typing although the server trims notes', async () => {
    vi.mocked(oxidnsDevicePolicyApi.get).mockResolvedValue(existing())
    vi.mocked(oxidnsDevicePolicyApi.save).mockImplementation(async (_id, data) =>
      response({ ...state(data.policies.map(row => ({ ...row, name: row.name.trim() }))), revision: 'v2' }))
    const wrapper = await render()
    vi.useFakeTimers()
    const note = wrapper.get('input[aria-label="192.168.50.51 备注"]')
    await note.setValue('客厅 ')
    vi.advanceTimersByTime(800); await flushPromises()
    expect(note.element.value).toBe('客厅 ')
    expect(wrapper.text()).toContain('草稿已自动保存')
    await note.setValue('客厅 电视')
    vi.advanceTimersByTime(800); await flushPromises()
    expect(vi.mocked(oxidnsDevicePolicyApi.save).mock.lastCall?.[1].policies[0].name).toBe('客厅 电视')
  })
  it('keeps the form editable during a save and lets the user leave once it finishes', async () => {
    vi.mocked(oxidnsDevicePolicyApi.get).mockResolvedValue(existing())
    let release!: () => void
    vi.mocked(oxidnsDevicePolicyApi.save).mockImplementationOnce((_id, data) =>
      new Promise(done => { release = () => done(response({ ...state(data.policies), revision: 'v2' })) }))
    const wrapper = await render()
    const note = wrapper.get('input[aria-label="192.168.50.51 备注"]')
    await note.setValue('电视'); await note.trigger('blur')
    expect(wrapper.get('fieldset').attributes('disabled')).toBeUndefined()
    expect(button(wrapper, '重新载入').attributes('disabled')).toBeUndefined()
    const leaving = (wrapper.vm as any).canLeave()
    release()
    await expect(leaving).resolves.toBe(true)
    expect(notify.info).not.toHaveBeenCalled()
    expect(confirm).not.toHaveBeenCalled()
  })
  it('retries a failed save on request and does not publish unsaved edits', async () => {
    vi.mocked(oxidnsDevicePolicyApi.save).mockRejectedValueOnce(new Error('网络中断'))
    const wrapper = await render()
    await add(wrapper); await flushPromises()
    expect(wrapper.text()).toContain('保存失败')
    expect(button(wrapper, '发布到当前设备').attributes('disabled')).toBeDefined()
    await button(wrapper, '重试').trigger('click'); await flushPromises()
    expect(oxidnsDevicePolicyApi.save).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('草稿已自动保存')
    expect(button(wrapper, '发布到当前设备').attributes('disabled')).toBeUndefined()
    expect(agentApi.pushConfig).not.toHaveBeenCalled()
  })
})

describe('OxiDNS device-policy publication diagnostics', () => {
  it('preserves late server diagnostics beside a deployment already confirmed by polling', async () => {
    vi.useFakeTimers()
    let rejectPublish!: (error: any) => void
    vi.mocked(agentApi.pushConfig).mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectPublish = reject }))
    vi.mocked(agentApi.getDeployment).mockImplementationOnce(async (_id, deploymentId) => response({ deployment_id: deploymentId, status: 'succeeded' }))
    const wrapper = await render()
    await button(wrapper, '发布到当前设备').trigger('click')
    await flushPromises()
    await vi.advanceTimersByTimeAsync(0)
    rejectPublish({ response: { status: 503, data: { message: '返回响应时连接中断', error_details: 'connection closed after service check' } } })
    await flushPromises()
    const row = wrapper.get('[data-agent-status-row="配置推送"]')
    expect(row.get('button[aria-expanded]').attributes('aria-label')).toBe('配置推送：成功')
    expect(row.get('[data-status-notice]').isVisible()).toBe(true)
    expect(row.get('[data-status-notice] pre').text()).toBe('connection closed after service check')
    expect(row.get('[data-status-details]').isVisible()).toBe(false)
  })

  it('opens successful steps directly while DNS warnings remain visible and independently collapsed', async () => {
    vi.mocked(agentApi.pushConfig).mockImplementationOnce(async (_id, deploymentId) => response({
      deployment_id: deploymentId, status: 'succeeded', message: '设备策略已生效',
      events: [{ at: '2026-10-19T01:00:00Z', status: 'verifying', message: '校验设备策略配置' }],
      dns_checks: [{ name: 'DNS 上游', level: 'warn', message: '上游解析有超时', fix: '检查上游连接' }],
    }))
    const wrapper = await render()
    await button(wrapper, '发布到当前设备').trigger('click')
    await flushPromises()
    const row = wrapper.get('[data-agent-status-row="配置推送"]')
    expect(row.get('[data-status-details]').isVisible()).toBe(false)
    expect(row.find('[data-status-notice]').exists()).toBe(false)
    const dns = wrapper.get('[data-agent-status-row="DNS 检查"]')
    expect(dns.element.closest('[data-status-details]')).toBeNull()
    expect(dns.get('[data-status-notice]').isVisible()).toBe(true)
    expect(dns.get('[data-status-notice]').text()).toContain('上游解析有超时')
    expect(dns.get('[data-status-details]').isVisible()).toBe(false)
    await row.get('button[aria-expanded]').trigger('click')
    expect(row.get('ol').isVisible()).toBe(true)
    expect(row.get('ol').text()).toContain('校验设备策略配置')
    expect(row.find('details, summary').exists()).toBe(false)
    expect(dns.get('[data-status-details]').isVisible()).toBe(false)
  })

  it('leaves a ready snapshot actionable while its recorded steps are collapsed', async () => {
    vi.mocked(agentApi.pushConfig).mockImplementationOnce(async (_id, deploymentId) => response({ deployment_id: deploymentId, status: 'ready', message: '设备策略配置已校验' }))
    vi.mocked(agentApi.activateDeployment).mockImplementationOnce(async (_id, deploymentId) => response({ deployment_id: deploymentId, status: 'succeeded' }))
    const wrapper = await render()
    await button(wrapper, '发布到当前设备').trigger('click')
    await flushPromises()
    const row = wrapper.get('[data-agent-status-row="配置推送"]')
    expect(row.get('[data-status-details]').isVisible()).toBe(false)
    expect(row.get('[data-status-notice]').isVisible()).toBe(true)
    expect(row.get('[data-status-notice]').text()).toContain('设备策略配置已校验')
    expect(button(wrapper, '应用并启动').isVisible()).toBe(true)
    await button(wrapper, '应用并启动').trigger('click')
    await flushPromises()
    expect(agentApi.activateDeployment).toHaveBeenCalledWith('one', vi.mocked(agentApi.pushConfig).mock.lastCall![1])
  })

  it('shows target validation and rollback details directly in the device policy panel', async () => {
    vi.mocked(agentApi.pushConfig).mockImplementationOnce(async (_id, deploymentId) => response({ deployment_id: deploymentId, status: 'rollback_failed', failed_stage: 'verifying', error: '配置校验失败', error_details: 'plugin resolver: unknown option', rollback_error: '启动失败', rollback_error_details: 'cannot bind :53' }))
    const wrapper = await render()
    await button(wrapper, '发布到当前设备').trigger('click')
    await flushPromises()
    expect(wrapper.findAll('pre').map(item => item.text())).toEqual(['plugin resolver: unknown option', 'cannot bind :53'])
    expect(wrapper.text()).toContain('失败步骤：校验中')
    const row = wrapper.get('[data-agent-status-row="配置推送"]')
    expect(row.get('[data-status-notice]').isVisible()).toBe(true)
    expect(row.get('[data-status-notice]').text()).toContain('配置校验失败')
    expect(row.get('[data-status-details]').isVisible()).toBe(false)
  })
  it('keeps rejection details even when no deployment was created', async () => {
    vi.mocked(agentApi.pushConfig).mockRejectedValueOnce({ response: { status: 400, data: { message: '生成失败', error_details: ruleLocationDetails } } })
    const wrapper = await render()
    await button(wrapper, '发布到当前设备').trigger('click')
    await flushPromises()
    expect(wrapper.get('pre').element.textContent).toBe(ruleLocationDetails)
    expect(wrapper.text()).toContain('生成失败')
    const row = wrapper.get('[data-agent-status-row="配置推送"]')
    expect(row.attributes('data-tone')).toBe('danger')
    expect(row.get('[data-status-notice]').isVisible()).toBe(true)
    expect(row.find('[aria-expanded], [data-agent-operation-steps]').exists()).toBe(false)
  })
})
