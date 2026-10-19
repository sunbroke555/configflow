import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import NodeLatencySettings from '@/components/nodes/NodeLatencySettings.vue'
import { nodeApi } from '@/api'
import { notify } from '@/lib/feedback'

vi.mock('@/api', () => ({ nodeApi: { latencySettings: vi.fn(), saveLatencySettings: vi.fn() } }))
vi.mock('@/lib/feedback', () => ({ notify: { success: vi.fn() } }))
let wrapper: VueWrapper | undefined

function response(dns_servers: string[] = []) { return { data: { success: true, dns_servers } } as any }
function button(label: string) {
  const found = [...document.querySelectorAll('button')].find(item => item.textContent?.trim() === label)
  expect(found, label).toBeDefined()
  return found!
}
async function click(label: string) { button(label).click(); await flushPromises() }
function input() { return document.getElementById('node-latency-dns') as HTMLTextAreaElement }
async function enter(value: string) {
  input().value = value
  input().dispatchEvent(new Event('input', { bubbles: true }))
  await flushPromises()
}
async function submit() {
  document.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
  await flushPromises()
}
async function render(disabled = false) {
  wrapper = mount(NodeLatencySettings, { props: { disabled }, attachTo: document.body })
  await flushPromises()
  return wrapper
}
beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(nodeApi.latencySettings).mockResolvedValue(response())
  vi.mocked(nodeApi.saveLatencySettings).mockResolvedValue(response())
})
afterEach(() => {
  wrapper?.unmount()
  wrapper = undefined
  document.body.innerHTML = ''
})

describe('节点库独立测速 DNS 设置', () => {
  it('仅打开时读取持久设置，不预填公共 DNS', async () => {
    await render()
    expect(nodeApi.latencySettings).not.toHaveBeenCalled()
    await click('测速设置')
    expect(nodeApi.latencySettings).toHaveBeenCalledOnce()
    expect(input().value).toBe('')
    expect(document.body.textContent).toContain('留空使用系统 DNS')
    expect(document.body.textContent).toContain('仅影响节点库测速')
  })

  it('加载已保存的地址，将逗号和换行输入只提交为 DNS 地址列表', async () => {
    vi.mocked(nodeApi.latencySettings).mockResolvedValue(response(['192.0.2.1', '192.0.2.2:5353']))
    await render()
    await click('测速设置')
    expect(input().value).toBe('192.0.2.1\n192.0.2.2:5353')
    await enter(' 192.0.2.3:5353, 192.0.2.4 \n192.0.2.5\n')
    await submit()
    expect(nodeApi.saveLatencySettings).toHaveBeenCalledExactlyOnceWith(['192.0.2.3:5353', '192.0.2.4', '192.0.2.5'])
    expect(wrapper!.emitted('saved')).toHaveLength(1)
    expect(notify.success).toHaveBeenCalledWith('测速设置已保存')
    expect(document.querySelector('[role="dialog"]')).toBeNull()
  })

  it('取消不提交，重新打开从服务器读取而不保留未保存输入', async () => {
    vi.mocked(nodeApi.latencySettings).mockResolvedValue(response(['192.0.2.1']))
    await render()
    await click('测速设置')
    await enter('192.0.2.2')
    await click('取消')
    expect(nodeApi.saveLatencySettings).not.toHaveBeenCalled()
    await click('测速设置')
    expect(nodeApi.latencySettings).toHaveBeenCalledTimes(2)
    expect(input().value).toBe('192.0.2.1')
  })

  it('清空提交空列表以恢复系统 DNS', async () => {
    vi.mocked(nodeApi.latencySettings).mockResolvedValue(response(['192.0.2.1']))
    await render()
    await click('测速设置')
    await enter(' \n ')
    await submit()
    expect(nodeApi.saveLatencySettings).toHaveBeenCalledExactlyOnceWith([])
  })

  it('保存失败保留输入与弹窗，展示后端原因并允许重试', async () => {
    vi.mocked(nodeApi.saveLatencySettings).mockRejectedValueOnce({ response: { data: { message: 'DNS 地址无效，请填写 IPv4 或 IPv4:端口' } } })
    await render()
    await click('测速设置')
    await enter('invalid-dns')
    await submit()
    expect(input().value).toBe('invalid-dns')
    expect(document.querySelector('[role="alert"]')?.textContent).toContain('DNS 地址无效')
    expect(wrapper!.emitted('saved')).toBeUndefined()
    expect(notify.success).not.toHaveBeenCalled()
    await enter('192.0.2.1')
    await submit()
    expect(wrapper!.emitted('saved')).toHaveLength(1)
  })

  it('读取失败时不提供空表单覆盖设置，可以重新读取', async () => {
    vi.mocked(nodeApi.latencySettings).mockRejectedValueOnce(new Error('network'))
    await render()
    await click('测速设置')
    expect(input()).toBeNull()
    expect(document.querySelector('[role="alert"]')?.textContent).toContain('读取测速设置失败')
    vi.mocked(nodeApi.latencySettings).mockResolvedValueOnce(response(['192.0.2.1']))
    await click('重新读取')
    expect(input().value).toBe('192.0.2.1')
  })

  it('测速进行时禁用设置入口与已打开的保存操作', async () => {
    await render(true)
    expect(button('测速设置').disabled).toBe(true)
    await click('测速设置')
    expect(nodeApi.latencySettings).not.toHaveBeenCalled()
    await wrapper!.setProps({ disabled: false })
    await click('测速设置')
    await wrapper!.setProps({ disabled: true })
    expect(input().disabled).toBe(true)
    expect(button('保存').disabled).toBe(true)
    await submit()
    expect(nodeApi.saveLatencySettings).not.toHaveBeenCalled()
  })

  it('保存中禁止重复提交和关闭，完成后发出刷新事件', async () => {
    let finish!: (value: any) => void
    vi.mocked(nodeApi.saveLatencySettings).mockReturnValueOnce(new Promise(resolve => { finish = resolve }))
    await render()
    await click('测速设置')
    await enter('192.0.2.1')
    await submit()
    expect(button('正在保存…').disabled).toBe(true)
    expect(button('取消').disabled).toBe(true)
    expect(document.querySelector('[data-slot="dialog-close"]')).toBeNull()
    await submit()
    expect(nodeApi.saveLatencySettings).toHaveBeenCalledOnce()
    finish(response(['192.0.2.1']))
    await flushPromises()
    expect(wrapper!.emitted('saved')).toHaveLength(1)
  })

  it('不将 success:false 当作保存成功', async () => {
    vi.mocked(nodeApi.saveLatencySettings).mockResolvedValueOnce({ data: { success: false } } as any)
    await render()
    await click('测速设置')
    await enter('192.0.2.1')
    await submit()
    expect(input().value).toBe('192.0.2.1')
    expect(document.querySelector('[role="alert"]')?.textContent).toContain('保存测速设置失败')
    expect(wrapper!.emitted('saved')).toBeUndefined()
  })
})
