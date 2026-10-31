import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import Routers from '@/views/Routers.vue'
import { agentApi, routerApi, ruleApi } from '@/api'
import { copyText } from '@/lib/clipboard'
import { notify } from '@/lib/feedback'

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
vi.mock('@/lib/clipboard', () => ({ copyText: vi.fn() }))
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
const wrappers: VueWrapper[] = []
const response = (data: unknown) => ({ data } as any)
const button = (wrapper: VueWrapper, text: string) => wrapper.findAll('button').find(item => item.text().trim() === text)!
async function openReport(hint?: Record<string, string>) {
  const routes = { ...report.routes, ...(hint ? { forward_hint: hint } : {}) }
  vi.mocked(routerApi.report).mockResolvedValue(response({ report: { ...report, routes }, generated_at: '2026-10-31T10:00:00Z' }))
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

const command = '/ip firewall filter add chain=forward action=accept in-interface=bridge out-interface=bridge comment="LAN to Mihomo via router" place-before=*9'

describe('RouterOS IPv4 same-port forward hint', () => {
  it('shows the reason and a copyable command right below the IPv4 proxy routes', async () => {
    const wrapper = await openReport({ status: 'drop', warning: 'IPv4 防火墙会丢弃同口转发的后续包：新连接偶尔会卡几秒。', fix_command: command })
    const hint = wrapper.get('[data-router-ipv4-forward-hint]')
    expect(hint.get('[role="status"]').text()).toContain('卡几秒')
    expect(hint.get('[data-router-ipv4-forward-command] pre').text()).toBe(command)
    // Placed in the proxy-route card, before any IPv6 section.
    expect(hint.element.previousElementSibling?.querySelector('table')?.textContent).toContain('198.18.0.0/16')
    vi.mocked(copyText).mockResolvedValue(undefined as any)
    await hint.get('button[aria-label="复制 IPv4 同口转发放行命令"]').trigger('click')
    await flushPromises()
    expect(copyText).toHaveBeenCalledWith(command)
    expect(notify.success).toHaveBeenCalled()
  })

  it('shows an unconfirmed check without a command and nothing when the path is fine', async () => {
    const wrapper = await openReport({ status: 'unknown', warning: '未能确认 IPv4 防火墙是否会丢弃转给 Mihomo 的同口转发：无法读取 RouterOS IPv4 防火墙。', fix_command: '' })
    const hint = wrapper.get('[data-router-ipv4-forward-hint]')
    expect(hint.text()).toContain('未能确认')
    expect(hint.find('[data-router-ipv4-forward-command]').exists()).toBe(false)
    wrapper.unmount()
    wrappers.splice(0)
    const clean = await openReport()
    expect(clean.find('[data-router-ipv4-forward-hint]').exists()).toBe(false)
  })
})
