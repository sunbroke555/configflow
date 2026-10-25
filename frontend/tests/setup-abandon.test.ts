import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import SetupEntry from '@/components/setup/SetupEntry.vue'
import { setupApi, type SetupAbandonPlan, type SetupSession } from '@/api/setup'
import { confirm, confirmDanger, notify } from '@/lib/feedback'

const refreshProfiles = vi.fn().mockResolvedValue([])
vi.mock('@/api/setup', () => ({ setupApi: { index: vi.fn(), abandon: vi.fn() } }))
vi.mock('@/stores/profile', () => ({ useProfileStore: () => ({ refreshProfiles }) }))
vi.mock('@/lib/feedback', () => ({ confirm: vi.fn(), confirmDanger: vi.fn(), notify: { error: vi.fn(), success: vi.fn() } }))

const session = (changes: Partial<SetupSession> = {}): SetupSession => ({
  id: 'a'.repeat(32), profile_id: 'setup_a', name: '我的家庭配置', goal: 'gateway', step: 2, max_step: 2,
  prepared: false, completed: false, subscription_ids: [], node_ids: [], updated_at: '', ...changes })
const plan = (changes: Partial<SetupAbandonPlan> = {}): SetupAbandonPlan => ({
  session_id: 'a'.repeat(32), name: '我的家庭配置', profile: { id: 'setup_a', name: '我的家庭配置' },
  delete_profile: true, kept_reason: null, guidance: '', subscription: { id: 'setup_sub_a', name: '我的家庭配置订阅' }, ...changes })

async function render() {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: { template: '<div />' } }, { path: '/setup', component: { template: '<div />' } }] })
  await router.push('/'); await router.isReady()
  const wrapper = mount(SetupEntry, { global: { plugins: [router] } })
  await flushPromises()
  return wrapper
}
const button = (wrapper: ReturnType<typeof mount>, text: string) => wrapper.findAll('button').find(item => item.text() === text)

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(setupApi.index).mockResolvedValue({ data: { sessions: [session()], suggested: false, subscriptions: [], nodes: [] } } as never)
})

describe('abandoning an unfinished setup', () => {
  it('deletes the unused setup profile after a destructive confirmation and hides the banner', async () => {
    vi.mocked(setupApi.abandon).mockImplementation(async (_id, dryRun) => ({ data: plan() }) as never)
    vi.mocked(confirmDanger).mockResolvedValue(true)
    const wrapper = await render()
    expect(wrapper.text()).toContain('继续配置 · 我的家庭配置')
    await button(wrapper, '放弃')!.trigger('click'); await flushPromises()
    const [description, options] = vi.mocked(confirmDanger).mock.calls[0]
    expect(description).toContain('删除它创建的配置空间「我的家庭配置」和订阅「我的家庭配置订阅」')
    expect(description).toContain('其他配置空间和已有设备不受影响')
    expect(options).toMatchObject({ title: '放弃向导', confirmText: '放弃并删除' })
    expect(setupApi.abandon).toHaveBeenNthCalledWith(1, 'a'.repeat(32), true)
    expect(setupApi.abandon).toHaveBeenNthCalledWith(2, 'a'.repeat(32))
    expect(refreshProfiles).toHaveBeenCalled()
    expect(notify.success).toHaveBeenCalledWith('已放弃向导，并删除了它创建的配置。')
    expect(wrapper.find('section').exists()).toBe(false)
    expect(wrapper.emitted('abandoned')).toHaveLength(1)
  })

  it.each(['', '。', '！'])('explains why a profile in use is kept without duplicate sentence punctuation (suffix=%j)', async suffix => {
    const kept = plan({ delete_profile: false, subscription: null, kept_reason: '已有设备在使用它（网关 Mihomo）' + suffix, guidance: '如不再需要，先在 Agent 页解绑或删除这些设备，再到「配置空间」删除。' })
    vi.mocked(setupApi.abandon).mockResolvedValue({ data: kept } as never)
    vi.mocked(confirm).mockResolvedValue(true)
    const wrapper = await render()
    await button(wrapper, '放弃')!.trigger('click'); await flushPromises()
    expect(confirmDanger).not.toHaveBeenCalled()
    expect(vi.mocked(confirm).mock.calls[0][0]).toContain('配置空间「我的家庭配置」会保留，因为已有设备在使用它（网关 Mihomo）。如不再需要')
    expect(refreshProfiles).not.toHaveBeenCalled()
    expect(notify.success).toHaveBeenCalledWith('已放弃向导，配置空间已保留。')
    expect(wrapper.find('section').exists()).toBe(false)
  })

  it('changes nothing when the confirmation is cancelled', async () => {
    vi.mocked(setupApi.abandon).mockResolvedValue({ data: plan() } as never)
    vi.mocked(confirmDanger).mockResolvedValue(false)
    const wrapper = await render()
    await button(wrapper, '放弃')!.trigger('click'); await flushPromises()
    expect(setupApi.abandon).toHaveBeenCalledTimes(1)
    expect(wrapper.text()).toContain('继续配置 · 我的家庭配置')
    expect(wrapper.emitted('abandoned')).toBeUndefined()
  })

  it('does not offer an abandoned setup in the banner', async () => {
    vi.mocked(setupApi.index).mockResolvedValue({ data: { sessions: [session({ abandoned: true })], suggested: false, subscriptions: [], nodes: [] } } as never)
    const wrapper = await render()
    expect(wrapper.find('section').exists()).toBe(false)
  })

  it('reports a failed abandon without hiding the banner', async () => {
    vi.mocked(setupApi.abandon).mockRejectedValue(new Error('网络中断'))
    const wrapper = await render()
    await button(wrapper, '放弃')!.trigger('click'); await flushPromises()
    expect(notify.error).toHaveBeenCalledWith('网络中断')
    expect(wrapper.text()).toContain('继续配置 · 我的家庭配置')
  })
})
