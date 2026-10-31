import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { defineComponent, h, nextTick, ref } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import Agents from '@/views/Agents.vue'
import AgentGatewayForwarding from '@/components/agents/AgentGatewayForwarding.vue'
import { agentApi, profileApi } from '@/api'
import { confirm, notify } from '@/lib/feedback'
import { setActiveProfileId } from '@/profileContext'
import { useAgentGatewayForwarding } from '@/composables/useAgentGatewayForwarding'
import type { GatewayForwardingEntry } from '@/lib/gatewayForwarding'
import { mayDrop } from './fixtures/forward-filter'

vi.mock('@/api', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
  agentApi: {
    getAll: vi.fn(), getDeployment: vi.fn(), getUpgrade: vi.fn(), bindProfile: vi.fn(), pushConfig: vi.fn(),
    gatewayForwarding: vi.fn(), persistGatewayForwarding: vi.fn()
  },
  profileApi: { list: vi.fn() }
}))
vi.mock('@/lib/feedback', async importOriginal => {
  const actual = await importOriginal<typeof import('@/lib/feedback')>()
  return { ...actual, confirm: vi.fn(), notify: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() } }
})

const entry = (changes: Partial<GatewayForwardingEntry> = {}): GatewayForwardingEntry => ({
  agent_id: 'a1', agent_name: 'pve-mihomo', agent_version: '1.8.41-go',
  routers: [{ id: 'r1', name: 'ROS', profile_id: 'default', profile_name: 'Default' }],
  state: 'needs_persist', can_persist: true, message: '', minimum_version: null,
  current: '1', persisted: false, config: 'absent', hook_script: 'absent', startup_hook: 'absent',
  layout: 'standalone', gateway_record: false, preserves_forwarding: false, ...changes
})
const PERSISTED = { state: 'persisted', can_persist: false, current: '1', persisted: true, config: 'managed', hook_script: 'managed', startup_hook: 'registered' } as const
const read = (gateways: GatewayForwardingEntry[]) => ({ data: { success: true, gateways } }) as any
const deferred = <T,>() => {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}

const wrappers: VueWrapper<any>[] = []
const mediaDescriptor = Object.getOwnPropertyDescriptor(window, 'matchMedia')
const canvasDescriptor = Object.getOwnPropertyDescriptor(HTMLCanvasElement.prototype, 'getContext')
beforeAll(() => {
  Object.defineProperty(window, 'matchMedia', { configurable: true, value: (query: string) => ({
    matches: query.includes('prefers-reduced-motion'), media: query,
    addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn()
  }) })
  Object.defineProperty(HTMLCanvasElement.prototype, 'getContext', { configurable: true, value: () => null })
})
afterAll(() => {
  if (mediaDescriptor) Object.defineProperty(window, 'matchMedia', mediaDescriptor)
  else Reflect.deleteProperty(window, 'matchMedia')
  if (canvasDescriptor) Object.defineProperty(HTMLCanvasElement.prototype, 'getContext', canvasDescriptor)
  else Reflect.deleteProperty(HTMLCanvasElement.prototype, 'getContext')
})
beforeEach(() => {
  vi.clearAllMocks()
  setActiveProfileId('default')
})
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  document.body.innerHTML = ''
})

