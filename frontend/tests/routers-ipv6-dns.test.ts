import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import Routers from '@/views/Routers.vue'
import SectionCard from '@/components/common/SectionCard.vue'
import StatusDot from '@/components/common/StatusDot.vue'
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
  verify_tls: true, proxy_gateway_agent_id: 'mihomo', manage_return_routing: false, dns_agent_id: 'oxidns', has_password: true }
const report = {
  in_sync: false, summary: { synced: 1, not_required: 1 },
  desired: { fake_ip_range: '198.18.0.0/16', proxy_gateway: '192.0.2.3', proxy_cidrs: [], warnings: [] },
  routes: { items: [{ cidr: '198.18.0.0/16', sources: ['fake-ip'], status: 'synced', detail: '' }], extra: [], unmanaged_other: [] },
  nat_bypass: { items: [{ cidr: '198.18.0.0/16', status: 'not_required', detail: '不匹配源 NAT。' }], disabled_rules: [] },
  dhcp_dns: { status: 'synced', message: 'IPv4 DHCP 已下发 OxiDNS。', target_dns: '192.0.2.53', items: [], blockers: [] },
  return_routing: { status: 'ok', nft_available: true, iproute_available: true, return_tables: [], fwmark_rules: [], table_routes: {}, tun_devices: [] },
}
const ipv6Dns = (changes: Record<string, unknown> = {}) => ({ enabled: true, status: 'pending', interface: 'bridge-lan', target_dns: '192.0.2.53', warnings: [], blockers: [],
  items: [{ id: '*1', interface: 'bridge-lan', source_interface: 'bridge-lan', advertise_dns: 'yes', dns_servers: ['2001:db8::53'], status: 'pending', detail: '发布时关闭该 LAN 的 IPv6 DNS 下发。' }], ...changes })
