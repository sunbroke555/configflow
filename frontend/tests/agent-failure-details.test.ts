import { afterEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import AgentFailureDetails from '@/components/agents/AgentFailureDetails.vue'
import { deploymentErrorSummary } from '@/lib/agentFailure'
import { notify } from '@/lib/feedback'
import { ruleLocationDetails } from './fixtures/agent-failure'

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
  it('renders rule locations and untrusted content as selectable text and copies every line', async () => {
    const details = `${ruleLocationDetails}\n出错内容：<script>alert(1)</script><img src=x onerror=alert(1)>\n${'长规则'.repeat(3000)}`
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
    const wrapper = render({ status: 'failed', error: '规则集内容无效', error_details: details })
    expect(wrapper.get('pre').element.textContent).toBe(details)
    expect(wrapper.find('script, img').exists()).toBe(false)
    expect(wrapper.get('pre').classes()).toContain('whitespace-pre-wrap')
    expect(wrapper.get('pre').classes()).toContain('overflow-y-auto')
    await wrapper.get('[aria-label="复制发布失败详情"]').trigger('click')
    await flushPromises()
    expect(writeText).toHaveBeenCalledExactlyOnceWith(details)
  })
  it('does not invent a rule or line for errors that only identify a configuration field', () => {
    const details = '配置文件：config.yaml\n配置项：proxy-groups[2].proxies[0]\n出错内容：missing-group\n原因：找不到引用的策略组'
    const wrapper = render({ status: 'failed', error_details: details })
    expect(wrapper.get('pre').element.textContent).toBe(details)
    expect(wrapper.text()).not.toContain('行号')
    expect(wrapper.text()).not.toContain('规则集')
  })
  it.each(['error', 'message'])('keeps all location lines from an older Agent %s without a details field', key => {
    const wrapper = render({ status: 'failed', [key]: ruleLocationDetails })
    expect(wrapper.get('pre').element.textContent).toBe(ruleLocationDetails)
    expect(wrapper.get('p').text()).toBe('规则集：grok / 自定义')
  })
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
