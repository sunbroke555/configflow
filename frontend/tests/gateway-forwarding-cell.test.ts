import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import Routers from '@/views/Routers.vue'
import { routerApi, agentApi, ruleApi } from '@/api'
import { activeProfileId } from '@/profileContext'
import { confirm, notify } from '@/lib/feedback'
import type { GatewayForwardingEntry } from '@/lib/gatewayForwarding'

vi.mock('@/api', () => ({
  agentApi: { getAll: vi.fn() },
  routerApi: {
    list: vi.fn(), create: vi.fn(), update: vi.fn(), remove: vi.fn(), probe: vi.fn(), report: vi.fn(), plan: vi.fn(), apply: vi.fn(),
    routes: vi.fn(), routePlan: vi.fn(), routeApply: vi.fn(), policy: vi.fn(), policySettings: vi.fn(), savePolicySettings: vi.fn(),
    previewPolicySettings: vi.fn(), gatewayForwarding: vi.fn(), persistGatewayForwarding: vi.fn()
  },
  ruleApi: { getAll: vi.fn() }, oxidnsMonitorApi: { overview: vi.fn() }
}))
vi.mock('@/lib/feedback', () => ({ notify: { success: vi.fn(), error: vi.fn(), info: vi.fn() }, confirmDanger: vi.fn(), confirm: vi.fn(), confirmState: { resolve: null }, settleConfirm: vi.fn() }))
vi.mock('@/api/setup', () => ({ setupApi: { get: vi.fn(), saveRouterDraft: vi.fn() } }))

const ROUTER = {
  id: 'r1', name: '主路由', type: 'ikuai', endpoint: 'http://192.0.2.1', username: 'admin', verify_tls: true,
  proxy_gateway_agent_id: 'a1', dns_agent_id: '', has_password: true
}
const entry = (changes: Partial<GatewayForwardingEntry> = {}): GatewayForwardingEntry => ({
  agent_id: 'a1', agent_name: 'pve-mihomo', agent_version: '1.8.41-go', routers: [{ id: 'r1', name: '主路由' }],
  state: 'needs_persist', can_persist: true, message: '', minimum_version: null,
  current: '0', persisted: false, config: 'missing', hook_script: 'missing', startup_hook: 'missing',
  layout: 'standalone', gateway_record: false, preserves_forwarding: false, ...changes
})
const PERSISTED = { state: 'persisted', can_persist: false, current: '1', persisted: true, config: 'managed', hook_script: 'managed', startup_hook: 'registered' } as const
const deferred = <T,>() => {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}

const wrappers: VueWrapper[] = []
beforeEach(() => {
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  vi.mocked(agentApi.getAll).mockResolvedValue({ data: [] } as any)
  vi.mocked(ruleApi.getAll).mockResolvedValue({ data: [] } as any)
  vi.mocked(routerApi.list).mockResolvedValue({ data: { success: true, routers: [ROUTER] } } as any)
})
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  activeProfileId.value = 'default'
  vi.resetAllMocks()
  vi.unstubAllGlobals()
})

const mountRouters = async (gateways: GatewayForwardingEntry[] | null = [entry()], props = {}) => {
  if (gateways) vi.mocked(routerApi.gatewayForwarding).mockResolvedValue({ data: { success: true, gateways } } as any)
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/routers', component: { render: () => null } }, { path: '/agents', component: { render: () => null } }] })
  await router.push('/routers')
  const wrapper = mount(Routers, { attachTo: document.body, props, global: { plugins: [router] } })
  wrappers.push(wrapper)
  await flushPromises()
  return wrapper
}
const cell = (wrapper: VueWrapper, index = 0) => wrapper.findAll('td[data-label="IPv4 转发"]')[index]!
const persistButton = (wrapper: VueWrapper) => cell(wrapper).findAll('button').find(button => button.text().includes('开启并持久化 IPv4 转发'))

