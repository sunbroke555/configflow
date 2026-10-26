import { afterEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import RouterRuleSync from '@/components/routers/RouterRuleSync.vue'
import { routerApi } from '@/api'
vi.mock('@/api', () => ({ routerApi: { ruleSync: vi.fn(), saveRuleSync: vi.fn(), startRuleSync: vi.fn() } }))
vi.mock('@/lib/feedback', () => ({ notify: { success: vi.fn() } }))
vi.mock('@/profileContext', () => ({ beginScopedRequest: vi.fn(), endScopedRequest: vi.fn() }))
const wrappers: VueWrapper[] = []
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); vi.clearAllMocks(); vi.useRealTimers() })
const data = (status = 'synced', enabled = true, ready = true) => ({ data: { success: true,
  settings: { auto_rule_sync: enabled, rule_sync_interval: 60 },
  sync: { enabled, status, ready, message: status === 'paused' ? '规则集的 IPv4 变为零，保留原状态' : '已核对远程规则',
    added: 1, removed: 2, log: [], rules: [{ id: 'r1', name: 'Remote', source_type: 'url',
      ip_count: 10, domain_count: 3, ignored_count: 1, invalid_count: 0, version: 'new-version', synced_version: 'old-version' }] } } })
const setup = async () => {
  const wrapper = mount(RouterRuleSync, { props: { id: 'ros', name: 'ROS', profile: 'home' } })
  wrappers.push(wrapper); await flushPromises(); return wrapper
}
const button = (wrapper: VueWrapper, text: string) => wrapper.findAll('button').find(item => item.text() === text)!

describe('remote rule synchronization', () => {
  it('shows versions and counts and requires a successful first publish', async () => {
    vi.mocked(routerApi.ruleSync).mockResolvedValue(data('waiting', false, false) as any)
    const wrapper = await setup()
    expect(routerApi.ruleSync).toHaveBeenCalledWith('ros', 'home')
    expect(wrapper.text()).toContain('自动同步已关闭')
    expect(wrapper.text()).toContain('先点「预览发布」，确认并成功发布一次，再开启自动同步。')
    expect(wrapper.text()).not.toContain('同步基准')
    expect(wrapper.find('dl').exists()).toBe(false)
    expect(wrapper.text()).toContain('最近读取版本')
    expect(wrapper.text()).toContain('new-version')
    expect(wrapper.text()).toContain('old-version')
    expect(button(wrapper, '立即同步').attributes('disabled')).toBeDefined()
    await button(wrapper, '预览发布').trigger('click')
    expect(wrapper.emitted('preview')).toHaveLength(1)
    expect(routerApi.startRuleSync).not.toHaveBeenCalled()
  })
  it('saves only the switch and interval to the pinned router/profile', async () => {
    vi.mocked(routerApi.ruleSync).mockResolvedValue(data('synced', false) as any)
    vi.mocked(routerApi.saveRuleSync).mockResolvedValue(data('synced', true) as any)
    const wrapper = await setup()
    await wrapper.get('[role="switch"]').trigger('click'); await flushPromises()
    expect(routerApi.saveRuleSync).toHaveBeenCalledWith('ros', { auto_rule_sync: true, rule_sync_interval: 60 }, 'home')
    expect(routerApi.startRuleSync).not.toHaveBeenCalled()
    expect(button(wrapper, '立即同步').attributes('disabled')).toBeUndefined()
  })
  it('blocks automatic publication of malformed sources but permits manual preview', async () => {
    vi.mocked(routerApi.ruleSync).mockResolvedValue(data('paused', false) as any)
    const wrapper = await setup()
    expect(wrapper.text()).toContain('已暂停，需核对')
    expect(wrapper.text()).toContain('规则集的 IPv4 变为零，保留原状态。处理后点「预览发布」并确认，重新建立同步基准。')
    expect(button(wrapper, '立即同步').attributes('disabled')).toBeDefined()
    expect(button(wrapper, '预览发布').attributes('disabled')).toBeUndefined()
  })
  it('does not repeat the resume hint when the pause reason already gives it', async () => {
    const paused = data('paused') as any
    paused.data.sync.message = '本次更新还需要修改「mihomo 回流规则」，请手动预览发布'
    vi.mocked(routerApi.ruleSync).mockResolvedValue(paused)
    const wrapper = await setup()
    expect(wrapper.text()).toContain('本次更新还需要修改「mihomo 回流规则」，请手动预览发布')
    expect(wrapper.text()).not.toContain('重新建立同步基准')
  })
  it('reloads its state once a release from the page finishes', async () => {
    vi.mocked(routerApi.ruleSync).mockResolvedValue(data('paused') as any)
    const wrapper = await setup()
    await wrapper.setProps({ disabled: true }); await flushPromises()
    expect(routerApi.ruleSync).toHaveBeenCalledTimes(1)
    vi.mocked(routerApi.ruleSync).mockResolvedValue(data('synced') as any)
    await wrapper.setProps({ disabled: false }); await flushPromises()
    expect(routerApi.ruleSync).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('已同步')
  })
  it('polls an explicitly started job and stops once complete', async () => {
    vi.useFakeTimers()
    vi.mocked(routerApi.ruleSync).mockResolvedValue(data() as any)
    vi.mocked(routerApi.startRuleSync).mockResolvedValue(data('queued') as any)
    const wrapper = await setup()
    await button(wrapper, '立即同步').trigger('click'); await flushPromises()
    expect(routerApi.startRuleSync).toHaveBeenCalledWith('ros', 'home')
    expect(button(wrapper, '同步中').attributes('disabled')).toBeDefined()
    await vi.advanceTimersByTimeAsync(2000); await flushPromises()
    expect(routerApi.ruleSync).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(4000); await flushPromises()
    expect(routerApi.ruleSync).toHaveBeenCalledTimes(2)
    expect(button(wrapper, '立即同步').attributes('disabled')).toBeUndefined()
  })
  it('drops stale responses when switching profile and presents load errors', async () => {
    let resolveOld!: (value: any) => void
    vi.mocked(routerApi.ruleSync).mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve }) as any)
    const wrapper = await setup()
    vi.mocked(routerApi.ruleSync).mockRejectedValueOnce(new Error('无法读取同步状态'))
    await wrapper.setProps({ id: 'other', profile: 'office' }); await flushPromises()
    resolveOld(data()); await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('无法读取同步状态')
    expect(wrapper.text()).not.toContain('Remote')
  })
})
