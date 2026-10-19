import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import api from '@/api'
import { notify } from '@/lib/feedback'
import OxiDNSRuleUpdate from '@/components/agents/OxiDNSRuleUpdate.vue'
import type { OxiDNSRuleUpdateTask } from '@/api/oxidnsRuleUpdate'

vi.mock('@/api', () => ({ default: { get: vi.fn(), post: vi.fn() } }))
vi.mock('@/lib/feedback', () => ({ notify: { success: vi.fn(), error: vi.fn(), warning: vi.fn() } }))
const wrappers: VueWrapper[] = []
const task = (status: OxiDNSRuleUpdateTask['status'] = 'idle', extra = {}): OxiDNSRuleUpdateTask => ({
  id: status === 'idle' ? '' : 'update-one', status, phase: status === 'running' ? 'downloading' : status === 'idle' ? 'idle' : 'done',
  downloaded: 0, download_total: 2, reloaded: 0, reload_total: 1, ...extra,
})
const response = (value: OxiDNSRuleUpdateTask) => ({ data: { success: true, task: value }, status: 202 } as any)
const done = () => response(task('succeeded', { downloaded: 2, reloaded: 1 }))
function deferred() {
  let resolve!: (value: any) => void
  let reject!: (value: any) => void
  return { promise: new Promise<any>((yes, no) => { resolve = yes; reject = no }), resolve: (v: any) => resolve(v), reject: (v: any) => reject(v) }
}
async function render(props = {}) {
  const wrapper = mount(OxiDNSRuleUpdate, { attachTo: document.body, props: { agentId: 'one', ...props } })
  wrappers.push(wrapper)
  await flushPromises()
  return wrapper
}
beforeEach(() => {
  vi.useFakeTimers()
  vi.clearAllMocks()
  vi.mocked(api.get).mockResolvedValue(response(task()))
  vi.mocked(api.post).mockResolvedValue(response(task('running')))
})
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); document.body.innerHTML = ''; vi.useRealTimers() })

