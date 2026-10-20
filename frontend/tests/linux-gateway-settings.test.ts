import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import LinuxGatewaySettings from '@/components/generate/LinuxGatewaySettings.vue'
import AgentStatusRow from '@/components/agents/AgentStatusRow.vue'
import SettingsWorkspace from '@/components/generate/SettingsWorkspace.vue'
import SettingsPublishBar from '@/components/generate/SettingsPublishBar.vue'
import { Switch } from '@/components/ui/switch'
import { Checkbox } from '@/components/ui/checkbox'
import { Select } from '@/components/ui/select'
import { agentApi, profileApi } from '@/api'
import { linuxGatewayApi } from '@/api/linuxGateway'
import { confirm, notify } from '@/lib/feedback'
import { gatewaySpecErrors } from '@/lib/linuxGateway'

vi.mock('@/api', () => ({ default: {}, agentApi: { getAll: vi.fn(), pushConfig: vi.fn(), getDeployment: vi.fn(), activateDeployment: vi.fn() }, profileApi: { list: vi.fn() } }))
vi.mock('@/api/linuxGateway', () => ({ linuxGatewayApi: {
  get: vi.fn(), save: vi.fn(), status: vi.fn(), preview: vi.fn(), apply: vi.fn(), remove: vi.fn(),
  previewDraft: vi.fn(), previewRestore: vi.fn(), restoreDraft: vi.fn(),
} }))
vi.mock('@/lib/feedback', () => ({ confirm: vi.fn(), notify: { success: vi.fn(), error: vi.fn() } }))

const spec = { interface: 'eth0', address: '192.168.1.2', lan_cidr: '192.168.1.0/24', upstream_gateway: '192.168.1.1' }
const guard = { expected_profile_id: 'office', expected_service_type: 'mihomo', expected_config_revision: 10 }
const response = (data: unknown) => ({ data }) as any
const originalClipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
const originalExecCommand = Object.getOwnPropertyDescriptor(document, 'execCommand')
const writeText = vi.fn()
const config = (enabled = false) => ({
  config_revision: 9, restore_point: null,
  settings: { enabled, rule_set_ids: ['telegram', 'disabled'] }, fake_ip_range: '198.18.0.1/16', refresh_interval: 300, warnings: [],
  candidates: [
    { id: 'telegram', name: 'Telegram 已改名', behavior: 'ipcidr', enabled: true, available: true, policy: '国外代理' },
    { id: 'disabled', name: '已停用 IP 集合', behavior: 'classical', enabled: false, available: false },
  ],
})
const draftPreview = (action: 'save' | 'restore' = 'save') => ({
  success: true, action, config_revision: 9, preview_token: 'reviewed-config', restore_point: null,
  changes: [{ path: 'Mihomo.dns.listen', before: '0.0.0.0:53', after: '127.0.0.1:1053' }], warnings: [],
})
const draftGuard = { expected_config_revision: 9, preview_token: 'reviewed-config' }
const hostStatus = () => ({ success: true, enabled_in_config: true, detection: { suggested: { ...spec } }, managed: { active: false } })
const agent = (id = 'one', extra = {}) => ({ id, name: id, profile_id: 'office', service_type: 'mihomo', status: 'online', enabled: true, deployment_method: 'shell', ...extra })
const workspaceStub = defineComponent({
  name: 'SettingsWorkspace', props: ['open', 'dirty', 'busy'], emits: ['save', 'update:open'],
  setup(props, { slots }) { return () => h('section', { role: 'dialog', 'data-state': props.open ? 'open' : 'closed' }, [slots.default?.(), slots.footer?.({ disabled: props.busy })]) },
})
const buttonStub = defineComponent({
  name: 'Button', setup(_props, { slots, attrs }) { return () => h('button', attrs, slots.default?.()) },
})
const wrappers: ReturnType<typeof shallowMount>[] = []
const attachedRoots: HTMLElement[] = []

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}
async function render(enabled = false, attached = false, extra = {}, realPublish = false) {
  vi.mocked(linuxGatewayApi.get).mockResolvedValue(response({ ...config(enabled), ...extra }))
  let attachTo: HTMLElement | undefined
  if (attached) {
    attachTo = document.createElement('div'); document.body.appendChild(attachTo); attachedRoots.push(attachTo)
  }
  const wrapper = shallowMount(LinuxGatewaySettings, {
    attachTo,
    props: { open: true, profileId: 'office' },
    global: { renderStubDefaultSlot: true, stubs: { SettingsWorkspace: workspaceStub, Button: buttonStub, Input: false, AgentStatusRow: false, ...(realPublish ? { SettingsPublishBar: false } : {}) } },
  })
  wrappers.push(wrapper)
  await flushPromises()
  return wrapper
}
function button(wrapper: ReturnType<typeof shallowMount>, label: string) {
  return wrapper.findAll('button').find(item => item.attributes('aria-label') === label || item.text() === label)!
}

