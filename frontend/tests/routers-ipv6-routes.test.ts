import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import Routers from '@/views/Routers.vue'
import { agentApi, routerApi, ruleApi } from '@/api'
import { routerCopy } from '@/lib/routerCopy'

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
const routes6 = (changes = {}) => ({ enabled: true, status: 'missing', gateway: 'fe80::4%bridge', skip: [], warnings: [],
  items: [
    { cidr: '2001:b28:f23d::/48', sources: ['Telegram'], gateway: 'fe80::4%bridge', status: 'missing', detail: '发布时添加指向 Mihomo 的路由' },
    { cidr: '2001:67c:4e8::/48', sources: ['Telegram'], gateway: 'fe80::4%bridge', status: 'borrowed', detail: '沿用已有正确路由，保留原归属' },
  ], ...changes })
const wrappers: VueWrapper[] = []
const response = (data: unknown) => ({ data } as any)
const button = (wrapper: VueWrapper, text: string) => wrapper.findAll('button').find(item => item.text().trim() === text)!
async function openReport(section?: ReturnType<typeof routes6>) {
  vi.mocked(routerApi.report).mockResolvedValue(response({ report: { ...report, ...(section ? { ipv6_routes: section } : {}) }, generated_at: '2026-10-31T10:00:00Z' }))
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

describe('RouterOS IPv6 rule-set routes', () => {
  it('lists each IPv6 destination with its source, next hop and ownership boundary', async () => {
    const wrapper = await openReport(routes6())
    const section = wrapper.get('[data-router-ipv6-routes]')
    expect(section.text()).toContain('IPv6 规则集路由')
    expect(section.text()).toContain('2001:b28:f23d::/48')
    expect(section.text()).toContain('Telegram')
    expect(section.text()).toContain('fe80::4%bridge')
    expect(section.text()).toContain('待添加')
    expect(section.text()).toContain('沿用已有路由')
    expect(section.text()).toContain('关闭 Mihomo IPv6 后不会自动撤销这条路由')
    expect(section.get('table').classes()).toContain('cf-mobile-records')
    expect(section.findAll('button').filter(item => item.text().trim())).toHaveLength(0)
    expect(routerApi.apply).not.toHaveBeenCalled()
  })

  it('shows a skipped item as unconfirmed and says the rest of the release continues', async () => {
    const wrapper = await openReport(routes6({ status: 'unknown', gateway: '', skip: ['未确认 Mihomo 唯一、稳定的同网段 IPv6 下一跳'],
      items: [{ cidr: '2001:b28:f23d::/48', sources: ['Telegram'], gateway: '', status: 'unknown', detail: '本次跳过，保留现状' }] }))
    const section = wrapper.get('[data-router-ipv6-routes]')
    expect(section.get('[role="alert"]').text()).toContain('IPv6 下一跳')
    expect(section.text()).toContain('只有这一项保留现状')
    expect(section.text()).toContain('待确认')
    expect(section.text()).not.toContain('已同步')
    expect(section.find('table').exists()).toBe(false)
  })

  it('explains withdrawal after Mihomo IPv6 is turned off and hides the section for older reports', async () => {
    const off = await openReport(routes6({ enabled: false, status: 'pending', gateway: '',
      items: [{ cidr: '2001:b28:f23d::/48', sources: [], gateway: 'fe80::4%bridge', status: 'pending', detail: '已关闭 Mihomo IPv6，发布时撤回' }] }))
    expect(off.get('[data-router-ipv6-routes]').text()).toContain('发布时撤回')
    expect(off.get('[data-router-ipv6-routes]').text()).toContain('—')
    const legacy = await openReport()
    expect(legacy.find('[data-router-ipv6-routes]').exists()).toBe(false)
  })

  it('states the RouterOS scope and that iKuai only routes IPv4', () => {
    expect(routerCopy.staticRules.help).toContain('iKuai 只处理 IPv4')
    expect(routerCopy.ipv6Routes).toContain('只跳过本项')
  })
})
