import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import WebPortSettings from '@/components/settings/WebPortSettings.vue'
import { webPortApi, type WebPortStatus } from '@/api'
import { confirm, notify } from '@/lib/feedback'

vi.mock('@/api', () => ({ webPortApi: { get: vi.fn(), update: vi.fn() } }))
vi.mock('@/lib/feedback', () => ({ confirm: vi.fn(), notify: { success: vi.fn() } }))

const response = (data: unknown) => ({ data }) as any
const current = (extra: Partial<WebPortStatus> = {}): WebPortStatus => ({
  supported: true, editable: true, port: 80, ipv6: true, source: 'default', ...extra,
})
const wrappers: ReturnType<typeof mount>[] = []
const input = (wrapper: ReturnType<typeof mount>) => wrapper.get('#web-listen-port')
const applyButton = (wrapper: ReturnType<typeof mount>) => wrapper.findAll('button').find(button => ['应用', '应用中…'].includes(button.text()))!
async function render() {
  const wrapper = mount(WebPortSettings, { attachTo: document.body })
  wrappers.push(wrapper)
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  vi.mocked(webPortApi.get).mockResolvedValue(response(current()))
  vi.mocked(webPortApi.update).mockImplementation(async ({ port }) => response({ success: true, ...current({ port, source: 'saved' }) }))
  vi.mocked(confirm).mockResolvedValue(true)
})
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  vi.unstubAllGlobals()
})

