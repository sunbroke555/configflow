import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import { RouterLink } from 'vue-router'
import { agentApi, profileApi, routerApi } from '@/api'
import MihomoIPv6Steps from '@/components/generate/MihomoIPv6Steps.vue'
import MihomoIPv6Verification from '@/components/generate/MihomoIPv6Verification.vue'
import UpstreamRouterPending from '@/components/generate/UpstreamRouterPendingPlaceholder.vue'
import { Select } from '@/components/ui/select'
import { buildMihomoIPv6Summary } from '@/lib/mihomoIPv6Summary'
import { firewallStep, hostStep, oxidnsStep, publishStep, stepOrder, stepVisible, upstreamStep, verifyStep, type RouterReportRead } from '@/lib/mihomoIPv6Steps'
import type { IPv6ReadinessCheck, MihomoIPv6Readiness as Readiness } from '@/types/mihomoIPv6'

vi.mock('@/router', () => ({ default: { push: vi.fn() } }))
vi.mock('@/lib/clipboard', () => ({ copyText: vi.fn().mockResolvedValue(undefined) }))

const response = (data: any) => ({ data } as any)
const gateway = { id: 'gw', name: '客厅 Mihomo', service_type: 'mihomo', profile_id: 'bound', status: 'online' }
const dnsAgent = { id: 'dns', name: '旁路 OxiDNS', service_type: 'oxidns', profile_id: 'bound', status: 'online' }
const ok = (id: string, label = id): IPv6ReadinessCheck => ({ id, label, status: 'ok', message: `${label} 通过` })
const command = '/ipv6 firewall filter add chain=forward action=accept in-interface=bridge out-interface=bridge comment="LAN to Mihomo via router" place-before=*5'
const hairpinWarning = 'IPv6 防火墙 forward 链会丢弃状态无效（invalid）的连接：每个新连接会卡住约 7 秒。'
const readiness = (overrides: Partial<Readiness> = {}): Readiness => ({
  success: true, agent_id: 'gw', profile_id: 'bound', config_revision: 7,
  configuration: { ipv6: true, dns_ipv6: true, fake_ipv6_range: 'fc00::/64', errors: [] },
  host: { core: { ipv6: true, 'dns.ipv6': true } },
  checks: [ok('configuration'), ok('host_address'), ok('host_default_route'), ok('host_forwarding_ra')],
  routers: [{ id: 'ros', name: '主路由', status: 'ok', can_prepare: false, checks: [ok('fake_ipv6'), { ...ok('fake6_0'), decision: 'allow' } as IPv6ReadinessCheck] }],
  can_prepare: false, prepare_interface: 'eth0', check_token: 'token',
  connectivity: { status: 'not_checked', message: '尚未检查' }, ...overrides,
})
const context = (overrides = {}) => ({ dirty: false, checking: false, readiness: readiness(), ...overrides })
const passedVerification = {
  status: 'ok' as const, scope: 'client_path' as const, config_revision: 7,
  checks: ['host_address', 'host_default_route', 'runtime_proxy_http', 'runtime_node_ipv6_transport',
    'client_http_ipv4', 'client_mihomo_path', 'client_ros_path', 'client_ipv6_transport'].map(id => ok(id)),
}

