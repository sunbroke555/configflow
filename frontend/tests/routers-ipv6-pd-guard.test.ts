import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import Routers from '@/views/Routers.vue'
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
const pdGuard = (changes: Record<string, unknown> = {}) => ({
  enabled: true, status: 'borrowed', lan: 'lan4', pool: 'ipv6pool', client: 'pppoe-out1', hold: 3600, managed: false,
  rollback_script: true, blockers: [], warnings: [],
  message: '已安装（沿用）：路由器上的手动安装与 ConfigFlow 模板一致，本次不改写。它不算 ConfigFlow 受管对象，关闭开关时不会删除。',
  items: [
    { role: 'script', object: '脚本 cf-pd-watch', status: 'borrowed', detail: '监控 lan4（地址池 ipv6pool），旧前缀弃用通告 3600 秒' },
    { role: 'scheduler', object: '定时任务 cf-pd-poll', status: 'borrowed', detail: '每 30 秒兜底检查一次' },
    { role: 'hook', object: 'dhcp-client 钩子', status: 'borrowed', detail: '前缀更新后每秒检查一次，共 10 次' },
  ], ...changes })
const wrappers: VueWrapper[] = []
const response = (data: unknown) => ({ data } as any)
const button = (wrapper: VueWrapper, text: string) => wrapper.findAll('button').find(item => item.text().trim() === text)!
async function openReport(section?: ReturnType<typeof pdGuard>, item = router) {
  vi.mocked(routerApi.list).mockResolvedValue(response({ routers: [item] }))
  vi.mocked(routerApi.report).mockResolvedValue(response({ report: { ...report, ...(section ? { ipv6_pd_guard: section } : {}) }, generated_at: '2026-10-31T10:00:00Z' }))
  const wrapper = mount(Routers, { attachTo: document.body })
  wrappers.push(wrapper)
  await flushPromises()
  await button(wrapper, '差异报告').trigger('click')
  await flushPromises()
  return wrapper
}
beforeEach(() => {
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
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

describe('RouterOS IPv6 prefix change guard', () => {
  it('shows a matching hand install as borrowed, with the detected LAN, pool and client', async () => {
    const wrapper = await openReport(pdGuard())
    const section = wrapper.get('[data-router-ipv6-pd-guard]')
    expect(section.findComponent(StatusDot).props()).toMatchObject({ label: '已安装（沿用）', tone: 'success' })
    expect(section.text()).toContain('lan4')
    expect(section.text()).toContain('ipv6pool')
    expect(section.text()).toContain('pppoe-out1')
    expect(section.text()).toContain('关闭开关时不会删除')
    expect(section.text()).toContain('cf-pd-rollback')
    expect(section.findAll('tbody tr')).toHaveLength(3)
    expect(section.findAll('tbody tr').every(row => row.text().includes('沿用'))).toBe(true)
    expect(routerApi.apply).not.toHaveBeenCalled()
  })

  it('shows a hook conflict as needing attention and says the rest of the release goes on', async () => {
    const blocker = 'dhcp-client（pppoe-out1）的 script 已有其他内容，不能覆盖。'
    const wrapper = await openReport(pdGuard({ status: 'blocked', message: blocker, blockers: [blocker], rollback_script: false,
      items: [{ role: 'hook', object: 'dhcp-client 钩子', status: 'conflict', detail: blocker }] }))
    const section = wrapper.get('[data-router-ipv6-pd-guard]')
    expect(section.findComponent(StatusDot).props()).toMatchObject({ label: '需处理', tone: 'warning' })
    expect(section.get('[role="alert"]').text()).toContain('不能覆盖')
    expect(section.text()).toContain('只有这一项保留现状')
    expect(section.text()).toContain('冲突')
    expect(section.text()).not.toContain('cf-pd-rollback')
  })

  it('shows pending updates and withdrawals as needing the preview', async () => {
    const wrapper = await openReport(pdGuard({ status: 'pending', message: '路由器上已有前缀保护，但与模板不一致；确认发布后按模板更新。',
      items: [{ role: 'script', object: '脚本 cf-pd-watch', status: 'update', detail: '弃用通告时长为 1800 秒，模板为 3600 秒' }] }))
    const section = wrapper.get('[data-router-ipv6-pd-guard]')
    expect(section.findComponent(StatusDot).props()).toMatchObject({ label: '待发布', tone: 'warning' })
    expect(section.text()).toContain('1800')
    expect(section.text()).toContain('待更新')
    expect(section.text()).toContain('在「预览发布」确认后才写入路由器')
  })

  it('hides the card while the switch is off and nothing is owned', async () => {
    const wrapper = await openReport(pdGuard({ enabled: false, status: 'disabled', items: [], message: '' }))
    expect(wrapper.find('[data-router-ipv6-pd-guard]').exists()).toBe(false)
  })

  it('offers the switch on RouterOS only and saves it', async () => {
    vi.mocked(routerApi.update).mockResolvedValue(response({ success: true }))
    const wrapper = await openReport(undefined)
    await wrapper.find('button[aria-label="编辑"]').trigger('click')
    await flushPromises()
    const toggle = document.body.querySelector<HTMLButtonElement>('#router-ipv6-pd-guard')!
    expect(toggle.getAttribute('aria-checked')).toBe('false')
    toggle.click()
    await flushPromises()
    ;[...document.body.querySelectorAll('button')].find(item => item.textContent?.trim() === '保存')!.click()
    await flushPromises()
    expect(routerApi.update).toHaveBeenCalledWith('ros', expect.objectContaining({ ipv6_pd_guard: { enabled: true } }))
  })

  it('does not offer the switch on iKuai and saves it off', async () => {
    vi.mocked(routerApi.update).mockResolvedValue(response({ success: true }))
    const wrapper = await openReport(undefined, { ...router, type: 'ikuai', ipv6_pd_guard: { enabled: true } } as any)
    await wrapper.find('button[aria-label="编辑"]').trigger('click')
    await flushPromises()
    expect(document.body.querySelector('#router-ipv6-pd-guard')).toBeNull()
    ;[...document.body.querySelectorAll('button')].find(item => item.textContent?.trim() === '保存')!.click()
    await flushPromises()
    expect(routerApi.update).toHaveBeenCalledWith('ros', expect.objectContaining({ ipv6_pd_guard: { enabled: false } }))
  })
})