describe('Web listen port settings', () => {
  it('shows the actual listener and only applies an explicit confirmed change', async () => {
    const wrapper = await render()
    expect((input(wrapper).element as HTMLInputElement).value).toBe('80')
    expect(wrapper.text()).toContain('IPv4 / IPv6')
    expect(applyButton(wrapper).attributes('disabled')).toBeDefined()
    await input(wrapper).setValue('8088')
    await input(wrapper).trigger('blur')
    expect(webPortApi.update).not.toHaveBeenCalled()
    await applyButton(wrapper).trigger('click')
    await flushPromises()
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining('bridge 需同步端口映射'), expect.objectContaining({ confirmText: '应用新端口' }))
    expect(webPortApi.update).toHaveBeenCalledWith({ port: 8088 })
    expect(wrapper.text()).toContain('监听端口已改为 8088')
    expect(wrapper.text()).toContain('服务域名和 Agent 的服务地址需另行更新')
    expect(notify.success).toHaveBeenCalledOnce()
  })

  it.each(['0', '-1', '65536', '1.5', ''])('does not apply invalid port %j', async value => {
    const wrapper = await render()
    await input(wrapper).setValue(value)
    await input(wrapper).trigger('keydown', { key: 'Enter' })
    expect(wrapper.text()).toContain('请输入 1–65535 之间的整数端口')
    expect(input(wrapper).attributes('aria-invalid')).toBe('true')
    expect(applyButton(wrapper).attributes('disabled')).toBeDefined()
    expect(confirm).not.toHaveBeenCalled()
    expect(webPortApi.update).not.toHaveBeenCalled()
  })

  it('keeps the draft when confirmation is cancelled', async () => {
    vi.mocked(confirm).mockResolvedValue(false)
    const wrapper = await render()
    await input(wrapper).setValue('8088')
    await applyButton(wrapper).trigger('click')
    await flushPromises()
    expect(webPortApi.update).not.toHaveBeenCalled()
    expect((input(wrapper).element as HTMLInputElement).value).toBe('8088')
    expect(applyButton(wrapper).attributes('disabled')).toBeUndefined()
  })

  it('disables changes while confirming or applying', async () => {
    let accept!: (value: boolean) => void
    vi.mocked(confirm).mockReturnValue(new Promise(resolve => { accept = resolve }))
    const wrapper = await render()
    await input(wrapper).setValue('8088')
    await applyButton(wrapper).trigger('click')
    expect(input(wrapper).attributes('disabled')).toBeDefined()
    expect(applyButton(wrapper).attributes('disabled')).toBeDefined()
    expect(webPortApi.update).not.toHaveBeenCalled()
    accept(true)
    await flushPromises()
    expect(webPortApi.update).toHaveBeenCalledOnce()
  })

  it('retains the current listener and draft when the server rejects an occupied port', async () => {
    vi.mocked(webPortApi.update).mockRejectedValue({ isAxiosError: true, response: { status: 409, data: { message: '端口 8088 已被占用' } } })
    const wrapper = await render()
    await input(wrapper).setValue('8088')
    await applyButton(wrapper).trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('端口 8088 已被占用')
    expect(wrapper.get('#web-port-status').text()).toContain('80')
    expect((input(wrapper).element as HTMLInputElement).value).toBe('8088')
    expect(wrapper.find('a').exists()).toBe(false)
    expect(notify.success).not.toHaveBeenCalled()
  })

  it('does not claim failure or success when the connection drops during a change', async () => {
    vi.mocked(webPortApi.update).mockRejectedValue({ isAxiosError: true, code: 'ERR_NETWORK' })
    const wrapper = await render()
    await input(wrapper).setValue('8088')
    await applyButton(wrapper).trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('应用结果尚未确认')
    expect(wrapper.text()).toContain('端口可能已切换')
    expect(wrapper.get('a').attributes('href')).toContain(':8088/system-settings')
    expect(notify.success).not.toHaveBeenCalled()
  })

  it.each([
    ['http://192.168.1.10:80/system-settings?tab=general#old', 'http://192.168.1.10:8088/system-settings'],
    ['http://[2001:db8::10]/system-settings', 'http://[2001:db8::10]:8088/system-settings'],
    ['https://config.example/system-settings', 'http://config.example:8088/system-settings'],
    ['http://192.168.1.10:5010/system-settings', 'http://192.168.1.10:8088/system-settings'],
  ])('offers a reference link without navigating away from %s', async (href, expected) => {
    const assign = vi.fn()
    const replace = vi.fn()
    vi.stubGlobal('location', { href, assign, replace })
    const wrapper = await render()
    await input(wrapper).setValue('8088')
    await applyButton(wrapper).trigger('click')
    await flushPromises()
    expect(wrapper.get('a').attributes('href')).toBe(expected)
    expect(wrapper.text()).toContain('尚未验证外部可达性')
    expect(window.location.href).toBe(href)
    expect(assign).not.toHaveBeenCalled()
    expect(replace).not.toHaveBeenCalled()
  })

  it('makes an environment override read-only and explains how to change it', async () => {
    vi.mocked(webPortApi.get).mockResolvedValue(response(current({ port: 9000, editable: false, source: 'environment', message: 'CONFIGFLOW_WEB_PORT 已指定端口，请修改环境变量并重建容器。' })))
    const wrapper = await render()
    expect(input(wrapper).attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('CONFIGFLOW_WEB_PORT')
    expect(applyButton(wrapper)).toBeUndefined()
  })

  it('does not offer a port editor on an unsupported deployment', async () => {
    vi.mocked(webPortApi.get).mockResolvedValue(response(current({ supported: false, editable: false, port: null, message: '当前部署不支持在 Web 中修改监听端口。' })))
    const wrapper = await render()
    expect(wrapper.find('#web-listen-port').exists()).toBe(false)
    expect(wrapper.text()).toContain('当前部署不支持')
    expect(webPortApi.update).not.toHaveBeenCalled()
  })

  it('can retry when the initial listener cannot be read', async () => {
    vi.mocked(webPortApi.get).mockRejectedValueOnce(new Error('offline'))
    const wrapper = await render()
    expect(wrapper.find('#web-listen-port').exists()).toBe(false)
    expect(wrapper.text()).toContain('读取监听端口失败')
    const retry = wrapper.findAll('button').find(button => button.text() === '重新读取')!
    await retry.trigger('click')
    await flushPromises()
    expect((input(wrapper).element as HTMLInputElement).value).toBe('80')
    expect(webPortApi.get).toHaveBeenCalledTimes(2)
  })
})