describe('IPv6 step derivation', () => {
  it('waits for a saved configuration and a device before claiming any step', () => {
    for (const make of [hostStep, publishStep] as const) {
      expect(make(context({ readiness: null }))).toMatchObject({ status: '待检查', tone: 'neutral' })
      expect(make(context({ readiness: null, checking: true }))).toMatchObject({ status: '检查中', tone: 'progress' })
    }
    expect(publishStep(context({ dirty: true }))).toMatchObject({ status: '待保存', note: expect.stringContaining('保存并发布') })
    expect(firewallStep(context({ dirty: true }), [], false)).toMatchObject({ status: '保存后检查', tone: 'neutral' })
    expect(verifyStep(context({ dirty: true }), [])).toMatchObject({ status: '保存后检查' })
  })

  it('takes host preparation from the existing host checks and their prepare hint', () => {
    expect(hostStep(context())).toMatchObject({ status: '已就绪', tone: 'success', issues: [] })
    const missing = readiness({ can_prepare: true, checks: [{ id: 'host_forwarding_ra', label: '转发与 RA', status: 'warning', message: '未满足' }] })
    expect(hostStep(context({ readiness: missing }))).toEqual(expect.objectContaining({ status: '需处理', tone: 'warning',
      issues: ['IPv6 转发和自动获取网关设置未齐全，可点击「补齐缺少的配置」。'] }))
    const failed = readiness({ checks: [{ id: 'host_ipv6_enabled', label: '网卡 IPv6', status: 'error', message: '上联网卡禁用了 IPv6' }] })
    expect(hostStep(context({ readiness: failed }))).toMatchObject({ status: '未通过', tone: 'danger', issues: ['上联网卡禁用了 IPv6'] })
    // Router checks and the saved-config summary are other steps.
    const routerOnly = readiness({ checks: [{ id: 'deployed_config', label: '设备上的配置', status: 'warning', message: '不同' }] })
    expect(hostStep(context({ readiness: routerOnly })).status).toBe('已就绪')
  })

  it('reports publication from the deployed-config comparison', () => {
    expect(publishStep(context())).toMatchObject({ status: '已发布', tone: 'success' })
    const pending = readiness({ checks: [{ id: 'deployed_config', label: '设备上的配置', status: 'warning', message: '已保存草稿与设备配置不同：DNS IPv6。请通过保存并发布应用配置。' }] })
    expect(publishStep(context({ readiness: pending }))).toMatchObject({ status: '待发布', issues: ['已保存草稿与设备配置不同：DNS IPv6。请通过保存并发布应用配置。'] })
    expect(publishStep(context({ readiness: readiness({ host: {} }) }))).toMatchObject({ status: '无法确认', tone: 'neutral' })
    expect(publishStep(context({ readiness: readiness({ host: { core: { error: '无法读取' } } }) })).status).toBe('无法确认')
  })

  it('maps the upstream reminder states', () => {
    expect(upstreamStep(null, false)).toMatchObject({ status: '检查中', tone: 'progress' })
    expect(upstreamStep({ state: 'synced', pending: 0 }, false)).toMatchObject({ status: '已同步', tone: 'success', issues: [] })
    expect(upstreamStep({ state: 'pending', pending: 2 }, false)).toMatchObject({ status: '2 项待发布', tone: 'neutral' })
    expect(upstreamStep({ state: 'none', pending: 0 }, false)).toMatchObject({ status: '未关联上级路由', tone: 'warning' })
    expect(upstreamStep({ state: 'unknown', pending: 0 }, false).status).toBe('无法确认')
    expect(upstreamStep({ state: 'synced', pending: 0 }, true)).toMatchObject({ status: '保存后核对' })
  })

  it('shows no router work when the existing checks and the difference report pass', () => {
    const reports: RouterReportRead[] = [{ id: 'ros', name: '主路由', report: { ipv6_fakeip: { enabled: true, status: 'synced', blockers: [], warnings: [], fix_command: '' } } }]
    expect(firewallStep(context(), reports, false)).toMatchObject({ status: '无需处理', tone: 'success', issues: [] })
    expect(firewallStep(context(), [], true)).toMatchObject({ status: '检查中' })
    expect(firewallStep(context({ readiness: readiness({ routers: [] }) }), [], false)).toMatchObject({ status: '未关联上级路由' })
  })

  it('explains the same-port forward drop once with the backend command', () => {
    const plan = { enabled: true, status: 'synced', blockers: [], warnings: [hairpinWarning], fix_command: command }
    const reports: RouterReportRead[] = [{ id: 'ros', name: '主路由', report: { ipv6_fakeip: plan, ipv6_routes: { ...plan, warnings: ['部分规则集未完整读取，不撤回其 IPv6 路由', hairpinWarning] } } }]
    const result = firewallStep(context(), reports, false)
    expect(result).toMatchObject({ status: '需处理', tone: 'warning', issues: [`「主路由」：${hairpinWarning}`] })
    expect(result.commands).toEqual([{ label: '在 RouterOS 终端执行（主路由）', command }])
    expect(result.note).toContain('ConfigFlow 不修改防火墙和 NAT')
  })

  it('names the NAT and forward-drop findings of the client Fake-IP path', () => {
    const routers = [{ id: 'ros', name: '主路由', status: 'warning', can_prepare: false, checks: [
      { id: 'fake6_0', label: '去程与 NAT', status: 'warning', decision: 'translate', message: '会改写来源地址' },
      { id: 'fake6_1', label: '去程与 NAT', status: 'error', decision: 'drop', message: '命中阻断' },
      // A rule-order guess about the return path is not a definite blocker.
      { id: 'fake6_0_return', label: '返回', status: 'warning', decision: 'drop', message: '' },
    ] as IPv6ReadinessCheck[] }]
    const result = firewallStep(context({ readiness: readiness({ routers }) }), [], false)
    expect(result.status).toBe('需处理')
    expect(result.issues[0]).toContain('IPv6 NAT 会改写客户端发往 Mihomo 的连接')
    expect(result.issues[0]).toContain('masquerade')
    expect(result.issues[1]).toContain('IPv6 防火墙会阻断')
    expect(result.issues).toHaveLength(2)
  })

  it('keeps unreadable or manual routers unconfirmed rather than passed', () => {
    const routers = [{ id: 'ikuai', name: '爱快', status: 'warning', can_prepare: false, checks: [{ id: 'manual', label: '路由器 IPv6', status: 'unknown' as const, message: '此型号请手工核对 IPv6 路由与 DNS' }] },
      { id: 'ros', name: '主路由', status: 'ok', can_prepare: false, checks: [] }]
    const result = firewallStep(context({ readiness: readiness({ routers }) }), [{ id: 'ros', name: '主路由', error: '连接超时' }], false)
    expect(result).toMatchObject({ status: '无法确认', tone: 'neutral' })
    expect(result.issues).toEqual(['「爱快」：此型号请手工核对 IPv6 路由与 DNS。', '「主路由」的差异报告读取失败：连接超时'])
    const blocked = firewallStep(context(), [{ id: 'ros', name: '主路由', report: { ipv6_fakeip: { enabled: true, status: 'conflict', blockers: ['未确认下一跳'], warnings: [], fix_command: '' } } }], false)
    expect(blocked).toMatchObject({ status: '无法确认' })
    expect(blocked.issues[0]).toContain('上级路由发布')
  })

  it('only hints at OxiDNS settings when a linked router uses OxiDNS', () => {
    expect(oxidnsStep([])).toBeNull()
    const result = oxidnsStep([{ router: '主路由', agent: '旁路 OxiDNS' }])!
    expect(result).toMatchObject({ status: '请核对', tone: 'neutral', issues: ['「主路由」的客户端 DNS 使用 OxiDNS「旁路 OxiDNS」。'] })
    expect(result.note).toContain('优先 IPv4')
    expect(result.note).toContain('不会修改')
  })

  it('summarizes acceptance from the existing three results', () => {
    const data = readiness()
    expect(verifyStep(context(), null)).toMatchObject({ status: '待检查' })
    expect(verifyStep(context(), buildMihomoIPv6Summary({ readiness: data }))).toMatchObject({ status: '待验证', tone: 'neutral' })
    expect(verifyStep(context(), buildMihomoIPv6Summary({ readiness: data, verification: passedVerification }))).toMatchObject({ status: '已通过', tone: 'success' })
    expect(verifyStep(context(), buildMihomoIPv6Summary({ readiness: data }), true)).toMatchObject({ status: '验证中', tone: 'progress' })
  })

  it('orders the steps per mode and shows node-only upstream work only while pending', () => {
    expect(stepOrder('nodes', true)).toEqual(['host', 'publish', 'upstream', 'verify'])
    expect(stepOrder('clients', false)).toEqual(['host', 'publish', 'upstream', 'firewall', 'verify'])
    expect(stepOrder('clients', true)).toEqual(['host', 'publish', 'upstream', 'firewall', 'oxidns', 'verify'])
    expect(stepVisible('nodes', 'upstream', { state: 'synced', pending: 0 })).toBe(false)
    expect(stepVisible('nodes', 'upstream', { state: 'pending', pending: 3 })).toBe(true)
    expect(stepVisible('clients', 'upstream', null)).toBe(true)
  })
})