describe('AgentGatewayForwarding row', () => {
  const render = (props: { entry: GatewayForwardingEntry; busy?: boolean; disabled?: boolean }) => {
    const wrapper = mount(AgentGatewayForwarding, { props: { busy: false, ...props } })
    wrappers.push(wrapper)
    return wrapper
  }
  const row = (wrapper: VueWrapper<any>) => wrapper.get('[data-agent-status-row="IPv4 转发"]')

  it('shows the state, offers the persist action and explains which routers need it', async () => {
    const wrapper = render({ entry: entry() })
    expect(row(wrapper).attributes('data-tone')).toBe('warning')
    expect(row(wrapper).text()).toContain('已开启，未持久化')
    const button = wrapper.get('[data-status-notice] button')
    expect(button.text()).toBe('开启并持久化')
    expect(button.classes()).toContain('min-h-11')
    await button.trigger('click')
    expect(wrapper.emitted('persist')).toHaveLength(1)
    // The reason is collapsed; expanding shows it.
    const details = wrapper.get('[data-status-details]')
    expect((details.element as HTMLElement).style.display).toBe('none')
    await row(wrapper).get('button[aria-expanded]').trigger('click')
    expect(details.text()).toContain('路由器「ROS」把要分流的流量转给这台设备，需要开启 IPv4 转发')
  })

  it('marks forwarding that is off as an alert', () => {
    const wrapper = render({ entry: entry({ current: '0', message: '读取到 ip_forward=0' }) })
    expect(row(wrapper).attributes('data-tone')).toBe('danger')
    expect(wrapper.get('[role="alert"]').text()).toBe('读取到 ip_forward=0')
  })

  it('shows no action once persisted and names the persisted file in the details', () => {
    const wrapper = render({ entry: entry(PERSISTED) })
    expect(row(wrapper).attributes('data-tone')).toBe('success')
    expect(wrapper.find('[data-status-notice]').exists()).toBe(false)
    expect(wrapper.get('[data-status-details]').text()).toContain('/etc/sysctl.d/99-configflow-aio-forward.conf')
  })

  it('shows system persistence as done, naming the host\'s own sysctl file', async () => {
    const wrapper = render({ entry: entry({ state: 'system_persisted', current: '1', system_source: '/etc/sysctl.d/99-ip-forward.conf:1' }) })
    expect(row(wrapper).attributes('data-tone')).toBe('success')
    expect(row(wrapper).text()).toContain('已开启，系统已持久化')
    expect(wrapper.find('[data-status-notice]').exists()).toBe(false)
    const details = wrapper.get('[data-status-details]').text()
    expect(details).toContain('/etc/sysctl.d/99-ip-forward.conf:1')
    expect(details).not.toContain('99-configflow-aio-forward.conf')
  })

  it('asks for an update without a persist button', () => {
    const wrapper = render({ entry: entry({ state: 'upgrade_required', can_persist: false, current: null, minimum_version: '1.8.41-go' }) })
    expect(row(wrapper).text()).toContain('请先更新 Agent')
    expect(wrapper.get('[data-status-notice]').text()).toContain('需要 1.8.41-go 或更新版本')
    expect(wrapper.find('[data-status-notice] button').exists()).toBe(false)
  })

  it('keeps the button width while busy and disables it for another device', () => {
    const busy = render({ entry: entry(), busy: true })
    const button = busy.get('[data-status-notice] button')
    expect(button.text()).toBe('开启中…')
    expect(button.attributes('aria-busy')).toBe('true')
    expect(button.attributes('disabled')).toBeDefined()
    expect(render({ entry: entry(), disabled: true }).get('[data-status-notice] button').attributes('disabled')).toBeDefined()
  })

  it('adds a firewall row with the remedy commands only when a firewall may drop traffic', async () => {
    expect(render({ entry: entry(PERSISTED) }).find('[data-forward-filter-row]').exists()).toBe(false)
    const wrapper = render({ entry: entry({ ...PERSISTED, forward_filter: mayDrop() }) })
    const filterRow = wrapper.get('[data-forward-filter-row]')
    expect(filterRow.attributes('data-tone')).toBe('warning')
    expect(filterRow.text()).toContain('可能拦截')
    await filterRow.get('button[aria-expanded]').trigger('click')
    expect(filterRow.find('[data-forward-filter-notice]').exists()).toBe(true)
    // The firewall reminder never changes the forwarding row.
    expect(row(wrapper).attributes('data-tone')).toBe('success')
  })
})

