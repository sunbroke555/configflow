import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import DeviceGroups from '@/views/DeviceGroups.vue'
import DeviceGroupEditor from '@/components/device-groups/DeviceGroupEditor.vue'
import { deviceGroupsApi } from '@/api/deviceGroups'
import { activeProfileId } from '@/profileContext'
import { confirm, confirmDanger, notify } from '@/lib/feedback'
import { deviceGroupsActivity, deviceGroupsPublication, deviceGroupsReview, deviceGroupsState } from './fixtures/device-groups'
import type { DeviceGroupsDraft } from '@/types/deviceGroups'

vi.mock('@/api', () => ({ agentApi: {}, oxidnsMonitorApi: { clients: vi.fn() } }))
vi.mock('@/api/deviceGroups', () => ({ deviceGroupsApi: { get: vi.fn(), preview: vi.fn(), save: vi.fn(), activity: vi.fn(), diagnostics: vi.fn(), migrationPreview: vi.fn(), migrate: vi.fn(), publish: vi.fn(), publication: vi.fn(), retryPublication: vi.fn() } }))
vi.mock('@/stores/profile', () => ({ useProfileStore: () => ({ activeProfileId }) }))
vi.mock('@/lib/feedback', () => ({ confirm: vi.fn(), confirmDanger: vi.fn(), notify: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() } }))
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value))
const response = (data: any) => ({ data } as any)
let state = deviceGroupsState()
const wrappers: VueWrapper[] = []
const button = (w: VueWrapper, label: string) => w.findAll('button').find(item => item.text() === label)!
async function click(w: VueWrapper, label: string) { const found = button(w, label); expect(found, label).toBeDefined(); await found.trigger('click'); await flushPromises() }
async function render() {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/device-groups', component: DeviceGroups }, { path: '/agents', component: { template: '<div>Agent</div>' } }, { path: '/routers', component: { template: '<div>路由</div>' } }] })
  await router.push('/device-groups'); await router.isReady()
  const w = mount({ template: '<RouterView />' }, { attachTo: document.body, global: { plugins: [router], stubs: { Teleport: { template: '<div><slot /></div>' } } } })
  wrappers.push(w); await flushPromises(); return { w, router }
}
async function edit(w: VueWrapper, name = '新的直连设备') {
  await w.get('[aria-label="编辑设备组 直连设备"]').trigger('click'); await flushPromises()
  const editor = w.getComponent(DeviceGroupEditor)
  editor.vm.$emit('apply', { ...state.device_groups.groups[0], name }, state.device_notes)
  await flushPromises()
}
beforeEach(() => {
  vi.resetAllMocks(); activeProfileId.value = 'office'; state = deviceGroupsState()
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: true, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} })))
  Element.prototype.scrollIntoView = vi.fn()
  vi.mocked(confirm).mockResolvedValue(false); vi.mocked(confirmDanger).mockResolvedValue(false)
  vi.mocked(deviceGroupsApi.get).mockImplementation(async () => response(clone(state)))
  vi.mocked(deviceGroupsApi.activity).mockResolvedValue(response(deviceGroupsActivity()))
  vi.mocked(deviceGroupsApi.diagnostics).mockResolvedValue(response({ success: true, diagnostics: [{ checked_at: '2026-10-24T10:00:00Z', state: 'uncertain', preserves_source_ip: null, deployment: 'selective_gateway', message: '尚未确认来源保留', warnings: [{ code: 'literal', message: '直接访问 IP 字面量可能不进入 Mihomo。' }] }] }))
  vi.mocked(deviceGroupsApi.preview).mockImplementation(async (_profile, draft) => response({ ...deviceGroupsReview(state), draft: clone(draft) }))
  vi.mocked(deviceGroupsApi.save).mockImplementation(async (_profile, draft) => { state = { ...state, ...clone(draft), revision: 'r2', config_revision: 11 }; return response(clone(state)) })
  vi.mocked(deviceGroupsApi.migrationPreview).mockImplementation(async () => response(deviceGroupsReview(state)))
  vi.mocked(deviceGroupsApi.migrate).mockImplementation(async () => { state = { ...state, migrated: true, migration: { available: false }, revision: 'r2', config_revision: 11 }; return response(clone(state)) })
  vi.mocked(deviceGroupsApi.publish).mockImplementation(async (_profile, batch) => response({ success: true, publication: { ...deviceGroupsPublication(), batch_id: batch } }))
  vi.mocked(deviceGroupsApi.publication).mockImplementation(async (_profile, batch) => response({ success: true, publication: { ...deviceGroupsPublication(), batch_id: batch } }))
  vi.mocked(deviceGroupsApi.retryPublication).mockResolvedValue(response({ success: true, publication: deviceGroupsPublication() }))
})
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); document.body.innerHTML = ''; vi.unstubAllGlobals() })