const RowStub = defineComponent({ props: ['label', 'status', 'tone'], setup(props, { slots }) {
  return () => h('section', { 'data-row': props.label, 'data-tone': props.tone }, [h('span', props.label), h('span', { 'data-status': '' }, props.status), slots.notice?.()])
} })
const wrappers: VueWrapper[] = []
async function mount(props = {}) {
  const wrapper = shallowMount(MihomoIPv6Steps, {
    props: { open: true, profileId: 'bound', preferredAgentId: 'gw', mode: 'clients', dirty: false, ...props },
    global: { stubs: { Button: false, Primitive: false, AgentStatusRow: RowStub } },
  })
  wrappers.push(wrapper)
  await flushPromises()
  return wrapper
}
const row = (wrapper: VueWrapper, id: string) => wrapper.get(`[data-ipv6-step="${id}"]`)
const status = (wrapper: VueWrapper, id: string) => row(wrapper, id).get('[data-status]').text()
const shown = (wrapper: VueWrapper, id: string) => (row(wrapper, id).element as HTMLElement).style.display !== 'none'

beforeEach(() => {
  vi.spyOn(agentApi, 'summaries').mockResolvedValue(response([gateway, dnsAgent]))
  vi.spyOn(profileApi, 'list').mockResolvedValue(response([{ id: 'bound' }, { id: 'default', is_default: true }]))
  vi.spyOn(agentApi, 'ipv6Readiness').mockResolvedValue(response(readiness()))
  vi.spyOn(routerApi, 'list').mockResolvedValue(response({ success: true, routers: [{ id: 'ros', name: '主路由', proxy_gateway_agent_id: 'gw', dns_agent_id: 'dns' }] }))
  vi.spyOn(routerApi, 'report').mockResolvedValue(response({ success: true, report: { ipv6_fakeip: { enabled: true, status: 'synced', blockers: [], warnings: [hairpinWarning], fix_command: command } } }))
})
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); vi.restoreAllMocks() })

