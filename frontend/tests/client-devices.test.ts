import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { agentApi, clientDeviceApi, oxidnsMonitorApi, type ClientDevice, type ClientDevicesState } from '@/api'
import { confirm, confirmDanger, notify } from '@/lib/feedback'
import ClientDevices from '@/views/ClientDevices.vue'
import OxiDNSMonitor from '@/views/OxiDNSMonitor.vue'
import { Select } from '@/components/ui/select'

vi.mock('@/api', () => ({
  clientDeviceApi: { get: vi.fn(), save: vi.fn() },
  oxidnsMonitorApi: { overview: vi.fn(), clients: vi.fn(), queries: vi.fn() },
  agentApi: { getAll: vi.fn(), pushConfig: vi.fn(), getDeployment: vi.fn(), activateDeployment: vi.fn() },
  profileApi: { list: vi.fn() }
}))
vi.mock('@/lib/feedback', () => ({ confirm: vi.fn(), confirmDanger: vi.fn(), notify: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() } }))

const response = (data: any) => ({ data } as any)
const agents = [{ id: 'one', name: 'DNS 1', host: '192.0.2.2', status: 'online', deployment: null },
  { id: 'two', name: 'DNS 2', host: '192.0.2.3', status: 'online', deployment: null },
  { id: 'off', name: 'DNS 3', host: '192.0.2.4', status: 'offline', deployment: null }]
const state = (devices: ClientDevice[] = [{ ip: '192.0.2.5', name: '电视', mode: 'direct' }, { ip: '192.0.2.9', name: '手机', mode: '' }],
  extra: Partial<ClientDevicesState> = {}): ClientDevicesState =>
  ({ success: true, profile_id: 'office', profile_name: '办公室', oxidns_revision: 'dns-1', devices, editable: true, reason: '', agents, ...extra })
const wrappers: VueWrapper[] = []
let stored: ClientDevice[]

async function render(path = '/client-devices') {
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/client-devices', component: ClientDevices },
    { path: '/agents/:agentId/oxidns', component: OxiDNSMonitor },
    { path: '/other', component: { template: '<div>other</div>' } }
  ] })
  await router.push(path)
  const wrapper = mount({ template: '<RouterView />' }, { global: { plugins: [router] }, attachTo: document.body })
  wrappers.push(wrapper)
  await flushPromises()
  return { wrapper, router }
}
const nameInput = (wrapper: VueWrapper, ip: string) => wrapper.get<HTMLInputElement>(`input[aria-label="${ip} 名称"]`)
const button = (wrapper: VueWrapper, text: string) => wrapper.findAll('button').find(item => item.text().includes(text))!

beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} })))
  Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, writable: true, value: vi.fn() })
  stored = state().devices
  vi.mocked(clientDeviceApi.get).mockImplementation(async () => response(state(stored)))
  // Like the server: one row changes, and a row without name and policy leaves the list.
  vi.mocked(clientDeviceApi.save).mockImplementation(async (_profile, device) => {
    const others = stored.filter(row => row.ip !== device.ip)
    const index = stored.findIndex(row => row.ip === device.ip)
    stored = device.name || device.mode ? (index < 0 ? [...stored, { ...device }] : stored.map(row => row.ip === device.ip ? { ...device } : row)) : others
    return response(state(stored, { oxidns_revision: 'dns-2' }))
  })
  vi.mocked(oxidnsMonitorApi.overview).mockResolvedValue(response({ recorders: ['queries'] }))
  vi.mocked(agentApi.pushConfig).mockImplementation(async (_id, deploymentId) => response({ deployment_id: deploymentId, status: 'succeeded' }))
  vi.mocked(confirm).mockResolvedValue(true)
  vi.mocked(confirmDanger).mockResolvedValue(true)
})
afterEach(async () => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  await flushPromises()
  document.body.innerHTML = ''
  vi.unstubAllGlobals()
})