describe('OxiDNS rule update', () => {
  it('keeps card progress visible from submission through completion while its action lives elsewhere', async () => {
    const pending = deferred()
    vi.mocked(api.post).mockReturnValueOnce(pending.promise)
    const wrapper = await render({ presentation: 'card' })
    expect(wrapper.find('button').exists()).toBe(false)
    expect(wrapper.find('[data-agent-status-row]').exists()).toBe(false)
    const controller = wrapper.vm as unknown as { act: () => Promise<void>; busy: boolean }
    const submitting = controller.act()
    await flushPromises()
    expect(wrapper.get('[data-agent-status-row="规则更新"]').text()).toContain('正在提交')
    await controller.act()
    expect(api.post).toHaveBeenCalledTimes(1)
    pending.resolve(response(task('running')))
    await submitting
    expect(controller.busy).toBe(true)
    expect(wrapper.get('[data-status-notice]').text()).toContain('下载 0/2 · 热重载 0/1')
    vi.mocked(api.get).mockResolvedValueOnce(done())
    await vi.advanceTimersByTimeAsync(2000)
    expect(wrapper.get('[data-agent-status-row]').attributes('data-tone')).toBe('success')
    expect(wrapper.get('[data-status-details]').isVisible()).toBe(false)
    await wrapper.get('button[aria-label="规则更新：已完成"]').trigger('click')
    expect(wrapper.get('[data-status-details]').isVisible()).toBe(true)
    expect(wrapper.get('[data-status-details]').text()).toContain('下载 2/2 · 热重载 1/1')
    expect(wrapper.emitted('updated')).toHaveLength(1)
  })

  it('keeps uncertain card results and read-only recovery available beside the status', async () => {
    const wrapper = await render({ presentation: 'card' })
    await (wrapper.vm as unknown as { act: () => Promise<void> }).act()
    vi.mocked(api.get).mockResolvedValueOnce(response(task('failed', {
      code: 'RULE_UPDATE_UNKNOWN', downloaded: 2, message: '暂时无法确认规则更新结果。',
    })))
    await vi.advanceTimersByTimeAsync(2000)
    expect(wrapper.get('[data-agent-status-row]').attributes('data-tone')).toBe('warning')
    expect(wrapper.get('[role="alert"]').text()).toContain('暂时无法确认')
    expect(wrapper.findAll('button').some(button => button.text() === '重新尝试更新')).toBe(true)
    vi.mocked(api.get).mockResolvedValueOnce(done())
    await wrapper.findAll('button').find(button => button.text() === '查询更新结果')!.trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledTimes(1)
    expect(wrapper.get('[data-agent-status-row]').attributes('data-tone')).toBe('success')
  })

  it('shows a failed card update and its retry without requiring expanded details', async () => {
    vi.mocked(api.get).mockResolvedValueOnce(response(task('failed', {
      code: 'RELOAD_FAILED', downloaded: 2, message: '部分规则热重载失败。',
    })))
    const wrapper = await render({ presentation: 'card' })
    expect(wrapper.get('[data-agent-status-row]').attributes('data-tone')).toBe('danger')
    expect(wrapper.get('[data-status-notice]').text()).toContain('部分规则热重载失败')
    expect(wrapper.find('[data-status-details]').exists()).toBe(false)
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledTimes(1)
    expect(wrapper.get('[data-agent-status-row]').attributes('data-tone')).toBe('progress')
  })

  it('starts the encoded agent endpoint and waits for completed download and reload before reporting success', async () => {
    const wrapper = await render({ agentId: 'host/one' })
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith('/agents/host%2Fone/oxidns/rules/update', {}, expect.objectContaining({ signal: expect.any(AbortSignal) }))
    expect(wrapper.get('button').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('下载 0/2 · 热重载 0/1')
    expect(notify.success).not.toHaveBeenCalled()
    vi.mocked(api.get).mockResolvedValueOnce(response(task('running', { phase: 'reloading', downloaded: 2 })))
    await vi.advanceTimersByTimeAsync(2000)
    expect(wrapper.text()).toContain('正在热重载规则')
    expect(notify.success).not.toHaveBeenCalled()
    vi.mocked(api.get).mockResolvedValueOnce(done())
    await vi.advanceTimersByTimeAsync(2000)
    expect(notify.success).toHaveBeenCalledTimes(1)
    expect(wrapper.emitted('updated')).toHaveLength(1)
    expect(wrapper.text()).toContain('下载 2/2 · 热重载 1/1')
    await vi.advanceTimersByTimeAsync(10000)
    expect(api.get).toHaveBeenCalledTimes(3)
  })

  it('blocks repeated clicks while POST is pending and while the task runs', async () => {
    const pending = deferred()
    vi.mocked(api.post).mockReturnValueOnce(pending.promise)
    const wrapper = await render()
    await wrapper.get('button').trigger('click')
    await wrapper.get('button').trigger('click')
    expect(api.post).toHaveBeenCalledTimes(1)
    pending.resolve(response(task('running')))
    await flushPromises()
    await wrapper.get('button').trigger('click')
    expect(api.post).toHaveBeenCalledTimes(1)
  })

  it('shows partial failure counts and the actionable backend reason, then allows retry', async () => {
    const wrapper = await render()
    await wrapper.get('button').trigger('click')
    await flushPromises()
    vi.mocked(api.get).mockResolvedValueOnce(response(task('failed', { downloaded: 1, message: '规则下载失败，旧文件已保留，请检查下载地址。' })))
    await vi.advanceTimersByTimeAsync(2000)
    expect(wrapper.get('[role="alert"]').text()).toContain('下载 1/2 · 热重载 0/1')
    expect(wrapper.text()).toContain('旧文件已保留')
    expect(notify.success).not.toHaveBeenCalled()
    expect(wrapper.emitted('updated')).toBeUndefined()
    await wrapper.get('button').trigger('click')
    expect(api.post).toHaveBeenCalledTimes(2)
  })

  it('presents an unknown terminal result as a warning with query and safe retry actions', async () => {
    const wrapper = await render()
    await wrapper.get('button').trigger('click')
    await flushPromises()
    vi.mocked(api.get).mockResolvedValueOnce(response(task('failed', {
      code: 'RULE_UPDATE_UNKNOWN', downloaded: 2,
      message: '暂时无法确认规则更新结果，请核对 OxiDNS 日志。',
    })))
    await vi.advanceTimersByTimeAsync(2000)
    const status = wrapper.get('[role="alert"]')
    expect(status.text()).toContain('规则更新结果待确认')
    expect(status.text()).not.toContain('规则更新失败')
    expect(status.classes()).toContain('text-warning-accent')
    expect(wrapper.get('button').text()).toBe('查询更新结果')
    expect(wrapper.findAll('button').some(button => button.text() === '重新尝试更新')).toBe(true)
    expect(notify.warning).toHaveBeenCalledWith('OxiDNS 规则更新结果待确认', '暂时无法确认规则更新结果，请核对 OxiDNS 日志。')
    expect(notify.error).not.toHaveBeenCalled()
    expect(notify.success).not.toHaveBeenCalled()
    expect(wrapper.emitted('updated')).toBeUndefined()
  })

  it('resumes a running task on page entry without posting again', async () => {
    vi.mocked(api.get).mockResolvedValueOnce(response(task('running'))).mockResolvedValueOnce(done())
    const wrapper = await render()
    expect(wrapper.get('button').attributes('disabled')).toBeDefined()
    await vi.advanceTimersByTimeAsync(2000)
    expect(api.post).not.toHaveBeenCalled()
    expect(wrapper.emitted('updated')).toHaveLength(1)
  })

  it('does not toast or refresh for an old completed task on page entry', async () => {
    vi.mocked(api.get).mockResolvedValueOnce(done())
    const wrapper = await render()
    expect(wrapper.text()).toContain('规则更新完成')
    expect(notify.success).not.toHaveBeenCalled()
    expect(wrapper.emitted('updated')).toBeUndefined()
  })

  it('recovers a lost POST response by GET, never by repeating POST', async () => {
    const wrapper = await render()
    vi.mocked(api.post).mockRejectedValueOnce(new Error('Network Error'))
    vi.mocked(api.get).mockResolvedValueOnce(response(task('running'))).mockResolvedValueOnce(done())
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(api.get).toHaveBeenCalledTimes(2)
    expect(api.post).toHaveBeenCalledTimes(1)
    expect(notify.success).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(2000)
    expect(notify.success).toHaveBeenCalledTimes(1)
    expect(api.post).toHaveBeenCalledTimes(1)
  })

  it('does not confuse an old success with a lost new request', async () => {
    vi.mocked(api.get).mockResolvedValue(done())
    const wrapper = await render()
    vi.mocked(api.post).mockRejectedValueOnce(new Error('Network Error'))
    await wrapper.get('button').trigger('click')
    await flushPromises()
    await vi.advanceTimersByTimeAsync(2000)
    expect(notify.success).not.toHaveBeenCalled()
    expect(api.post).toHaveBeenCalledTimes(1)
    expect(wrapper.text()).toContain('更新请求尚未确认')
  })

  it('retains the last task identity across a rejected update and a subsequent lost response', async () => {
    vi.mocked(api.get).mockResolvedValue(done())
    const wrapper = await render()
    vi.mocked(api.post).mockRejectedValueOnce({ response: { status: 409, data: { code: 'CORE_BUSY', message: '核心忙，请稍后重试。' } } })
    await wrapper.get('button').trigger('click')
    await flushPromises()
    vi.mocked(api.post).mockRejectedValueOnce(new Error('Network Error'))
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('更新请求尚未确认')
    expect(notify.success).not.toHaveBeenCalled()
  })

  it('bounds polling even when the Agent reports an indefinitely running task', async () => {
    vi.mocked(api.get).mockResolvedValue(response(task('running')))
    const wrapper = await render()
    await vi.advanceTimersByTimeAsync(600000)
    expect(wrapper.get('button').text()).toBe('查询更新结果')
    const calls = vi.mocked(api.get).mock.calls.length
    await vi.advanceTimersByTimeAsync(60000)
    expect(api.get).toHaveBeenCalledTimes(calls)
    expect(notify.success).not.toHaveBeenCalled()
  })

  it('stops after repeated disconnections and offers a read-only status retry', async () => {
    const wrapper = await render()
    await wrapper.get('button').trigger('click')
    await flushPromises()
    vi.mocked(api.get).mockRejectedValue(new Error('Network Error'))
    await vi.advanceTimersByTimeAsync(10000)
    expect(wrapper.get('button').text()).toBe('查询更新结果')
    const calls = vi.mocked(api.get).mock.calls.length
    await vi.advanceTimersByTimeAsync(20000)
    expect(api.get).toHaveBeenCalledTimes(calls)
    expect(notify.success).not.toHaveBeenCalled()
    vi.mocked(api.get).mockResolvedValueOnce(done())
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledTimes(1)
    expect(notify.success).toHaveBeenCalledTimes(1)
  })

  it('reports an unknown outcome if the Agent loses a known running task', async () => {
    const wrapper = await render()
    await wrapper.get('button').trigger('click')
    await flushPromises()
    vi.mocked(api.get).mockResolvedValue(response(task()))
    await vi.advanceTimersByTimeAsync(2000)
    expect(wrapper.get('[role="alert"]').text()).toContain('更新任务已丢失或被替换，结果未知')
    expect(wrapper.get('button').text()).toBe('查询更新结果')
    const calls = vi.mocked(api.get).mock.calls.length
    await vi.advanceTimersByTimeAsync(20000)
    expect(api.get).toHaveBeenCalledTimes(calls)
    expect(notify.success).not.toHaveBeenCalled()
  })

  it('does not turn a definitive core API failure into an ambiguous network result', async () => {
    const wrapper = await render()
    vi.mocked(api.post).mockRejectedValueOnce({ response: { status: 502, data: { code: 'CORE_UPDATE_UNSUPPORTED', message: '当前核心不支持规则更新接口，请升级核心。' } } })
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('请升级核心')
    expect(wrapper.get('button').text()).toBe('更新规则')
    expect(api.get).toHaveBeenCalledTimes(1)
  })

  it('allows an explicit uncertain-result retry only after checking that no task is running', async () => {
    const wrapper = await render()
    vi.mocked(api.post).mockRejectedValueOnce(new Error('Network Error'))
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('更新请求尚未确认')
    vi.mocked(api.get).mockResolvedValueOnce(response(task('running')))
    await wrapper.findAll('button').find(button => button.text() === '重新尝试更新')!.trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledTimes(1)
    vi.mocked(api.get).mockResolvedValueOnce(response(task()))
    await vi.advanceTimersByTimeAsync(2000)
    expect(wrapper.text()).toContain('结果未知')
    vi.mocked(api.get).mockResolvedValueOnce(response(task()))
    await wrapper.findAll('button').find(button => button.text() === '重新尝试更新')!.trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledTimes(2)
  })

  it('ignores old device responses and aborts them when switching devices', async () => {
    const old = deferred()
    const wrapper = await render()
    vi.mocked(api.post).mockReturnValueOnce(old.promise)
    await wrapper.get('button').trigger('click')
    const signal = vi.mocked(api.post).mock.calls[0][2]?.signal as AbortSignal
    await wrapper.setProps({ agentId: 'two' })
    await flushPromises()
    expect(signal.aborted).toBe(true)
    old.resolve(done())
    await flushPromises()
    expect(wrapper.text()).not.toContain('规则更新完成')
    expect(notify.success).not.toHaveBeenCalled()
    await wrapper.get('button').trigger('click')
    expect(api.post).toHaveBeenLastCalledWith('/agents/two/oxidns/rules/update', {}, expect.anything())
  })

  it('cleans pending polling and in-flight requests on unmount', async () => {
    const pending = deferred()
    vi.mocked(api.get).mockResolvedValueOnce(response(task('running'))).mockReturnValueOnce(pending.promise)
    const wrapper = await render()
    await vi.advanceTimersByTimeAsync(2000)
    const signal = vi.mocked(api.get).mock.calls[1][1]?.signal as AbortSignal
    wrapper.unmount()
    expect(signal.aborted).toBe(true)
    pending.resolve(done())
    await flushPromises()
    await vi.advanceTimersByTimeAsync(10000)
    expect(api.get).toHaveBeenCalledTimes(2)
    expect(notify.success).not.toHaveBeenCalled()
  })

  it('points missing update configuration to downloadable rules and republishing without requiring a schedule', async () => {
    const wrapper = await render()
    vi.mocked(api.post).mockRejectedValueOnce({ response: { status: 409, data: { code: 'RULE_UPDATE_NOT_CONFIGURED' } } })
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toBe('请先配置可下载的规则并重新发布 OxiDNS 配置，然后重试。')
    expect(notify.success).not.toHaveBeenCalled()
    expect(wrapper.get('button').attributes('disabled')).toBeUndefined()
    expect(api.get).toHaveBeenCalledTimes(1)
    expect(api.post).toHaveBeenCalledTimes(1)
    expect(wrapper.emitted('updated')).toBeUndefined()
  })

  it('prevents updates while the device is offline or busy', async () => {
    const wrapper = await render({ disabled: true })
    await wrapper.get('button').trigger('click')
    expect(api.post).not.toHaveBeenCalled()
    expect(wrapper.get('button').classes()).toContain('min-h-11')
  })

  it('identifies the first released Agent version supporting rule updates', async () => {
    const wrapper = await render()
    vi.mocked(api.post).mockRejectedValueOnce({ response: { status: 409, data: { code: 'UPGRADE_REQUIRED' } } })
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('1.8.30-go')
    expect(notify.success).not.toHaveBeenCalled()
  })
})