describe('Mihomo IPv6 step list', () => {
  it('checks the fixed device once and reads the router report only for client IPv6', async () => {
    const wrapper = await mount()
    expect(agentApi.ipv6Readiness).toHaveBeenCalledExactlyOnceWith('gw', 'bound')
    expect(routerApi.report).toHaveBeenCalledExactlyOnceWith('ros', 'bound')
    expect(routerApi.list).toHaveBeenCalledExactlyOnceWith('bound')
    expect(wrapper.findAll('[data-ipv6-step]').map(item => item.attributes('data-ipv6-step'))).toEqual(['host', 'publish', 'upstream', 'firewall', 'oxidns', 'verify'])
    expect(status(wrapper, 'host')).toBe('已就绪')
    expect(status(wrapper, 'publish')).toBe('已发布')
    expect(status(wrapper, 'firewall')).toBe('需处理')
    expect(row(wrapper, 'firewall').get('pre').text()).toBe(command)
    expect(row(wrapper, 'oxidns').text()).toContain('OxiDNS「旁路 OxiDNS」')
    expect(row(wrapper, 'oxidns').text()).toContain('优先 IPv4')
    expect(row(wrapper, 'oxidns').getComponent(RouterLink).props('to')).toBe('/agents')
    expect(wrapper.text()).toContain('设备：客厅 Mihomo')
  })

  it('copies the backend command without assembling one', async () => {
    const { copyText } = await import('@/lib/clipboard')
    const wrapper = await mount()
    await row(wrapper, 'firewall').get('[aria-label="复制 IPv6 同口转发放行命令"]').trigger('click')
    expect(copyText).toHaveBeenCalledWith(command)
  })

  it('keeps node-only steps short and does not read routers', async () => {
    const wrapper = await mount({ mode: 'nodes' })
    expect(routerApi.report).not.toHaveBeenCalled()
    expect(routerApi.list).not.toHaveBeenCalled()
    expect(wrapper.findAll('[data-ipv6-step]').map(item => item.attributes('data-ipv6-step'))).toEqual(['host', 'publish', 'upstream', 'verify'])
    expect(shown(wrapper, 'upstream')).toBe(false)
    wrapper.getComponent(UpstreamRouterPending).vm.$emit('status', { state: 'pending', pending: 4 })
    await flushPromises()
    expect(shown(wrapper, 'upstream')).toBe(true)
    expect(status(wrapper, 'upstream')).toBe('4 项待发布')
  })

  it('passes the device and a refresh key to the upstream reminder and follows its status', async () => {
    const wrapper = await mount()
    const pending = wrapper.getComponent(UpstreamRouterPending)
    expect(pending.props()).toMatchObject({ agentId: 'gw', compact: true })
    const key = pending.props('refreshKey')
    pending.vm.$emit('status', { state: 'synced', pending: 0 })
    await flushPromises()
    expect(status(wrapper, 'upstream')).toBe('已同步')
    expect(row(wrapper, 'upstream').find('[data-row] p').exists()).toBe(false)
    await wrapper.setProps({ refreshKey: 1 })
    await flushPromises()
    expect(agentApi.ipv6Readiness).toHaveBeenCalledTimes(2)
    expect(pending.props('refreshKey')).not.toBe(key)
    pending.vm.$emit('status', { state: 'none', pending: 0 })
    await flushPromises()
    expect(status(wrapper, 'upstream')).toBe('未关联上级路由')
    expect(row(wrapper, 'upstream').getComponent(RouterLink).props('to')).toBe('/routers')
  })

  it('collapses acceptance to one line once the embedded verification passes', async () => {
    const wrapper = await mount()
    const verifier = wrapper.getComponent(MihomoIPv6Verification)
    expect(verifier.props()).toMatchObject({ open: true, agentId: 'gw', profileId: 'bound', configRevision: 7 })
    verifier.vm.$emit('result', { verification: null, fresh: false })
    await flushPromises()
    expect(status(wrapper, 'verify')).toBe('待验证')
    expect((wrapper.get('[data-ipv6-step-verify]').element as HTMLElement).style.display).not.toBe('none')
    verifier.vm.$emit('result', { verification: { ...passedVerification, status: 'running' }, fresh: true })
    await flushPromises()
    expect(status(wrapper, 'verify')).toBe('验证中')
    verifier.vm.$emit('result', { verification: passedVerification, fresh: true })
    await flushPromises()
    expect(status(wrapper, 'verify')).toBe('已通过')
    expect((wrapper.get('[data-ipv6-step-verify]').element as HTMLElement).style.display).toBe('none')
  })

  it('reads only saved configurations: waits while dirty and checks again after saving', async () => {
    const wrapper = await mount({ dirty: true })
    expect(agentApi.ipv6Readiness).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('保存草稿后自动重新检查')
    expect(status(wrapper, 'publish')).toBe('待保存')
    expect(status(wrapper, 'upstream')).toBe('保存后核对')
    expect(wrapper.findComponent(MihomoIPv6Verification).exists()).toBe(false)
    await wrapper.setProps({ dirty: false })
    await flushPromises()
    expect(agentApi.ipv6Readiness).toHaveBeenCalledExactlyOnceWith('gw', 'bound')
    expect(status(wrapper, 'publish')).toBe('已发布')
  })

  it('asks the device check window to open for the selected device', async () => {
    const wrapper = await mount()
    await wrapper.get('[aria-label="检查设备 IPv6"]').trigger('click')
    expect(wrapper.emitted('check')).toEqual([['gw']])
  })

  it('offers host preparation through the device check when host settings are missing', async () => {
    vi.mocked(agentApi.ipv6Readiness).mockResolvedValue(response(readiness({ can_prepare: true,
      checks: [{ id: 'host_forwarding_ra', label: '转发与 RA', status: 'warning', message: '未满足' }] })))
    const wrapper = await mount({ mode: 'nodes' })
    expect(status(wrapper, 'host')).toBe('需处理')
    expect(row(wrapper, 'host').text()).toContain('补齐缺少的配置')
    await row(wrapper, 'host').findAll('button').find(item => item.text().includes('检查设备'))!.trigger('click')
    expect(wrapper.emitted('check')).toEqual([['gw']])
  })

  it('requires an explicit device when several are bound and none is fixed', async () => {
    vi.mocked(agentApi.summaries).mockResolvedValue(response([gateway, { ...gateway, id: 'gw2', name: '书房 Mihomo' }]))
    const wrapper = await mount({ preferredAgentId: undefined })
    expect(agentApi.ipv6Readiness).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('选择要检查的 Mihomo 设备')
    wrapper.getComponent(Select).vm.$emit('update:modelValue', 'gw2')
    await flushPromises()
    expect(agentApi.ipv6Readiness).toHaveBeenCalledExactlyOnceWith('gw2', 'bound')
  })

  it('never replaces an unavailable entry device or checks an offline one', async () => {
    vi.mocked(agentApi.summaries).mockResolvedValue(response([{ ...gateway, status: 'offline' }]))
    let wrapper = await mount()
    expect(agentApi.ipv6Readiness).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('离线，上线后重新检查')
    vi.mocked(agentApi.summaries).mockResolvedValue(response([gateway, dnsAgent]))
    await wrapper.get('[aria-label="重新检查步骤"]').trigger('click')
    await flushPromises()
    expect(agentApi.ipv6Readiness).toHaveBeenCalledExactlyOnceWith('gw', 'bound')
    vi.mocked(agentApi.ipv6Readiness).mockClear()
    vi.mocked(agentApi.summaries).mockResolvedValue(response([{ ...gateway, status: 'offline' }]))
    wrapper = await mount({ preferredAgentId: 'missing' })
    expect(agentApi.ipv6Readiness).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('原目标设备已不属于此配置空间')
  })

  it('shows a readiness failure once and keeps every step unconfirmed', async () => {
    vi.mocked(agentApi.ipv6Readiness).mockRejectedValue({ response: { data: { message: '无法连接 Agent 进行 IPv6 检查' } } })
    const wrapper = await mount()
    expect(wrapper.text()).toContain('无法连接 Agent 进行 IPv6 检查')
    for (const id of ['host', 'publish', 'firewall', 'verify']) expect(status(wrapper, id)).toBe('待检查')
    expect(routerApi.report).not.toHaveBeenCalled()
  })
})
