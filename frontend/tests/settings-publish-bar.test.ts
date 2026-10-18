import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { agentApi, profileApi, type PublishGuard } from '@/api'
import { notify } from '@/lib/feedback'
import { Select } from '@/components/ui/select'
import SettingsPublishBar from '@/components/generate/SettingsPublishBar.vue'

vi.mock('@/api', () => ({
  agentApi: { getAll: vi.fn(), pushConfig: vi.fn(), getDeployment: vi.fn(), activateDeployment: vi.fn() },
  profileApi: { list: vi.fn() },
}))
vi.mock('@/lib/feedback', () => ({ notify: { success: vi.fn(), error: vi.fn(), info: vi.fn() } }))
const wrappers: VueWrapper[] = []
const response = (data: any) => ({ data } as any)
const guard = (extra: Partial<PublishGuard> = {}): PublishGuard => ({ expected_profile_id: 'office', expected_service_type: 'oxidns', expected_config_revision: 7, ...extra })
const agent = (extra: Record<string, any> = {}) => ({ id: 'one', name: '客厅 DNS', profile_id: 'office', service_type: 'oxidns', status: 'online', enabled: true, ...extra })
const button = (wrapper: VueWrapper, text: string) => wrapper.findAll('button').find(item => item.text() === text)!
const publishButton = (wrapper: VueWrapper) => wrapper.findAll('button').find(item => /^(保存并发布|发布中…|保存中…)$/.test(item.text()))!
const deferred = <T,>() => {
  let resolve!: (value: T) => void
  let reject!: (value: any) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
async function render(props: Record<string, any> = {}) {
  const wrapper = mount(SettingsPublishBar, {
    props: { open: true, profileId: 'office', serviceType: 'oxidns', save: vi.fn().mockResolvedValue(guard()), ...props },
    attachTo: document.body,
  })
  wrappers.push(wrapper)
  await flushPromises()
  return wrapper
}
async function publish(wrapper: VueWrapper) { await publishButton(wrapper).trigger('click'); await flushPromises() }
async function choose(wrapper: VueWrapper, id: string) {
  wrapper.getComponent(Select).vm.$emit('update:modelValue', id)
  await flushPromises()
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.resetAllMocks()
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: true, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} })))
  vi.mocked(agentApi.getAll).mockResolvedValue(response([agent()]))
  vi.mocked(profileApi.list).mockResolvedValue(response([{ id: 'office', name: '办公室', is_default: true }, { id: 'home', name: '家里' }]))
  vi.mocked(agentApi.pushConfig).mockImplementation(async (_id, deploymentId) => response({ deployment_id: deploymentId, status: 'succeeded' }))
  vi.mocked(agentApi.getDeployment).mockImplementation(async (_id, deploymentId) => response({ deployment_id: deploymentId, status: 'uploading' }))
  vi.mocked(agentApi.activateDeployment).mockImplementation(async (_id, deploymentId) => response({ deployment_id: deploymentId, status: 'succeeded' }))
})
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  document.body.innerHTML = ''
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('settings save and publish', () => {
  it('shows a single bound target and publishes only after its saved revision is confirmed', async () => {
    const saved = deferred<PublishGuard>()
    const save = vi.fn(() => saved.promise)
    const wrapper = await render({ save })
    expect(wrapper.text()).toContain('办公室')
    expect(wrapper.text()).toContain('发布到：客厅 DNS')
    await publish(wrapper)
    expect(save).toHaveBeenCalledOnce()
    expect(agentApi.pushConfig).not.toHaveBeenCalled()
    expect(publishButton(wrapper).attributes('disabled')).toBeDefined()
    saved.resolve(guard())
    await flushPromises()
    expect(agentApi.getAll).toHaveBeenCalledTimes(2)
    expect(agentApi.pushConfig).toHaveBeenCalledWith('one', expect.any(String), guard())
    expect(wrapper.text()).toContain('草稿已保存，发布成功，服务检查已通过。')
  })

  it('keeps saving a draft separate from publication', async () => {
    const save = vi.fn()
    const wrapper = await render({ save })
    await button(wrapper, '保存草稿').trigger('click')
    expect(wrapper.emitted('save-draft')).toHaveLength(1)
    expect(save).not.toHaveBeenCalled()
    expect(agentApi.pushConfig).not.toHaveBeenCalled()
  })

  it('does not publish after a rejected or canceled save', async () => {
    const save = vi.fn().mockResolvedValueOnce(null).mockRejectedValueOnce(new Error('校验未通过'))
    const wrapper = await render({ save })
    await publish(wrapper)
    expect(agentApi.pushConfig).not.toHaveBeenCalled()
    expect(wrapper.text()).not.toContain('草稿已保存')
    await publish(wrapper)
    expect(agentApi.pushConfig).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('草稿保存失败，未发布：校验未通过')
  })

  it.each([
    undefined,
    {},
    guard({ expected_profile_id: 'home' }),
    guard({ expected_service_type: 'mihomo' }),
    guard({ expected_config_revision: Number.NaN }),
    guard({ expected_config_revision: -1 }),
  ])('does not publish without a valid guard for the saved profile and service (%j)', async savedGuard => {
    const wrapper = await render({ save: vi.fn().mockResolvedValue(savedGuard) })
    await publish(wrapper)
    expect(agentApi.pushConfig).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('无法确认本次保存的配置版本')
  })

  it('uses the actual default profile for unbound legacy agents and excludes other profiles and services', async () => {
    vi.mocked(agentApi.getAll).mockResolvedValue(response([
      agent({ profile_id: undefined }),
      agent({ id: 'other', name: '家里 DNS', profile_id: 'home' }),
      agent({ id: 'wrong-service', name: '代理核心', service_type: 'mihomo' }),
    ]))
    const wrapper = await render()
    expect(wrapper.text()).toContain('发布到：客厅 DNS')
    expect(wrapper.findComponent(Select).exists()).toBe(false)
    await publish(wrapper)
    expect(agentApi.pushConfig).toHaveBeenCalledWith('one', expect.any(String), guard())
  })

  it('requires an explicit selection when several targets match and sends to only that device', async () => {
    vi.mocked(agentApi.getAll).mockResolvedValue(response([agent(), agent({ id: 'two', name: '书房 DNS' })]))
    const save = vi.fn().mockResolvedValue(guard())
    const wrapper = await render({ save })
    expect(publishButton(wrapper).attributes('disabled')).toBeDefined()
    await publish(wrapper)
    expect(save).not.toHaveBeenCalled()
    await choose(wrapper, 'two')
    await publish(wrapper)
    expect(agentApi.pushConfig).toHaveBeenCalledOnce()
    expect(agentApi.pushConfig).toHaveBeenCalledWith('two', expect.any(String), guard())
  })

  it.each([{ status: 'offline' }, { enabled: false }])('keeps offline or disabled targets unavailable (%j)', async extra => {
    vi.mocked(agentApi.getAll).mockResolvedValue(response([agent(extra)]))
    const wrapper = await render()
    expect(publishButton(wrapper).attributes('disabled')).toBeDefined()
    expect(button(wrapper, '保存草稿').attributes('disabled')).toBeUndefined()
    await publish(wrapper)
    expect(agentApi.pushConfig).not.toHaveBeenCalled()
  })

  it('allows saving a draft when no targets exist or the list cannot be read', async () => {
    vi.mocked(agentApi.getAll).mockResolvedValueOnce(response([]))
    const empty = await render()
    expect(empty.text()).toContain('尚未绑定 OxiDNS 设备')
    expect(button(empty, '保存草稿').attributes('disabled')).toBeUndefined()
    vi.mocked(agentApi.getAll).mockRejectedValueOnce(new Error('网络错误'))
    const failed = await render()
    expect(failed.text()).toContain('暂时无法读取目标设备')
    expect(button(failed, '保存草稿').attributes('disabled')).toBeUndefined()
    await button(failed, '重试读取').trigger('click')
    await flushPromises()
    expect(failed.text()).toContain('发布到：客厅 DNS')
  })

  it.each([{ profile_id: 'home' }, { service_type: 'mihomo' }, { status: 'offline' }, { enabled: false }])('rechecks the selected device after saving (%j)', async change => {
    vi.mocked(agentApi.getAll).mockResolvedValueOnce(response([agent()])).mockResolvedValueOnce(response([agent(change)]))
    const wrapper = await render()
    await publish(wrapper)
    expect(agentApi.pushConfig).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('目标设备的绑定、类型或在线状态已改变')
  })

  it.each([{ open: false }, { profileId: 'home' }, { serviceType: 'mihomo' }])('does not continue from a late save after the settings context changes (%j)', async changed => {
    const saved = deferred<PublishGuard>()
    const wrapper = await render({ save: vi.fn(() => saved.promise) })
    await publish(wrapper)
    await wrapper.setProps(changed)
    saved.resolve(guard())
    await flushPromises()
    expect(agentApi.pushConfig).not.toHaveBeenCalled()
    expect(wrapper.text()).not.toContain('草稿已保存')
  })

  it('does not submit after closing while the final target check is pending', async () => {
    const check = deferred<any>()
    vi.mocked(agentApi.getAll).mockResolvedValueOnce(response([agent()])).mockImplementationOnce(() => check.promise)
    const wrapper = await render()
    await publish(wrapper)
    await wrapper.setProps({ open: false })
    check.resolve(response([agent()]))
    await flushPromises()
    expect(agentApi.pushConfig).not.toHaveBeenCalled()
  })

  it('shows real stages while POST is pending and cannot submit twice or regress on its late response', async () => {
    const post = deferred<any>()
    vi.mocked(agentApi.pushConfig).mockImplementationOnce(() => post.promise)
    vi.mocked(agentApi.getDeployment)
      .mockRejectedValueOnce({ response: { status: 404 } })
      .mockImplementationOnce(async (_id, deploymentId) => response({ deployment_id: deploymentId, status: 'uploading', message: '正在上传 8 个文件' }))
      .mockImplementationOnce(async (_id, deploymentId) => response({ deployment_id: deploymentId, status: 'succeeded' }))
    const wrapper = await render()
    await publish(wrapper)
    await publish(wrapper)
    expect(agentApi.pushConfig).toHaveBeenCalledOnce()
    await vi.advanceTimersByTimeAsync(0)
    expect(wrapper.text()).toContain('准备文件')
    expect(wrapper.text()).not.toContain('暂时无法确认发布结果')
    await vi.advanceTimersByTimeAsync(2000)
    expect(wrapper.text()).toContain('正在上传 8 个文件')
    expect(wrapper.text()).toContain('可以离开此页面')
    await vi.advanceTimersByTimeAsync(2000)
    expect(wrapper.text()).toContain('发布成功')
    expect(publishButton(wrapper).attributes('disabled')).toBeDefined()
    post.resolve(response({ deployment_id: vi.mocked(agentApi.pushConfig).mock.lastCall![1], status: 'verifying' }))
    await flushPromises()
    expect(wrapper.text()).toContain('发布成功')
    expect(wrapper.text()).not.toContain('校验中')
    expect(publishButton(wrapper).attributes('disabled')).toBeUndefined()
  })

  it('continues tracking an uncertain transport result instead of offering a duplicate publication', async () => {
    vi.mocked(agentApi.pushConfig).mockRejectedValueOnce(new Error('timeout'))
    const wrapper = await render()
    await publish(wrapper)
    expect(wrapper.text()).toContain('结果待确认')
    expect(publishButton(wrapper).attributes('disabled')).toBeDefined()
    await vi.advanceTimersByTimeAsync(2000)
    expect(agentApi.getDeployment).toHaveBeenCalled()
    expect(wrapper.text()).toContain('上传中')
  })

  it.each(['failed', 'rolled_back'])('allows a fresh save and retry after a confirmed %s result', async status => {
    vi.mocked(agentApi.pushConfig).mockImplementationOnce(async (_id, deploymentId) => response({ deployment_id: deploymentId, status, error: '规则校验失败' }))
    const save = vi.fn().mockResolvedValue(guard())
    const wrapper = await render({ save })
    await publish(wrapper)
    expect(wrapper.text()).toContain('草稿已保存，发布失败')
    expect(wrapper.text()).toContain('规则校验失败')
    expect(publishButton(wrapper).attributes('disabled')).toBeUndefined()
    await publish(wrapper)
    expect(save).toHaveBeenCalledTimes(2)
    expect(agentApi.pushConfig).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('发布成功')
  })

  it('reports a rejected revision without leaving the user stuck in an in-progress state', async () => {
    vi.mocked(agentApi.pushConfig).mockRejectedValueOnce({ response: { status: 409, data: { message: '配置版本已改变，请重新保存' } } })
    const wrapper = await render()
    await publish(wrapper)
    expect(wrapper.text()).toContain('草稿已保存，发布失败')
    expect(wrapper.text()).toContain('配置版本已改变')
    expect(publishButton(wrapper).attributes('disabled')).toBeUndefined()
  })

  it('does not display or announce an old publication in a new profile', async () => {
    const post = deferred<any>()
    vi.mocked(agentApi.pushConfig).mockImplementationOnce(() => post.promise)
    const wrapper = await render()
    await publish(wrapper)
    const deploymentId = vi.mocked(agentApi.pushConfig).mock.lastCall![1]
    await wrapper.setProps({ profileId: 'home' })
    await flushPromises()
    post.resolve(response({ deployment_id: deploymentId, status: 'succeeded' }))
    await flushPromises()
    expect(wrapper.text()).not.toContain('草稿已保存')
    expect(wrapper.text()).not.toContain('发布成功')
    expect(notify.success).not.toHaveBeenCalled()
  })

  it('does not restore an in-flight old task when the same device is rebound to a new profile', async () => {
    const post = deferred<any>()
    vi.mocked(agentApi.pushConfig).mockImplementationOnce(() => post.promise)
    const wrapper = await render()
    await publish(wrapper)
    vi.mocked(agentApi.getAll).mockResolvedValue(response([agent({ profile_id: 'home' })]))
    await wrapper.setProps({ profileId: 'home' })
    await flushPromises()
    expect(wrapper.text()).not.toContain('准备文件')
    expect(wrapper.text()).not.toContain('草稿已保存')
    expect(publishButton(wrapper).attributes('disabled')).toBeDefined()
    post.resolve(response({ deployment_id: vi.mocked(agentApi.pushConfig).mock.lastCall![1], status: 'succeeded' }))
    await flushPromises()
    expect(wrapper.text()).not.toContain('发布成功')
    expect(notify.success).not.toHaveBeenCalled()
  })

  it('lets the user activate a ready snapshot without saving or publishing it again', async () => {
    vi.mocked(agentApi.pushConfig).mockImplementationOnce(async (_id, deploymentId) => response({ deployment_id: deploymentId, status: 'ready' }))
    const save = vi.fn().mockResolvedValue(guard())
    const wrapper = await render({ save })
    await publish(wrapper)
    expect(wrapper.text()).toContain('已校验，待激活')
    expect(wrapper.text()).toContain('不包含之后保存的草稿修改')
    expect(publishButton(wrapper).attributes('disabled')).toBeDefined()
    await button(wrapper, '应用并启动').trigger('click')
    await flushPromises()
    expect(agentApi.activateDeployment).toHaveBeenCalledWith('one', vi.mocked(agentApi.pushConfig).mock.lastCall![1])
    expect(save).toHaveBeenCalledOnce()
    expect(agentApi.pushConfig).toHaveBeenCalledOnce()
    expect(wrapper.text()).toContain('发布成功')
  })

  it('restores a previously ready snapshot and blocks a new publication', async () => {
    vi.mocked(agentApi.getAll).mockResolvedValue(response([agent({ latest_deployment: { deployment_id: 'old-ready', profile_id: 'office', status: 'ready' } })]))
    const wrapper = await render()
    expect(wrapper.text()).toContain('设备上次发布：已校验，待激活')
    expect(publishButton(wrapper).attributes('disabled')).toBeDefined()
    await button(wrapper, '应用并启动').trigger('click')
    await flushPromises()
    expect(agentApi.activateDeployment).toHaveBeenCalledWith('one', 'old-ready')
  })

  it('checks the binding again before activating an already validated snapshot', async () => {
    vi.mocked(agentApi.getAll).mockResolvedValueOnce(response([agent({ latest_deployment: { deployment_id: 'old-ready', profile_id: 'office', status: 'ready' } })]))
    const wrapper = await render()
    vi.mocked(agentApi.getAll).mockResolvedValueOnce(response([agent({ profile_id: 'home' })]))
    await button(wrapper, '应用并启动').trigger('click')
    await flushPromises()
    expect(agentApi.activateDeployment).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('未激活：目标设备的绑定、类型或在线状态已改变')
  })

  it('does not restore a ready candidate created for a different service type', async () => {
    vi.mocked(agentApi.getAll).mockResolvedValue(response([agent({ service_type: 'mosdns', latest_deployment: {
      deployment_id: 'old-mihomo', profile_id: 'office', service_type: 'mihomo', status: 'ready',
    } })]))
    const wrapper = await render({ serviceType: 'mosdns', save: vi.fn().mockResolvedValue(guard({ expected_service_type: 'mosdns' })) })
    expect(button(wrapper, '应用并启动')).toBeUndefined()
    expect(wrapper.text()).not.toContain('已校验，待激活')
    expect(agentApi.activateDeployment).not.toHaveBeenCalled()
    await publish(wrapper)
    expect(agentApi.pushConfig).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('设备有尚未结束的发布')
  })

  it.each([{ profile_id: 'home' }, { service_type: 'mosdns' }])('rechecks a ready candidate scope using fresh deployment metadata before activation (%j)', async change => {
    const recorded = { deployment_id: 'old-ready', profile_id: 'office', service_type: 'oxidns', status: 'ready' }
    vi.mocked(agentApi.getAll).mockResolvedValueOnce(response([agent({ latest_deployment: recorded })]))
    const wrapper = await render()
    vi.mocked(agentApi.getAll).mockResolvedValueOnce(response([agent({ latest_deployment: { ...recorded, ...change } })]))
    await button(wrapper, '应用并启动').trigger('click')
    await flushPromises()
    expect(agentApi.activateDeployment).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('未激活：待应用配置的配置空间或软件类型与当前设备不一致')
  })

  it('does not treat a transactional HTTP success without a deployment result as completed', async () => {
    vi.mocked(agentApi.pushConfig).mockResolvedValueOnce(response({ success: true }))
    const wrapper = await render()
    await publish(wrapper)
    expect(wrapper.text()).toContain('结果待确认')
    expect(wrapper.text()).not.toContain('发布成功')
    expect(publishButton(wrapper).attributes('disabled')).toBeDefined()
  })

  it('ignores an accepted response with a different deployment identity', async () => {
    vi.mocked(agentApi.pushConfig).mockResolvedValueOnce(response({ deployment_id: 'unrelated', status: 'succeeded' }))
    const wrapper = await render()
    await publish(wrapper)
    expect(wrapper.text()).toContain('结果待确认')
    expect(wrapper.text()).not.toContain('发布成功')
    await vi.advanceTimersByTimeAsync(2000)
    expect(agentApi.getDeployment).toHaveBeenCalledWith('one', vi.mocked(agentApi.pushConfig).mock.lastCall![1])
  })

  it('handles Surge synchronous success without claiming transactional service checks', async () => {
    vi.mocked(agentApi.getAll).mockResolvedValue(response([agent({ service_type: 'surge' })]))
    vi.mocked(agentApi.pushConfig).mockResolvedValueOnce(response({ success: true, restart: { success: true } }))
    const wrapper = await render({ serviceType: 'surge', save: vi.fn().mockResolvedValue(guard({ expected_service_type: 'surge' })) })
    await publish(wrapper)
    expect(wrapper.text()).toContain('草稿已保存，配置已推送。')
    expect(wrapper.text()).not.toContain('服务检查已通过')
    await vi.advanceTimersByTimeAsync(5000)
    expect(agentApi.getDeployment).not.toHaveBeenCalled()
    expect(publishButton(wrapper).attributes('disabled')).toBeUndefined()
  })

  it('keeps an uncertain Surge result separate from a confirmed restart failure', async () => {
    vi.mocked(agentApi.getAll).mockResolvedValue(response([agent({ service_type: 'surge' })]))
    vi.mocked(agentApi.pushConfig).mockResolvedValueOnce(response({ success: true, restart: { success: false, message: '启动失败' } }))
    const wrapper = await render({ serviceType: 'surge', save: vi.fn().mockResolvedValue(guard({ expected_service_type: 'surge' })) })
    await publish(wrapper)
    expect(wrapper.text()).toContain('草稿已保存，配置已写入，但重启失败')
    expect(wrapper.text()).toContain('启动失败')
    vi.mocked(agentApi.pushConfig).mockRejectedValueOnce(new Error('timeout'))
    await publish(wrapper)
    expect(wrapper.text()).toContain('发布结果待确认')
    expect(publishButton(wrapper).attributes('disabled')).toBeDefined()
    await vi.advanceTimersByTimeAsync(5000)
    expect(agentApi.getDeployment).not.toHaveBeenCalled()
  })

  it('does not continue a save after the component is unmounted', async () => {
    const saved = deferred<PublishGuard>()
    const wrapper = await render({ save: vi.fn(() => saved.promise) })
    await publish(wrapper)
    wrapper.unmount()
    wrappers.splice(wrappers.indexOf(wrapper), 1)
    saved.resolve(guard())
    await flushPromises()
    expect(agentApi.pushConfig).not.toHaveBeenCalled()
  })
})

