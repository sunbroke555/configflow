import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import MihomoLogs from '@/components/mihomo/MihomoLogs.vue'
import AgentLogLines from '@/components/agents/AgentLogLines.vue'
import { Select } from '@/components/ui/select'
import { agentApi } from '@/api'

vi.mock('@/api', () => ({ agentApi: { getLogSources: vi.fn(), getLogs: vi.fn() } }))
vi.mock('vue-router', () => ({ RouterLink: { props: ['to'], template: '<a :href="to"><slot /></a>' } }))
vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })

const coreSource = { id: 'core', label: 'Mihomo 运行日志', kind: 'journal', unit: 'mihomo' }
const sources = (items: unknown[] = [coreSource]) => ({ data: { success: true, sources: items } } as any)
const logs = (text: string) => ({ data: { success: true, logs: text } } as any)
const wrappers: VueWrapper[] = []
const deferred = <T,>() => {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}

async function render(props: Partial<{ agentId: string; active: boolean; reloadToken: number }> = {}) {
  const wrapper = mount(MihomoLogs, { props: { agentId: 'gw', active: true, reloadToken: 0, ...props } })
  wrappers.push(wrapper)
  await flushPromises()
  return wrapper
}

const logText = (wrapper: VueWrapper) => wrapper.findComponent(AgentLogLines).props('text')
const button = (wrapper: VueWrapper, label: string) => {
  const match = wrapper.findAll('button').find(item => item.attributes('aria-label') === label || item.text() === label)
  if (!match) throw new Error(`Missing button: ${label}`)
  return match
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'] })
  vi.resetAllMocks()
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
  Object.defineProperty(document, 'hidden', { configurable: true, value: false })
  vi.mocked(agentApi.getLogSources).mockResolvedValue(sources())
  vi.mocked(agentApi.getLogs).mockResolvedValue(logs('core startup complete'))
})

afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  vi.useRealTimers()
  delete (document as any).visibilityState
  delete (document as any).hidden
})