describe('device groups workspace', () => {
  it('keeps the default group last and reports bounded samples without calling them proof of effect', async () => {
    const { w } = await render()
    expect(w.findAll('[data-device-group]').map(row => row.attributes('data-device-group'))).toEqual(['dg_direct', 'dg_filter', 'default'])
    expect(w.text()).toContain('38.0%'); expect(w.text()).toContain('部分 Agent 的记录不可用')
    expect(w.text()).toContain('OxiDNS 2：Agent 离线'); expect(w.text()).not.toContain('未生效')
    expect(deviceGroupsApi.save).not.toHaveBeenCalled(); expect(deviceGroupsApi.migrate).not.toHaveBeenCalled()
  })
  it('previews actual draft changes before saving and retains input on a revision conflict', async () => {
    const { w, router } = await render(); await edit(w)
    await router.push('/agents'); expect(router.currentRoute.value.path).toBe('/device-groups')
    await click(w, '预览并保存草稿')
    expect(deviceGroupsApi.preview).toHaveBeenCalledWith('office', expect.objectContaining({ device_groups: expect.any(Object) }), 'r1', expect.any(AbortSignal))
    expect(deviceGroupsApi.save).not.toHaveBeenCalled()
    vi.mocked(deviceGroupsApi.save).mockRejectedValueOnce({ response: { status: 409, data: { message: '配置已改变，请重新预览。' } } })
    await click(w, '确认保存草稿')
    expect(w.text()).toContain('配置已改变'); expect(w.text()).toContain('新的直连设备')
    expect(deviceGroupsApi.publish).not.toHaveBeenCalled()
  })
  it('uses the saved revision to obtain a publication token and starts only the batch API', async () => {
    const { w } = await render(); await edit(w); await click(w, '保存并发布'); await click(w, '确认保存并发布')
    expect(deviceGroupsApi.save).toHaveBeenCalledWith('office', expect.any(Object), 'r1', 'preview-r1')
    expect(deviceGroupsApi.preview).toHaveBeenLastCalledWith('office', expect.any(Object), 'r2', expect.any(AbortSignal))
    expect(deviceGroupsApi.publish).toHaveBeenCalledWith('office', expect.any(String), 'preview-r2', ['m1', 'o1'])
    expect(w.text()).toContain('Mihomo 待激活'); expect(w.text()).toContain('等待发布')
    await click(w, '应用并启动 Mihomo 1')
    expect(deviceGroupsApi.retryPublication).toHaveBeenCalledWith('office', expect.any(String), 'activate', 'm1')
  })
  it('does not publish when target identity changes between review and saved preview', async () => {
    const { w } = await render(); await edit(w); await click(w, '保存并发布')
    vi.mocked(deviceGroupsApi.save).mockImplementationOnce(async (_profile, draft) => { state = { ...state, ...clone(draft), revision: 'r2', config_revision: 11 }; state.affected_agents[0].target_fingerprint = 'changed'; return response(clone(state)) })
    await click(w, '确认保存并发布')
    expect(deviceGroupsApi.publish).not.toHaveBeenCalled(); expect(w.text()).toContain('配置或发布目标已变化')
    expect(notify.success).toHaveBeenCalled()
  })
  it('recovers a lost publication create response using the same preassigned batch identity', async () => {
    vi.mocked(deviceGroupsApi.publish).mockRejectedValueOnce(new Error('网络中断'))
    const { w } = await render(); await click(w, '发布全部受影响 Agent'); await click(w, '确认保存并发布')
    const request = vi.mocked(deviceGroupsApi.publish).mock.lastCall!
    expect(deviceGroupsApi.publication).toHaveBeenCalledWith('office', request[1], expect.any(AbortSignal))
    expect(deviceGroupsApi.publish).toHaveBeenCalledOnce(); expect(w.text()).toContain('Mihomo 待激活')
  })
  it('retries an unrecorded registration with the same batch identity, preview token and targets', async () => {
    vi.mocked(deviceGroupsApi.publish).mockRejectedValueOnce(new Error('请求超时'))
    vi.mocked(deviceGroupsApi.publication).mockRejectedValueOnce({ response: { status: 404, data: { message: '暂未登记此批次' } } })
    const { w, router } = await render(); await click(w, '发布全部受影响 Agent'); await click(w, '确认保存并发布')
    const original = [...vi.mocked(deviceGroupsApi.publish).mock.lastCall!]
    await router.push('/agents'); expect(router.currentRoute.value.path).toBe('/device-groups')
    const beforeUnload = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(beforeUnload); expect(beforeUnload.defaultPrevented).toBe(true)
    await click(w, '重试发布登记')
    expect(vi.mocked(deviceGroupsApi.publish).mock.lastCall).toEqual(original)
    expect(w.text()).toContain('Mihomo 待激活')
    await router.push('/agents'); expect(router.currentRoute.value.path).toBe('/agents')
  })
  it('does not poll a registration that the server definitively rejected', async () => {
    vi.mocked(deviceGroupsApi.publish).mockRejectedValueOnce({ response: { status: 409, data: { message: '发布目标已改变，请重新预览' } } })
    const { w } = await render(); await click(w, '发布全部受影响 Agent'); await click(w, '确认保存并发布')
    expect(deviceGroupsApi.publication).not.toHaveBeenCalled()
    expect(w.text()).toContain('发布目标已改变，请重新预览')
    expect(button(w, '发布全部受影响 Agent').attributes('disabled')).toBeUndefined()
  })
  it('requires an explicit migration review and keeps native post-upgrade blocking advice hidden', async () => {
    state.migrated = false; state.migration = { available: true }; state.capabilities = { oxidns_mode: 'native', keep_blocking_editable: false }
    const { w } = await render()
    expect(button(w, '新建设备组').attributes('disabled')).toBeDefined()
    await click(w, '升级为设备组')
    expect(deviceGroupsApi.migrate).not.toHaveBeenCalled(); expect(w.text()).toContain('将创建的设备组')
    await click(w, '升级并保存草稿')
    expect(deviceGroupsApi.migrate).toHaveBeenCalledWith('office', 'r1', 'preview-r1')
    expect(notify.info).not.toHaveBeenCalled(); expect(deviceGroupsApi.publish).not.toHaveBeenCalled()
  })
  it('warns about excluding-group deletion and retains the group when confirmation is declined', async () => {
    const { w } = await render()
    await w.get('[aria-label="删除设备组 直连设备"]').trigger('click'); await flushPromises()
    expect(confirmDanger).toHaveBeenCalledWith(expect.stringContaining('成员可能重新进入策略路由'), expect.any(Object))
    expect(w.find('[data-device-group=dg_direct]').exists()).toBe(true)
    vi.mocked(confirmDanger).mockResolvedValueOnce(true)
    await w.get('[aria-label="删除设备组 直连设备"]').trigger('click'); await flushPromises()
    await click(w, '预览并保存草稿')
    const draft = vi.mocked(deviceGroupsApi.preview).mock.lastCall![1] as DeviceGroupsDraft
    expect(draft.policy_routing_sources.exclude_groups).toEqual([])
  })
  it('shows unavailable recorder state without claiming a device failed and loads diagnostics on request', async () => {
    vi.mocked(deviceGroupsApi.activity).mockResolvedValueOnce(response({ ...deviceGroupsActivity(), sample_size: 0, clients: [], infrastructure_samples: [], agents: [{ id: 'o1', name: 'OxiDNS 1', state: 'recorder_disabled' }] }))
    const { w } = await render(); expect(w.text()).toContain('无查询记录，不据此判断设备组是否生效')
    expect(deviceGroupsApi.diagnostics).not.toHaveBeenCalled()
    await click(w, '检查客户端 IP'); expect(w.text()).toContain('直接访问 IP 字面量可能不进入 Mihomo')
  })
})
