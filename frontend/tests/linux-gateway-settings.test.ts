import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import LinuxGatewaySettings from '@/components/generate/LinuxGatewaySettings.vue'
import SettingsWorkspace from '@/components/generate/SettingsWorkspace.vue'
import SettingsPublishBar from '@/components/generate/SettingsPublishBar.vue'
import { Switch } from '@/components/ui/switch'
import { Checkbox } from '@/components/ui/checkbox'
import { Select } from '@/components/ui/select'
import { agentApi, profileApi } from '@/api'
import { linuxGatewayApi } from '@/api/linuxGateway'
import { confirm } from '@/lib/feedback'
import { gatewaySpecErrors } from '@/lib/linuxGateway'

vi.mock('@/api', () => ({ default: {}, agentApi: { getAll: vi.fn() }, profileApi: { list: vi.fn() } }))
vi.mock('@/api/linuxGateway', () => ({ linuxGatewayApi: {
  get: vi.fn(), save: vi.fn(), status: vi.fn(), preview: vi.fn(), apply: vi.fn(), remove: vi.fn(),
} }))
vi.mock('@/lib/feedback', () => ({ confirm: vi.fn(), notify: { success: vi.fn(), error: vi.fn() } }))

const spec = { interface: 'eth0', address: '192.168.1.2', lan_cidr: '192.168.1.0/24', upstream_gateway: '192.168.1.1' }
const guard = { expected_profile_id: 'office', expected_service_type: 'mihomo', expected_config_revision: 10 }
const response = (data: unknown) => ({ data }) as any
const config = (enabled = false) => ({
  settings: { enabled, rule_set_ids: ['telegram', 'disabled'] }, fake_ip_range: '198.18.0.1/16', refresh_interval: 300, warnings: [],
  candidates: [
    { id: 'telegram', name: 'Telegram 已改名', behavior: 'ipcidr', enabled: true, available: true, policy: '国外代理' },
    { id: 'disabled', name: '已停用 IP 集合', behavior: 'classical', enabled: false, available: false },
  ],
})
const hostStatus = () => ({ success: true, enabled_in_config: true, detection: { suggested: { ...spec } }, managed: { active: false } })
const agent = (id = 'one', extra = {}) => ({ id, name: id, profile_id: 'office', service_type: 'mihomo', status: 'online', enabled: true, deployment_method: 'shell', ...extra })
const workspaceStub = defineComponent({
  name: 'SettingsWorkspace', props: ['open', 'dirty', 'busy'], emits: ['save', 'update:open'],
  setup(props, { slots }) { return () => h('section', [slots.default?.(), slots.footer?.({ disabled: props.busy })]) },
})
const buttonStub = defineComponent({
  name: 'Button', setup(_props, { slots, attrs }) { return () => h('button', attrs, slots.default?.()) },
})
const wrappers: ReturnType<typeof shallowMount>[] = []

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}
async function render(enabled = false) {
  vi.mocked(linuxGatewayApi.get).mockResolvedValue(response(config(enabled)))
  const wrapper = shallowMount(LinuxGatewaySettings, {
    props: { open: true, profileId: 'office' },
    global: { renderStubDefaultSlot: true, stubs: { SettingsWorkspace: workspaceStub, Button: buttonStub, Input: false } },
  })
  wrappers.push(wrapper)
  await flushPromises()
  return wrapper
}
function button(wrapper: ReturnType<typeof shallowMount>, label: string) {
  return wrapper.findAll('button').find(item => item.text() === label)!
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(agentApi.getAll).mockResolvedValue(response([agent()]))
  vi.mocked(profileApi.list).mockResolvedValue(response([{ id: 'office', name: '办公室' }]))
  vi.mocked(linuxGatewayApi.status).mockResolvedValue(response(hostStatus()))
  vi.mocked(linuxGatewayApi.save).mockImplementation(async (_profile, settings) => response({ success: true, settings, publish_guard: guard }))
  vi.mocked(linuxGatewayApi.preview).mockResolvedValue(response({ ...hostStatus(), ready: true, blockers: [], warnings: [], commands: ['nft list ruleset'], spec }))
  vi.mocked(linuxGatewayApi.apply).mockResolvedValue(response({ success: true, managed: { active: true, healthy: true, spec } }))
  vi.mocked(linuxGatewayApi.remove).mockResolvedValue(response({ success: true, managed: { active: false } }))
  vi.mocked(confirm).mockResolvedValue(true)
})
afterEach(() => { wrappers.splice(0).forEach(item => item.unmount()) })

