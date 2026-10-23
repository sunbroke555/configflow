import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import Routers from '@/views/Routers.vue'
import { routerApi, agentApi, ruleApi } from '@/api'
import { activeProfileId } from '@/profileContext'
import { confirm, notify } from '@/lib/feedback'
import type { GatewayForwardingEntry } from '@/lib/gatewayForwarding'
import { DOCKER_COMMANDS, DOCKER_HINT, UFW_COMMANDS, mayDrop, quiet, ufwDrop } from './fixtures/forward-filter'

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
// The column header already says IPv4 转发: the button shows the action, and its full name is the accessible name.
const persistButtonIn = (wrapper: VueWrapper, index = 0) => cell(wrapper, index).find('button[aria-label="开启并持久化 IPv4 转发"]')
const persistButton = (wrapper: VueWrapper) => {
  const button = persistButtonIn(wrapper)
  return button.exists() ? button : undefined
}

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

  it('labels the persist button with the short action and keeps the full name for assistive tech', async () => {
    const wrapper = await mountRouters()
    const button = persistButton(wrapper)!
    expect(button.text()).toBe('开启并持久化')
    expect(button.attributes('title')).toBe('开启并持久化 IPv4 转发')
    expect(button.attributes('aria-busy')).toBeUndefined()
    // Touch targets stay 44px in the stacked phone layout.
    expect(button.classes()).toContain('max-md:min-h-11')
  })

  it('keeps the forwarding column narrow: the table opts into the compact columns and router status stays on one line', async () => {
    const wrapper = await mountRouters([entry({ state: 'blocked', can_persist: false, current: '1', message: 'IPv4 转发持久化文件已被修改，请先在设备上核对。' })])
    expect(wrapper.get('table').classes()).toEqual(expect.arrayContaining(['cf-mobile-records', 'router-records', 'router-records-forwarding']))
    expect(wrapper.get('td[data-label="状态"] > span').classes()).toContain('whitespace-nowrap')
    // Labels and notes in the column are no wider than the button on desktop and wrap instead of widening it.
    expect(cell(wrapper).get('[data-gateway-forwarding] > span').classes()).toContain('sm:max-w-[102px]')
    expect(cell(wrapper).get('[role="alert"]').classes()).toEqual(expect.arrayContaining(['max-w-[260px]', 'sm:max-w-[102px]']))
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
    // While the request runs the shorter text keeps the button from widening the column.
    expect(persistButton(wrapper)!.text()).toBe('开启中…')
    expect(persistButton(wrapper)!.attributes('aria-busy')).toBe('true')
    done.resolve({ data: { success: true, gateway: entry({ ...PERSISTED, gateway_record: true, preserves_forwarding: true }) } })
    await flushPromises()
    expect(notify.success).toHaveBeenCalledWith('IPv4 转发已开启并持久化。')
    expect(cell(wrapper, 0).text()).toContain('已开启并持久化')
    expect(cell(wrapper, 1).text()).toContain('已开启并持久化')
    expect(routerApi.gatewayForwarding).toHaveBeenCalledTimes(1)
  })

  it('re-reads the column after a persist that overtook a read, so a newly linked gateway is not left blank', async () => {
    vi.mocked(confirm).mockResolvedValue(true)
    vi.mocked(routerApi.probe).mockResolvedValue({ data: { success: true, probe: { version: '4.0' } } } as any)
    const posted = deferred<any>()
    vi.mocked(routerApi.persistGatewayForwarding).mockReturnValue(posted.promise)
    const wrapper = await mountRouters()
    await persistButton(wrapper)!.trigger('click')
    await flushPromises()

    // While the POST waits, the router list now links a second gateway and a read starts for it.
    const B = entry({ agent_id: 'a2', agent_name: 'lxc-mihomo', routers: [{ id: 'r2', name: '旁路由' }], current: '1' })
    vi.mocked(routerApi.list).mockResolvedValue({ data: { success: true, routers: [ROUTER, { ...ROUTER, id: 'r2', name: '旁路由', proxy_gateway_agent_id: 'a2' }] } } as any)
    const staleRead = deferred<any>()
    vi.mocked(routerApi.gatewayForwarding).mockReturnValueOnce(staleRead.promise)
    await wrapper.findAll('button').find(button => button.text().includes('探测'))!.trigger('click')
    await flushPromises()
    expect(routerApi.gatewayForwarding).toHaveBeenCalledTimes(2)
    expect(cell(wrapper, 1).text()).toContain('读取中…')

    const freshRead = deferred<any>()
    vi.mocked(routerApi.gatewayForwarding).mockReturnValueOnce(freshRead.promise)
    posted.resolve({ data: { success: true, gateway: entry(PERSISTED) } })
    await flushPromises()
    expect(routerApi.gatewayForwarding).toHaveBeenCalledTimes(3)
    expect(cell(wrapper, 0).text()).toContain('已开启并持久化')

    // The read issued before the POST finished is discarded even if it lands last.
    freshRead.resolve({ data: { success: true, gateways: [entry(PERSISTED), B] } })
    await flushPromises()
    staleRead.resolve({ data: { success: true, gateways: [entry(), B] } })
    await flushPromises()
    expect(cell(wrapper, 0).text()).toContain('已开启并持久化')
    expect(cell(wrapper, 1).text()).toContain('已开启，未持久化')
  })

  it('does not issue an extra read after a persist when no read was in flight', async () => {
    vi.mocked(confirm).mockResolvedValue(true)
    vi.mocked(routerApi.persistGatewayForwarding).mockResolvedValue({ data: { success: true, gateway: entry(PERSISTED) } } as any)
    const wrapper = await mountRouters()
    await persistButton(wrapper)!.trigger('click')
    await flushPromises()
    expect(routerApi.gatewayForwarding).toHaveBeenCalledTimes(1)
    expect(cell(wrapper).text()).toContain('已开启并持久化')
  })

  it('disables the other gateways\' persist buttons while one gateway is being persisted', async () => {
    vi.mocked(routerApi.list).mockResolvedValue({ data: { success: true, routers: [ROUTER, { ...ROUTER, id: 'r2', name: '旁路由', proxy_gateway_agent_id: 'a2' }] } } as any)
    vi.mocked(confirm).mockResolvedValue(true)
    const posted = deferred<any>()
    vi.mocked(routerApi.persistGatewayForwarding).mockReturnValue(posted.promise)
    const wrapper = await mountRouters([entry(), entry({ agent_id: 'a2', agent_name: 'lxc-mihomo', routers: [{ id: 'r2', name: '旁路由' }] })])
    const buttonOf = (index: number) => persistButtonIn(wrapper, index)
    expect(buttonOf(1).attributes('disabled')).toBeUndefined()
    await buttonOf(0).trigger('click')
    await flushPromises()
    expect(buttonOf(0).attributes('disabled')).toBeDefined()
    expect(buttonOf(1).attributes('disabled')).toBeDefined()
    posted.resolve({ data: { success: true, gateway: entry(PERSISTED) } })
    await flushPromises()
    expect(buttonOf(1).attributes('disabled')).toBeUndefined()
    expect(routerApi.persistGatewayForwarding).toHaveBeenCalledExactlyOnceWith('a1', 'default')
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

describe('routers page forward-filter warning', () => {
  const filterButton = (wrapper: VueWrapper, index = 0) => cell(wrapper, index).find('button[aria-label="查看放行 TUN 转发的命令"]')

  it('adds a short warning and a commands dialog without changing the forwarding status or action', async () => {
    const wrapper = await mountRouters([entry({ forward_filter: mayDrop() })])
    expect(cell(wrapper).text()).toContain('未开启')
    expect(persistButton(wrapper)).toBeDefined()
    const short = cell(wrapper).get('[data-forward-filter-short]')
    expect(short.text()).toBe('可能拦截 UDP')
    // One line within the 102px column (lab v5-2: the longer phrase wrapped at 1400px).
    expect(short.classes()).toEqual(expect.arrayContaining(['text-[11.5px]', 'text-warning-accent', 'sm:max-w-[102px]', 'whitespace-nowrap']))
    const button = filterButton(wrapper)
    expect(button.text()).toBe('放行命令')
    expect(button.attributes('title')).toBe('查看放行 TUN 转发的命令')
    expect(button.attributes('data-variant')).toBe('ghost')
    expect(button.classes()).toContain('max-md:min-h-11')

    await button.trigger('click'); await flushPromises()
    const dialog = document.body.querySelector<HTMLElement>('[role="dialog"]')!
    expect(dialog.textContent).toContain('放行「pve-mihomo」的 TUN 转发')
    expect(dialog.textContent).toContain(DOCKER_HINT)
    expect(dialog.querySelector('pre')!.textContent).toBe(DOCKER_COMMANDS.join('\n'))
    expect(dialog.className).toContain('sm:max-w-[560px]')
  })

  it('says forwarding may be blocked when redirected TCP is, and shows the UFW commands verbatim', async () => {
    const wrapper = await mountRouters([entry({ forward_filter: ufwDrop() })])
    expect(cell(wrapper).get('[data-forward-filter-short]').text()).toBe('可能拦截转发')
    await filterButton(wrapper).trigger('click'); await flushPromises()
    const dialog = document.body.querySelector<HTMLElement>('[role="dialog"]')!
    expect(dialog.querySelector('pre')!.textContent).toBe(UFW_COMMANDS.join('\n'))
    expect(dialog.textContent).toContain('主路由转来的 TCP 连接会失败')
  })

  it.each([
    ['an older Agent', undefined],
    ['no report', null],
    ['ok', quiet('ok')],
    ['unknown', quiet('unknown', 'timeout')],
    ['a selective gateway', quiet('not_applicable', 'linux_gateway')],
  ])('adds nothing for %s', async (_case, forward_filter) => {
    const wrapper = await mountRouters([entry({ ...PERSISTED, forward_filter })])
    expect(cell(wrapper).find('[data-forward-filter-short]').exists()).toBe(false)
    expect(filterButton(wrapper).exists()).toBe(false)
    expect(cell(wrapper).text()).toBe('已开启并持久化')
  })

  it('keeps the last warning when the persist response carries none', async () => {
    vi.mocked(confirm).mockResolvedValue(true)
    vi.mocked(routerApi.persistGatewayForwarding).mockResolvedValue({ data: { success: true, gateway: entry({ ...PERSISTED, forward_filter: null }) } } as any)
    const wrapper = await mountRouters([entry({ forward_filter: mayDrop() })])
    await persistButton(wrapper)!.trigger('click'); await flushPromises()
    expect(cell(wrapper).text()).toContain('已开启并持久化')
    expect(cell(wrapper).get('[data-forward-filter-short]').text()).toBe('可能拦截 UDP')
  })
})
