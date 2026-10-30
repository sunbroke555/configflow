import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import Routers from '@/views/Routers.vue'
import { agentApi, routerApi, ruleApi } from '@/api'

vi.mock('@/api', () => ({
  agentApi: { getAll: vi.fn() },
  routerApi: {
    list: vi.fn(), create: vi.fn(), update: vi.fn(), remove: vi.fn(), probe: vi.fn(), report: vi.fn(),
    plan: vi.fn(), apply: vi.fn(), routes: vi.fn(), routePlan: vi.fn(), routeApply: vi.fn(), policy: vi.fn(),
    policySettings: vi.fn(), savePolicySettings: vi.fn(), previewPolicySettings: vi.fn(),
    gatewayForwarding: vi.fn(), persistGatewayForwarding: vi.fn(),
  },
  ruleApi: { getAll: vi.fn() }, oxidnsMonitorApi: { overview: vi.fn() },
}))
vi.mock('@/lib/feedback', () => ({ notify: { success: vi.fn(), error: vi.fn(), info: vi.fn() }, confirmDanger: vi.fn(), confirm: vi.fn(), confirmState: { resolve: null }, settleConfirm: vi.fn() }))
vi.mock('@/api/setup', () => ({ setupApi: { get: vi.fn(), saveRouterDraft: vi.fn() } }))

const router = { id: 'ros', name: '家里 ROS', type: 'routeros', endpoint: 'http://192.0.2.1', username: 'admin',
  verify_tls: true, proxy_gateway_agent_id: 'mihomo', manage_return_routing: false, dns_agent_id: '', has_password: true }
const report = {
  in_sync: false, summary: { synced: 1, not_required: 1 },
  desired: { fake_ip_range: '198.18.0.0/16', proxy_gateway: '192.0.2.3', proxy_cidrs: [], warnings: [] },
  routes: { items: [{ cidr: '198.18.0.0/16', sources: ['fake-ip'], status: 'synced', detail: '' }], extra: [], unmanaged_other: [] },
  nat_bypass: { items: [{ cidr: '198.18.0.0/16', status: 'not_required', detail: '不匹配源 NAT。' }], disabled_rules: [] },
  dhcp_dns: { status: 'disabled', message: '未关联 OxiDNS。', target_dns: '', items: [], blockers: [] },
  return_routing: { status: 'ok', nft_available: true, iproute_available: true, return_tables: [], fwmark_rules: [], table_routes: {}, tun_devices: [] },
}
const ipv6 = (changes = {}) => ({ enabled: true, status: 'missing', warnings: [], blockers: [],
  items: [{ cidr: 'fc00::/64', gateway: 'fe80::1234:5678:abcd:ef90%bridge-lan', status: 'missing', detail: '发布时添加指向 Mihomo 的路由。' }], ...changes })
const wrappers: VueWrapper[] = []
const response = (data: unknown) => ({ data } as any)
const button = (wrapper: VueWrapper, text: string) => wrapper.findAll('button').find(item => item.text().trim() === text)!
async function openReport(ipv6Report?: ReturnType<typeof ipv6>) {
  vi.mocked(routerApi.report).mockResolvedValue(response({ report: { ...report, ...(ipv6Report ? { ipv6_fakeip: ipv6Report } : {}) }, generated_at: '2026-10-30T10:00:00Z' }))
  const wrapper = mount(Routers, { attachTo: document.body })
  wrappers.push(wrapper)
  await flushPromises()
  await button(wrapper, '差异报告').trigger('click')
  await flushPromises()
  return wrapper
}
beforeEach(() => {
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  vi.mocked(routerApi.list).mockResolvedValue(response({ routers: [router] }))
  vi.mocked(agentApi.getAll).mockResolvedValue(response([]))
  vi.mocked(ruleApi.getAll).mockResolvedValue(response([]))
  vi.mocked(routerApi.gatewayForwarding).mockResolvedValue(response({ gateways: [] }))
})
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  document.body.innerHTML = ''
  vi.clearAllMocks()
  vi.unstubAllGlobals()
})