describe('Linux gateway opt-in workflow', () => {
  it('stays off by default and neither mode switches nor saves execute host networking', async () => {
    const wrapper = await render()
    expect(wrapper.getComponent(Switch).props('modelValue')).toBe(false)
    expect(button(wrapper, '预览并检查').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('自动包含 fake-IP 网段')
    expect(wrapper.text()).toContain('198.18.0.1/16')
    wrapper.getComponent(Switch).vm.$emit('update:modelValue', true)
    await flushPromises()
    expect(wrapper.getComponent(SettingsWorkspace).props('dirty')).toBe(true)
    expect(button(wrapper, '预览并检查').attributes('disabled')).toBeDefined()
    expect(await wrapper.getComponent(SettingsPublishBar).props('save')()).toEqual(guard)
    await flushPromises()
    expect(linuxGatewayApi.save).toHaveBeenCalledWith('office', { enabled: true, rule_set_ids: ['telegram', 'disabled'] })
    expect(linuxGatewayApi.preview).not.toHaveBeenCalled()
    expect(linuxGatewayApi.apply).not.toHaveBeenCalled()
    expect(linuxGatewayApi.remove).not.toHaveBeenCalled()
    expect(wrapper.getComponent(SettingsWorkspace).props('dirty')).toBe(false)
  })

  it('preserves stable rule ids through renames and permits removing inactive selections', async () => {
    const wrapper = await render(true)
    const boxes = wrapper.findAllComponents(Checkbox)
    expect(wrapper.text()).toContain('Telegram 已改名')
    expect(boxes[0].props('modelValue')).toBe(true)
    expect(boxes[1].props('disabled')).toBe(false)
    boxes[1].vm.$emit('update:modelValue', false)
    await flushPromises()
    await wrapper.getComponent(SettingsPublishBar).props('save')()
    expect(linuxGatewayApi.save).toHaveBeenLastCalledWith('office', { enabled: true, rule_set_ids: ['telegram'] })
  })

  it('requires successful preview of the current host inputs before explicit apply', async () => {
    const wrapper = await render(true)
    expect(button(wrapper, '应用网关网络').attributes('disabled')).toBeDefined()
    await button(wrapper, '预览并检查').trigger('click')
    await flushPromises()
    expect(linuxGatewayApi.preview).toHaveBeenCalledWith('one', 'office', spec)
    expect(button(wrapper, '应用网关网络').attributes('disabled')).toBeUndefined()
    await wrapper.get('#gateway-address').setValue('192.168.1.3')
    expect(button(wrapper, '应用网关网络').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).not.toContain('检查通过，可以应用')
    await button(wrapper, '预览并检查').trigger('click')
    await flushPromises()
    await button(wrapper, '应用网关网络').trigger('click')
    await flushPromises()
    expect(linuxGatewayApi.apply).toHaveBeenCalledExactlyOnceWith('one', 'office', { ...spec, address: '192.168.1.3' })
    expect(wrapper.text()).toContain('已应用 ConfigFlow 网关网络')
  })

  it('does not reuse a preview after a rule selection changes', async () => {
    const wrapper = await render(true)
    await button(wrapper, '预览并检查').trigger('click')
    await flushPromises()
    wrapper.findAllComponents(Checkbox)[0].vm.$emit('update:modelValue', false)
    await flushPromises()
    expect(button(wrapper, '应用网关网络').attributes('disabled')).toBeDefined()
    expect(button(wrapper, '预览并检查').attributes('disabled')).toBeDefined()
  })

  it('blocks apply on failed preflight and keeps the concrete reason visible', async () => {
    vi.mocked(linuxGatewayApi.preview).mockResolvedValueOnce(response({ ...hostStatus(), ready: false, blockers: ['请先发布 Mihomo 网关配置'], commands: [], warnings: [], spec }))
    const wrapper = await render(true)
    await button(wrapper, '预览并检查').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('请先发布 Mihomo 网关配置')
    expect(button(wrapper, '应用网关网络').attributes('disabled')).toBeDefined()
  })

  it('clears host inputs and ignores late detection after switching devices', async () => {
    vi.mocked(agentApi.getAll).mockResolvedValue(response([agent(), agent('two')]))
    const old = deferred<any>()
    const next = deferred<any>()
    vi.mocked(linuxGatewayApi.status).mockReturnValueOnce(old.promise).mockReturnValueOnce(next.promise)
    const wrapper = await render(true)
    const select = wrapper.getComponent(Select)
    select.vm.$emit('update:modelValue', 'one')
    await flushPromises()
    select.vm.$emit('update:modelValue', 'two')
    await flushPromises()
    old.resolve(response(hostStatus()))
    await flushPromises()
    expect((wrapper.get('#gateway-address').element as HTMLInputElement).value).toBe('')
    next.resolve(response({ ...hostStatus(), detection: { suggested: { ...spec, address: '192.168.1.8' } } }))
    await flushPromises()
    expect((wrapper.get('#gateway-address').element as HTMLInputElement).value).toBe('192.168.1.8')
  })

  it('ignores a late preview from a closed settings session', async () => {
    const pending = deferred<any>()
    vi.mocked(linuxGatewayApi.preview).mockReturnValueOnce(pending.promise)
    const wrapper = await render(true)
    await button(wrapper, '预览并检查').trigger('click')
    await wrapper.setProps({ open: false })
    pending.resolve(response({ ...hostStatus(), ready: true, blockers: [], commands: [], warnings: [], spec }))
    await flushPromises()
    expect(wrapper.text()).not.toContain('检查通过，可以应用')
    expect(linuxGatewayApi.apply).not.toHaveBeenCalled()
  })

  it('shows an actionable old-Agent error while draft saving remains available', async () => {
    vi.mocked(linuxGatewayApi.status).mockRejectedValueOnce({ response: { status: 404 } })
    const wrapper = await render(true)
    expect(wrapper.text()).toContain('请在 Agent 页面升级后重试')
    expect(button(wrapper, '预览并检查').attributes('disabled')).toBeDefined()
    await wrapper.getComponent(SettingsPublishBar).props('save')()
    expect(linuxGatewayApi.save).toHaveBeenCalledOnce()
  })

  it('offers restore for managed hosts even when the mode is switched off', async () => {
    vi.mocked(linuxGatewayApi.status).mockResolvedValueOnce(response({ ...hostStatus(), managed: { active: true, spec } }))
    const wrapper = await render(true)
    wrapper.getComponent(Switch).vm.$emit('update:modelValue', false)
    await flushPromises()
    expect(button(wrapper, '恢复所选设备网络').attributes('disabled')).toBeUndefined()
    await button(wrapper, '恢复所选设备网络').trigger('click')
    await flushPromises()
    expect(confirm).toHaveBeenCalledOnce()
    expect(linuxGatewayApi.remove).toHaveBeenCalledExactlyOnceWith('one', 'office')
    expect(linuxGatewayApi.save).not.toHaveBeenCalled()
  })

  it('requires a new preview after a transport failure instead of reusing an uncertain apply', async () => {
    vi.mocked(linuxGatewayApi.apply).mockRejectedValueOnce(new Error('连接中断'))
    const wrapper = await render(true)
    await button(wrapper, '预览并检查').trigger('click')
    await flushPromises()
    await button(wrapper, '应用网关网络').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('请刷新检测，确认设备实际状态')
    expect(button(wrapper, '应用网关网络').attributes('disabled')).toBeDefined()
  })

  it('allows restoring a partially applied pending journal and displays its error', async () => {
    vi.mocked(linuxGatewayApi.status).mockResolvedValueOnce(response({ ...hostStatus(), managed: { active: false, pending: true, error: '上次应用中断，需要恢复', spec } }))
    const wrapper = await render(false)
    expect(wrapper.text()).toContain('应用未完成，需要恢复或重新检查')
    expect(wrapper.text()).toContain('上次应用中断，需要恢复')
    expect(button(wrapper, '恢复所选设备网络').attributes('disabled')).toBeUndefined()
    await button(wrapper, '恢复所选设备网络').trigger('click')
    await flushPromises()
    expect(linuxGatewayApi.remove).toHaveBeenCalledExactlyOnceWith('one', 'office')
  })

  it('does not apply without a deployed gateway marker even if a preview says ready', async () => {
    vi.mocked(linuxGatewayApi.status).mockResolvedValueOnce(response({ ...hostStatus(), enabled_in_config: false }))
    const wrapper = await render(true)
    await button(wrapper, '预览并检查').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('设备尚未检测到已部署的选择性网关配置')
    expect(button(wrapper, '应用网关网络').attributes('disabled')).toBeDefined()
  })
})

describe('Gateway LAN input validation', () => {
  it('accepts a single-NIC LAN and rejects wrong subnet, duplicate addresses and non-address input', () => {
    expect(gatewaySpecErrors(spec)).toEqual([])
    expect(gatewaySpecErrors({ ...spec, upstream_gateway: spec.address })).toContain('Linux 地址与主路由地址不能相同。')
    expect(gatewaySpecErrors({ ...spec, address: '10.1.1.2' })).toContain('Linux 地址必须在填写的 LAN 网段内。')
    expect(gatewaySpecErrors({ ...spec, lan_cidr: '192.168.1.5/24' })).toContain('LAN 网段请填写网络地址，例如 192.168.1.0/24，而不是设备地址。')
    expect(gatewaySpecErrors({ ...spec, address: 'example.com' }).length).toBeGreaterThan(0)
  })
})
