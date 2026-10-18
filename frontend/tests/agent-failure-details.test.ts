import { afterEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import AgentFailureDetails from '@/components/agents/AgentFailureDetails.vue'
import { deploymentErrorSummary } from '@/lib/agentFailure'
import { notify } from '@/lib/feedback'

vi.mock('@/lib/feedback', () => ({ notify: { success: vi.fn(), error: vi.fn() } }))
const wrappers: ReturnType<typeof mount>[] = []
const clipboardDescriptor = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
const execDescriptor = Object.getOwnPropertyDescriptor(document, 'execCommand')
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  document.body.innerHTML = ''
  if (clipboardDescriptor) Object.defineProperty(navigator, 'clipboard', clipboardDescriptor)
  else Reflect.deleteProperty(navigator, 'clipboard')
  if (execDescriptor) Object.defineProperty(document, 'execCommand', execDescriptor)
  else Reflect.deleteProperty(document, 'execCommand')
  vi.clearAllMocks()
})
function render(task: Record<string, unknown>) {
  const wrapper = mount(AgentFailureDetails, { props: { task }, attachTo: document.body })
  wrappers.push(wrapper)
  return wrapper
}

describe('Agent failure diagnostics', () => {
  it.each(['mihomo', 'mosdns', 'oxidns', 'surge'])('keeps full %s errors separately from rollback errors and collapsed by default', async service_type => {
    const primary = 'line 18: unknown plugin\n' + 'unbroken'.repeat(1500)
    const rollback = 'service start failed\nexit status 1'
    const wrapper = render({ deployment_id: 'one', service_type, status: 'rollback_failed', failed_stage: 'verifying', error: '配置检查失败', error_details: primary, rollback_error: '无法恢复服务', rollback_error_details: rollback })
    expect(wrapper.text()).toContain('失败步骤：校验中')
    expect(wrapper.findAll('details')).toHaveLength(2)
    expect(wrapper.findAll('details').every(item => !(item.element as HTMLDetailsElement).open)).toBe(true)
    expect(wrapper.findAll('pre').map(item => item.text())).toEqual([primary, rollback])
    expect(wrapper.findAll('pre').every(item => item.classes().includes('[overflow-wrap:anywhere]'))).toBe(true)
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: vi.fn().mockResolvedValue(undefined) } })
    await wrapper.get('[aria-label="复制回滚失败详情"]').trigger('click')
    await flushPromises()
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(rollback)
    expect(notify.success).toHaveBeenCalledWith('失败详情已复制')
  })
  it('shows the message from an old Agent without an empty details disclosure', () => {
    const wrapper = render({ status: 'failed', message: '配置检查失败' })
    expect(wrapper.text()).toBe('配置检查失败')
    expect(wrapper.find('details').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('失败步骤')
  })
  it('shows the Chinese summary while retaining original errors and codes in details', () => {
    const original = 'service start failed: exit status 7\nCORE_START_FAILED'
    const wrapper = render({ status: 'failed', error: '服务启动失败，请查看设备上的核心日志。\n原始错误：service start failed', error_details: original })
    expect(wrapper.get('p').text()).toBe('服务启动失败，请查看设备上的核心日志。')
    expect(wrapper.get('pre').text()).toBe(original)
    expect((wrapper.get('details').element as HTMLDetailsElement).open).toBe(false)
  })
  it('preserves old multiline errors in details and keeps toast summaries short', () => {
    const error = `exit status 1\n${'long'.repeat(300)}`
    const wrapper = render({ status: 'failed', error })
    expect(wrapper.get('pre').text()).toBe(error)
    expect(deploymentErrorSummary({ error })).toBe('exit status 1')
    expect(deploymentErrorSummary({ error: 'x'.repeat(300) })).toHaveLength(241)
  })
  it('copies on LAN HTTP without the secure Clipboard API', async () => {
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined })
    const exec = vi.fn(() => true)
    Object.defineProperty(document, 'execCommand', { configurable: true, value: exec })
    const wrapper = render({ error_details: 'line 17: missing proxy group' })
    await wrapper.get('[aria-label="复制发布失败详情"]').trigger('click')
    expect(exec).toHaveBeenCalledWith('copy')
    expect(document.querySelector('textarea')).toBeNull()
    expect(notify.success).toHaveBeenCalledWith('失败详情已复制')
  })
  it('reports copy failures without discarding selectable error text', async () => {
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: vi.fn().mockRejectedValue(new Error('denied')) } })
    const wrapper = render({ error_details: 'line 17: missing proxy group' })
    await wrapper.get('[aria-label="复制发布失败详情"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('pre').text()).toContain('missing proxy group')
    expect(notify.error).toHaveBeenCalledWith('复制失败，请手动选择下方内容复制')
  })
  it('falls back to selection copy when clipboard permission is denied', async () => {
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: vi.fn().mockRejectedValue(new Error('denied')) } })
    const exec = vi.fn(() => true)
    Object.defineProperty(document, 'execCommand', { configurable: true, value: exec })
    const wrapper = render({ error_details: 'line 17: missing proxy group' })
    await wrapper.get('[aria-label="复制发布失败详情"]').trigger('click')
    await flushPromises()
    expect(exec).toHaveBeenCalledWith('copy')
    expect(document.querySelector('textarea')).toBeNull()
    expect(notify.success).toHaveBeenCalledWith('失败详情已复制')
    expect(notify.error).not.toHaveBeenCalled()
  })
  it('resets expanded state when another failure replaces the current task', async () => {
    const wrapper = render({ deployment_id: 'old', error: 'failed', error_details: 'old details' })
    ;(wrapper.get('details').element as HTMLDetailsElement).open = true
    await wrapper.setProps({ task: { deployment_id: 'new', error: 'failed', error_details: 'new details' } })
    expect((wrapper.get('details').element as HTMLDetailsElement).open).toBe(false)
    expect(wrapper.get('pre').text()).toBe('new details')
  })
})