describe('routers page IPv4 forwarding column', () => {
  it('appears only on the standalone page and reads the active profile', async () => {
    const embedded = await mountRouters([entry()], { embedded: true, profileId: 'setup-profile' })
    expect(embedded.findAll('th').map(th => th.text())).not.toContain('IPv4 转发')
    expect(embedded.find('td[data-label="IPv4 转发"]').exists()).toBe(false)
    expect(routerApi.gatewayForwarding).not.toHaveBeenCalled()

    activeProfileId.value = 'profile-b'
    const page = await mountRouters()
    expect(page.findAll('th').map(th => th.text())).toContain('IPv4 转发')
    expect(routerApi.gatewayForwarding).toHaveBeenCalledExactlyOnceWith('profile-b')
    expect(cell(page).text()).toContain('未开启')
  })

  it('shows no action once forwarding is persisted, and a dash for routers without a proxy gateway', async () => {
    vi.mocked(routerApi.list).mockResolvedValue({ data: { success: true, routers: [ROUTER, { ...ROUTER, id: 'r2', name: '旁路由', proxy_gateway_agent_id: '' }] } } as any)
    const wrapper = await mountRouters([entry(PERSISTED)])
    expect(cell(wrapper, 0).text()).toContain('已开启并持久化')
    expect(cell(wrapper, 0).find('button').exists()).toBe(false)
    expect(cell(wrapper, 1).text()).toBe('—')
  })

  it('asks a 1.8.40 standalone Agent to update first, with a link to the Agent page', async () => {
    const wrapper = await mountRouters([entry({ state: 'upgrade_required', can_persist: false, minimum_version: '1.8.41-go', agent_version: '1.8.40-go', current: '0' })])
    expect(cell(wrapper).text()).toContain('请先更新 Agent')
    expect(cell(wrapper).text()).toContain('需要 1.8.41-go 或更新版本')
    expect(cell(wrapper).get('a').attributes('href')).toBe('/agents')
    expect(cell(wrapper).get('a').text()).toBe('前往 Agent 页更新')
    expect(persistButton(wrapper)).toBeUndefined()
  })

  it('confirms with the gateway record note, posts the Agent id and replaces the entry for every router of that Agent', async () => {
    vi.mocked(routerApi.list).mockResolvedValue({ data: { success: true, routers: [ROUTER, { ...ROUTER, id: 'r2', name: '备用主路由' }] } } as any)
    vi.mocked(confirm).mockResolvedValue(true)
    const done = deferred<any>()
    vi.mocked(routerApi.persistGatewayForwarding).mockReturnValue(done.promise)
    const wrapper = await mountRouters([entry({ gateway_record: true })])
    await persistButton(wrapper)!.trigger('click')
    await flushPromises()
    expect(confirm).toHaveBeenCalledExactlyOnceWith(
      '将在「pve-mihomo」写入 /etc/sysctl.d/99-configflow-aio-forward.conf，并在 Mihomo Agent 启动前注册转发检查，随后立即开启 IPv4 转发；不修改核心配置，也不重启 Mihomo 或 Agent。这台设备有 Linux 网关记录：之后撤销网关时会保留 IPv4 转发开启。',
      { title: '开启「pve-mihomo」的 IPv4 转发？', confirmText: '开启并持久化' })
    expect(routerApi.persistGatewayForwarding).toHaveBeenCalledExactlyOnceWith('a1', 'default')
    expect(persistButton(wrapper)!.attributes('disabled')).toBeDefined()
    done.resolve({ data: { success: true, gateway: entry({ ...PERSISTED, gateway_record: true, preserves_forwarding: true }) } })
    await flushPromises()
    expect(notify.success).toHaveBeenCalledWith('IPv4 转发已开启并持久化。')
    expect(cell(wrapper, 0).text()).toContain('已开启并持久化')
    expect(cell(wrapper, 1).text()).toContain('已开启并持久化')
    expect(routerApi.gatewayForwarding).toHaveBeenCalledTimes(1)
  })

  it('does not post when the confirmation is cancelled', async () => {
    vi.mocked(confirm).mockResolvedValue(false)
    const wrapper = await mountRouters([entry({ current: '1' })])
    expect(cell(wrapper).text()).toContain('已开启，未持久化')
    await persistButton(wrapper)!.trigger('click')
    await flushPromises()
    expect(confirm).toHaveBeenCalledOnce()
    expect(vi.mocked(confirm).mock.calls[0]![0]).not.toContain('Linux 网关记录')
    expect(routerApi.persistGatewayForwarding).not.toHaveBeenCalled()
  })

  it('reports the server reason on failure and reloads the column', async () => {
    vi.mocked(confirm).mockResolvedValue(true)
    vi.mocked(routerApi.persistGatewayForwarding).mockRejectedValue({ response: { status: 409, data: {
      success: false, code: 'FORWARDING_PERSIST_FAILED', message: 'IPv4 转发持久化文件已被修改，请先在设备上核对。' } } })
    const wrapper = await mountRouters()
    vi.mocked(routerApi.gatewayForwarding).mockResolvedValue({ data: { success: true, gateways: [entry({
      state: 'blocked', can_persist: false, config: 'foreign', message: 'IPv4 转发持久化文件已被修改，请先在设备上核对。' })] } } as any)
    await persistButton(wrapper)!.trigger('click')
    await flushPromises()
    expect(notify.error).toHaveBeenCalledWith('IPv4 转发持久化文件已被修改，请先在设备上核对。')
    expect(routerApi.gatewayForwarding).toHaveBeenCalledTimes(2)
    expect(cell(wrapper).text()).toContain('持久化文件已被修改')
    expect(cell(wrapper).get('[role="alert"]').text()).toBe('IPv4 转发持久化文件已被修改，请先在设备上核对。')
    expect(persistButton(wrapper)).toBeUndefined()
  })

  it('shows 无法读取 on linked rows without a toast when the column cannot be read', async () => {
    vi.mocked(routerApi.gatewayForwarding).mockRejectedValue(new Error('timeout'))
    const wrapper = await mountRouters(null)
    expect(cell(wrapper).text()).toContain('无法读取')
    expect(notify.error).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('主路由')
  })

  it('discards a response that arrives after the profile changed', async () => {
    const first = deferred<any>()
    vi.mocked(routerApi.gatewayForwarding).mockReturnValueOnce(first.promise)
    const wrapper = await mountRouters(null)
    expect(cell(wrapper).text()).toContain('读取中…')
    vi.mocked(routerApi.gatewayForwarding).mockResolvedValueOnce({ data: { success: true, gateways: [entry({ current: '1' })] } } as any)
    activeProfileId.value = 'profile-b'
    await flushPromises()
    expect(routerApi.gatewayForwarding).toHaveBeenLastCalledWith('profile-b')
    first.resolve({ data: { success: true, gateways: [entry(PERSISTED)] } })
    await flushPromises()
    expect(cell(wrapper).text()).toContain('已开启，未持久化')
  })
})