describe('client devices page', () => {
  it('lists named and policy devices of the active profile and targets online OxiDNS by default', async () => {
    const { wrapper } = await render()
    expect(clientDeviceApi.get).toHaveBeenCalledWith({ agentId: undefined })
    expect(nameInput(wrapper, '192.0.2.5').element.value).toBe('电视')
    expect(nameInput(wrapper, '192.0.2.9').element.value).toBe('手机')
    const boxes = wrapper.findAll('[role="checkbox"]')
    expect(boxes.map(box => box.attributes('aria-checked'))).toEqual(['true', 'true', 'false'])
    expect(button(wrapper, '发布到 2 台 OxiDNS').exists()).toBe(true)
    expect(oxidnsMonitorApi.overview).toHaveBeenCalledWith('one')
  })

  it('saves a rename on its own with the values the editor showed', async () => {
    const { wrapper } = await render()
    await nameInput(wrapper, '192.0.2.5').setValue('客厅电视 ')
    await nameInput(wrapper, '192.0.2.5').trigger('blur')
    await flushPromises()
    expect(clientDeviceApi.save).toHaveBeenCalledTimes(1)
    expect(clientDeviceApi.save).toHaveBeenCalledWith('office', { ip: '192.0.2.5', name: '客厅电视', mode: 'direct' }, { name: '电视', mode: 'direct' })
    expect(wrapper.text()).toContain('已保存')
    await nameInput(wrapper, '192.0.2.5').trigger('blur')
    await flushPromises()
    expect(clientDeviceApi.save).toHaveBeenCalledTimes(1)
  })

  it('maps the default DNS policy to no policy and saves policy changes immediately', async () => {
    const { wrapper } = await render()
    wrapper.findAllComponents(Select)[1].vm.$emit('update:modelValue', 'smart')
    await flushPromises()
    expect(clientDeviceApi.save).toHaveBeenLastCalledWith('office', { ip: '192.0.2.9', name: '手机', mode: 'smart' }, { name: '手机', mode: '' })
    wrapper.findAllComponents(Select)[0].vm.$emit('update:modelValue', 'default')
    await flushPromises()
    expect(clientDeviceApi.save).toHaveBeenLastCalledWith('office', { ip: '192.0.2.5', name: '电视', mode: '' }, { name: '电视', mode: 'direct' })
  })

  it('keeps a manually added address in place until it is named, then saves it', async () => {
    const { wrapper } = await render()
    await wrapper.get('#client-device-ip').setValue('192.0.2.77/24')
    await button(wrapper, '添加').trigger('click')
    await flushPromises()
    expect(wrapper.findAll('tbody tr')[0].text()).toContain('192.0.2.0/24')
    expect(wrapper.text()).toContain('未保存')
    await nameInput(wrapper, '192.0.2.0/24').trigger('blur')
    await flushPromises()
    expect(clientDeviceApi.save).not.toHaveBeenCalled()
    await nameInput(wrapper, '192.0.2.0/24').setValue('访客网段')
    await nameInput(wrapper, '192.0.2.0/24').trigger('keydown', { key: 'Enter' })
    await flushPromises()
    expect(clientDeviceApi.save).toHaveBeenCalledWith('office', { ip: '192.0.2.0/24', name: '访客网段', mode: '' }, { name: '', mode: '' })
    // The saved row stays where the user added it.
    expect(wrapper.findAll('tbody tr')[0].text()).toContain('192.0.2.0/24')
    expect(wrapper.text()).not.toContain('未保存')
  })

  it('removes a device after confirmation by clearing its name and policy', async () => {
    const { wrapper } = await render()
    await wrapper.get('[aria-label="移除 192.0.2.5"]').trigger('click')
    await flushPromises()
    expect(confirmDanger).toHaveBeenCalled()
    expect(clientDeviceApi.save).toHaveBeenCalledWith('office', { ip: '192.0.2.5', name: '', mode: '' }, { name: '电视', mode: 'direct' })
    expect(wrapper.find('input[aria-label="192.0.2.5 名称"]').exists()).toBe(false)
  })

  it('reloads instead of overwriting a device changed elsewhere', async () => {
    const { wrapper } = await render()
    vi.mocked(clientDeviceApi.save).mockRejectedValueOnce({ response: { status: 409, data: { message: '此设备已在其他页面修改，请重新载入后再保存。' } } })
    stored = [{ ip: '192.0.2.5', name: '电视机', mode: 'direct' }]
    await nameInput(wrapper, '192.0.2.5').setValue('客厅')
    await nameInput(wrapper, '192.0.2.5').trigger('blur')
    await flushPromises()
    expect(notify.error).toHaveBeenCalledWith('设备已在其他页面修改', '此设备已在其他页面修改，请重新载入后再保存。')
    expect(nameInput(wrapper, '192.0.2.5').element.value).toBe('电视机')
  })

  it('keeps other failures on the row with a retry', async () => {
    const { wrapper } = await render()
    vi.mocked(clientDeviceApi.save).mockRejectedValueOnce({ response: { status: 400, data: { message: '设备备注最多 80 个字符' } } })
    await nameInput(wrapper, '192.0.2.5').setValue('新名')
    await nameInput(wrapper, '192.0.2.5').trigger('blur')
    await flushPromises()
    expect(wrapper.text()).toContain('设备备注最多 80 个字符')
    await button(wrapper, '重试').trigger('click')
    await flushPromises()
    expect(stored[0].name).toBe('新名')
  })

  it('opens the bound profile from an OxiDNS Agent and targets only that device', async () => {
    const { wrapper, router } = await render('/client-devices?agent=two')
    expect(clientDeviceApi.get).toHaveBeenCalledWith({ agentId: 'two' })
    expect(wrapper.text()).toContain('配置：办公室')
    expect(wrapper.findAll('[role="checkbox"]').map(box => box.attributes('aria-checked'))).toEqual(['false', 'true', 'false'])
    expect(oxidnsMonitorApi.overview).toHaveBeenCalledWith('two')
    await button(wrapper, '返回 OxiDNS 面板').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/agents/two/oxidns')
  })

  it('publishes the saved draft to each selected OxiDNS with the profile guard', async () => {
    const { wrapper } = await render()
    await button(wrapper, '发布到 2 台 OxiDNS').trigger('click')
    await flushPromises()
    expect(agentApi.pushConfig).toHaveBeenCalledTimes(2)
    expect(agentApi.pushConfig).toHaveBeenNthCalledWith(1, 'one', expect.any(String), { expected_profile_id: 'office', expected_oxidns_revision: 'dns-1' })
    expect(agentApi.pushConfig).toHaveBeenNthCalledWith(2, 'two', expect.any(String), { expected_profile_id: 'office', expected_oxidns_revision: 'dns-1' })
  })

  it('stops publishing when the draft changed after loading', async () => {
    const { wrapper } = await render()
    vi.mocked(agentApi.pushConfig).mockRejectedValueOnce({ response: { status: 409, data: { message: '配置或设备绑定已改变，请重新载入设备策略后发布。' } } })
    await button(wrapper, '发布到 2 台 OxiDNS').trigger('click')
    await flushPromises()
    expect(agentApi.pushConfig).toHaveBeenCalledTimes(1)
    expect(notify.error).toHaveBeenCalledWith('发布已停止', '配置或设备绑定已改变，请重新载入设备策略后发布。')
    expect(wrapper.text()).toContain('配置或设备绑定已改变')
  })

  it('publishes with the revision returned by the latest save', async () => {
    const { wrapper } = await render()
    wrapper.findAllComponents(Select)[1].vm.$emit('update:modelValue', 'direct')
    await button(wrapper, '发布到 2 台 OxiDNS').trigger('click')
    await flushPromises()
    expect(agentApi.pushConfig).toHaveBeenCalledWith('one', expect.any(String), { expected_profile_id: 'office', expected_oxidns_revision: 'dns-2' })
  })

  it('makes DNS policies read-only when the native configuration was edited by hand', async () => {
    vi.mocked(clientDeviceApi.get).mockResolvedValue(response(state(undefined, { editable: false, reason: '处理链已手动修改。' })))
    const { wrapper } = await render()
    expect(wrapper.text()).toContain('处理链已手动修改。 名称仍可修改。')
    expect(wrapper.get('[aria-label="192.0.2.5 DNS 策略"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[aria-label="移除 192.0.2.5"]').attributes('disabled')).toBeDefined()
    expect(nameInput(wrapper, '192.0.2.5').attributes('disabled')).toBeUndefined()
  })
})

it('sends old OxiDNS device policy links to the client devices page of that Agent', async () => {
  vi.mocked(agentApi.getAll).mockResolvedValue(response([{ id: 'one', name: 'DNS 1', service_type: 'oxidns' }]))
  vi.mocked(oxidnsMonitorApi.overview).mockRejectedValue(new Error('核心离线'))
  const { router } = await render('/agents/one/oxidns?tab=devices')
  expect(router.currentRoute.value.fullPath).toBe('/client-devices?agent=one')
})
