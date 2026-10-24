import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, RouterLinkStub, type VueWrapper } from '@vue/test-utils'
import DeviceGroupEditor from '@/components/device-groups/DeviceGroupEditor.vue'
import DeviceGroupsPreview from '@/components/device-groups/DeviceGroupsPreview.vue'
import DeviceGroupLookup from '@/components/device-groups/DeviceGroupLookup.vue'
import DeviceGroupsPublication from '@/components/device-groups/DeviceGroupsPublication.vue'
import OxiDNSClientSelect from '@/components/agents/OxiDNSClientSelect.vue'
import Segmented from '@/components/common/Segmented.vue'
import { Select } from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { deviceGroupsApi } from '@/api/deviceGroups'
import { oxidnsMonitorApi } from '@/api'
import { confirm } from '@/lib/feedback'
import { memberSample } from '@/lib/deviceGroupActivity'
import { deviceGroupsActivity, deviceGroupsPublication, deviceGroupsReview, deviceGroupsState } from './fixtures/device-groups'

vi.mock('@/api', () => ({ agentApi: {}, oxidnsMonitorApi: { clients: vi.fn() } }))
vi.mock('@/api/deviceGroups', () => ({ deviceGroupsApi: { lookup: vi.fn() } }))
vi.mock('@/lib/feedback', () => ({ confirm: vi.fn(), notify: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() } }))
const wrappers: VueWrapper[] = []
const global = { stubs: { Teleport: { template: '<div><slot /></div>' }, RouterLink: RouterLinkStub } }
const button = (wrapper: VueWrapper, label: string) => wrapper.findAll('button').find(item => item.text() === label)!
function render(component: any, props: Record<string, unknown>) {
  const wrapper = mount(component, { props, global, attachTo: document.body }); wrappers.push(wrapper); return wrapper
}
beforeEach(() => {
  vi.resetAllMocks()
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: true, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} })))
  Element.prototype.scrollIntoView = vi.fn()
  vi.mocked(confirm).mockResolvedValue(false)
})
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); document.body.innerHTML = ''; vi.unstubAllGlobals() })

describe('device group editor', () => {
  it('normalizes members, prevents cross-group duplicates and applies shared notes without writing', async () => {
    const state = deviceGroupsState()
    const w = render(DeviceGroupEditor, { group: state.device_groups.groups[0], snapshot: state, notes: state.device_notes, activity: deviceGroupsActivity() })
    await w.get('#device-group-member').setValue('192.0.2.60'); await button(w, '添加').trigger('click')
    expect(w.text()).toContain('已在「加强过滤」中')
    await w.get('#device-group-member').setValue('2001:0db8::1/64'); await button(w, '添加').trigger('click')
    expect(w.text()).toContain('2001:db8::/64')
    expect(w.text()).toContain('IPv6 路径需单独核查')
    expect(w.text()).toContain('样本内 100 次查询 · 2 个客户端')
    const input = w.findAll('input').find(item => item.attributes('aria-label') === '2001:db8::/64 设备备注')!
    await input.setValue('  测试网段  '); await w.get('form').trigger('submit')
    expect(w.emitted('apply')?.[0]).toEqual([expect.objectContaining({ members: ['192.0.2.0/25', '2001:db8::/64'] }), expect.objectContaining({ '2001:db8::/64': '测试网段', '192.0.2.60': '客厅设备' })])
  })
  it('requires an enabled policy group for proxy mode and locks native blocking visibly', async () => {
    const state = deviceGroupsState(); state.capabilities = { oxidns_mode: 'native', keep_blocking_editable: false, keep_blocking_reason: '完整参数中的规则不会自动更新副本。' }
    const w = render(DeviceGroupEditor, { group: state.device_groups.groups[0], snapshot: state, notes: {} })
    w.getComponent(Segmented).vm.$emit('update:modelValue', 'proxy'); await flushPromises()
    expect(w.getComponent(Switch).props('disabled')).toBe(true)
    expect(w.text()).toContain('完整参数中的规则不会自动更新副本')
    await w.get('form').trigger('submit'); expect(w.text()).toContain('请选择一个已启用的策略组')
    w.getComponent(Select).vm.$emit('update:modelValue', '节点选择'); await w.get('form').trigger('submit')
    expect(w.emitted('apply')?.[0][0]).toMatchObject({ mode: 'proxy', policy: '节点选择' })
    expect(w.emitted('apply')?.[0][0]).not.toHaveProperty('dns')
  })
  it('keeps default exceptions free of mode controls and protects unadded member input', async () => {
    const state = deviceGroupsState()
    const w = render(DeviceGroupEditor, { group: state.device_groups.groups[2], snapshot: state, notes: {} })
    expect(w.findComponent(Segmented).exists()).toBe(false)
    await w.get('#device-group-member').setValue('192.0.2.')
    await button(w, '取消').trigger('click'); await flushPromises()
    expect(confirm).toHaveBeenCalledOnce(); expect(w.emitted('close')).toBeUndefined()
    await w.get('form').trigger('submit'); expect(w.text()).toContain('请先添加正在输入的成员')
  })
  it('uses aggregated clients without contacting a single Agent', async () => {
    const w = render(OxiDNSClientSelect, { modelValue: '', agentId: '', recorder: '', range: 'all', externalClients: deviceGroupsActivity().clients, externalAvailable: true, allowAll: false })
    await w.get('button[role=combobox]').trigger('click'); await flushPromises()
    expect(oxidnsMonitorApi.clients).not.toHaveBeenCalled(); expect(w.text()).toContain('样本内 62 次')
  })
})

