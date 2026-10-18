import { canReloadPage } from '@/lib/pageReloadGuard'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import PolicyRoutingPanel from '@/components/routers/PolicyRoutingPanel.vue'
import { agentApi, routerApi, oxidnsMonitorApi } from '@/api'
import { confirm } from '@/lib/feedback'
import { scopedRequests } from '@/profileContext'
vi.mock('@/api', () => ({
  routerApi: { policySettings: vi.fn(), savePolicySettings: vi.fn(), previewPolicySettings: vi.fn(), apply: vi.fn(), policy: vi.fn() },
  agentApi: { pushConfig: vi.fn(), getDeployment: vi.fn(), activateDeployment: vi.fn() }, oxidnsMonitorApi: { overview: vi.fn(), clients: vi.fn() }
}))
vi.mock('@/lib/feedback', () => ({ confirm: vi.fn(), notify: { success: vi.fn(), error: vi.fn(), info: vi.fn() } }))
const clone = <T,>(v: T): T => JSON.parse(JSON.stringify(v))
const initial = () => ({ profile_id: 'office', profile_name: '家庭', revision: 'rev-1', oxidns_revision: 'dns-1',
  router: { id: 'ros', name: 'ROS', ip_rule_sets: ['static'], ros_policy_rule_sets: ['proxy'], proxy_gateway_agent_id: 'mihomo' },
  sources: { editable: true, reason: '', policies: [{ ip: '192.0.2.105', name: 'NAS', mode: 'smart' as const }] },
  source_agents: [{ id: 'dns1', name: 'DNS one', host: '192.0.2.2', status: 'online' }, { id: 'dns2', name: 'DNS two', host: '192.0.2.3', status: 'online' }],
  gateways: [{ id: 'mihomo', name: 'mihomo', host: '192.0.2.4' }], rule_sets: [{ id: 'static', name: 'Static', policy: '代理' }, { id: 'proxy', name: 'Telegram', policy: '代理' }] })
let state = initial()
const wrappers: VueWrapper[] = []
beforeEach(() => {
  state = initial(); scopedRequests.value = 0
  vi.mocked(oxidnsMonitorApi.overview).mockResolvedValue({ data: { recorders: ['query_recorder'] } } as any)
  vi.mocked(confirm).mockResolvedValue(false)
  vi.mocked(routerApi.policySettings).mockImplementation(async () => ({ data: { success: true, workspace: clone(state) } }) as any)
  vi.mocked(routerApi.savePolicySettings).mockImplementation(async (_id, body) => {
    state = { ...state, revision: 'rev-2', oxidns_revision: 'dns-2', sources: { ...state.sources, policies: clone(body.policies) as any }, router: { ...state.router, ...body.router } }
    return { data: { success: true, workspace: clone(state) } } as any
  })
  vi.mocked(routerApi.previewPolicySettings).mockImplementation(async (_id, revision, targets) => ({ data: { success: true, preview: {
    revision, profile_id: 'office', config_revision: 42, oxidns_revision: state.oxidns_revision,
    targets: state.source_agents.filter(agent => targets.includes(agent.id)), dns_content: targets.length ? 'plugins: []' : '',
    router_plan: { fingerprint: 'ros-plan', empty: false, blockers: [], warnings: [], stages: [{ key: 'policy', title: '策略路由', ops: [{ summary: '更新来源与目的列表' }] }] }
  } } }) as any)
  vi.mocked(agentApi.pushConfig).mockImplementation(async (_id, deploymentId) => ({ data: { deployment_id: deploymentId, status: 'succeeded' } }) as any)
  vi.mocked(routerApi.apply).mockResolvedValue({ data: { success: true, result: { ok: true, log: [], rollback: [] } } } as any)
})
afterEach(async () => { wrappers.splice(0).forEach(w => w.unmount()); await flushPromises(); vi.useRealTimers(); vi.clearAllMocks() })
async function setup() {
  const wrapper = mount(PolicyRoutingPanel, { props: { id: 'ros', name: 'ROS', profile: 'office' } })
  wrappers.push(wrapper); await flushPromises(); return wrapper
}
async function click(w: VueWrapper, text: string) {
  const button = w.findAll('button').find(b => b.text() === text)!
  expect(button, text).toBeDefined()
  if (button.attributes('role') === 'tab') await button.trigger('mousedown', { button: 0 })
  await button.trigger('click'); await flushPromises()
}

