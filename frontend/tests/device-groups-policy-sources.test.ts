import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, RouterLinkStub, type VueWrapper } from '@vue/test-utils'
import PolicyRoutingPanel from '@/components/routers/PolicyRoutingPanel.vue'
import PolicySourceEditor from '@/components/routers/PolicySourceEditor.vue'
import MultiSelect from '@/components/common/MultiSelect.vue'
import OxiDNSDevicePolicies from '@/components/agents/OxiDNSDevicePolicies.vue'
import { agentApi, oxidnsDevicePolicyApi, oxidnsMonitorApi, routerApi } from '@/api'
import { confirm } from '@/lib/feedback'
import { activeProfileId, scopedRequests } from '@/profileContext'

vi.mock('@/api', () => ({
  routerApi: { policySettings: vi.fn(), savePolicySettings: vi.fn(), previewPolicySettings: vi.fn(), apply: vi.fn(), policy: vi.fn() },
  agentApi: { pushConfig: vi.fn(), getDeployment: vi.fn(), activateDeployment: vi.fn() },
  oxidnsMonitorApi: { overview: vi.fn(), clients: vi.fn() },
  oxidnsDevicePolicyApi: { get: vi.fn(), save: vi.fn() },
}))
vi.mock('@/lib/feedback', () => ({ confirm: vi.fn(), notify: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() } }))
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value))
const response = (data: any) => ({ data } as any)
const initial = () => ({ profile_id: 'office', profile_name: '办公室', revision: 'r1', oxidns_revision: 'dns1',
  router: { id: 'ros', name: 'ROS', ip_rule_sets: [], ros_policy_rule_sets: [], proxy_gateway_agent_id: 'm1' },
  sources: { editable: true, reason: '', policies: [], kind: 'device_groups', policy_routing_sources: { include: ['192.0.2.0/24'], exclude_groups: ['dg_direct'] }, groups: [{ id: 'dg_direct', name: '直连设备' }, { id: 'dg_filter', name: '加强过滤' }, { id: 'default', name: '默认组' }] },
  source_agents: [{ id: 'o1', name: 'OxiDNS 1', host: '192.0.2.2', status: 'online' }], gateways: [{ id: 'm1', name: 'Mihomo 1', host: '192.0.2.3' }], rule_sets: [],
})
let state = initial()
const wrappers: VueWrapper[] = []
const button = (w: VueWrapper, label: string) => w.findAll('button').find(item => item.text() === label)!
async function click(w: VueWrapper, label: string) { const found = button(w, label); expect(found, label).toBeDefined(); await found.trigger('click'); await flushPromises() }
function render(component: any, props: any) { const w = mount(component, { props, attachTo: document.body, global: { stubs: { RouterLink: RouterLinkStub } } }); wrappers.push(w); return w }
beforeEach(() => {
  vi.resetAllMocks(); state = initial(); scopedRequests.value = 0
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: true, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} })))
  vi.mocked(confirm).mockResolvedValue(true)
  vi.mocked(oxidnsMonitorApi.overview).mockResolvedValue(response({ recorders: [] }))
  vi.mocked(routerApi.policySettings).mockImplementation(async () => response({ success: true, workspace: clone(state) }))
  vi.mocked(routerApi.savePolicySettings).mockImplementation(async (_id, draft) => { state = { ...state, revision: 'r2', sources: { ...state.sources, policy_routing_sources: clone(draft.policy_routing_sources!) } }; return response({ success: true, workspace: clone(state) }) })
  vi.mocked(routerApi.previewPolicySettings).mockImplementation(async (_id, revision, targets) => response({ success: true, preview: { revision, config_revision: 10, profile_id: 'office', oxidns_revision: 'dns1', targets: [], dns_content: '', router_plan: { fingerprint: 'plan', empty: false, blockers: [], warnings: [], stages: [] } } }))
  vi.mocked(routerApi.apply).mockResolvedValue(response({ success: true, result: { ok: true, log: [], rollback: [] } }))
})
afterEach(async () => { wrappers.splice(0).forEach(w => w.unmount()); await flushPromises(); document.body.innerHTML = ''; vi.unstubAllGlobals() })

describe('independent policy routing sources', () => {
  it('edits include/exclude data independently and warns before an added exclusion leaves routing', async () => {
    const w = render(PolicyRoutingPanel, { id: 'ros', name: 'ROS', profile: 'office' }); await flushPromises()
    expect(w.text()).toContain('独立策略路由来源'); expect(w.text()).not.toContain('智能分流')
    await w.get('#policy-source-ip').setValue('192.0.3.42/24'); await click(w, '添加')
    w.getComponent(MultiSelect).vm.$emit('update:modelValue', ['dg_direct', 'dg_filter']); await flushPromises()
    expect(w.text()).toContain('将不再经过 RouterOS 策略路由')
    await click(w, '保存并预览')
    expect(routerApi.savePolicySettings).toHaveBeenCalledWith('ros', expect.objectContaining({ policy_routing_sources: { include: ['192.0.2.0/24', '192.0.3.0/24'], exclude_groups: ['dg_direct', 'dg_filter'] } }), 'office')
    expect(vi.mocked(routerApi.savePolicySettings).mock.lastCall![1]).not.toHaveProperty('policies')
    expect(routerApi.previewPolicySettings).toHaveBeenCalledWith('ros', 'r2', [], 'office')
    await click(w, '确认发布')
    expect(agentApi.pushConfig).not.toHaveBeenCalled(); expect(routerApi.apply).toHaveBeenCalledWith('ros', 'plan', 'office')
  })
  it('opens the source editor in include mode and preserves group exclusions', async () => {
    const w = render(PolicyRoutingPanel, { id: 'ros', name: 'ROS', profile: 'office' }); await flushPromises()
    await w.get('[aria-label="编辑 192.0.2.0/24"]').trigger('click'); await flushPromises()
    const editor = w.getComponent(PolicySourceEditor)
    expect(editor.props('sourceMode')).toBe(true); expect(editor.props('source')).toBe('192.0.2.0/24')
    editor.vm.$emit('apply-source', '192.0.2.0/25'); await flushPromises()
    expect(w.text()).toContain('将不再经过 RouterOS 策略路由')
    await click(w, '保存并预览')
    expect(vi.mocked(routerApi.savePolicySettings).mock.lastCall![1].policy_routing_sources).toEqual({ include: ['192.0.2.0/25'], exclude_groups: ['dg_direct'] })
  })
  it('makes the migrated Agent entry read-only and routes its link to the actual bound profile', async () => {
    activeProfileId.value = 'other'
    vi.mocked(oxidnsDevicePolicyApi.get).mockResolvedValue(response({ success: true, draft: true, profile_id: 'office', profile_name: '办公室', revision: 'r1', policies: [], editable: false, reason: '代理组无法完整投影为旧策略。', migrated: true, agents: [] }))
    const w = render(OxiDNSDevicePolicies, { agentId: 'o1', recorder: '' }); await flushPromises()
    expect(w.text()).toContain('此入口只读'); expect(w.find('#policy-client-ip').exists()).toBe(false)
    expect(oxidnsDevicePolicyApi.save).not.toHaveBeenCalled()
    await w.getComponent(RouterLinkStub).trigger('click'); expect(activeProfileId.value).toBe('office')
  })
})