describe('RouterOS managed IPv6 Fake-IP report', () => {
  it('shows the IPv6 destination and scoped next hop while preserving the NAT status meaning', async () => {
    const wrapper = await openReport(ipv6())
    const section = wrapper.get('[data-router-ipv6-fakeip]')
    expect(section.text()).toContain('IPv6 Fake-IP 分流路由')
    expect(section.text()).toContain('fc00::/64')
    expect(section.text()).toContain('fe80::1234:5678:abcd:ef90%bridge-lan')
    expect(section.text()).toContain('待添加')
    expect(section.text()).not.toContain('无需免转换')
    expect(wrapper.text()).toContain('无需免转换')
    expect(section.get('table').classes()).toContain('cf-mobile-records')
    expect(section.get('td[data-label="下一跳"]').classes()).toContain('[overflow-wrap:anywhere]')
    expect(section.findAll('button')).toHaveLength(0)
    expect(routerApi.apply).not.toHaveBeenCalled()
  })

  it('keeps borrowed routes visibly distinct from managed routes and states the removal boundary', async () => {
    const wrapper = await openReport(ipv6({ status: 'borrowed', items: [
      { cidr: 'fc00::/64', gateway: 'fd12::3%lan', status: 'borrowed', detail: '已有可用路由。' },
    ] }))
    const section = wrapper.get('[data-router-ipv6-fakeip]')
    expect(section.text()).toContain('沿用已有路由')
    expect(section.text()).toContain('本次仅沿用，不接管')
    expect(section.text()).toContain('不会自动撤销这条路由')
    expect(section.text()).not.toContain('可接管')
    expect(section.text()).not.toContain('无需免转换')
    expect(section.find('details').exists()).toBe(false)
    expect(routerApi.apply).not.toHaveBeenCalled()
  })

  it('keeps conflicts and read failures visible before opening the shared publication preview', async () => {
    const wrapper = await openReport(ipv6({ status: 'conflict', blockers: ['Fake-IP 与 LAN 网段重叠，请先调整。'], warnings: ['无法读取原有路由归属。'],
      items: [{ cidr: 'fc00::/64', gateway: '', status: 'conflict', detail: '保留原有路由。' }] }))
    const section = wrapper.get('[data-router-ipv6-fakeip]')
    expect(section.get('[role="alert"]').text()).toContain('Fake-IP 与 LAN 网段重叠')
    expect(section.get('[role="status"]').text()).toContain('无法读取原有路由归属')
    expect(section.get('[role="alert"]').isVisible()).toBe(true)
    expect(section.text()).toContain('未确定')
    expect(section.text()).toContain('冲突')
    expect(button(wrapper, '预览发布')).toBeDefined()
    expect(routerApi.apply).not.toHaveBeenCalled()
  })

  it('distinguishes disabled configuration from an older report without this capability', async () => {
    const disabled = await openReport(ipv6({ enabled: false, status: 'disabled', items: [] }))
    expect(disabled.get('[data-router-ipv6-fakeip]').text()).toContain('未启用')
    expect(disabled.get('[data-router-ipv6-fakeip]').text()).not.toContain('无需免转换')
    const legacy = await openReport()
    expect(legacy.find('[data-router-ipv6-fakeip]').exists()).toBe(false)
  })

  it('includes IPv6 operations in the existing publication preview without adding another write entry', async () => {
    vi.mocked(routerApi.plan).mockResolvedValue(response({ plan: { fingerprint: 'current-plan', empty: false, warnings: [], blockers: [], stages: [
      { key: 'ipv6_fakeip', title: 'IPv6 Fake-IP 分流路由', ops: [{ kind: 'ipv6_route_add', summary: '添加 fc00::/64 → fd12::3%lan' }] },
    ] } }))
    const wrapper = await openReport(ipv6())
    expect(wrapper.findAll('button').filter(item => item.text().trim() === '预览发布')).toHaveLength(1)
    await button(wrapper, '预览发布').trigger('click')
    await flushPromises()
    expect(routerApi.plan).toHaveBeenCalledExactlyOnceWith('ros')
    expect(document.body.textContent).toContain('添加 fc00::/64 → fd12::3%lan')
    expect(routerApi.apply).not.toHaveBeenCalled()
  })
})