describe('one-place DNS and ROS workflow', () => {

  it('preserves a source address still being entered before it reaches the saved draft', async () => {
    const w = await setup()
    expect(canReloadPage()).toBe(true)
    const input = w.get<HTMLInputElement>('#policy-source-ip')
    await input.setValue('192.0.2.')
    expect(canReloadPage()).toBe(false)
    expect(routerApi.savePolicySettings).not.toHaveBeenCalled()
    expect(input.element.value).toBe('192.0.2.')
    await input.setValue('')
    expect(canReloadPage()).toBe(true)
  })


  it('edits an existing source through the dialog and retains routing choices until explicit publish', async () => {
    const w = await setup()
    await w.get('[aria-label="编辑 192.0.2.105"]').trigger('click'); await flushPromises()
    const editor = w.findComponent({ name: 'PolicySourceEditor' })
    expect(editor.props('policy')).toEqual(initial().sources.policies[0])
    expect(w.findAll('button').find(b => b.text() === '保存并预览')?.attributes('disabled')).toBeDefined()
    editor.vm.$emit('apply', { ip: '192.0.2.101', name: '本机', mode: 'smart' }); await flushPromises()
    expect(routerApi.savePolicySettings).not.toHaveBeenCalled()
    await click(w, '保存并预览')
    expect(routerApi.savePolicySettings).toHaveBeenCalledWith('ros', expect.objectContaining({
      policies: [{ ip: '192.0.2.101', name: '本机', mode: 'smart' }], router: {
        ip_rule_sets: ['static'], ros_policy_rule_sets: ['proxy'], proxy_gateway_agent_id: 'mihomo'
      }
    }), 'office')
    expect(routerApi.apply).not.toHaveBeenCalled()
  })
  it('clears an old preview after rule content changes without auto-selecting a cancelled policy', async () => {
    state.router.ros_policy_rule_sets = []
    const w = await setup()
    await click(w, '保存并预览')
    expect(w.text()).toContain('确认发布')
    await click(w, '规则集与网关'); await click(w, '编辑规则内容')
    w.findComponent({ name: 'PolicyDestinationEditor' }).vm.$emit('saved'); await flushPromises()
    await click(w, '发布预览')
    expect(w.findAll('button').some(button => button.text() === '确认发布')).toBe(false)
    await click(w, '保存并预览')
    expect(routerApi.savePolicySettings).not.toHaveBeenCalled()
    expect(state.router.ros_policy_rule_sets).toEqual([])
    expect(routerApi.apply).not.toHaveBeenCalled()
  })
  it('saves sources and routing together, previews, and only publishes after explicit confirmation', async () => {
    const w = await setup()
    await w.get('#policy-source-ip').setValue('192.0.2.101')
    await click(w, '添加')
    await click(w, '保存并预览')
    expect(routerApi.savePolicySettings).toHaveBeenCalledWith('ros', expect.objectContaining({
      revision: 'rev-1', policies: [initial().sources.policies[0], { ip: '192.0.2.101', name: '', mode: 'smart' }],
      router: { ip_rule_sets: ['static'], ros_policy_rule_sets: ['proxy'], proxy_gateway_agent_id: 'mihomo' }
    }), 'office')
    expect(w.text()).toContain('新增来源 192.0.2.101')
    expect(agentApi.pushConfig).not.toHaveBeenCalled(); expect(routerApi.apply).not.toHaveBeenCalled()
    await click(w, '确认发布')
    expect(agentApi.pushConfig).toHaveBeenCalledTimes(2)
    expect(agentApi.pushConfig).toHaveBeenNthCalledWith(1, 'dns1', expect.any(String), { expected_profile_id: 'office', expected_oxidns_revision: 'dns-2', expected_config_revision: 42 })
    expect(routerApi.apply).toHaveBeenCalledWith('ros', 'ros-plan', 'office')
    expect(routerApi.apply.mock.invocationCallOrder[0]).toBeGreaterThan(agentApi.pushConfig.mock.invocationCallOrder[1]!)
    expect(scopedRequests.value).toBe(0)
  })
  it('prepares the visible client picker without eagerly scanning query records', async () => {
    const w = await setup()
    expect(oxidnsMonitorApi.overview).toHaveBeenCalledWith('dns1')
    expect(oxidnsMonitorApi.clients).not.toHaveBeenCalled()
    const picker = w.findComponent({ name: 'OxiDNSClientSelect' })
    expect(picker.props('recorder')).toBe('query_recorder')
    expect(picker.element.closest('details')).toBeNull()
    picker.vm.$emit('update:modelValue', '192.0.2.106'); await flushPromises()
    await click(w, '保存并预览')
    expect(routerApi.savePolicySettings).toHaveBeenCalledWith('ros', expect.objectContaining({
      policies: expect.arrayContaining([{ ip: '192.0.2.106', name: '', mode: 'smart' }])
    }), 'office')
  })
  it('does not prepare a preview after a rejected atomic save and retains edited input', async () => {
    const w = await setup()
    await w.get('[aria-label="192.0.2.105 备注"]').setValue('changed')
    vi.mocked(routerApi.savePolicySettings).mockRejectedValueOnce(new Error('配置已更新'))
    await click(w, '保存并预览')
    expect(w.text()).toContain('配置已更新')
    expect(w.get('[aria-label="192.0.2.105 备注"]').element.value).toBe('changed')
    expect(routerApi.previewPolicySettings).not.toHaveBeenCalled()
  })
  it('invalidates the preview when targets or settings change', async () => {
    const w = await setup(); await click(w, '保存并预览')
    await w.find('input[type="checkbox"]').setValue(false)
    expect(w.findAll('button').some(b => b.text() === '确认发布')).toBe(false)
    expect(agentApi.pushConfig).not.toHaveBeenCalled()
  })
  it('stops after a failed DNS release and never writes the next target or ROS', async () => {
    const w = await setup(); await click(w, '保存并预览')
    vi.mocked(agentApi.pushConfig).mockImplementationOnce(async (_id, deploymentId) => ({ data: { deployment_id: deploymentId, status: 'rolled_back', error: '核心检查失败，已回滚' } }) as any)
    await click(w, '确认发布')
    expect(agentApi.pushConfig).toHaveBeenCalledTimes(1)
    expect(routerApi.apply).not.toHaveBeenCalled()
    expect(w.text()).toContain('后续设备及 ROS 已暂停')
  })
  it('rejects a changed binding before making any external writes', async () => {
    const w = await setup(); await click(w, '保存并预览')
    state.source_agents = []
    await click(w, '确认发布')
    expect(agentApi.pushConfig).not.toHaveBeenCalled(); expect(routerApi.apply).not.toHaveBeenCalled()
    expect(w.text()).toContain('发布目标已变化')
  })
  it('polls a lost DNS response by the same deployment id without resubmitting', async () => {
    const w = await setup(); await click(w, '保存并预览'); vi.useFakeTimers()
    vi.mocked(agentApi.pushConfig).mockRejectedValueOnce(new Error('network timeout'))
    vi.mocked(agentApi.getDeployment).mockImplementation(async (_id, id) => ({ data: { deployment_id: id, status: 'succeeded' } }) as any)
    await click(w, '确认发布')
    expect(agentApi.pushConfig).toHaveBeenCalledTimes(1); expect(routerApi.apply).not.toHaveBeenCalled()
    const originalId = vi.mocked(agentApi.pushConfig).mock.calls[0]![1]
    await vi.advanceTimersByTimeAsync(2000); await flushPromises()
    expect(agentApi.getDeployment).toHaveBeenCalledWith('dns1', originalId)
    expect(agentApi.pushConfig).toHaveBeenCalledTimes(2)
    expect(routerApi.apply).toHaveBeenCalledOnce()
  })
  it('activates a verified candidate once and waits for success before ROS', async () => {
    const w = await setup(); await click(w, '保存并预览'); vi.useFakeTimers()
    vi.mocked(agentApi.pushConfig).mockImplementationOnce(async (_id, id) => ({ data: { deployment_id: id, status: 'ready' } }) as any)
    vi.mocked(agentApi.activateDeployment).mockRejectedValueOnce(new Error('network timeout'))
    vi.mocked(agentApi.getDeployment).mockImplementation(async (_id, id) => ({ data: { deployment_id: id, status: 'succeeded' } }) as any)
    await click(w, '确认发布')
    expect(agentApi.activateDeployment).toHaveBeenCalledOnce(); expect(routerApi.apply).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(2000); await flushPromises()
    expect(agentApi.activateDeployment).toHaveBeenCalledOnce(); expect(routerApi.apply).toHaveBeenCalledOnce()
  })
  it('refreshes the saved ownership revision after success without clearing results', async () => {
    const w = await setup(); await click(w, '保存并预览')
    vi.mocked(routerApi.apply).mockImplementationOnce(async () => {
      state.revision = 'published-revision'
      return { data: { success: true, result: { ok: true } } } as any
    })
    await click(w, '确认发布')
    expect(w.text()).toContain('发布结果')
    await click(w, '来源设备'); await w.get('[aria-label="192.0.2.105 备注"]').setValue('new name')
    await click(w, '保存并预览')
    expect(routerApi.savePolicySettings).toHaveBeenCalledWith('ros', expect.objectContaining({ revision: 'published-revision' }), 'office')
  })
  it('can publish only ROS when DNS targets are explicitly cleared', async () => {
    const w = await setup(); await click(w, '发布预览')
    for (const input of w.findAll('input[type="checkbox"]')) await input.setValue(false)
    await click(w, '保存并预览'); await click(w, '确认发布')
    expect(agentApi.pushConfig).not.toHaveBeenCalled(); expect(routerApi.apply).toHaveBeenCalledOnce()
  })
  it('rechecks an empty ROS preview after DNS publishing and preserves success for earlier targets', async () => {
    const preview = vi.mocked(routerApi.previewPolicySettings).getMockImplementation()!
    vi.mocked(routerApi.previewPolicySettings).mockImplementation(async (...args) => {
      const result = await preview(...args)
      result.data.preview.router_plan.empty = true
      result.data.preview.router_plan.stages = []
      return result
    })
    const w = await setup(); await click(w, '保存并预览')
    vi.mocked(routerApi.apply).mockRejectedValueOnce({ response: { status: 409, data: { message: '设备配置已变化，请重新预览' } } })
    await click(w, '确认发布')
    expect(agentApi.pushConfig).toHaveBeenCalledTimes(2)
    expect(routerApi.apply).toHaveBeenCalledWith('ros', 'ros-plan', 'office')
    expect(routerApi.apply.mock.invocationCallOrder[0]).toBeGreaterThan(agentApi.pushConfig.mock.invocationCallOrder[1]!)
    expect(w.text()).toContain('设备配置已变化，请重新预览')
    expect(w.text()).toContain('已完成的 OxiDNS 发布结果保留')
    const rows = w.get('[aria-label="发布结果"]').findAll('li')
    expect(rows[0]!.text()).toContain('成功')
    expect(rows[1]!.text()).toContain('成功')
    expect(rows[2]!.text()).toContain('失败')
  })
  it('keeps unsaved work when panel collapse is cancelled', async () => {
    const w = await setup(); await w.get('[aria-label="192.0.2.105 备注"]').setValue('changed')
    await click(w, '收起面板')
    expect(confirm).toHaveBeenCalled(); expect(w.emitted('close')).toBeUndefined()
  })
  it.each(['unmount', 'profile round trip'])('does not preview after a pending save and %s', async change => {
    let finish!: (value: any) => void
    const w = await setup()
    await w.get('[aria-label="192.0.2.105 备注"]').setValue('changed')
    vi.mocked(routerApi.savePolicySettings).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }) as any)
    await click(w, '保存并预览')
    if (change === 'unmount') w.unmount()
    else {
      await w.setProps({ profile: 'home' }); await flushPromises()
      await w.setProps({ profile: 'office' }); await flushPromises()
    }
    finish({ data: { success: true, workspace: initial() } }); await flushPromises()
    expect(routerApi.previewPolicySettings).not.toHaveBeenCalled()
    if (change !== 'unmount') {
      expect(w.findAll('button').find(button => button.text() === '保存并预览')!.attributes('disabled')).toBeUndefined()
    }
  })
  it('ignores a stale initial response after switching profile', async () => {
    let resolve!: (value: any) => void
    vi.mocked(routerApi.policySettings).mockImplementationOnce(() => new Promise(r => { resolve = r }) as any)
    const w = await setup(); state.profile_id = 'home'; state.profile_name = 'New profile'
    await w.setProps({ profile: 'home' }); await flushPromises()
    resolve({ data: { success: true, workspace: initial() } }); await flushPromises()
    expect(w.text()).toContain('New profile'); expect(w.text()).not.toContain('家庭 · 共用来源设备')
  })
})