describe('Mihomo logs', () => {
  it('reads only a core source and searches the bounded snapshot as literal text', async () => {
    vi.mocked(agentApi.getLogSources).mockResolvedValue(sources([
      { id: 'agent', label: 'Agent 自身日志', kind: 'file' },
      coreSource,
      { id: 'core-error', label: 'Mihomo 错误日志', kind: 'file' },
      { id: 'supervisor', label: 'Supervisor 日志', kind: 'file' }
    ]))
    vi.mocked(agentApi.getLogs).mockResolvedValue(logs('connection example.test[1]\nconnection exampleXtest1\nother entry'))
    const wrapper = await render()
    expect(agentApi.getLogs).toHaveBeenCalledWith('gw', 500, { source: 'core' }, expect.objectContaining({ signal: expect.any(AbortSignal) }))
    expect(wrapper.text()).not.toContain('Agent 自身日志')
    expect(wrapper.text()).not.toContain('Supervisor 日志')
    await wrapper.get('input[placeholder="搜索日志"]').setValue('EXAMPLE.TEST[1]')
    expect(logText(wrapper)).toContain('connection example.test[1]')
    expect(logText(wrapper)).not.toContain('exampleXtest1')
    await wrapper.get('input[placeholder="搜索日志"]').setValue('absent')
    expect(wrapper.text()).toContain('没有匹配的日志')
  })

  it('keeps the last 500 actual lines when the snapshot ends with a newline', async () => {
    const lines = Array.from({ length: 501 }, (_, index) => `log entry ${index + 1}`)
    vi.mocked(agentApi.getLogs).mockResolvedValue(logs(`${lines.join('\n')}\n`))
    const wrapper = await render()
    expect(logText(wrapper).split('\n')).toEqual(lines.slice(1))
  })

  it('does not overlap requests and pauses automatic refresh until resumed', async () => {
    const next = deferred<any>()
    vi.mocked(agentApi.getLogs).mockResolvedValueOnce(logs('first')).mockReturnValueOnce(next.promise)
    const wrapper = await render()
    await vi.advanceTimersByTimeAsync(3000)
    expect(agentApi.getLogs).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(12000)
    expect(agentApi.getLogs).toHaveBeenCalledTimes(2)
    next.resolve(logs('second'))
    await flushPromises()
    await button(wrapper, '暂停刷新').trigger('click')
    await vi.advanceTimersByTimeAsync(9000)
    expect(agentApi.getLogs).toHaveBeenCalledTimes(2)
    await button(wrapper, '恢复刷新').trigger('click')
    await flushPromises()
    expect(agentApi.getLogs).toHaveBeenCalledTimes(3)
  })

  it('waits until active and cancels a pending request when the tab is deactivated', async () => {
    const wrapper = await render({ active: false })
    expect(agentApi.getLogSources).not.toHaveBeenCalled()
    await wrapper.setProps({ active: true })
    await flushPromises()
    const pending = deferred<any>()
    vi.mocked(agentApi.getLogs).mockReturnValueOnce(pending.promise)
    await vi.advanceTimersByTimeAsync(3000)
    const signal = vi.mocked(agentApi.getLogs).mock.calls.at(-1)![3]!.signal!
    await wrapper.setProps({ active: false })
    expect(signal.aborted).toBe(true)
    pending.resolve(logs('stale response'))
    await flushPromises()
    expect(logText(wrapper)).not.toContain('stale response')
    const requests = vi.mocked(agentApi.getLogs).mock.calls.length
    await vi.advanceTimersByTimeAsync(12000)
    expect(agentApi.getLogs).toHaveBeenCalledTimes(requests)
  })

  it('suspends refresh while the page is hidden and cancels on unmount', async () => {
    const wrapper = await render()
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' })
    Object.defineProperty(document, 'hidden', { configurable: true, value: true })
    document.dispatchEvent(new Event('visibilitychange'))
    await vi.advanceTimersByTimeAsync(9000)
    expect(agentApi.getLogs).toHaveBeenCalledTimes(1)
    const pending = deferred<any>()
    vi.mocked(agentApi.getLogs).mockReturnValueOnce(pending.promise)
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
    Object.defineProperty(document, 'hidden', { configurable: true, value: false })
    document.dispatchEvent(new Event('visibilitychange'))
    await flushPromises()
    expect(agentApi.getLogs).toHaveBeenCalledTimes(2)
    const signal = vi.mocked(agentApi.getLogs).mock.calls.at(-1)![3]!.signal!
    wrapper.unmount()
    wrappers.splice(wrappers.indexOf(wrapper), 1)
    expect(signal.aborted).toBe(true)
    pending.resolve(logs('late unmounted response'))
    await flushPromises()
    await vi.advanceTimersByTimeAsync(9000)
    expect(agentApi.getLogs).toHaveBeenCalledTimes(2)
  })

  it('discards a previous device response without mixing it into the new device', async () => {
    const previous = deferred<any>()
    vi.mocked(agentApi.getLogs).mockReturnValueOnce(previous.promise).mockResolvedValue(logs('new gateway log'))
    const wrapper = await render()
    const signal = vi.mocked(agentApi.getLogs).mock.calls[0][3]!.signal!
    await wrapper.setProps({ agentId: 'gw-next' })
    await flushPromises()
    expect(signal.aborted).toBe(true)
    expect(agentApi.getLogSources).toHaveBeenLastCalledWith('gw-next', expect.objectContaining({ signal: expect.any(AbortSignal) }))
    expect(logText(wrapper)).toBe('new gateway log')
    previous.resolve(logs('old gateway log'))
    await flushPromises()
    expect(logText(wrapper)).toBe('new gateway log')
  })

  it('clears the old source snapshot immediately when switching core log sources', async () => {
    vi.mocked(agentApi.getLogSources).mockResolvedValue(sources([coreSource, { id: 'core-error', label: 'Mihomo 错误日志', kind: 'file' }]))
    const wrapper = await render()
    const pending = deferred<any>()
    vi.mocked(agentApi.getLogs).mockReturnValueOnce(pending.promise)
    wrapper.getComponent(Select).vm.$emit('update:modelValue', 'core-error')
    await flushPromises()
    expect(agentApi.getLogs).toHaveBeenLastCalledWith('gw', 500, { source: 'core-error' }, expect.objectContaining({ signal: expect.any(AbortSignal) }))
    expect(wrapper.text()).not.toContain('core startup complete')
    pending.resolve(logs('core error output'))
    await flushPromises()
    expect(logText(wrapper)).toBe('core error output')
  })

  it('keeps the last successful snapshot visible after failure and supports retry', async () => {
    const wrapper = await render()
    vi.mocked(agentApi.getLogs).mockRejectedValueOnce({ response: { data: { message: 'Agent 暂时不可达' } } })
    await button(wrapper, '刷新日志').trigger('click')
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('Agent 暂时不可达')
    expect(logText(wrapper)).toBe('core startup complete')
    vi.mocked(agentApi.getLogs).mockResolvedValueOnce(logs('recovered output'))
    await button(wrapper, '刷新日志').trigger('click')
    await flushPromises()
    expect(logText(wrapper)).toBe('recovered output')
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
  })

  it('explains unreadable core logs instead of falling back to the Agent log', async () => {
    vi.mocked(agentApi.getLogSources).mockResolvedValue(sources([
      { id: 'agent', label: 'Agent 日志', kind: 'file' },
      { ...coreSource, kind: 'unavailable', note: '请在宿主机用 docker logs 查看' }
    ]))
    const wrapper = await render()
    expect(wrapper.text()).toContain('请在宿主机用 docker logs 查看')
    expect(agentApi.getLogs).not.toHaveBeenCalled()
  })

  it('requires a source-aware Agent rather than showing ambiguous legacy logs', async () => {
    vi.mocked(agentApi.getLogSources).mockResolvedValue({ data: { success: true, legacy: true } } as any)
    const wrapper = await render()
    expect(wrapper.text()).toContain('旧版 Agent')
    expect(wrapper.text()).toContain('升级 Agent')
    expect(agentApi.getLogs).not.toHaveBeenCalled()
  })
})