describe('device group review, lookup and publication', () => {
  it('shows actual generated differences, route exits, blocking precedence and all affected Agents before saving', async () => {
    const w = render(DeviceGroupsPreview, { preview: deviceGroupsReview(), migration: true, confirmLabel: '升级并保存草稿', busy: false })
    expect(w.text()).toContain('将创建的设备组'); expect(w.text()).toContain('将不再经过 RouterOS 策略路由')
    expect(w.text()).toContain('拦截优先'); expect(w.text()).toContain('MATCH 后拦截')
    expect(w.text()).toContain('Mihomo 1'); expect(w.text()).toContain('OxiDNS 1')
    expect(w.text()).toContain('cf_dg_dispatch'); expect(w.emitted('confirm')).toBeUndefined()
    expect(w.text()).toContain('查询样本中将离开策略路由的客户端：192.0.2.60')
    expect(w.text()).toContain('样本不代表全部受影响设备')
    await button(w, '升级并保存草稿').trigger('click'); expect(w.emitted('confirm')).toHaveLength(1)
  })
  it('shows per-router migration sources collapsed and preserves each actual source entry in order', async () => {
    const preview = deviceGroupsReview()
    preview.migration = {
      sources_before: { router_a: ['192.0.2.128/25', '192.0.2.0/25'], router_empty: [] },
      sources_after: { router_a: ['192.0.2.128/25', '192.0.2.0/25'], router_empty: [] },
    }
    const w = render(DeviceGroupsPreview, { preview, migration: true, confirmLabel: '升级并保存草稿', busy: false })
    const details = w.get('[data-migration-sources]')
    expect(details.attributes('open')).toBeUndefined()
    expect(details.get('summary').text()).toContain('2 台 RouterOS')
    const source = w.get('[data-router-source-comparison=router_a]')
    for (const phase of ['before', 'after']) {
      expect(source.get(`[data-source-phase=${phase}]`).findAll('li').map(item => item.text())).toEqual(['192.0.2.128/25', '192.0.2.0/25'])
    }
    const empty = w.get('[data-router-source-comparison=router_empty]')
    expect(empty.text()).toContain('升级前没有有效来源'); expect(empty.text()).toContain('升级后没有有效来源')
    expect(w.emitted('confirm')).toBeUndefined()
  })
  it('shows top-level infrastructure samples and hides absent migration source data', () => {
    const preview = deviceGroupsReview()
    preview.routing_changes.affected_clients = []
    preview.infrastructure_samples = [{ ip: '192.0.2.1', count: 38, share: .38 }]
    const w = render(DeviceGroupsPreview, { preview, migration: true, confirmLabel: '升级并保存草稿', busy: false })
    expect(w.text()).toContain('基础设施 192.0.2.1：样本内 38 次查询，占 38.0%')
    expect(w.find('[data-migration-sources]').exists()).toBe(false)
    expect(w.text()).not.toContain('查询样本中将离开策略路由的客户端')
  })
  it('returns server ownership and infrastructure exclusions for an IP without inventing rules', async () => {
    vi.mocked(deviceGroupsApi.lookup).mockResolvedValue({ data: { success: true, ip: '192.0.2.1', group_id: 'default', group_name: '默认组', mode: 'follow', matched_member: null, overridden_members: [{ member: '192.0.2.0/25', group_name: '直连设备' }], infrastructure_exclusion: '这是已登记的路由器地址', policy_routing_included: false } } as any)
    const w = render(DeviceGroupLookup, { profileId: 'office' })
    await w.get('input').setValue('192.0.2.1'); await w.get('form').trigger('submit'); await flushPromises()
    expect(deviceGroupsApi.lookup).toHaveBeenCalledWith('office', '192.0.2.1', expect.any(AbortSignal))
    expect(w.text()).toContain('这是已登记的路由器地址'); expect(w.text()).toContain('不在有效来源内')
  })
  it('keeps unsubmitted targets queued and sends activation through the batch coordinator', async () => {
    const w = render(DeviceGroupsPublication, { publication: deviceGroupsPublication() })
    const queued = w.get('[data-group-publish-target=o1]')
    expect(queued.text()).toContain('等待发布'); expect(queued.find('[data-agent-operation-steps]').exists()).toBe(false)
    await button(w, '应用并启动 Mihomo 1').trigger('click')
    expect(w.emitted('action')?.[0]).toEqual(['activate', 'm1'])
  })
  it('counts CIDR sample clients without treating this as longest-prefix ownership', () => {
    expect(memberSample('192.0.2.0/25', deviceGroupsActivity().clients)).toEqual({ clients: 2, queries: 100 })
    expect(memberSample('2001:db8::/64', [{ ip: '2001:db8::2', count: 4 }, { ip: '2001:db9::2', count: 8 }])).toEqual({ clients: 1, queries: 4 })
  })
})
