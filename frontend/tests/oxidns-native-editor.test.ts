import { afterEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { dump, JSON_SCHEMA, load } from 'js-yaml'
import OxidnsNativeEditor from '@/components/generate/OxidnsNativeEditor.vue'
import OxidnsSchemaValueEditor from '@/components/generate/OxidnsSchemaValueEditor.vue'
import OxidnsParameterPicker from '@/components/generate/OxidnsParameterPicker.vue'
import OxidnsConfigNavigation from '@/components/generate/OxidnsConfigNavigation.vue'
import { Select } from '@/components/ui/select'
import { DropdownMenuItem } from '@/components/ui/dropdown-menu'
import { configNodeToValue, valueToConfigNode } from '@/lib/oxidnsConfigEditor'

// Small official-shaped catalog keeps these tests focused on editor semantics.
// The complete, generated documentation catalog has its own coverage tests.
vi.mock('@/lib/oxidnsSchema', () => ({
  oxidnsGlobalSchema: { type: 'object', help: '全局配置', properties: {
    runtime: { type: 'object', label: '运行时', help: '线程设置', properties: { worker_threads: { type: 'number', label: '线程数', help: '工作线程', default: 2 } } },
    api: { type: 'object', label: '管理 API', help: 'API', properties: {
      password: { type: 'string', help: '密码' },
      http: { type: 'object', label: 'HTTP 服务', help: 'HTTP 管理服务', properties: {
        listen: { type: 'string', label: '监听地址', help: '绑定地址', default: '127.0.0.1:9090' },
        auth: { type: 'object', label: '身份认证', help: '管理认证', properties: {
          enabled: { type: 'boolean', help: '认证开关', default: false },
          password: { type: 'string', help: '认证密码' },
        } },
        webui: { type: 'object', label: 'WebUI', help: '管理界面', properties: { enabled: { type: 'boolean', help: '界面开关', default: false } } },
      } },
    } },
  } },
  oxidnsPluginCatalog: [
    {type: 'cache', category: 'executor', label: 'DNS 缓存', help: '缓存 DNS 响应', args: {type: 'object', properties: {size: {type: 'number', help: '缓存容量'}}}},
    {type: 'query_recorder', category: 'executor', label: '查询记录', help: '记录 DNS 查询', args: {type: 'object', properties: {path: {type: 'string', help: '记录文件'}}}},
    {type: 'fallback', category: 'executor', label: '主备回退', help: '主备执行器', args: {type: 'object', properties: {primary: {type: 'string', help: '主执行器'}}}},
    { type: 'forward', category: 'executor', label: '转发', help: '向上游转发查询', documentation: 'https://oxidns.org/plugin-reference/executor/resolution/', args: { type: 'object', label: '参数', help: '转发参数', properties: {
      concurrent: { type: 'number', label: '并发数', help: '并发上游数', default: 1 },
      upstreams: { type: 'array', help: '上游列表', items: { type: 'object', help: '单个上游', properties: { addr: { type: 'string', help: '上游地址' }, enable_pipeline: { type: 'boolean', help: '连接流水线', default: false } } } },
    } } },
    { type: 'sequence', category: 'executor', label: '执行序列', help: '依次执行规则', documentation: 'https://oxidns.org/', args: { type: 'array', help: '序列条目', items: { type: 'object', help: '规则', properties: { exec: { type: 'string', help: '执行动作' } } } } },
  ],
}))

const wrappers: VueWrapper[] = []
afterEach(() => wrappers.splice(0).forEach(wrapper => wrapper.unmount()))
function render(modelValue = 'plugins: []\n', reconcile?: (before: string, after: string, renames?: Record<string, string>, standardAdd?: string[]) => Promise<{native_config: string; notices?: string[]}>) {
  const wrapper = mount(OxidnsNativeEditor, { props: { modelValue, initialView: 'full', reconcile } })
  wrappers.push(wrapper)
  return wrapper
}
async function click(wrapper: VueWrapper, text: string) {
  const button = wrapper.findAll('button').find(button => button.text().trim() === text)
  expect(button, text).toBeDefined()
  await button!.trigger('click')
  await flushPromises()
}
async function select(wrapper: VueWrapper, ariaLabel: string, value: string) {
  const control = wrapper.findAllComponents(Select).find(select => select.find(`[aria-label="${ariaLabel}"]`).exists())
  expect(control, ariaLabel).toBeDefined()
  control!.vm.$emit('update:modelValue', value)
  await flushPromises()
}
async function pick(wrapper: VueWrapper, label: string, key: string) {
  const picker = wrapper.findAllComponents(OxidnsParameterPicker).find(component => component.props('label') === label)
  expect(picker, label).toBeDefined()
  expect(picker!.props('fields').some((field: any) => field.key === key && field.schema.help)).toBe(true)
  picker!.vm.$emit('select', key)
  await flushPromises()
}
async function menuAction(wrapper: VueWrapper, label: string, action: string) {
  await wrapper.get(`[aria-label="${label} 更多操作"]`).trigger('keydown', { key: 'Enter' })
  await flushPromises()
  const item = wrapper.findAllComponents(DropdownMenuItem).find(item => item.attributes('aria-label') === `${label} ${action}`)
  expect(item, `${label} ${action}`).toBeDefined()
  item!.vm.$emit('select')
  await flushPromises()
}
interface NavigationItem { id: string; label: string; code?: string; children?: NavigationItem[]; available?: boolean; disabled?: boolean }
function navigationItems(wrapper: VueWrapper): NavigationItem[] {
  const flatten = (items: NavigationItem[]): NavigationItem[] => items.flatMap(item => [item, ...flatten(item.children ?? [])])
  return flatten(wrapper.getComponent(OxidnsConfigNavigation).props('items'))
}
async function navigate(wrapper: VueWrapper, path: string) {
  const item = navigationItems(wrapper).find(item => item.id === path || item.code === path || item.code?.endsWith(` · ${path}`) || item.label === path)
  expect(item, `navigation target ${path}`).toBeDefined()
  wrapper.getComponent(OxidnsConfigNavigation).vm.$emit('select', item!.id)
  await flushPromises()
  return item!
}
function selectedId(wrapper: VueWrapper) { return wrapper.getComponent(OxidnsConfigNavigation).props('selectedId') }
function model(wrapper: VueWrapper): any { return load(wrapper.emitted('update:modelValue')!.at(-1)![0] as string, { schema: JSON_SCHEMA }) }
function error(wrapper: VueWrapper): string { return wrapper.emitted('error')!.at(-1)![0] as string }

function backupDocument() {
  return { plugins: [
    { tag: 'forward_fallback', type: 'forward', args: { upstreams: [{ addr: '192.0.2.53' }, { addr: '192.0.2.54' }] } },
    { tag: 'fallback_dns', type: 'sequence', args: [{ exec: '$forward_fallback' }, { matches: ['!has_resp'], exec: 'reject 3' }] },
    { tag: 'fallback', type: 'fallback', args: { primary: 'remote_dns', secondary: 'fallback_dns', threshold: 500 } },
    { tag: 'proxy_dns_seq', type: 'sequence', args: [{ exec: '$fallback' }, { exec: 'accept' }] },
    { tag: 'remote_dns', type: 'sequence', args: [{ exec: '$forward_remote' }] },
    { tag: 'forward_remote', type: 'forward', args: { upstreams: [{ addr: '192.0.2.3:1053' }] } },
    { tag: 'forward_local', type: 'forward', args: { upstreams: [{ addr: '192.0.2.53' }] } },
    { tag: 'china_dns', type: 'sequence', args: [{ exec: '$forward_local' }] },
  ] }
}

describe('complete OxiDNS editor', () => {
  it('offers separate standard main/probe cache entries and applies the shared restored parameters', async () => {
    const restored = {plugins: [{tag: 'lazy_cache', type: 'cache', args: {size: 4096, extension: false}}, {tag: 'main', type: 'sequence', args: [{exec: '$lazy_cache'}]}]}
    const reconcile = vi.fn().mockResolvedValue({native_config: dump(restored), notices: ['主缓存已恢复']})
    const wrapper = render('plugins: []', reconcile)
    const standard = wrapper.findAllComponents(OxidnsParameterPicker).find(picker => picker.props('label') === '标准功能')!
    expect(standard.props('buttonText')).toBe('添加 / 恢复功能')
    expect(standard.props('fields')).toEqual(expect.arrayContaining([
      expect.objectContaining({key: 'lazy_cache', schema: expect.objectContaining({label: '主缓存'})}),
      expect.objectContaining({key: 'local_probe_cache', schema: expect.objectContaining({label: '国内探测缓存'})}),
    ]))
    expect(wrapper.findAllComponents(OxidnsParameterPicker).find(picker => picker.props('label') === '原生插件')!.props('buttonText')).toBe('添加自定义实例')
    await pick(wrapper, '标准功能', 'lazy_cache')
    expect(reconcile.mock.calls[0][3]).toEqual(['lazy_cache'])
    expect(load(reconcile.mock.calls[0][1])).toEqual({plugins: [{tag: 'lazy_cache', type: 'cache', args: {}}]})
    expect(model(wrapper)).toEqual(restored)
    expect(standard.props('fields').some((field: any) => field.key === 'lazy_cache')).toBe(false)
    expect(standard.props('fields').some((field: any) => field.key === 'local_probe_cache')).toBe(true)
    await pick(wrapper, '原生插件', 'sequence')
    expect(reconcile.mock.calls.at(-1)![3]).toBeUndefined()
  })

  it('places both mobile addition menus above their directory sheet', async () => {
    const wrapper = render('plugins: []', vi.fn(async (_before, candidate) => ({native_config: candidate})))
    await wrapper.get('[aria-label="目录"]').trigger('click'); await flushPromises()
    const pickers = wrapper.findAllComponents(OxidnsParameterPicker)
    for (const label of ['移动端标准功能', '移动端原生插件']) expect(pickers.find(picker => picker.props('label') === label)!.props('contentClass')).toBe('z-[70]')
    expect(pickers.find(picker => picker.props('label') === '标准功能')!.props('contentClass')).toBeUndefined()
  })

  it('retains standard intent across retry and clears it when the failed draft is discarded', async () => {
    const reconcile = vi.fn().mockRejectedValue(new Error('联动不可用'))
    const wrapper = render('plugins: []', reconcile)
    await navigate(wrapper, 'add:standard:query_recorder')
    expect(error(wrapper)).toContain('联动不可用')
    await click(wrapper, '重试同步')
    expect(reconcile.mock.calls.map(call => call[3])).toEqual([['query_recorder'], ['query_recorder']])
    await click(wrapper, '撤销未同步修改')
    expect(navigationItems(wrapper).find(item => item.id === 'add:standard:query_recorder')?.available).toBe(true)
    reconcile.mockImplementation(async (_before, candidate) => ({native_config: candidate}))
    await pick(wrapper, '原生插件', 'query_recorder')
    expect(reconcile.mock.calls.at(-1)![3]).toBeUndefined()
    expect(model(wrapper).plugins).toEqual([{tag: 'query_recorder_custom', type: 'query_recorder', args: {}}])
    expect(wrapper.text()).toContain('自定义实例需在监听入口或执行序列表单中配置引用')
  })

  it('accumulates multiple pending standard additions and keeps later form typing in the candidate', async () => {
    const requests: Array<{candidate: string; resolve: (result: {native_config: string}) => void}> = []
    const reconcile = vi.fn((_before: string, candidate: string, _renames?: Record<string, string>, _standardAdd?: string[]) => new Promise<{native_config: string}>(resolve => requests.push({candidate, resolve})))
    const wrapper = render('plugins: []', reconcile)
    await pick(wrapper, '标准功能', 'lazy_cache')
    await pick(wrapper, '标准功能', 'local_probe_cache')
    await pick(wrapper, '插件 2.args', 'size')
    await wrapper.get('[aria-label="插件 2.args.size 值"]').setValue('4096')
    expect(reconcile.mock.calls.at(-1)![3]).toEqual(['lazy_cache', 'local_probe_cache'])
    const latest = requests.at(-1)!
    latest.resolve({native_config: latest.candidate}); await flushPromises()
    for (const request of requests.slice(0, -1)) request.resolve({native_config: request.candidate})
    await flushPromises()
    expect(wrapper.emitted('update:modelValue')).toHaveLength(1)
    expect(model(wrapper).plugins[1]).toEqual({tag: 'local_probe_cache', type: 'cache', args: {size: 4096}})
  })

  it.each(['delete', 'rename'])('drops standard intent after a pending new node is changed by %s', async action => {
    const requests: Array<{candidate: string; resolve: (result: {native_config: string}) => void}> = []
    const reconcile = vi.fn((_before: string, candidate: string, _renames?: Record<string, string>, _standardAdd?: string[]) => new Promise<{native_config: string}>(resolve => requests.push({candidate, resolve})))
    const wrapper = render('plugins: []', reconcile)
    await pick(wrapper, '标准功能', 'lazy_cache')
    if (action === 'delete') await wrapper.get('[aria-label="删除原生插件 1"]').trigger('click')
    else await wrapper.get('[aria-label="插件 1.tag 值"]').setValue('lazy_cache_custom')
    expect(reconcile.mock.calls.at(-1)![3]).toBeUndefined()
    expect(reconcile.mock.calls.at(-1)![2]).toBeUndefined()
    requests.at(-1)!.resolve({native_config: requests.at(-1)!.candidate}); await flushPromises()
    requests[0].resolve({native_config: requests[0].candidate}); await flushPromises()
    expect(wrapper.emitted('update:modelValue')).toHaveLength(1)
    expect(model(wrapper).plugins).toEqual(action === 'delete' ? [] : [{tag: 'lazy_cache_custom', type: 'cache', args: {}}])
  })

  it('offers standard restoration only after its pending deletion is accepted', async () => {
    const requests: Array<{candidate: string; resolve: (result: {native_config: string}) => void; reject: (error: unknown) => void}> = []
    const reconcile = vi.fn((_before: string, candidate: string, _renames?: Record<string, string>, _standardAdd?: string[]) => new Promise<{native_config: string}>((resolve, reject) => requests.push({candidate, resolve, reject})))
    const original = {plugins: [{tag: 'lazy_cache', type: 'cache', args: {size: 8192, extension: false}}, {tag: 'main', type: 'sequence', args: [{exec: '$lazy_cache'}]}]}
    const baseline = dump(original)
    const wrapper = render(baseline, reconcile)
    await navigate(wrapper, 'lazy_cache')
    await wrapper.get('[aria-label="删除原生插件 1"]').trigger('click'); await flushPromises()
    const standard = wrapper.findAllComponents(OxidnsParameterPicker).find(picker => picker.props('label') === '标准功能')!
    expect(standard.props('fields').some((field: any) => field.key === 'lazy_cache')).toBe(false)
    expect(navigationItems(wrapper).some(item => item.id === 'add:standard:lazy_cache')).toBe(false)
    expect(standard.props('fields').some((field: any) => field.key === 'local_probe_cache')).toBe(true)
    standard.vm.$emit('select', 'lazy_cache'); await flushPromises()
    expect(reconcile).toHaveBeenCalledOnce()
    expect(reconcile.mock.calls[0][0]).toBe(baseline)
    expect((load(requests[0].candidate) as any).plugins.some((plugin: any) => plugin.tag === 'lazy_cache')).toBe(false)

    const removed = dump({plugins: [{tag: 'main', type: 'sequence', args: []}]})
    requests[0].resolve({native_config: removed}); await flushPromises()
    expect(standard.props('fields').some((field: any) => field.key === 'lazy_cache')).toBe(true)
    await pick(wrapper, '标准功能', 'lazy_cache')
    expect(reconcile.mock.calls[1][0]).toBe(removed)
    expect((load(requests[1].candidate) as any).plugins.find((plugin: any) => plugin.tag === 'lazy_cache')).toEqual({tag: 'lazy_cache', type: 'cache', args: {}})
    expect(reconcile.mock.calls[1].slice(2)).toEqual([undefined, ['lazy_cache']])
    requests[1].resolve({native_config: baseline}); await flushPromises()
    await navigate(wrapper, 'lazy_cache')
    expect((wrapper.get('[aria-label="插件 1.args.size 值"]').element as HTMLInputElement).value).toBe('8192')
    expect(error(wrapper)).toBe('')
    expect(model(wrapper)).toEqual(original)
    expect(wrapper.emitted('update:modelValue')).toHaveLength(2)
  })

  it('does not fill backup addresses or overwrite an occupied standard tag', async () => {
    const reconcile = vi.fn(async (_before: string, candidate: string, _renames?: Record<string, string>, _standardAdd?: string[]) => ({native_config: candidate}))
    const original = {plugins: [{tag: 'lazy_cache', type: 'forward', args: {upstreams: [{addr: '192.0.2.53'}]}}]}
    const wrapper = render(dump(original), reconcile)
    const choices = () => wrapper.findAllComponents(OxidnsParameterPicker).find(picker => picker.props('label') === '标准功能')!.props('fields')
    expect(choices().some((field: any) => field.key === 'lazy_cache')).toBe(false)
    await pick(wrapper, '标准功能', 'forward_fallback')
    expect(reconcile.mock.calls[0][3]).toEqual(['forward_fallback'])
    expect(model(wrapper).plugins).toEqual([...original.plugins, {tag: 'forward_fallback', type: 'forward', args: {}}])
    expect(wrapper.findAll('[role="switch"]')).toHaveLength(0)
  })

  it('keeps custom fallback instances away from managed helper tags without requesting standard activation', async () => {
    const reconcile = vi.fn(async (_before: string, candidate: string, _renames?: Record<string, string>, _standardAdd?: string[]) => ({native_config: candidate}))
    const wrapper = render('plugins: []', reconcile)
    await pick(wrapper, '原生插件', 'fallback')
    expect(model(wrapper)).toEqual({plugins: [{tag: 'fallback_custom', type: 'fallback', args: {}}]})
    expect(reconcile.mock.calls[0][3]).toBeUndefined()
    expect(navigationItems(wrapper).find(item => item.id === 'add:standard:forward_fallback')?.available).toBe(true)
    await pick(wrapper, '原生插件', 'fallback')
    expect(model(wrapper).plugins[1].tag).toBe('fallback_custom_2')
  })

  it('uses the shared lifecycle result when a managed plugin is removed', async () => {
    const original = backupDocument()
    const repaired = backupDocument()
    repaired.plugins = repaired.plugins.filter(p => !['forward_fallback', 'fallback_dns', 'fallback'].includes(p.tag))
    repaired.plugins.find(p => p.tag === 'proxy_dns_seq')!.args = [{exec: 'goto remote_dns'}]
    const reconcile = vi.fn().mockResolvedValue({native_config: dump(repaired), notices: ['已停用代理 DNS 回退']})
    const wrapper = render(dump(original), reconcile)
    await navigate(wrapper, 'forward_fallback')
    await wrapper.get('[aria-label="删除原生插件 1"]').trigger('click')
    await flushPromises()
    expect(reconcile).toHaveBeenCalledOnce()
    expect(load(reconcile.mock.calls[0][0])).toEqual(original)
    const proposed: any = load(reconcile.mock.calls[0][1])
    expect(proposed.plugins.some((p: any) => p.tag === 'forward_fallback')).toBe(false)
    expect(proposed.plugins.find((p: any) => p.tag === 'proxy_dns_seq')).toEqual(original.plugins[3])
    expect(model(wrapper)).toEqual(repaired)
    expect(wrapper.get('[role="status"]').text()).toContain('停用代理 DNS 回退')
    expect(wrapper.emitted('pending')!.map(event => event[0]).slice(-2)).toEqual([true, false])
    expect(error(wrapper)).toBe('')
  })

  it('locates a shared reference in the candidate and lets the form repair it', async () => {
    const original = backupDocument()
    original.plugins.push({tag: 'custom', type: 'sequence', args: [{exec: '$forward_fallback'}]})
    const reconcile = vi.fn().mockRejectedValueOnce({response: {data: {
      message: '不能自动删除共享插件', references: [{path: ['plugins', 7, 'args', 0, 'exec'], tag: 'forward_fallback'}],
    }}}).mockImplementation(async (_before, candidate) => ({native_config: candidate}))
    const wrapper = render(dump(original), reconcile)
    await navigate(wrapper, 'forward_fallback')
    await wrapper.get('[aria-label="删除原生插件 1"]').trigger('click')
    await flushPromises()
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
    expect(error(wrapper)).toContain('共享插件')
    await click(wrapper, '查看引用 1')
    const field = wrapper.get('[aria-label="插件 8.args[1].exec 值"]')
    expect((field.element as HTMLInputElement).value).toBe('$forward_fallback')
    await field.setValue('$forward_remote')
    await flushPromises()
    expect(reconcile).toHaveBeenCalledTimes(2)
    expect(model(wrapper).plugins.at(-1).args[0].exec).toBe('$forward_remote')
    expect(error(wrapper)).toBe('')
  })

  it('keeps failed edits local, supports retry and undo without YAML', async () => {
    const reconcile = vi.fn().mockRejectedValue(new Error('网络不可用'))
    const wrapper = render(dump(backupDocument()), reconcile)
    await navigate(wrapper, 'forward_fallback')
    await wrapper.get('[aria-label="删除原生插件 1"]').trigger('click')
    await flushPromises()
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
    await click(wrapper, '重试同步')
    expect(reconcile).toHaveBeenCalledTimes(2)
    await click(wrapper, 'YAML')
    expect(wrapper.find('textarea[aria-label="OxiDNS 完整配置 YAML"]').exists()).toBe(false)
    await click(wrapper, '撤销未同步修改')
    expect(error(wrapper)).toBe('')
    expect(navigationItems(wrapper).some(item => item.label === 'forward_fallback')).toBe(true)
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
  })

  it('ignores late dependency responses after an external configuration replaces the draft', async () => {
    let resolve!: (result: {native_config: string}) => void
    const reconcile = vi.fn(() => new Promise<{native_config: string}>(done => {resolve = done}))
    const wrapper = render(dump(backupDocument()), reconcile)
    await navigate(wrapper, 'forward_fallback')
    await wrapper.get('[aria-label="删除原生插件 1"]').trigger('click')
    await wrapper.setProps({modelValue: 'plugins: [{tag: different, type: sequence, args: []}]'})
    resolve({native_config: 'plugins: []'})
    await flushPromises()
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
    expect(navigationItems(wrapper).some(item => item.label === 'different')).toBe(true)
  })

  it('keeps newer typing when dependency requests finish out of order', async () => {
    const requests: Array<{candidate: string; resolve: (result: {native_config: string}) => void}> = []
    const reconcile = vi.fn((_before: string, candidate: string) => new Promise<{native_config: string}>(resolve => requests.push({candidate, resolve})))
    const wrapper = render(dump(backupDocument()), reconcile)
    await navigate(wrapper, 'forward_fallback')
    await wrapper.get('[aria-label="插件 1.args.upstreams 删除条目 1"]').trigger('click')
    const field = wrapper.get('[aria-label="插件 1.args.upstreams[1].addr 值"]')
    await field.setValue('192.0.2.9')
    expect(requests).toHaveLength(2)
    requests[1].resolve({native_config: requests[1].candidate})
    await flushPromises()
    requests[0].resolve({native_config: requests[0].candidate})
    await flushPromises()
    expect(wrapper.emitted('update:modelValue')).toHaveLength(1)
    expect(model(wrapper).plugins[0].args.upstreams).toEqual([{addr: '192.0.2.9'}])
    expect(wrapper.get('[aria-label="插件 1.args.upstreams[1].addr 值"]').element).toBe(field.element)
  })

  it('preserves explicit rename intent while another field changes before synchronization finishes', async () => {
    const requests: Array<{candidate: string; resolve: (result: {native_config: string}) => void}> = []
    const reconcile = vi.fn((_before: string, candidate: string, _renames?: Record<string, string>) => new Promise<{native_config: string}>(resolve => requests.push({candidate, resolve})))
    const wrapper = render('plugins: [{tag: original, type: forward, args: {concurrent: 1}}]', reconcile)
    await navigate(wrapper, 'original')
    await wrapper.get('[aria-label="插件 1.tag 值"]').setValue('renamed')
    await wrapper.get('[aria-label="插件 1.args.concurrent 值"]').setValue('4')
    expect(reconcile).toHaveBeenCalledTimes(2)
    expect(reconcile.mock.calls[1][2]).toEqual({original: 'renamed'})
    requests[1].resolve({native_config: requests[1].candidate})
    await flushPromises()
    requests[0].resolve({native_config: requests[0].candidate})
    await flushPromises()
    expect(model(wrapper).plugins[0]).toMatchObject({tag: 'renamed', args: {concurrent: 4}})
  })

  it('does not repair or rewrite native YAML on load or YAML edits', async () => {
    const original = backupDocument()
    original.plugins[0].args = { upstreams: [] }
    const wrapper = render(dump(original))
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
    await click(wrapper, 'YAML')
    await wrapper.get('[aria-label="OxiDNS 完整配置 YAML"]').setValue(dump(original) + '# manual\n')
    expect(model(wrapper)).toEqual(original)
  })

  it('does not emit defaults or rewrite comments on navigation or mode changes; preserves unknown fields when edited', async () => {
    const original = '# retained until a form edit\nfuture: {off: false, zero: 0, empty: "", nullish: null}\nplugins:\n  - tag: custom\n    type: future_type\n    args: {list: [0, false, "", null]}\n    future_flag: true\n'
    const wrapper = render(original)
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
    expect(wrapper.find('[aria-label="插件 1.tag 值"]').exists()).toBe(false)
    await navigate(wrapper, 'plugins[1]')
    expect(wrapper.get('[aria-label="插件 1.tag 值"]').exists()).toBe(true)
    await navigate(wrapper, 'future')
    await navigate(wrapper, 'plugins[1]')
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
    const activeId = selectedId(wrapper)
    await click(wrapper, 'YAML')
    expect((wrapper.get('[aria-label="OxiDNS 完整配置 YAML"]').element as HTMLTextAreaElement).value).toBe(original)
    await click(wrapper, '完整参数')
    expect(selectedId(wrapper)).toBe(activeId)
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
    await wrapper.get('[aria-label="插件 1.tag 值"]').setValue('renamed')
    expect(model(wrapper)).toEqual({ future: { off: false, zero: 0, empty: '', nullish: null }, plugins: [{ tag: 'renamed', type: 'future_type', args: { list: [0, false, '', null] }, future_flag: true }] })
    expect(error(wrapper)).toBe('')
  })

  it('adds native plugin types with native argument shapes and only explicit parameters', async () => {
    const wrapper = render()
    await pick(wrapper, '原生插件', 'forward')
    expect(model(wrapper)).toEqual({ plugins: [{ tag: 'forward', type: 'forward', args: {} }] })
    expect(wrapper.find('[aria-label="插件 1.args 展开参数"]').exists()).toBe(false)
    await pick(wrapper, '插件 1.args', 'concurrent')
    expect(model(wrapper).plugins[0].args).toEqual({ concurrent: 1 })
    await wrapper.get('[aria-label="插件 1.args.concurrent 值"]').setValue('0')
    expect(model(wrapper).plugins[0].args).toEqual({ concurrent: 0 })
    await pick(wrapper, '原生插件', 'sequence')
    expect(model(wrapper).plugins[1]).toEqual({ tag: 'sequence', type: 'sequence', args: [] })
    await pick(wrapper, '原生插件', 'forward')
    expect(model(wrapper).plugins[2].tag).toBe('forward_2')
  })

  it('shows upstream fields directly after selecting a plugin and adds described parameters in one choice', async () => {
    const wrapper = render('plugins: [{tag: forward_local, type: forward, args: {concurrent: 2, upstreams: [{addr: "223.5.5.5"}, {addr: "1.1.1.1"}]}}]')
    await navigate(wrapper, 'plugins[1]')
    expect(wrapper.find('[aria-label="插件 1 展开设置"]').exists()).toBe(false)
    expect(wrapper.find('[aria-label="插件 1.args 展开参数"]').exists()).toBe(false)
    expect(wrapper.get('[aria-label="插件 1.args.concurrent 值"]').exists()).toBe(true)
    expect(wrapper.get('[aria-label="插件 1.args.upstreams[1].addr 值"]').exists()).toBe(true)
    expect(wrapper.get('[aria-label="插件 1.args.upstreams[2].addr 值"]').exists()).toBe(true)
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
    await pick(wrapper, '插件 1.args.upstreams[1]', 'enable_pipeline')
    expect(model(wrapper).plugins[0].args.upstreams[0]).toEqual({ addr: '223.5.5.5', enable_pipeline: false })
    expect(wrapper.get('[aria-label="插件 1.args.upstreams[1].enable_pipeline说明"]').exists()).toBe(true)
    expect(wrapper.find('[aria-label="插件 1.args.upstreams[1] 添加所选参数"]').exists()).toBe(false)
    await wrapper.get('[aria-label="插件 1.args.concurrent 值"]').setValue('3')
    await navigate(wrapper, 'globals')
    await navigate(wrapper, 'plugins[1]')
    expect((wrapper.get('[aria-label="插件 1.args.concurrent 值"]').element as HTMLInputElement).value).toBe('3')
    await click(wrapper, 'YAML')
    expect(load((wrapper.get('[aria-label="OxiDNS 完整配置 YAML"]').element as HTMLTextAreaElement).value)).toEqual(model(wrapper))
  })

  it('retains malformed YAML and incomplete numeric drafts across groups, blocking unsafe mode changes', async () => {
    const wrapper = render('plugins: [broken')
    expect(error(wrapper)).not.toBe('')
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
    await click(wrapper, '完整参数')
    expect(wrapper.find('[aria-label="OxiDNS 完整配置 YAML"]').exists()).toBe(true)
    await wrapper.get('[aria-label="OxiDNS 完整配置 YAML"]').setValue('runtime: {worker_threads: 2}\napi: {password: private}\nplugins: []')
    await click(wrapper, '完整参数')
    await navigate(wrapper, 'runtime')
    const before = wrapper.emitted('update:modelValue')!.length
    await wrapper.get('[aria-label="全局参数.runtime.worker_threads 值"]').setValue('2e')
    expect(error(wrapper)).toContain('数字')
    expect(wrapper.emitted('update:modelValue')!.length).toBe(before)
    await navigate(wrapper, 'api')
    await wrapper.get('[aria-label="全局参数.api.password 值"]').setValue('updated-private')
    expect(error(wrapper)).toContain('数字')
    await navigate(wrapper, 'runtime')
    expect((wrapper.get('[aria-label="全局参数.runtime.worker_threads 值"]').element as HTMLInputElement).value).toBe('2e')
    expect(wrapper.emitted('update:modelValue')!.length).toBe(before)
    const yaml = wrapper.findAll('button').find(button => button.text().trim() === 'YAML')!
    expect(yaml.attributes('disabled')).toBeDefined()
    await wrapper.get('[aria-label="全局参数.runtime.worker_threads 值"]').setValue('3')
    expect(error(wrapper)).toBe('')
    expect(model(wrapper).runtime.worker_threads).toBe(3)
    expect(model(wrapper).api.password).toBe('updated-private')
  })

  it('returns to the remaining invalid input after another invalid group is repaired', async () => {
    const wrapper = render('runtime: {worker_threads: 2}\nplugins: [{tag: dns, type: forward, args: {concurrent: 1, upstreams: [{addr: dns.example}]}}]')
    await navigate(wrapper, 'runtime')
    await wrapper.get('[aria-label="全局参数.runtime.worker_threads 值"]').setValue('2e')
    await navigate(wrapper, 'plugins[1]')
    await wrapper.get('[aria-label="插件 1.args.concurrent 值"]').setValue('3e')
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
    await navigate(wrapper, 'runtime')
    await wrapper.get('[aria-label="全局参数.runtime.worker_threads 值"]').setValue('4')
    expect(error(wrapper)).toContain('数字')
    await click(wrapper, '返回未完成输入')
    expect(wrapper.getComponent(OxidnsSchemaValueEditor).props('label')).toBe('插件 1.args.concurrent')
    expect((wrapper.get('[aria-label="插件 1.args.concurrent 值"]').element as HTMLInputElement).value).toBe('3e')
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
    await wrapper.get('[aria-label="插件 1.args.concurrent 值"]').setValue('3')
    expect(error(wrapper)).toBe('')
    expect(model(wrapper)).toEqual({ runtime: { worker_threads: 4 }, plugins: [{ tag: 'dns', type: 'forward', args: { concurrent: 3, upstreams: [{ addr: 'dns.example' }] } }] })
  })

  it('keeps empty and duplicate-tag drafts editable for shared server validation', async () => {
    const wrapper = render('')
    expect(error(wrapper)).toBe('')
    await wrapper.setProps({ modelValue: 'plugins: [{tag: same, type: forward}, {tag: same, type: sequence}]' })
    expect(error(wrapper)).toBe('')
    await click(wrapper, 'YAML')
    await wrapper.get('[aria-label="OxiDNS 完整配置 YAML"]').setValue('plugins: [{tag: one, type: forward, args: {upstreams: [{addr: dns.example}]}}, {tag: two, type: sequence}]')
    expect(error(wrapper)).toBe('')
    await click(wrapper, '完整参数')
    await navigate(wrapper, 'plugins[2]')
    await wrapper.get('[aria-label="上移原生插件 2"]').trigger('click')
    expect(model(wrapper).plugins.map((plugin: any) => plugin.tag)).toEqual(['two', 'one'])
    await wrapper.get('[aria-label="删除原生插件 1"]').trigger('click')
    expect(model(wrapper).plugins).toEqual([{ tag: 'one', type: 'forward', args: { upstreams: [{ addr: 'dns.example' }] } }])
    expect(navigationItems(wrapper).some(item => item.id === selectedId(wrapper))).toBe(true)
  })

  it('keeps help available for selected groups and masks native password fields', async () => {
    const wrapper = render('api: {password: private}\nplugins: [{tag: dns, type: forward, args: {}}]')
    await navigate(wrapper, 'api')
    expect(wrapper.get('[aria-label="全局参数.api.password说明"]').exists()).toBe(true)
    expect(wrapper.get('[aria-label="全局参数.api.password 值"]').attributes('type')).toBe('password')
    await wrapper.setProps({ disabled: true })
    expect(wrapper.get('[aria-label="全局参数.api.password 值"]').attributes('disabled')).toBeDefined()
    expect(wrapper.getComponent(OxidnsConfigNavigation).props('disabled')).toBe(true)
  })

  it('exposes the actual parent-child hierarchy without changing any configured values', async () => {
    const original = 'api: {http: {listen: "127.0.0.1:9090", auth: {enabled: false, password: secret}, webui: {enabled: false}}}\nfuture: {zero: 0, nil: null, flag: false}\nplugins: []'
    const wrapper = render(original)
    const items = navigationItems(wrapper)
    const api = items.find(item => item.code === 'api')!
    const http = api.children!.find(item => item.code === 'api.http')!
    expect(http.children!.map(item => item.code)).toEqual(expect.arrayContaining(['api.http.auth', 'api.http.webui']))
    for (const path of ['api', 'api.http', 'api.http.auth', 'api.http.webui', 'future', 'globals']) await navigate(wrapper, path)
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
    await navigate(wrapper, 'api.http')
    expect(wrapper.get('[aria-label="全局参数.api.http.listen 值"]').exists()).toBe(true)
    await wrapper.get('[aria-label="全局参数.api.http.listen 值"]').setValue('127.0.0.1:9199')
    const expected = load(original, { schema: JSON_SCHEMA }) as any
    expected.api.http.listen = '127.0.0.1:9199'
    expect(model(wrapper)).toEqual(expected)
    await navigate(wrapper, 'api.http.auth')
    expect(wrapper.get('[aria-label="全局参数.api.http.auth.password 值"]').attributes('type')).toBe('password')
    await click(wrapper, 'YAML')
    expect(load((wrapper.get('[aria-label="OxiDNS 完整配置 YAML"]').element as HTMLTextAreaElement).value, { schema: JSON_SCHEMA })).toEqual(expected)
    await click(wrapper, '完整参数')
    expect(wrapper.get('[aria-label="全局参数.api.http.auth.password 值"]').exists()).toBe(true)
  })

  it('keeps the same plugin selected after reordering and edits that plugin at its new index', async () => {
    const wrapper = render('plugins: [{tag: first, type: forward, args: {concurrent: 1}}, {tag: second, type: forward, args: {concurrent: 2}}]')
    const second = await navigate(wrapper, 'plugins[2]')
    await wrapper.get('[aria-label="上移原生插件 2"]').trigger('click')
    expect(selectedId(wrapper)).toBe(second.id)
    expect((wrapper.get('[aria-label="插件 1.tag 值"]').element as HTMLTextAreaElement).value).toBe('second')
    await wrapper.get('[aria-label="插件 1.args.concurrent 值"]').setValue('0')
    expect(model(wrapper).plugins).toEqual([{ tag: 'second', type: 'forward', args: { concurrent: 0 } }, { tag: 'first', type: 'forward', args: { concurrent: 1 } }])
  })

  it('restores selection after YAML edits and falls back safely when the selected group is removed', async () => {
    const wrapper = render('api: {http: {listen: "127.0.0.1:9090", auth: {enabled: false}}}\nplugins: []')
    await navigate(wrapper, 'api.http.auth')
    await click(wrapper, 'YAML')
    await wrapper.get('[aria-label="OxiDNS 完整配置 YAML"]').setValue('api: {http: {listen: "127.0.0.1:9199", auth: {enabled: true}}}\nplugins: []')
    await click(wrapper, '完整参数')
    expect(navigationItems(wrapper).find(item => item.id === selectedId(wrapper))?.code).toBe('api.http.auth')
    await click(wrapper, 'YAML')
    await wrapper.get('[aria-label="OxiDNS 完整配置 YAML"]').setValue('runtime: {worker_threads: 0}\nplugins: []')
    await click(wrapper, '完整参数')
    expect(navigationItems(wrapper).some(item => item.id === selectedId(wrapper))).toBe(true)
    await navigate(wrapper, 'runtime')
    expect((wrapper.get('[aria-label="全局参数.runtime.worker_threads 值"]').element as HTMLInputElement).value).toBe('0')
    expect(error(wrapper)).toBe('')
  })

  it('tracks a selected plugin by its tag when YAML changes plugin order', async () => {
    const wrapper = render('plugins: [{tag: first, type: forward, args: {concurrent: 1}}, {tag: second, type: forward, args: {concurrent: 2}}]')
    await navigate(wrapper, 'plugins[2]')
    await click(wrapper, 'YAML')
    await wrapper.get('[aria-label="OxiDNS 完整配置 YAML"]').setValue('plugins: [{tag: second, type: forward, args: {concurrent: 0}}, {tag: first, type: forward, args: {concurrent: 1}}]')
    await click(wrapper, '完整参数')
    expect(navigationItems(wrapper).find(item => item.id === selectedId(wrapper))?.label).toBe('second')
    expect((wrapper.get('[aria-label="插件 1.tag 值"]').element as HTMLTextAreaElement).value).toBe('second')
    expect((wrapper.get('[aria-label="插件 1.args.concurrent 值"]').element as HTMLInputElement).value).toBe('0')
  })

  it('locks the global document to an object and retains all configured groups', async () => {
    const wrapper = render('runtime: {worker_threads: 2}\nplugins: [{tag: dns, type: forward, args: {}}]')
    await navigate(wrapper, 'globals')
    expect(wrapper.getComponent(OxidnsSchemaValueEditor).props('typeLocked')).toBe(true)
    await wrapper.get('[aria-label="全局参数 更多操作"]').trigger('keydown', { key: 'Enter' })
    await flushPromises()
    expect(wrapper.findAllComponents(DropdownMenuItem).some(item => item.attributes('aria-label') === '全局参数 更改值类型')).toBe(false)
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
    await navigate(wrapper, 'runtime')
    expect(wrapper.getComponent(OxidnsSchemaValueEditor).props('typeLocked')).toBe(false)
    expect((wrapper.get('[aria-label="全局参数.runtime.worker_threads 值"]').element as HTMLInputElement).value).toBe('2')
  })

  it('commits a selected nested object changed to a scalar and keeps the resulting value editable', async () => {
    const wrapper = render('api: {http: {auth: {enabled: false}, webui: {enabled: false}}}\nplugins: []')
    const auth = await navigate(wrapper, 'api.http.auth')
    await menuAction(wrapper, '全局参数.api.http.auth', '更改值类型')
    await select(wrapper, '全局参数.api.http.auth 类型', 'string')
    expect(selectedId(wrapper)).toBe(auth.id)
    expect(model(wrapper)).toEqual({ api: { http: { auth: '', webui: { enabled: false } } }, plugins: [] })
    await wrapper.get('[aria-label="全局参数.api.http.auth 值"]').setValue('${API_AUTH}')
    expect(model(wrapper).api.http.auth).toBe('${API_AUTH}')
    await navigate(wrapper, 'api.http')
    expect((wrapper.get('[aria-label="全局参数.api.http.auth 值"]').element as HTMLTextAreaElement).value).toBe('${API_AUTH}')
  })

  it('removes a selected argument group from its real owner when navigation skips args', async () => {
    const wrapper = render('plugins: [{tag: dns, type: forward, args: {concurrent: 0, upstreams: [{addr: "223.5.5.5", future: false}]}, future: null}]')
    const plugin = navigationItems(wrapper).find(item => item.label === 'dns')!
    const upstream = await navigate(wrapper, 'plugins[1].args.upstreams')
    expect(plugin.children!.some(item => item.id === upstream.id)).toBe(true)
    expect(wrapper.getComponent(OxidnsSchemaValueEditor).props('removable')).toBe(true)
    await menuAction(wrapper, '插件 1.args.upstreams', '删除字段')
    expect(model(wrapper)).toEqual({ plugins: [{ tag: 'dns', type: 'forward', args: { concurrent: 0 }, future: null }] })
    expect(selectedId(wrapper)).toBe(plugin.id)
    expect(wrapper.get('[aria-label="插件 1.args.concurrent 值"]').exists()).toBe(true)
  })

  it('keeps unknown keys containing dots distinct from visually similar nested paths', async () => {
    const wrapper = render('future: {"a.b": {flag: false}, a: {b: {zero: 0}}}\nplugins: []')
    const paths = navigationItems(wrapper).filter(item => item.code === 'future.a.b')
    expect(paths).toHaveLength(2)
    wrapper.getComponent(OxidnsConfigNavigation).vm.$emit('select', paths[0].id)
    await flushPromises()
    expect(wrapper.getComponent(OxidnsSchemaValueEditor).props('node').entries[0].key).toBe('flag')
    wrapper.getComponent(OxidnsConfigNavigation).vm.$emit('select', paths[1].id)
    await flushPromises()
    await wrapper.get('[aria-label="全局参数.future.a.b.zero 值"]').setValue('1')
    expect(model(wrapper)).toEqual({ future: { 'a.b': { flag: false }, a: { b: { zero: 1 } } }, plugins: [] })
    await click(wrapper, 'YAML')
    await click(wrapper, '完整参数')
    expect(wrapper.getComponent(OxidnsSchemaValueEditor).props('node').entries[0].key).toBe('zero')
  })

  it('allows every parent and child schema parameter to be added in its own selected group', async () => {
    const wrapper = render('plugins: []')
    await navigate(wrapper, 'globals')
    await pick(wrapper, '全局参数', 'runtime')
    await navigate(wrapper, 'runtime')
    await pick(wrapper, '全局参数.runtime', 'worker_threads')
    await navigate(wrapper, 'globals')
    await pick(wrapper, '全局参数', 'api')
    await navigate(wrapper, 'api')
    await pick(wrapper, '全局参数.api', 'password')
    await pick(wrapper, '全局参数.api', 'http')
    await navigate(wrapper, 'api.http')
    await pick(wrapper, '全局参数.api.http', 'listen')
    await pick(wrapper, '全局参数.api.http', 'auth')
    await navigate(wrapper, 'api.http.auth')
    await pick(wrapper, '全局参数.api.http.auth', 'enabled')
    await pick(wrapper, '全局参数.api.http.auth', 'password')
    await navigate(wrapper, 'api.http')
    await pick(wrapper, '全局参数.api.http', 'webui')
    await navigate(wrapper, 'api.http.webui')
    await pick(wrapper, '全局参数.api.http.webui', 'enabled')
    expect(model(wrapper)).toEqual({ runtime: { worker_threads: 2 }, api: { password: '', http: { listen: '127.0.0.1:9090', auth: { enabled: false, password: '' }, webui: { enabled: false } } }, plugins: [] })
  })
  it('browses functional categories and absent features without writing any defaults', async () => {
    const original = '# untouched comment\nplugins: []\n'
    const wrapper = render(original)
    const groups = wrapper.getComponent(OxidnsConfigNavigation).props('items')
    expect(groups.map((item: NavigationItem) => item.label)).toEqual(['运行设置', '监听服务', 'DNS 上游', '缓存', '分流规则', '规则资源', '特色功能', '网络出站', '扩展配置'])
    expect(navigationItems(wrapper).find(item => item.code === 'api.http.webui')?.available).toBe(true)
    expect(navigationItems(wrapper).find(item => item.id === 'add:plugin:forward')?.available).toBe(true)
    for (const group of groups) await navigate(wrapper, group.id)
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
    await click(wrapper, 'YAML')
    expect((wrapper.get('[aria-label="OxiDNS 完整配置 YAML"]').element as HTMLTextAreaElement).value).toBe(original)
    await click(wrapper, '完整参数')
    expect(selectedId(wrapper)).toBe('category:advanced')
  })

  it('adds an absent nested global feature in one action without sibling fields or default values', async () => {
    const wrapper = render('future: {off: false, zero: 0, nil: null}\nplugins: []')
    await navigate(wrapper, 'api.http.webui')
    expect(model(wrapper)).toEqual({ future: { off: false, zero: 0, nil: null }, api: { http: { webui: {} } }, plugins: [] })
    expect(wrapper.getComponent(OxidnsSchemaValueEditor).props('label')).toBe('全局参数.api.http.webui')
    expect(navigationItems(wrapper).find(item => item.code === 'api.http.webui')?.available).not.toBe(true)
    expect(navigationItems(wrapper).find(item => item.code === 'api.http.auth')?.available).toBe(true)
    await navigate(wrapper, 'api.http.auth')
    expect(model(wrapper).api).toEqual({ http: { webui: {}, auth: {} } })
    await click(wrapper, 'YAML')
    expect(load((wrapper.get('[aria-label="OxiDNS 完整配置 YAML"]').element as HTMLTextAreaElement).value)).toEqual(model(wrapper))
    await click(wrapper, '完整参数')
    expect(wrapper.getComponent(OxidnsSchemaValueEditor).props('label')).toBe('全局参数.api.http.auth')
  })

  it.each(['api: null', 'api: {http: "${HTTP}"}', 'api: {http: []}'])('does not replace existing native alternatives while exposing missing features: %s', async api => {
    const original = `${api}\nplugins: []`
    const wrapper = render(original)
    expect(navigationItems(wrapper).some(item => item.code === 'api.http.webui')).toBe(false)
    await navigate(wrapper, api === 'api: null' ? 'api' : 'api.http')
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
    await navigate(wrapper, 'add:plugin:forward')
    expect(model(wrapper).api).toEqual((load(original) as any).api)
    expect(model(wrapper).plugins).toEqual([{ tag: 'forward', type: 'forward', args: {} }])
  })

  it('adds and re-adds a plugin from its category while preserving same-type instances and YAML order', async () => {
    const wrapper = render('plugins: [{tag: first, type: forward, args: {concurrent: 0}}, {tag: sequence, type: future_plugin, args: false}, {tag: second, type: forward, args: {}}]')
    const groups = wrapper.getComponent(OxidnsConfigNavigation).props('items')
    expect(groups.find((item: NavigationItem) => item.id === 'category:upstreams').children.filter((item: NavigationItem) => !item.available).map((item: NavigationItem) => item.label)).toEqual(['first', 'second'])
    expect(groups.find((item: NavigationItem) => item.id === 'category:advanced').children.some((item: NavigationItem) => item.label === 'sequence')).toBe(true)
    expect(navigationItems(wrapper).some(item => item.id === 'add:plugin:forward')).toBe(false)
    await navigate(wrapper, 'add:plugin:sequence')
    expect(model(wrapper).plugins.map((plugin: any) => plugin.tag)).toEqual(['first', 'sequence', 'second', 'sequence_2'])
    expect(model(wrapper).plugins[3].args).toEqual([])
    expect(model(wrapper).plugins[1].args).toBe(false)
    await wrapper.get('[aria-label="删除原生插件 4"]').trigger('click')
    expect(navigationItems(wrapper).find(item => item.id === 'add:plugin:sequence')?.available).toBe(true)
    await navigate(wrapper, 'add:plugin:sequence')
    expect(model(wrapper).plugins[3]).toEqual({ tag: 'sequence_2', type: 'sequence', args: [] })
    await navigate(wrapper, 'category:upstreams')
    await pick(wrapper, 'DNS 上游', 'forward')
    expect(model(wrapper).plugins[4]).toEqual({ tag: 'forward', type: 'forward', args: {} })
  })

  it('retains unfinished input and feature additions together until the value is repaired', async () => {
    const wrapper = render('runtime: {worker_threads: 2}\nplugins: []')
    await navigate(wrapper, 'runtime')
    await wrapper.get('[aria-label="全局参数.runtime.worker_threads 值"]').setValue('2e')
    await navigate(wrapper, 'add:plugin:sequence')
    await navigate(wrapper, 'api.http.webui')
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
    expect(error(wrapper)).toContain('数字')
    await click(wrapper, '返回未完成输入')
    expect((wrapper.get('[aria-label="全局参数.runtime.worker_threads 值"]').element as HTMLInputElement).value).toBe('2e')
    await wrapper.get('[aria-label="全局参数.runtime.worker_threads 值"]').setValue('3')
    expect(error(wrapper)).toBe('')
    expect(model(wrapper)).toEqual({ runtime: { worker_threads: 3 }, api: { http: { webui: {} } }, plugins: [{ tag: 'sequence', type: 'sequence', args: [] }] })
  })

  it('locates server-reported paths while keeping incomplete plugins editable in YAML', async () => {
    const wrapper = render('plugins: [{tag: dns, type: forward, args: {}}]')
    expect(error(wrapper)).toBe('')
    await navigate(wrapper, 'category:runtime')
    ;(wrapper.vm as any).locateIssue(['plugins', 0, 'args', 'upstreams'])
    await flushPromises()
    expect(wrapper.getComponent(OxidnsSchemaValueEditor).props('label')).toBe('插件 1.args')
    await click(wrapper, 'YAML')
    expect((wrapper.get('[aria-label="OxiDNS 完整配置 YAML"]').element as HTMLTextAreaElement).value).toContain('args: {}')
    await wrapper.get('[aria-label="OxiDNS 完整配置 YAML"]').setValue('plugins: [{tag: dns, type: forward, args: {upstreams: [{addr: dns.example}]}}]')
    expect(error(wrapper)).toBe('')
    await click(wrapper, '完整参数')
    expect(wrapper.findAll('button').some(button => button.text().trim() === '补全必填参数')).toBe(false)
  })

  it('keeps invalid plugin structures visible and disables additions until their type is repaired', async () => {
    const wrapper = render('plugins: {future: null}')
    expect(navigationItems(wrapper).find(item => item.id === 'add:plugin:forward')?.disabled).toBe(true)
    await navigate(wrapper, 'add:plugin:forward')
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
    await navigate(wrapper, 'plugins')
    await wrapper.setProps({ modelValue: 'plugins: [null, {tag: missing_type}, {type: future_plugin, args: false}]' })
    for (const path of ['plugins[1]', 'plugins[2]', 'plugins[3]']) await navigate(wrapper, path)
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
    expect(error(wrapper)).toBe('')
  })

})

describe('schema-driven nested values', () => {
  it('distinguishes parent and nested parameter destinations and writes only to the chosen group', async () => {
    const node = valueToConfigNode({ log: { level: 'info', rotation: {} } })
    const wrapper = mount(OxidnsSchemaValueEditor, { props: { node, label: '全局参数', autoExpandDepth: 3, schema: { type: 'object', help: '全局配置', properties: {
      log: { type: 'object', label: '日志', help: '日志设置', properties: {
        level: { type: 'string', label: '日志级别', help: '详细程度' },
        file: { type: 'string', label: '日志文件', help: '保存路径' },
        rotation: { type: 'object', label: '日志轮转', help: '轮转设置', properties: {
          type: { type: 'string', label: '轮转方式', help: '切分周期', default: 'daily' },
          max_files: { type: 'number', label: '保留文件数', help: '保留数量', default: 7 },
        } },
      } },
    } } } })
    wrappers.push(wrapper)
    const picker = (label: string) => wrapper.findAllComponents(OxidnsParameterPicker).find(component => component.props('label') === label)!
    expect(picker('全局参数.log').props('scopeTitle')).toBe('日志')
    expect(picker('全局参数.log.rotation').props('scopeTitle')).toBe('日志轮转')
    expect(picker('全局参数.log').props('fields').map((field: any) => field.key)).toEqual(['file'])
    expect(picker('全局参数.log.rotation').props('fields').map((field: any) => field.key)).toEqual(['type', 'max_files'])
    await pick(wrapper, '全局参数.log.rotation', 'type')
    await pick(wrapper, '全局参数.log', 'file')
    expect(configNodeToValue(node)).toEqual({ log: { level: 'info', rotation: { type: 'daily' }, file: '' } })
    await wrapper.get('[aria-label="全局参数.log.file 值"]').setValue('state/dns.log')
    await wrapper.get('[aria-label="全局参数.log.rotation 展开参数"]').trigger('click')
    expect(wrapper.find('[aria-label="全局参数.log.rotation.type 值"]').exists()).toBe(false)
    await wrapper.get('[aria-label="全局参数.log.rotation 展开参数"]').trigger('click')
    expect((wrapper.get('[aria-label="全局参数.log.file 值"]').element as HTMLTextAreaElement).value).toBe('state/dns.log')
    expect(configNodeToValue(node).log.rotation).toEqual({ type: 'daily' })
  })

  it('adds nested array/map entries using schema, preserves false, and lets env references use text', async () => {
    const node = valueToConfigNode({ profiles: {} })
    const wrapper = mount(OxidnsSchemaValueEditor, { props: { node, label: '出口', schema: { type: 'object', help: '出口', properties: {
      profiles: { type: 'object', help: '配置', additionalProperties: { type: 'object', help: '配置项', properties: { timeout: { type: 'number', help: '超时' }, enabled: { type: 'boolean', help: '启用', default: false } } } },
    } } } })
    wrappers.push(wrapper)
    await wrapper.get('[aria-label="出口.profiles 展开参数"]').trigger('click')
    await wrapper.get('[aria-label="出口.profiles 添加自定义字段"]').trigger('click')
    await wrapper.get('[aria-label="出口.profiles 字段名 1"]').setValue('proxy')
    await pick(wrapper, '出口.profiles.proxy', 'enabled')
    expect(configNodeToValue(node)).toEqual({ profiles: { proxy: { enabled: false } } })
    await pick(wrapper, '出口.profiles.proxy', 'timeout')
    await wrapper.get('[aria-label="出口.profiles.proxy.timeout 更改值类型"]').trigger('click')
    await select(wrapper, '出口.profiles.proxy.timeout 类型', 'string')
    await wrapper.get('[aria-label="出口.profiles.proxy.timeout 值"]').setValue('${TIMEOUT:-5}')
    expect(configNodeToValue(node)).toEqual({ profiles: { proxy: { enabled: false, timeout: '${TIMEOUT:-5}' } } })
  })
})