describe('Settings publication failure details', () => {
  it.each(['mihomo', 'mosdns', 'oxidns'])('keeps %s validation and rollback diagnostics in the settings workspace', async serviceType => {
    vi.mocked(agentApi.getAll).mockResolvedValue(response([agent({ service_type: serviceType })]))
    vi.mocked(agentApi.pushConfig).mockImplementationOnce(async (_id, deploymentId) => response({ deployment_id: deploymentId, status: 'rollback_failed', failed_stage: 'verifying', error: '配置检查失败', error_details: 'line 17: unknown group', rollback_error: '恢复失败', rollback_error_details: 'failed to bind :53' }))
    const wrapper = await render({ serviceType, save: vi.fn().mockResolvedValue(guard({ expected_service_type: serviceType as any })) })
    await publish(wrapper)
    expect(wrapper.findAll('pre').map(item => item.text())).toEqual(['line 17: unknown group', 'failed to bind :53'])
    expect(wrapper.text()).toContain('失败步骤：校验中')
    expect(notify.error).toHaveBeenCalledWith('回滚失败，需要处理', '配置检查失败')
  })
  it('keeps Surge partial-success restart details without reporting the file write as failed', async () => {
    vi.mocked(agentApi.getAll).mockResolvedValue(response([agent({ service_type: 'surge' })]))
    vi.mocked(agentApi.pushConfig).mockResolvedValueOnce(response({ success: true, restart: { success: false, message: 'Surge reload failed', error_details: 'invalid policy on line 4' } }))
    const wrapper = await render({ serviceType: 'surge', save: vi.fn().mockResolvedValue(guard({ expected_service_type: 'surge' })) })
    await publish(wrapper)
    expect(wrapper.text()).toContain('配置已写入，但重启失败')
    expect(wrapper.get('pre').text()).toBe('invalid policy on line 4')
    expect(wrapper.text()).not.toContain('配置已推送')
  })
  it('preserves a pre-submission rejection and clears it when changing the editing session', async () => {
    vi.mocked(agentApi.pushConfig).mockRejectedValueOnce({ response: { status: 400, data: { message: '本地校验失败', error_details: 'line 19: invalid rule' } } })
    const wrapper = await render()
    await publish(wrapper)
    expect(wrapper.get('pre').text()).toBe('line 19: invalid rule')
    await wrapper.setProps({ open: false })
    await wrapper.setProps({ open: true })
    await flushPromises()
    expect(wrapper.find('details').exists()).toBe(false)
  })
})

it('shows server diagnostics while a 5xx publication result remains uncertain', async () => {
  vi.mocked(agentApi.pushConfig).mockRejectedValueOnce({ response: { status: 503, data: { message: '目标设备暂时无响应', error_details: 'response ended while checking startup' } } })
  const wrapper = await render()
  await publish(wrapper)
  expect(wrapper.text()).toContain('结果待确认')
  expect(wrapper.get('pre').text()).toBe('response ended while checking startup')
  expect(publishButton(wrapper).attributes('disabled')).toBeDefined()
})
