import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import api from '@/api'
import { confirm, notify } from '@/lib/feedback'
import OxiDNSCacheFlush from '@/components/agents/OxiDNSCacheFlush.vue'

vi.mock('@/api', () => ({ default: { get: vi.fn(), post: vi.fn() } }))
vi.mock('@/lib/feedback', () => ({ confirm: vi.fn(), notify: { success: vi.fn(), error: vi.fn(), warning: vi.fn() } }))
const wrappers: VueWrapper[] = []
function deferred<T>() {
  let resolve!: (value: T) => void
  return { promise: new Promise<T>(yes => { resolve = yes }), resolve: (value: T) => resolve(value) }
}
async function render(props = {}) {
  const wrapper = mount(OxiDNSCacheFlush, { attachTo: document.body, props: { agentId: 'one', deviceName: '客厅 DNS', ...props } })
  wrappers.push(wrapper)
  await flushPromises()
  return wrapper
}
const listed = (caches: string[]) => ({ data: { success: true, caches } } as any)
beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.get).mockResolvedValue(listed(['lazy_cache', 'renamed_probe']))
  vi.mocked(api.post).mockImplementation(async (_path, body: any) => ({ data: { success: true, flushed: body.tags } } as any))
  vi.mocked(confirm).mockResolvedValue(true)
})
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); document.body.innerHTML = '' })

describe('OxiDNS cache flush', () => {
  it('flushes every cache configured on the selected device after confirmation', async () => {
    const wrapper = await render()
    expect(wrapper.get('button').text()).toBe('清理缓存')
    expect(wrapper.get('button').classes()).toContain('max-sm:min-h-11')
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(api.get).toHaveBeenCalledWith('/agents/one/oxidns/cache')
    expect(vi.mocked(confirm).mock.calls[0][0]).toContain('「客厅 DNS」的全部 DNS 缓存（主缓存、renamed_probe）')
    expect(api.post).toHaveBeenCalledWith('/agents/one/oxidns/cache/flush', { tags: ['lazy_cache', 'renamed_probe'] })
    expect(notify.success).toHaveBeenCalledWith('已清理缓存', '「客厅 DNS」的 DNS 缓存已清空。')
    expect(wrapper.emitted('flushed')).toEqual([['one']])
  })

  it('explains a configuration without caches instead of asking to confirm', async () => {
    vi.mocked(api.get).mockResolvedValueOnce(listed([]))
    const wrapper = await render()
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(confirm).not.toHaveBeenCalled()
    expect(api.post).not.toHaveBeenCalled()
    expect(notify.warning).toHaveBeenCalledWith('没有可清理的缓存', '「客厅 DNS」当前配置没有缓存插件。')
  })

  it('never flushes another device when the view switches before confirmation', async () => {
    const answer = deferred<boolean>()
    vi.mocked(confirm).mockReturnValueOnce(answer.promise)
    const wrapper = await render()
    await wrapper.get('button').trigger('click')
    await flushPromises()
    await wrapper.setProps({ agentId: 'two', deviceName: '书房 DNS' })
    answer.resolve(true)
    await flushPromises()
    expect(api.post).not.toHaveBeenCalled()
  })

  it('keeps an in-flight result with the device it was started for', async () => {
    const request = deferred<any>()
    vi.mocked(api.post).mockReturnValueOnce(request.promise)
    const wrapper = await render()
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(wrapper.get('button').text()).toBe('清理缓存中…')
    await wrapper.setProps({ agentId: 'two', deviceName: '书房 DNS' })
    expect(wrapper.get('button').text()).toBe('清理缓存')
    expect(wrapper.get('button').attributes('disabled')).toBeUndefined()
    request.resolve({ data: { success: true, flushed: ['lazy_cache', 'renamed_probe'] } })
    await flushPromises()
    expect(notify.success).toHaveBeenCalledWith('已清理缓存', expect.stringContaining('客厅 DNS'))
    expect(wrapper.emitted('flushed')).toEqual([['one']])
  })

  it('reports completed caches of a partial failure', async () => {
    vi.mocked(api.post).mockRejectedValueOnce({ response: { data: {
      success: false, code: 'CACHE_FLUSH_FAILED', message: '清理缓存失败，请检查 OxiDNS 日志后重试。', flushed: ['lazy_cache'] } } })
    const wrapper = await render()
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(notify.error).toHaveBeenCalledWith('清理缓存未全部完成', '清理缓存失败，请检查 OxiDNS 日志后重试。 已清理：主缓存。')
    expect(wrapper.emitted('flushed')).toEqual([['one']])
  })

  it('stays inert while disabled or after cancellation', async () => {
    const wrapper = await render({ disabled: true })
    expect(wrapper.get('button').attributes('disabled')).toBeDefined()
    await wrapper.setProps({ disabled: false })
    vi.mocked(confirm).mockResolvedValueOnce(false)
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(api.post).not.toHaveBeenCalled()
    expect(wrapper.get('button').attributes('disabled')).toBeUndefined()
  })
})
