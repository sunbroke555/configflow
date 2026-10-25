import { afterEach, describe, expect, it } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import OxidnsEntryFlowEditor from '@/components/generate/OxidnsEntryFlowEditor.vue'
import OxidnsFlowNodeEditor from '@/components/generate/OxidnsFlowNodeEditor.vue'
import { Select } from '@/components/ui/select'
import { flowAffectedEntries, flowClone, type OxidnsEntryFlow, type OxidnsFlowPatch, type OxidnsFlowPlugin } from '@/lib/oxidnsEntryFlows'

const wrappers: VueWrapper[] = []
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); document.body.innerHTML = '' })
function fixture(): OxidnsEntryFlow {
  const targets = [
    { tag: 'local_dns', type: 'sequence', exec: 'goto local_dns', source: 'config.yaml' },
    { tag: 'cache', type: 'cache', exec: '$cache', source: 'config.yaml' },
    { tag: 'included_forward', type: 'forward', exec: '$included_forward', source: 'plugins/extra.yaml' },
  ]
  const plugin = (tag: string, type: string, args: unknown, index: number, source = 'config.yaml'): OxidnsFlowPlugin => ({
    id: `${source}:${index}`, tag, type, kind: type.endsWith('_server') ? 'server' : 'executor', source, index, editable: true, targets,
    value: { tag, type, args, extension: { keep: [0, false, null] } },
  })
  const plugins = [
    plugin('udp_a', 'udp_server', { listen: '127.0.0.1:15353', entry: 'main_a' }, 0),
    plugin('tcp_a', 'tcp_server', { listen: '127.0.0.1:15353', entry: 'main_a' }, 1),
    plugin('udp_b', 'udp_server', { listen: '127.0.0.1:15354', entry: 'main_b' }, 2),
    plugin('main_a', 'sequence', [
      { exec: '$cache', future: { off: false, zero: 0 } },
      { matches: 'qname full:office.test', exec: 'goto local_dns', note: 'keep string matches' },
      { exec: 'accept', extra: [false, null, 0] },
    ], 3),
    plugin('main_b', 'sequence', [{ exec: '$included_forward', extension: false }], 4),
    plugin('local_dns', 'sequence', [{ exec: '$included_forward' }, { exec: 'accept' }], 5),
    plugin('cache', 'cache', { size: 1024, future: false }, 6),
    plugin('included_forward', 'forward', { concurrent: 1, upstreams: [{ addr: '192.0.2.53' }], future: { nil: null } }, 0, 'plugins/extra.yaml'),
  ]
  const id = (tag: string) => plugins.find(plugin => plugin.tag === tag)!.id
  const entries = ['udp_a', 'tcp_a', 'udp_b'].map(tag => {
    const listener = plugins.find(plugin => plugin.tag === tag)!
    const args = listener.value.args as { listen: string; entry: string }
    return { id: `entry:${tag}`, listener_id: listener.id, listener_tag: tag, listener_type: listener.type, ...args }
  })
  return {
    entries, plugins,
    edges: [['udp_a', 'main_a'], ['tcp_a', 'main_a'], ['udp_b', 'main_b'], ['main_a', 'cache'], ['main_a', 'local_dns'], ['local_dns', 'included_forward'], ['main_b', 'included_forward']]
      .map(([from, to]) => ({ from: id(from!), to: id(to!), target: to!, path: ['args'] })),
    globals: { source: 'config.yaml', value: { runtime: { worker_threads: 2 }, future_global: { off: false } } },
  }
}
function render(flow = fixture(), slots?: { simple: string }) {
  const wrapper = mount(OxidnsEntryFlowEditor, { props: { flow }, slots, attachTo: document.body })
  wrappers.push(wrapper)
  return wrapper
}
function control(wrapper: VueWrapper, label: string) {
  const select = wrapper.findAllComponents(Select).find(component => component.find(`[aria-label="${label}"]`).exists())
  expect(select, label).toBeDefined()
  return select!
}
async function select(wrapper: VueWrapper, label: string, value: string) {
  control(wrapper, label).vm.$emit('update:modelValue', value)
  await flushPromises()
}
function button(wrapper: VueWrapper, text: string) {
  const result = wrapper.findAll('button').find(button => button.text().trim() === text)
  expect(result, text).toBeDefined()
  return result!
}
async function click(wrapper: VueWrapper, text: string) { await button(wrapper, text).trigger('click'); await flushPromises() }
function latest(wrapper: VueWrapper) { return wrapper.emitted('change')?.at(-1)?.[0] as OxidnsFlowPatch | undefined }
function pending(wrapper: VueWrapper) { return wrapper.emitted('pending')?.at(-1)?.[0] }
function problem(wrapper: VueWrapper) { return wrapper.emitted('error')?.at(-1)?.[0] }
const main = (flow: OxidnsEntryFlow) => flow.plugins.find(plugin => plugin.tag === 'main_a')!
async function nodeYaml(wrapper: VueWrapper, text: string) {
  const node = wrapper.getComponent(OxidnsFlowNodeEditor)
  await click(node, 'YAML')
  await node.get('textarea').setValue(text)
  await flushPromises()
}
async function acceptPatch(wrapper: VueWrapper, flow: OxidnsEntryFlow) {
  const patch = latest(wrapper)!
  const next = flowClone(flow)
  if ('kind' in patch) next.globals.value = flowClone(patch.value)
  else next.plugins.find(plugin => plugin.source === patch.source && plugin.index === patch.index)!.value = flowClone(patch.value)
  await wrapper.setProps({ flow: next })
  await flushPromises()
  return next
}