beforeEach(() => {
  vi.resetAllMocks()
  writeText.mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
  vi.mocked(agentApi.getAll).mockResolvedValue(response([agent()]))
  vi.mocked(agentApi.pushConfig).mockImplementation(async (_id, id) => response({ deployment_id: id, status: 'succeeded' }))
  vi.mocked(agentApi.getDeployment).mockImplementation(async (_id, id) => response({ deployment_id: id, status: 'uploading' }))
  vi.mocked(agentApi.activateDeployment).mockImplementation(async (_id, id) => response({ deployment_id: id, status: 'succeeded' }))
  vi.mocked(profileApi.list).mockResolvedValue(response([{ id: 'office', name: '办公室' }]))
  vi.mocked(linuxGatewayApi.status).mockResolvedValue(response(hostStatus()))
  vi.mocked(linuxGatewayApi.save).mockImplementation(async (_profile, settings) => response({ ...config(), success: true, settings, config_revision: 10, publish_guard: guard }))
  vi.mocked(linuxGatewayApi.previewDraft).mockResolvedValue(response(draftPreview()))
  vi.mocked(linuxGatewayApi.previewRestore).mockResolvedValue(response(draftPreview('restore')))
  vi.mocked(linuxGatewayApi.restoreDraft).mockResolvedValue(response({ ...config(), success: true, config_revision: 10 }))
  vi.mocked(linuxGatewayApi.preview).mockResolvedValue(response({ ...hostStatus(), ready: true, blockers: [], warnings: [], commands: ['nft list ruleset'], spec }))
  vi.mocked(linuxGatewayApi.apply).mockResolvedValue(response({ success: true, managed: { active: true, healthy: true, spec } }))
  vi.mocked(linuxGatewayApi.remove).mockResolvedValue(response({ success: true, managed: { active: false } }))
  vi.mocked(confirm).mockResolvedValue(true)
})
afterEach(() => {
  wrappers.splice(0).forEach(item => item.unmount())
  attachedRoots.splice(0).forEach(root => root.remove())
  if (originalClipboard) Object.defineProperty(navigator, 'clipboard', originalClipboard)
  else delete (navigator as any).clipboard
  if (originalExecCommand) Object.defineProperty(document, 'execCommand', originalExecCommand)
  else delete (document as any).execCommand
})