const wrappers: VueWrapper[] = []
const response = (data: unknown) => ({ data } as any)
const button = (wrapper: VueWrapper, text: string) => wrapper.findAll('button').find(item => item.text().trim() === text)!
async function openReport(ipv6Report?: ReturnType<typeof ipv6Dns>) {
  vi.mocked(routerApi.report).mockResolvedValue(response({ report: { ...report, ...(ipv6Report ? { ipv6_dns: ipv6Report } : {}) }, generated_at: '2026-10-31T10:00:00Z' }))
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

describe('RouterOS IPv6 DNS checklist', () => {
  it('shows the target LAN and pending action next to IPv4 DHCP DNS, without a separate write control', async () => {
    const wrapper = await openReport(ipv6Dns())
    const section = wrapper.get('[data-router-ipv6-dns]')
    expect(section.text()).toContain('IPv6 DNS 下发')
    expect(section.text()).toContain('bridge-lan')
    expect(section.text()).toContain('待关闭')
    expect(section.text()).toContain('统一使用 OxiDNS')
    expect(section.text()).toContain('在「预览发布」确认后关闭')
    expect(section.text()).toContain('IPv6 地址和默认网关继续通告')
    expect(section.find('table').exists()).toBe(false)
    expect(section.find('details').exists()).toBe(false)
    expect(section.find('[role="switch"]').exists()).toBe(false)
    expect(section.findAll('button').every(item => item.attributes('aria-label')?.includes('说明'))).toBe(true)
    const cards = wrapper.findAllComponents(SectionCard).map(card => card.props('title'))
    expect(cards[cards.indexOf('DHCP DNS') + 1]).toBe('IPv6 DNS 下发')
    expect(wrapper.text()).toContain('通过 IPv4 DHCP 下发 DNS')
    expect(routerApi.apply).not.toHaveBeenCalled()
  })

  it.each(['synced', 'not_required'])('shows %s as no adjustment, without claiming a completed change or disabling IPv6', async status => {
    const wrapper = await openReport(ipv6Dns({ status, message: '该 LAN 未下发 IPv6 DNS，无需调整。', items: [] }))
    const section = wrapper.get('[data-router-ipv6-dns]')
    expect(section.text()).toContain('无需调整')
    expect(section.text()).not.toContain('已关闭')
    expect(section.text()).not.toContain('待关闭')
    expect(section.findComponent(StatusDot).props('tone')).toBe('success')
    expect(routerApi.apply).not.toHaveBeenCalled()
  })

  it.each(['blocked', 'unknown'])('keeps %s reasons visible when the LAN cannot be safely changed', async status => {
    const wrapper = await openReport(ipv6Dns({ status, interface: '',
      blockers: ['当前通告来自 all，无法安全限定到目标 LAN，请先明确 LAN 的通告配置。'],
      warnings: ['动态通告项不能直接修改。'] }))
    const section = wrapper.get('[data-router-ipv6-dns]')
    expect(section.text()).toContain('需处理')
    expect(section.text()).not.toContain('目标 LAN：')
    expect(section.get('[role="alert"]').isVisible()).toBe(true)
    expect(section.get('[role="alert"]').text()).toContain('all')
    expect(section.get('[role="status"]').text()).toContain('动态通告项不能直接修改')
    expect(section.findComponent(StatusDot).props('tone')).toBe('warning')
    expect(routerApi.apply).not.toHaveBeenCalled()
  })

  it('does not treat an unlinked or absent IPv6 DNS report as checked', async () => {
    const disabled = await openReport(ipv6Dns({ enabled: false, status: 'disabled', interface: '', target_dns: '', items: [] }))
    const section = disabled.get('[data-router-ipv6-dns]')
    expect(section.text()).toContain('未检查')
    expect(section.text()).toContain('未关联 OxiDNS')
    expect(section.text()).toContain('不检查或修改')
    expect(section.text()).not.toContain('已关闭')
    expect(section.findComponent(StatusDot).props('tone')).toBe('muted')
    const legacy = await openReport()
    expect(legacy.find('[data-router-ipv6-dns]').exists()).toBe(false)
  })

  it('reviews the DNS change through the existing publication preview before any write', async () => {
    vi.mocked(routerApi.plan).mockResolvedValue(response({ plan: { fingerprint: 'current-plan', empty: false, warnings: [], blockers: [], stages: [
      { key: 'ipv6_dns', title: 'IPv6 DNS 下发', ops: [{ kind: 'ipv6_dns_update', summary: '关闭 bridge-lan 的 IPv6 DNS 下发',
        interface: 'bridge-lan', source_interface: 'bridge-lan', expected: { 'advertise-dns': 'yes' }, values: { 'advertise-dns': 'no' } }] },
    ] } }))
    const wrapper = await openReport(ipv6Dns())
    expect(wrapper.findAll('button').filter(item => item.text().trim() === '预览发布')).toHaveLength(1)
    await button(wrapper, '预览发布').trigger('click')
    await flushPromises()
    expect(routerApi.plan).toHaveBeenCalledExactlyOnceWith('ros')
    expect(document.body.textContent).toContain('关闭 bridge-lan 的 IPv6 DNS 下发')
    expect(routerApi.apply).not.toHaveBeenCalled()
  })

  it('explains that selecting OxiDNS also checks RouterOS LAN IPv6 DNS announcements', async () => {
    const wrapper = await openReport()
    await wrapper.get('button[aria-label="编辑"]').trigger('click')
    await flushPromises()
    document.querySelector<HTMLButtonElement>('button[aria-label="DHCP DNS 服务器（OxiDNS Agent）说明"]')!.click()
    await flushPromises()
    const help = document.querySelector('[data-field-help-content]')!.textContent
    expect(help).toContain('通过 IPv4 DHCP 下发此 OxiDNS')
    expect(help).toContain('列入预览并在发布时关闭')
    expect(help).toContain('IPv6 地址和默认网关继续通告')
  })
})