describe('OxiDNS entry flow graph and node editing', () => {
  it('requires an explicit choice among independent entry flows and identifies shared listeners', async () => {
    const wrapper = render()
    expect(control(wrapper, '监听入口流程').props('modelValue')).toBe('')
    expect(wrapper.text()).toContain('请选择入口流程')
    expect(wrapper.find('[aria-label="处理流程图"]').exists()).toBe(false)
    expect(latest(wrapper)).toBeUndefined()
    await select(wrapper, '监听入口流程', 'main_a')
    expect(wrapper.text()).toContain('2 个监听入口共用 main_a')
    expect(wrapper.find('[aria-label="编辑监听 udp_a"]').exists()).toBe(true)
    expect(wrapper.find('[aria-label="编辑监听 tcp_a"]').exists()).toBe(true)
    expect(wrapper.find('[aria-label="编辑监听 udp_b"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('无条件执行')
    expect(wrapper.text()).toContain('goto local_dns')
    expect(wrapper.findAll('[aria-label^="编辑步骤 "]')).toHaveLength(3)
    expect(latest(wrapper)).toBeUndefined()
  })

  it('keeps invalid simple-mode drafts mounted and blocks switching modes or entries until repaired', async () => {
    const wrapper = render(fixture(), { simple: '<input aria-label="简洁草稿" />' })
    await wrapper.setProps({ simpleEnabled: true, simpleTarget: 'main_a' })
    await select(wrapper, '监听入口流程', 'main_a')
    await click(wrapper, '简洁设置')
    const draft = wrapper.get('[aria-label="简洁草稿"]')
    await draft.setValue('unfinished YAML: [')
    await wrapper.setProps({ simpleInvalid: true })
    expect(button(wrapper, '简洁设置').attributes('aria-pressed')).toBe('true')
    expect(button(wrapper, '流程图').attributes('disabled')).toBeDefined()
    for (const label of ['监听入口流程', '查看流程或插件', '编辑监听 udp_a']) expect(wrapper.get(`[aria-label="${label}"]`).attributes('disabled')).toBeDefined()
    expect(button(wrapper, '全局参数').attributes('disabled')).toBeDefined()
    await click(wrapper, '流程图')
    await select(wrapper, '监听入口流程', 'main_b')
    expect(button(wrapper, '简洁设置').attributes('aria-pressed')).toBe('true')
    expect(control(wrapper, '监听入口流程').props('modelValue')).toBe('main_a')
    expect(wrapper.find('[aria-label="处理流程图"]').exists()).toBe(false)
    expect(wrapper.get('[aria-label="简洁草稿"]').element).toBe(draft.element)
    expect(draft.element).toHaveProperty('value', 'unfinished YAML: [')
    expect(latest(wrapper)).toBeUndefined()
    await wrapper.setProps({ simpleInvalid: false })
    await click(wrapper, '流程图')
    expect(wrapper.find('[aria-label="处理流程图"]').exists()).toBe(true)
    await select(wrapper, '监听入口流程', 'main_b')
    expect(control(wrapper, '监听入口流程').props('modelValue')).toBe('main_b')
  })

  it('counts HTTP paths by their actual root and only includes both paths when editing their shared listener', async () => {
    const flow = fixture()
    const listener = flow.plugins.find(plugin => plugin.tag === 'udp_a')!
    listener.tag = 'doh'
    listener.type = 'http_server'
    listener.value = { tag: 'doh', type: 'http_server', args: { listen: ':8443', entries: [{ path: '/office', exec: 'main_a' }, { path: '/guest', exec: 'main_b' }] } }
    flow.entries = [
      { id: 'doh-office', listener_id: listener.id, listener_tag: 'doh', listener_type: 'http_server', listen: ':8443', entry: 'main_a', path: '/office' },
      { id: 'doh-guest', listener_id: listener.id, listener_tag: 'doh', listener_type: 'http_server', listen: ':8443', entry: 'main_b', path: '/guest' },
    ]
    const rootA = main(flow)
    const rootB = flow.plugins.find(plugin => plugin.tag === 'main_b')!
    const cache = flow.plugins.find(plugin => plugin.tag === 'cache')!
    const forward = flow.plugins.find(plugin => plugin.tag === 'included_forward')!
    rootA.value.args = [{ exec: '$cache' }]
    rootB.value.args = [{ exec: '$included_forward' }]
    flow.plugins = [listener, rootA, rootB, cache, forward]
    flow.edges = [[listener, rootA], [listener, rootB], [rootA, cache], [rootB, forward]].map(([from, to]) => ({ from: from!.id, to: to!.id, target: to!.tag, path: ['args'] }))
    for (const plugin of [rootA, cache]) expect(flowAffectedEntries(flow, plugin.id).map(entry => entry.path)).toEqual(['/office'])
    for (const plugin of [rootB, forward]) expect(flowAffectedEntries(flow, plugin.id).map(entry => entry.path)).toEqual(['/guest'])
    expect(flowAffectedEntries(flow, listener.id).map(entry => entry.path)).toEqual(['/office', '/guest'])
    const wrapper = render(flow)
    await select(wrapper, '监听入口流程', 'main_a')
    expect(wrapper.get('[aria-label="关联监听入口"]').text()).toContain('/office')
    expect(wrapper.get('[aria-label="关联监听入口"]').text()).not.toContain('/guest')
    expect(wrapper.text()).not.toContain('此节点影响 2 个监听入口')
    await select(wrapper, '查看流程或插件', listener.id)
    const affected = wrapper.findAll('[role="status"]').find(status => status.text().includes('此节点影响'))!
    expect(affected.text()).toContain('此节点影响 2 个监听入口')
    expect(affected.text()).toContain('/office')
    expect(affected.text()).toContain('/guest')
    expect(latest(wrapper)).toBeUndefined()
  })

  it('identifies environment-driven entries without guessing their target or reporting a missing plugin', async () => {
    const flow = fixture()
    const listener = flow.plugins.find(plugin => plugin.tag === 'udp_a')!
    ;(listener.value.args as Record<string, unknown>).entry = '${DNS_ENTRY}'
    flow.entries = [{ ...flow.entries[0]!, entry: '${DNS_ENTRY}', dynamic: true }]
    flow.edges = flow.edges.filter(edge => edge.from !== listener.id)
    const wrapper = render(flow)
    expect(control(wrapper, '监听入口流程').props('modelValue')).toBe('${DNS_ENTRY}')
    expect(wrapper.text()).toContain('入口由环境变量决定')
    expect(wrapper.text()).toContain('实际目标在 OxiDNS 运行时解析')
    expect(wrapper.text()).not.toContain('此入口引用的插件不存在或名称重复')
    expect(wrapper.find('[aria-label="处理流程图"]').exists()).toBe(false)
    expect(control(wrapper, '查看流程或插件').props('modelValue')).toBe('')
    await select(wrapper, '查看流程或插件', main(flow).id)
    expect(wrapper.find('[aria-label="处理流程图"]').exists()).toBe(true)
    expect((listener.value.args as Record<string, unknown>).entry).toBe('${DNS_ENTRY}')
    expect(latest(wrapper)).toBeUndefined()
  })

  it('patches exactly one unconditional step while retaining all other steps and plugin fields', async () => {
    const flow = fixture()
    const wrapper = render(flow)
    await select(wrapper, '监听入口流程', 'main_a')
    await wrapper.get('[aria-label="编辑步骤 1"]').trigger('click')
    await flushPromises()
    await wrapper.get('[aria-label="步骤 1.exec 值"]').setValue('$included_forward')
    expect(latest(wrapper)).toBeUndefined()
    expect(pending(wrapper)).toBe(true)
    await click(wrapper, '应用节点修改')
    const before = main(flow).value
    const args = flowClone(before.args as any[])
    args[0].exec = '$included_forward'
    expect(latest(wrapper)).toEqual({ source: 'config.yaml', index: 3, before, value: { ...before, args } })
    expect(args[0]).not.toHaveProperty('matches')
    expect((before.args as any[])[0].exec).toBe('$cache')
    expect(pending(wrapper)).toBe(false)
  })

  it.each([['前', 1], ['后', 2]] as const)('inserts before or after the exact selected step (%s) without changing its string conditions', async (side, insertionIndex) => {
    const flow = fixture()
    const wrapper = render(flow)
    await select(wrapper, '监听入口流程', 'main_a')
    await wrapper.get(`[aria-label="在步骤 2 ${side}插入"]`).trigger('click')
    await flushPromises()
    expect(button(wrapper, '应用节点修改').attributes('disabled')).toBeDefined()
    expect(pending(wrapper)).toBe(true)
    await select(wrapper, '步骤动作快捷选择', 'reject 3')
    await click(wrapper, '应用节点修改')
    const before = main(flow).value
    const args = flowClone(before.args as any[])
    args.splice(insertionIndex, 0, { exec: 'reject 3' })
    expect(latest(wrapper)).toEqual({ source: 'config.yaml', index: 3, before, value: { ...before, args } })
    expect(args.find(item => item.note)?.matches).toBe('qname full:office.test')
  })

  it('reorders and deletes only the requested step while preserving extensions and order of survivors', async () => {
    let flow = fixture()
    const wrapper = render(flow)
    await select(wrapper, '监听入口流程', 'main_a')
    const initial = flowClone(main(flow).value)
    await wrapper.get('[aria-label="上移步骤 2"]').trigger('click')
    const originalArgs = initial.args as unknown[]
    expect(latest(wrapper)).toEqual({ source: 'config.yaml', index: 3, before: initial, value: { ...initial, args: [originalArgs[1], originalArgs[0], originalArgs[2]] } })
    flow = await acceptPatch(wrapper, flow)
    await wrapper.get('[aria-label="删除步骤 2"]').trigger('click')
    expect(latest(wrapper)).toEqual({ source: 'config.yaml', index: 3, before: main(flow).value, value: { ...initial, args: [originalArgs[1], originalArgs[2]] } })
    expect(main(fixture()).value).toEqual(initial)
  })

  it('blocks entry and node navigation while dirty, and cancel discards changes without emitting a patch', async () => {
    const wrapper = render()
    await select(wrapper, '监听入口流程', 'main_a')
    await wrapper.get('[aria-label="编辑步骤 1"]').trigger('click')
    await flushPromises()
    await wrapper.get('[aria-label="步骤 1.exec 值"]').setValue('return')
    for (const label of ['监听入口流程', '查看流程或插件', '编辑步骤 2', '编辑监听 udp_a', '上移步骤 2']) expect(wrapper.get(`[aria-label="${label}"]`).attributes('disabled')).toBeDefined()
    await select(wrapper, '监听入口流程', 'main_b')
    expect(control(wrapper, '监听入口流程').props('modelValue')).toBe('main_a')
    await click(wrapper, '取消节点修改')
    expect(pending(wrapper)).toBe(false)
    expect(problem(wrapper)).toBe('')
    expect(latest(wrapper)).toBeUndefined()
    await wrapper.get('[aria-label="编辑步骤 1"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[aria-label="步骤 1.exec 值"]').element).toHaveProperty('value', '$cache')
  })

  it('keeps invalid node YAML pending and cannot overwrite it through the action shortcut', async () => {
    const wrapper = render()
    await select(wrapper, '监听入口流程', 'main_a')
    await wrapper.get('[aria-label="编辑步骤 1"]').trigger('click')
    await flushPromises()
    await nodeYaml(wrapper, 'exec: [unfinished')
    expect(problem(wrapper)).toBeTruthy()
    expect(pending(wrapper)).toBe(true)
    expect(button(wrapper, '应用节点修改').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[aria-label="步骤动作快捷选择"]').attributes('disabled')).toBeDefined()
    await click(wrapper, '应用节点修改')
    expect(latest(wrapper)).toBeUndefined()
    expect(wrapper.get('[aria-label="步骤 1 YAML"]').element).toHaveProperty('value', 'exec: [unfinished')
    await click(wrapper, '取消节点修改')
    expect(problem(wrapper)).toBe('')
    expect(pending(wrapper)).toBe(false)
  })

  it('refreshes shortcut targets from current flow metadata while keeping an untouched editor open', async () => {
    const flow = fixture()
    const wrapper = render(flow)
    await select(wrapper, '监听入口流程', 'main_a')
    await wrapper.get('[aria-label="编辑步骤 1"]').trigger('click')
    await flushPromises()
    const next = flowClone(flow)
    main(next).targets = [{ tag: 'new_dns', type: 'sequence', exec: 'goto new_dns', source: 'plugins/new.yaml' }]
    await wrapper.setProps({ flow: next })
    await wrapper.get('[aria-label="步骤动作快捷选择"]').trigger('keydown', { key: 'ArrowDown' })
    await flushPromises()
    const options = Array.from(document.querySelectorAll('[role="option"]')).map(option => option.textContent)
    expect(options.some(text => text?.includes('new_dns'))).toBe(true)
    expect(options.some(text => text?.includes('included_forward'))).toBe(false)
    expect(latest(wrapper)).toBeUndefined()
    expect(pending(wrapper)).toBe(false)
  })

  it('edits an included non-sequence executor without moving it into the main file or exposing tag and type', async () => {
    const flow = fixture()
    const plugin = flow.plugins.find(plugin => plugin.tag === 'included_forward')!
    const wrapper = render(flow)
    await select(wrapper, '查看流程或插件', plugin.id)
    expect(wrapper.find('[aria-label="流程步骤"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('plugins/extra.yaml')
    expect(wrapper.find('[aria-label="included_forward 参数.tag 值"]').exists()).toBe(false)
    expect(wrapper.find('[aria-label="included_forward 参数.type 值"]').exists()).toBe(false)
    await wrapper.get('[aria-label="included_forward 参数.args.concurrent 值"]').setValue('2')
    await click(wrapper, '应用节点修改')
    expect(latest(wrapper)).toEqual({ source: plugin.source, index: plugin.index, before: plugin.value, value: { ...plugin.value, args: { ...(plugin.value.args as object), concurrent: 2 } } })
    expect((plugin.value.args as any).concurrent).toBe(1)
  })

  it('edits global parameters without adding plugins or losing unknown top-level fields', async () => {
    const flow = fixture()
    const wrapper = render(flow)
    await click(wrapper, '全局参数')
    await wrapper.get('[aria-label="全局参数.runtime.worker_threads 值"]').setValue('3')
    await click(wrapper, '应用节点修改')
    expect(latest(wrapper)).toEqual({ kind: 'globals', before: flow.globals.value, value: { runtime: { worker_threads: 3 }, future_global: { off: false } } })
    expect(flow.globals.value.runtime).toEqual({ worker_threads: 2 })
  })

  it.each([
    ['scalar node', 'null', '对象'],
    ['missing execution', 'future: false', '执行动作'],
  ])('blocks applying a structurally inappropriate step (%s)', async (_, text, message) => {
    const wrapper = render()
    await select(wrapper, '监听入口流程', 'main_a')
    await wrapper.get('[aria-label="编辑步骤 1"]').trigger('click')
    await flushPromises()
    await nodeYaml(wrapper, text)
    expect(problem(wrapper)).toContain(message)
    expect(button(wrapper, '应用节点修改').attributes('disabled')).toBeDefined()
    expect(latest(wrapper)).toBeUndefined()
  })

  it('keeps malformed duplicate-target plugins read-only and displays missing reference targets', async () => {
    const flow = fixture()
    main(flow).editable = false
    main(flow).reason = '此插件名称重复，请在完整参数中修正。'
    ;(main(flow).value.args as any[])[0].exec = 'goto missing'
    const wrapper = render(flow)
    await select(wrapper, '监听入口流程', 'main_a')
    expect(wrapper.text()).toContain('此插件名称重复')
    expect(wrapper.text()).toContain('目标不可用')
    for (const label of ['编辑流程参数 main_a', '编辑步骤 1', '删除步骤 1']) expect(wrapper.get(`[aria-label="${label}"]`).attributes('disabled')).toBeDefined()
    expect(latest(wrapper)).toBeUndefined()
  })
})