describe('useAgentGatewayForwarding', () => {
  const harness = () => {
    const agents = ref([{ id: 'a1', status: 'online', version: '1.8.41-go', last_heartbeat: '1' }])
    let api!: ReturnType<typeof useAgentGatewayForwarding>
    const wrapper = mount(defineComponent({ setup() { api = useAgentGatewayForwarding(agents); return () => h('div') } }))
    wrappers.push(wrapper)
    return { agents, api: () => api }
  }

  it('reads once per device change, not on every heartbeat', async () => {
    vi.mocked(agentApi.gatewayForwarding).mockResolvedValue(read([entry()]))
    const { agents, api } = harness()
    await flushPromises()
    expect(agentApi.gatewayForwarding).toHaveBeenCalledTimes(1)
    expect(api().entries.value.a1?.current).toBe('1')
    agents.value = [{ ...agents.value[0]!, last_heartbeat: '2' }]
    await flushPromises()
    expect(agentApi.gatewayForwarding).toHaveBeenCalledTimes(1)
    agents.value = [{ ...agents.value[0]!, version: '1.8.42-go' }]
    await flushPromises()
    expect(agentApi.gatewayForwarding).toHaveBeenCalledTimes(2)
  })

  it('does not read before any device is listed', async () => {
    const agents = ref<{ id: string }[]>([])
    mount(defineComponent({ setup() { useAgentGatewayForwarding(agents); return () => h('div') } }))
    await flushPromises()
    expect(agentApi.gatewayForwarding).not.toHaveBeenCalled()
  })

  it('keeps the last state without a toast when a read fails', async () => {
    vi.mocked(agentApi.gatewayForwarding).mockResolvedValueOnce(read([entry()])).mockRejectedValueOnce(new Error('timeout'))
    const { agents, api } = harness()
    await flushPromises()
    agents.value = [{ ...agents.value[0]!, status: 'offline' }]
    await flushPromises()
    expect(api().entries.value.a1?.state).toBe('needs_persist')
    expect(notify.error).not.toHaveBeenCalled()
  })

  it('confirms, posts only the Agent id and keeps the last firewall check', async () => {
    vi.mocked(agentApi.gatewayForwarding).mockResolvedValue(read([entry({ gateway_record: true, forward_filter: mayDrop() })]))
    vi.mocked(confirm).mockResolvedValue(true)
    vi.mocked(agentApi.persistGatewayForwarding).mockResolvedValue({ data: { success: true, gateway: entry({ ...PERSISTED, forward_filter: null }) } } as any)
    const { api } = harness()
    await flushPromises()
    await api().persist('a1')
    expect(vi.mocked(confirm).mock.calls[0]![0]).toContain('之后撤销网关时会保留 IPv4 转发开启')
    expect(agentApi.persistGatewayForwarding).toHaveBeenCalledExactlyOnceWith('a1')
    expect(notify.success).toHaveBeenCalledWith('IPv4 转发已开启并持久化。')
    expect(api().entries.value.a1?.state).toBe('persisted')
    expect(api().entries.value.a1?.forward_filter?.state).toBe('may_drop')
    expect(api().busy.value).toBe('')
    // No read was in flight, so no extra read.
    expect(agentApi.gatewayForwarding).toHaveBeenCalledTimes(1)
  })

  it('does not post when the confirmation is cancelled', async () => {
    vi.mocked(agentApi.gatewayForwarding).mockResolvedValue(read([entry()]))
    vi.mocked(confirm).mockResolvedValue(false)
    const { api } = harness()
    await flushPromises()
    await api().persist('a1')
    expect(agentApi.persistGatewayForwarding).not.toHaveBeenCalled()
  })

  it('lets one device persist at a time', async () => {
    vi.mocked(agentApi.gatewayForwarding).mockResolvedValue(read([entry(), entry({ agent_id: 'a2', agent_name: 'other' })]))
    vi.mocked(confirm).mockResolvedValue(true)
    const pending = deferred<any>()
    vi.mocked(agentApi.persistGatewayForwarding).mockReturnValueOnce(pending.promise)
    const { api } = harness()
    await flushPromises()
    const first = api().persist('a1')
    await nextTick()
    await nextTick()
    expect(api().busy.value).toBe('a1')
    await api().persist('a2')
    expect(notify.info).toHaveBeenCalledWith('另一台设备正在开启 IPv4 转发，请稍候。')
    expect(agentApi.persistGatewayForwarding).toHaveBeenCalledTimes(1)
    pending.resolve({ data: { success: true, gateway: entry(PERSISTED) } })
    await first
  })

  it('re-reads after a persist that overtook a read', async () => {
    vi.mocked(agentApi.gatewayForwarding).mockResolvedValueOnce(read([entry()]))
    vi.mocked(confirm).mockResolvedValue(true)
    const { agents, api } = harness()
    await flushPromises()
    const stale = deferred<any>()
    vi.mocked(agentApi.gatewayForwarding).mockReturnValueOnce(stale.promise)
    agents.value = [{ ...agents.value[0]!, version: '1.8.42-go' }]
    await nextTick()
    vi.mocked(agentApi.persistGatewayForwarding).mockResolvedValue({ data: { success: true, gateway: entry(PERSISTED) } } as any)
    vi.mocked(agentApi.gatewayForwarding).mockResolvedValueOnce(read([entry(PERSISTED)]))
    await api().persist('a1')
    stale.resolve(read([entry()]))
    await flushPromises()
    expect(agentApi.gatewayForwarding).toHaveBeenCalledTimes(3)
    expect(api().entries.value.a1?.state).toBe('persisted')
  })

  it('reports the server reason on failure and reads again', async () => {
    vi.mocked(agentApi.gatewayForwarding).mockResolvedValue(read([entry()]))
    vi.mocked(confirm).mockResolvedValue(true)
    vi.mocked(agentApi.persistGatewayForwarding).mockRejectedValue({ response: { data: { code: 'AGENT_OFFLINE', message: 'Agent 离线，无法开启转发。' } } })
    const { api } = harness()
    await flushPromises()
    await api().persist('a1')
    await flushPromises()
    expect(notify.error).toHaveBeenCalledWith('Agent 离线，无法开启转发。')
    expect(agentApi.gatewayForwarding).toHaveBeenCalledTimes(2)
    expect(api().busy.value).toBe('')
  })
})

