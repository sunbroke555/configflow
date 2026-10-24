import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import Routers from '@/views/Routers.vue'
import { routerApi, agentApi, ruleApi } from '@/api'
import { activeProfileId } from '@/profileContext'
import { notify } from '@/lib/feedback'
import type { GatewayForwardingEntry } from '@/lib/gatewayForwarding'
import { mayDrop, quiet } from './fixtures/forward-filter'

vi.mock('@/api', () => ({
  agentApi: { getAll: vi.fn() },
  routerApi: {
    list: vi.fn(), create: vi.fn(), update: vi.fn(), remove: vi.fn(), probe: vi.fn(), report: vi.fn(), plan: vi.fn(), apply: vi.fn(),
    routes: vi.fn(), routePlan: vi.fn(), routeApply: vi.fn(), policy: vi.fn(), policySettings: vi.fn(), savePolicySettings: vi.fn(),
    previewPolicySettings: vi.fn(), gatewayForwarding: vi.fn()
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
const deferred = <T,>() => {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}

const wrappers: VueWrapper[] = []
beforeEach(() => {
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  vi.mocked(agentApi.getAll).mockResolvedValue({ data: [] } as any)
  vi.mocked(ruleApi.getAll).mockResolvedValue({ data: [] } as any)
  vi.mocked(routerApi.list).mockResolvedValue({ data: { success: true, routers: [ROUTER, { ...ROUTER, id: 'r2', name: '旁路', proxy_gateway_agent_id: '' }] } } as any)
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
const warnings = (wrapper: VueWrapper) => wrapper.findAll('[data-router-forwarding-warning]')

describe('routers page proxy gateway forwarding', () => {
  it('has no forwarding column or persist button; the state and the action live on the Agent page', async () => {
    const wrapper = await mountRouters([entry({ current: '1' })])
    expect(wrapper.findAll('th').map(th => th.text())).not.toContain('IPv4 转发')
    expect(wrapper.find('td[data-label="IPv4 转发"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('开启并持久化')
    expect(wrapper.get('table').classes()).toContain('min-w-[880px]')
    expect(wrapper.get('table').classes()).not.toContain('router-records-forwarding')
    // Enabled but not persisted is not a traffic problem: the router row stays quiet.
    expect(warnings(wrapper)).toHaveLength(0)
  })

  it('warns under the status of the routers using a gateway with forwarding off, linking to the Agent page', async () => {
    activeProfileId.value = 'profile-b'
    const wrapper = await mountRouters()
    expect(routerApi.gatewayForwarding).toHaveBeenCalledExactlyOnceWith('profile-b')
    const [warning] = warnings(wrapper)
    expect(warnings(wrapper)).toHaveLength(1)
    expect(warning!.element.closest('td')!.getAttribute('data-label')).toBe('状态')
    expect(warning!.text()).toContain('代理网关「pve-mihomo」未开启 IPv4 转发')
    expect(warning!.classes()).toContain('text-warning-accent')
    // The device name never breaks in the middle; a long one is truncated with its full name on hover.
    const name = warning!.get('span[title="pve-mihomo"]')
    expect(name.classes()).toEqual(expect.arrayContaining(['inline-block', 'truncate']))
    const link = warning!.get('a')
    expect(link.text()).toBe('前往 Agent 页处理')
    expect(link.attributes('href')).toBe('/agents')
    expect(link.classes()).toContain('max-md:min-h-11')
  })

  it('warns when a firewall may drop the forwarded traffic', async () => {
    const wrapper = await mountRouters([entry({ state: 'persisted', can_persist: false, current: '1', persisted: true, forward_filter: mayDrop() })])
    expect(warnings(wrapper)[0]!.text()).toContain('代理网关「pve-mihomo」可能拦截')
  })

  it.each([
    ['persisted', entry({ state: 'persisted', can_persist: false, current: '1', persisted: true, forward_filter: quiet('ok') })],
    ['offline', entry({ state: 'offline', can_persist: false, current: null })],
    ['update needed', entry({ state: 'upgrade_required', can_persist: false, current: null })],
  ])('stays quiet when the gateway is %s', async (_name, gateway) => {
    expect(warnings(await mountRouters([gateway]))).toHaveLength(0)
  })

  it('shows nothing and no toast when the forwarding state cannot be read', async () => {
    vi.mocked(routerApi.gatewayForwarding).mockRejectedValue(new Error('timeout'))
    const wrapper = await mountRouters(null)
    expect(warnings(wrapper)).toHaveLength(0)
    expect(notify.error).not.toHaveBeenCalled()
  })

  it('does not read or warn when embedded in the setup wizard', async () => {
    const wrapper = await mountRouters([entry()], { embedded: true, profileId: 'setup-profile' })
    expect(routerApi.gatewayForwarding).not.toHaveBeenCalled()
    expect(warnings(wrapper)).toHaveLength(0)
  })

  it('discards a response that arrives after the profile changed', async () => {
    const stale = deferred<any>()
    vi.mocked(routerApi.gatewayForwarding).mockReturnValueOnce(stale.promise)
    const wrapper = await mountRouters(null)
    vi.mocked(routerApi.gatewayForwarding).mockResolvedValueOnce({ data: { success: true, gateways: [] } } as any)
    activeProfileId.value = 'profile-b'
    await flushPromises()
    stale.resolve({ data: { success: true, gateways: [entry()] } })
    await flushPromises()
    expect(routerApi.gatewayForwarding).toHaveBeenLastCalledWith('profile-b')
    expect(warnings(wrapper)).toHaveLength(0)
  })
})