describe('Linux gateway opt-in workflow', () => {
  it('previews local settings offline without saving or changing host networking, then invalidates edited previews', async () => {
    vi.mocked(agentApi.getAll).mockResolvedValue(response([agent('one', { status: 'offline' })]))
    const wrapper = await render()
    wrapper.getComponent(Switch).vm.$emit('update:modelValue', true)
    await flushPromises()
    expect(wrapper.getComponent(SettingsPublishBar).props('disabled')).toBe(true)
    await button(wrapper, '预览配置变更').trigger('click'); await flushPromises()
    expect(linuxGatewayApi.previewDraft).toHaveBeenCalledWith('office', { enabled: true, rule_set_ids: ['telegram', 'disabled'] }, 9)
    expect(wrapper.text()).toContain('试用预览 · 尚未保存')
    expect(wrapper.text()).toContain('127.0.0.1:1053')
    expect(wrapper.getComponent(SettingsPublishBar).props('disabled')).toBe(false)
    expect(linuxGatewayApi.save).not.toHaveBeenCalled()
    expect(linuxGatewayApi.status).not.toHaveBeenCalled()
    expect(linuxGatewayApi.apply).not.toHaveBeenCalled()
    wrapper.findAllComponents(Checkbox)[0].vm.$emit('update:modelValue', false)
    await flushPromises()
    expect(wrapper.text()).not.toContain('试用预览 · 尚未保存')
    expect(wrapper.getComponent(SettingsPublishBar).props('disabled')).toBe(true)
  })

  it('canceling the explicit save leaves the trial dirty and does not claim success', async () => {
    const wrapper = await render()
    wrapper.getComponent(Switch).vm.$emit('update:modelValue', true)
    vi.mocked(confirm).mockResolvedValue(false)
    await flushPromises()
    await wrapper.getComponent(SettingsPublishBar).vm.$emit('save-draft')
    await flushPromises()
    expect(linuxGatewayApi.save).not.toHaveBeenCalled()
    expect(notify.success).not.toHaveBeenCalled()
    expect(wrapper.getComponent(SettingsWorkspace).props('dirty')).toBe(true)
  })

  it('keeps a stale-save conflict visible without discarding the local trial', async () => {
    const wrapper = await render()
    wrapper.getComponent(Switch).vm.$emit('update:modelValue', true)
    vi.mocked(linuxGatewayApi.save).mockRejectedValue({ response: { status: 409, data: { message: '配置或共享资源已更新，请重新打开' } } })
    await flushPromises()
    await expect(wrapper.getComponent(SettingsPublishBar).props('save')()).rejects.toBeTruthy()
    expect(wrapper.text()).toContain('配置或共享资源已更新')
    expect(wrapper.getComponent(SettingsWorkspace).props('dirty')).toBe(true)
    expect(wrapper.getComponent(SettingsPublishBar).props('disabled')).toBe(true)
    expect(linuxGatewayApi.apply).not.toHaveBeenCalled()
  })

  it('ignores draft preview responses after closing or changing profiles', async () => {
    const pending = deferred<any>()
    vi.mocked(linuxGatewayApi.previewDraft).mockImplementationOnce(() => pending.promise)
    const wrapper = await render()
    await button(wrapper, '预览配置变更').trigger('click')
    await wrapper.setProps({ profileId: 'other' })
    pending.resolve(response(draftPreview()))
    await flushPromises()
    expect(wrapper.text()).not.toContain('试用预览 · 尚未保存')
    expect(notify.error).not.toHaveBeenCalled()
    expect(linuxGatewayApi.save).not.toHaveBeenCalled()
  })

  it('does not save to the old profile after switching during confirmation', async () => {
    const pending = deferred<boolean>()
    const wrapper = await render()
    vi.mocked(confirm).mockImplementationOnce(() => pending.promise)
    const saving = wrapper.getComponent(SettingsPublishBar).props('save')()
    await flushPromises()
    await wrapper.setProps({ profileId: 'other' })
    pending.resolve(true)
    expect(await saving).toBeNull()
    expect(linuxGatewayApi.save).not.toHaveBeenCalled()
  })

  it('previews recovery before allowing confirmation, supports cancellation and never publishes or applies networking', async () => {
    const wrapper = await render(true, false, { restore_point: { id: 'snapshot', created_at: '2026-10-20T01:00:00Z' } })
    await button(wrapper, '预览恢复变更').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('恢复预览 · 尚未恢复')
    expect(confirm).not.toHaveBeenCalled()
    expect(linuxGatewayApi.restoreDraft).not.toHaveBeenCalled()
    vi.mocked(confirm).mockResolvedValueOnce(false)
    await button(wrapper, '恢复这份草稿').trigger('click'); await flushPromises()
    expect(linuxGatewayApi.restoreDraft).not.toHaveBeenCalled()
    await button(wrapper, '恢复这份草稿').trigger('click'); await flushPromises()
    expect(linuxGatewayApi.restoreDraft).toHaveBeenCalledExactlyOnceWith('office', draftGuard)
    expect(wrapper.getComponent(Switch).props('modelValue')).toBe(false)
    expect(wrapper.findAll('button').some(item => item.text() === '预览恢复变更')).toBe(false)
    expect(linuxGatewayApi.apply).not.toHaveBeenCalled()
    expect(linuxGatewayApi.remove).not.toHaveBeenCalled()
    expect(agentApi.pushConfig).not.toHaveBeenCalled()
  })

  it('blocks a stale recovery without losing the original saved state', async () => {
    const wrapper = await render(true, false, { restore_point: { id: 'snapshot', created_at: '2026-10-20T01:00:00Z' } })
    await button(wrapper, '预览恢复变更').trigger('click'); await flushPromises()
    vi.mocked(linuxGatewayApi.restoreDraft).mockRejectedValueOnce(new Error('配置已更新，请重新预览'))
    await button(wrapper, '恢复这份草稿').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('配置已更新，请重新预览')
    expect(wrapper.getComponent(Switch).props('modelValue')).toBe(true)
    expect(notify.success).not.toHaveBeenCalled()
  })

  it('continues normal save-and-publish with exactly the committed revision after preview', async () => {
    const wrapper = await render(false, false, {}, true)
    wrapper.getComponent(Switch).vm.$emit('update:modelValue', true)
    await flushPromises()
    expect(button(wrapper, '保存并发布').attributes('disabled')).toBeDefined()
    await button(wrapper, '预览配置变更').trigger('click'); await flushPromises()
    await button(wrapper, '保存并发布').trigger('click'); await flushPromises()
    expect(linuxGatewayApi.save).toHaveBeenCalledExactlyOnceWith('office', { enabled: true, rule_set_ids: ['telegram', 'disabled'] }, draftGuard)
    expect(agentApi.pushConfig).toHaveBeenCalledExactlyOnceWith('one', expect.any(String), guard)
    expect(wrapper.getComponent(SettingsPublishBar).props('disabled')).toBe(false)
  })

  it('keeps ready-candidate activation available after saving clears the draft preview', async () => {
    vi.mocked(agentApi.pushConfig).mockImplementationOnce(async (_id, id) => response({ deployment_id: id, status: 'ready' }))
    const wrapper = await render(false, false, {}, true)
    wrapper.getComponent(Switch).vm.$emit('update:modelValue', true)
    await flushPromises()
    await button(wrapper, '预览配置变更').trigger('click'); await flushPromises()
    await button(wrapper, '保存并发布').trigger('click'); await flushPromises()
    expect(wrapper.text()).not.toContain('试用预览 · 尚未保存')
    const activate = button(wrapper, '应用并启动')
    expect(activate.attributes('disabled')).toBeUndefined()
    await activate.trigger('click'); await flushPromises()
    expect(agentApi.activateDeployment).toHaveBeenCalledWith('one', expect.any(String))
  })

  it('stays off by default and neither mode switches nor saves execute host networking', async () => {
    const wrapper = await render()
    expect(wrapper.getComponent(Switch).props('modelValue')).toBe(false)
    expect(button(wrapper, '预览并检查').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('自动接管的临时地址范围（fake-IP）：198.18.0.1/16')
    wrapper.getComponent(Switch).vm.$emit('update:modelValue', true)
    await flushPromises()
    expect(wrapper.getComponent(SettingsWorkspace).props('dirty')).toBe(true)
    expect(button(wrapper, '预览并检查').attributes('disabled')).toBeDefined()
    expect(await wrapper.getComponent(SettingsPublishBar).props('save')()).toEqual(guard)
    await flushPromises()
    expect(linuxGatewayApi.save).toHaveBeenCalledWith('office', { enabled: true, rule_set_ids: ['telegram', 'disabled'] }, draftGuard)
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
    expect(linuxGatewayApi.save).toHaveBeenLastCalledWith('office', { enabled: true, rule_set_ids: ['telegram'] }, draftGuard)
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
    expect(wrapper.findAllComponents(AgentStatusRow).find(item => item.props('label') === '网络状态')?.props()).toMatchObject({ status: '已应用', tone: 'success' })
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

  describe('operation notes clipboard fallback', () => {
    async function setupCopy(failure?: 'false' | 'throws') {
      const commands = ['创建 ConfigFlow 网关转发表', '设置 LAN 接口 eth0 的转发规则']
      vi.mocked(linuxGatewayApi.preview).mockResolvedValue(response({ ...hostStatus(), ready: true,
        blockers: [], warnings: [], commands, spec }))
      const wrapper = await render(true, true)
      await button(wrapper, '预览并检查').trigger('click'); await flushPromises()
      const copyButton = wrapper.get('button[aria-label="复制操作说明"]')
      const details = copyButton.element.closest('details')
      if (details) details.open = true
      const dialog = wrapper.get('[role="dialog"]').element
      const focusTarget = copyButton.element as HTMLButtonElement
      focusTarget.focus()
      const trap = () => { if (!dialog.contains(document.activeElement)) focusTarget.focus() }
      document.addEventListener('focusin', trap)
      const previousTextareas = document.querySelectorAll('textarea').length
      const selections: { insideDialog: boolean; text: string }[] = []
      const execCommand = vi.fn(() => {
        const active = document.activeElement
        selections.push({ insideDialog: dialog.contains(active), text: active instanceof HTMLTextAreaElement
          ? active.value.slice(active.selectionStart, active.selectionEnd) : '' })
        if (failure === 'throws') throw new Error('Clipboard denied')
        return failure !== 'false'
      })
      Object.defineProperty(document, 'execCommand', { configurable: true, value: execCommand })
      vi.mocked(notify.success).mockClear(); vi.mocked(notify.error).mockClear()
      return { copyButton, expected: commands.join('\n'), execCommand, selections, focusTarget, previousTextareas,
        cleanup: () => document.removeEventListener('focusin', trap) }
    }

    it.each(['unavailable', 'rejected'])('copies complete operation notes inside the dialog when clipboard is %s', async modern => {
      const state = await setupCopy()
      if (modern === 'unavailable') Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined })
      else writeText.mockRejectedValue(new Error('Permission denied'))
      try {
        await state.copyButton.trigger('click'); await flushPromises()
        expect(state.execCommand).toHaveBeenCalledExactlyOnceWith('copy')
        expect(state.selections).toEqual([{ insideDialog: true, text: state.expected }])
        expect(document.querySelectorAll('textarea')).toHaveLength(state.previousTextareas)
        expect(document.activeElement).toBe(state.focusTarget)
        expect(notify.success).toHaveBeenCalledOnce()
        expect(notify.error).not.toHaveBeenCalled()
        if (modern === 'rejected') expect(writeText).toHaveBeenCalledExactlyOnceWith(state.expected)
        else expect(writeText).not.toHaveBeenCalled()
        expect(linuxGatewayApi.apply).not.toHaveBeenCalled()
        expect(linuxGatewayApi.remove).not.toHaveBeenCalled()
      } finally { state.cleanup() }
    })

    it.each(['false', 'throws'] as const)('reports failure without success when selection copy %s', async failure => {
      const state = await setupCopy(failure)
      Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined })
      try {
        await state.copyButton.trigger('click'); await flushPromises()
        expect(state.execCommand).toHaveBeenCalledExactlyOnceWith('copy')
        expect(state.selections).toEqual([{ insideDialog: true, text: state.expected }])
        expect(notify.error).toHaveBeenCalledOnce()
        expect(notify.success).not.toHaveBeenCalled()
        expect(document.querySelectorAll('textarea')).toHaveLength(state.previousTextareas)
        expect(document.activeElement).toBe(state.focusTarget)
      } finally { state.cleanup() }
    })
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