describe('Agents page', () => {
  const agent = (overrides: Record<string, unknown> = {}) => ({
    id: 'a1', name: 'pve-mihomo', host: '192.0.2.3', port: 8080, profile_id: 'default', status: 'online',
    service_type: 'mihomo', deployment_method: 'shell', version: '1.8.41-go', last_heartbeat: '', enabled: true, ...overrides
  })

  it('shows the forwarding row only on devices a router uses as its proxy gateway', async () => {
    vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
    vi.mocked(agentApi.getAll).mockResolvedValue({ data: [agent(), agent({ id: 'a2', name: 'local-only' })] } as any)
    vi.mocked(profileApi.list).mockResolvedValue({ data: [{ id: 'default', name: 'Default', revision: 1 }] } as any)
    vi.mocked(agentApi.gatewayForwarding).mockResolvedValue(read([entry()]))
    vi.mocked(confirm).mockResolvedValue(true)
    vi.mocked(agentApi.persistGatewayForwarding).mockResolvedValue({ data: { success: true, gateway: entry(PERSISTED) } } as any)
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/agents', component: { render: () => null } }] })
    await router.push('/agents')
    await router.isReady()
    const wrapper = mount(Agents, { attachTo: document.body, global: { plugins: [router] } })
    wrappers.push(wrapper)
    await flushPromises()
    const rows = wrapper.findAll('[data-agent-status-row="IPv4 转发"]')
    expect(rows).toHaveLength(1)
    const card = rows[0]!.element.closest('.rounded-\\[18px\\]')!
    expect(card.textContent).toContain('pve-mihomo')
    await rows[0]!.get('[data-status-notice] button').trigger('click')
    await flushPromises()
    expect(agentApi.persistGatewayForwarding).toHaveBeenCalledExactlyOnceWith('a1')
    expect(wrapper.get('[data-agent-status-row="IPv4 转发"]').text()).toContain('已开启并持久化')
    vi.unstubAllGlobals()
  })
})
