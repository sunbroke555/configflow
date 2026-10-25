import { describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import { dump, load } from 'js-yaml'
import { oxidnsApi } from '@/api'
import OxidnsCommonSettings from '@/components/generate/OxidnsCommonSettings.vue'
import OxidnsEntryFlowEditor from '@/components/generate/OxidnsEntryFlowEditor.vue'
import OxidnsFlowNodeEditor from '@/components/generate/OxidnsFlowNodeEditor.vue'
import { settings, render, button, click, settleValidation, savedPayload } from './helpers/oxidns-settings'
import type { OxidnsEntryFlow } from '@/lib/oxidnsEntryFlows'

vi.mock('@/api', () => ({
  agentApi: { getAll: vi.fn(), pushConfig: vi.fn(), getDeployment: vi.fn(), activateDeployment: vi.fn() },
  profileApi: { list: vi.fn() },
  oxidnsApi: { getSettings: vi.fn(), saveSettings: vi.fn(), prepareEditor: vi.fn(), readCommonSettings: vi.fn(), applyCommonSettings: vi.fn(), applyNativeSettings: vi.fn(), validateSettings: vi.fn() },
  ruleApi: { getAll: vi.fn() }, ruleSetApi: { getAll: vi.fn() },
}))
const source = () => ({ ...settings(), native_config: dump({
  include: ['flows.yaml'], plugins: [{ tag: 'udp', type: 'udp_server', args: { listen: ':1053', entry: 'handwritten' } }],
}), additional_files: [{ path: 'flows.yaml', encoding: 'text', content: dump({ plugins: [
  { tag: 'handwritten', type: 'sequence', args: [{ matches: 'qname full:before.test', exec: 'reject', extra: { keep: [false, 0, null] } }, { exec: 'reject 3' }] },
] }) }] })
function projection(value: ReturnType<typeof source>) {
  const original = (load(value.additional_files[0]!.content) as any).plugins[0]
  const listener = (load(value.native_config) as any).plugins[0]
  const flow: OxidnsEntryFlow = {
    globals: { source: 'config.yaml', value: { include: ['flows.yaml'] } },
    entries: [{ id: 'udp-entry', listener_id: 'config.yaml#0', listener_tag: 'udp', listener_type: 'udp_server', listen: ':1053', entry: 'handwritten' }],
    plugins: [
      { id: 'config.yaml#0', tag: 'udp', type: 'udp_server', kind: 'server', source: 'config.yaml', index: 0, value: listener, editable: true, targets: [] },
      { id: 'flows.yaml#0', tag: 'handwritten', type: 'sequence', kind: 'executor', source: 'flows.yaml', index: 0, value: original, editable: true, targets: [] },
    ], edges: [{ from: 'config.yaml#0', to: 'flows.yaml#0', target: 'handwritten', path: ['args', 'entry'] }],
  }
  return { values: {}, capabilities: { custom_matches: { editable: false, reason: '手写结构', target: 'sequence_main' } }, entry_flow: flow }
}
function mockSource() {
  const original = source()
  vi.mocked(oxidnsApi.getSettings).mockResolvedValue({ data: original } as any)
  vi.mocked(oxidnsApi.readCommonSettings).mockImplementation(async value => ({ data: projection(value as typeof original) }) as any)
  vi.mocked(oxidnsApi.applyCommonSettings).mockImplementation(async (value, changes: any) => {
    const result = JSON.parse(JSON.stringify(value))
    const doc = load(result.additional_files[0].content) as any
    expect(doc.plugins[0]).toEqual(changes.entry_flow.before)
    doc.plugins[0] = changes.entry_flow.value
    result.additional_files[0].content = dump(doc)
    return { data: { settings: result, ...projection(result) } } as any
  })
  return original
}
async function sync() { await vi.advanceTimersByTimeAsync(180); await flushPromises(); await settleValidation() }

describe('entry flow integration with the actual settings draft', () => {
  it('shows a handwritten include flow and saves only the selected sequence source after a node edit', async () => {
    const original = mockSource()
    const wrapper = await render()
    await click('高级')
    expect(wrapper.getComponent(OxidnsCommonSettings).props('entryFlow')).toEqual(projection(original).entry_flow)
    const graph = wrapper.getComponent(OxidnsEntryFlowEditor)
    await graph.get('[aria-label="编辑步骤 1"]').trigger('click')
    const editor = graph.getComponent(OxidnsFlowNodeEditor)
    editor.vm.$emit('update:modelValue', { matches: 'qname full:after.test', exec: 'reject 0', extra: { keep: [false, 0, null] } })
    await flushPromises()
    await click('应用节点修改')
    await sync()
    expect(oxidnsApi.applyCommonSettings).toHaveBeenCalledOnce()
    const patch = vi.mocked(oxidnsApi.applyCommonSettings).mock.calls[0]![1] as any
    expect(patch.entry_flow.source).toBe('flows.yaml')
    expect(patch.entry_flow.value.args[1]).toEqual({ exec: 'reject 3' })
    expect(graph.text()).toContain('qname full:after.test')
    await click('保存草稿')
    const saved = savedPayload()
    expect(saved.native_config).toBe(original.native_config)
    expect((load(saved.additional_files[0].content) as any).plugins[0].args[0]).toEqual(patch.entry_flow.value.args[0])
  })

  it('blocks saving and leaving the editor while a valid node change is not applied, then cancels without writing', async () => {
    mockSource()
    const wrapper = await render()
    await click('高级')
    const graph = wrapper.getComponent(OxidnsEntryFlowEditor)
    await graph.get('[aria-label="编辑步骤 1"]').trigger('click')
    graph.getComponent(OxidnsFlowNodeEditor).vm.$emit('update:modelValue', { exec: 'reject 0' })
    await flushPromises()
    expect(button('保存草稿').disabled).toBe(true)
    expect(button('分流规则').disabled).toBe(true)
    expect(graph.get('[aria-label="监听入口流程"]').attributes('disabled')).toBeDefined()
    await click('取消节点修改')
    expect(button('保存草稿').disabled).toBe(false)
    expect(oxidnsApi.applyCommonSettings).not.toHaveBeenCalled()
  })

  it('keeps an unsuccessful local patch retryable and does not save the old configuration as success', async () => {
    mockSource()
    const originalApply = vi.mocked(oxidnsApi.applyCommonSettings).getMockImplementation()!
    vi.mocked(oxidnsApi.applyCommonSettings).mockRejectedValueOnce({ response: { data: { message: '节点已变化' } } })
    const wrapper = await render()
    await click('高级')
    const graph = wrapper.getComponent(OxidnsEntryFlowEditor)
    await graph.get('[aria-label="上移步骤 2"]').trigger('click')
    await sync()
    expect(document.body.textContent).toContain('节点已变化')
    expect(button('保存草稿').disabled).toBe(true)
    vi.mocked(oxidnsApi.applyCommonSettings).mockImplementation(originalApply)
    await click('重试同步')
    await sync()
    expect(button('保存草稿').disabled).toBe(false)
    expect(oxidnsApi.applyCommonSettings).toHaveBeenCalledTimes(2)
    const calls = vi.mocked(oxidnsApi.applyCommonSettings).mock.calls
    expect(calls[1]![1]).toEqual(calls[0]![1])
  })
})
