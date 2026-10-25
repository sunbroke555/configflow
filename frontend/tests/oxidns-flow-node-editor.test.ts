import { afterEach, describe, expect, it } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { load, JSON_SCHEMA } from 'js-yaml'
import OxidnsFlowNodeEditor from '@/components/generate/OxidnsFlowNodeEditor.vue'
import OxidnsParameterPicker from '@/components/generate/OxidnsParameterPicker.vue'
import OxidnsSchemaValueEditor from '@/components/generate/OxidnsSchemaValueEditor.vue'
import { oxidnsPluginCatalog, type OxidnsSchema } from '@/lib/oxidnsSchema'

const wrappers: VueWrapper[] = []
afterEach(() => wrappers.splice(0).forEach(wrapper => wrapper.unmount()))
const numeric: OxidnsSchema = { type: 'object', help: '参数', properties: {
  count: { type: 'number', label: '数量', help: '当前数量', default: 8 },
} }
function render(modelValue: unknown, schema?: OxidnsSchema) {
  const wrapper = mount(OxidnsFlowNodeEditor, { props: { modelValue, schema, label: '节点参数' } })
  wrappers.push(wrapper)
  return wrapper
}
function latest(wrapper: VueWrapper) { return wrapper.emitted('update:modelValue')?.at(-1)?.[0] as any }
function error(wrapper: VueWrapper) { return wrapper.emitted('error')?.at(-1)?.[0] }
function dirty(wrapper: VueWrapper) { return wrapper.emitted('dirty')?.at(-1)?.[0] }
async function mode(wrapper: VueWrapper, name: string) {
  const button = wrapper.findAll('button').find(button => button.text().trim() === name)
  expect(button, name).toBeDefined()
  await button!.trigger('click')
  await flushPromises()
}
function yaml(wrapper: VueWrapper) { return wrapper.get('[aria-label="节点参数 YAML"]') }

async function echoModel(wrapper: VueWrapper) {
  await wrapper.setProps({ 'onUpdate:modelValue': (value: unknown) => wrapper.setProps({ modelValue: value }) })
}

describe('OxiDNS flow node parameter editor', () => {
  it('edits a known plugin with its schema without mutating the source or dropping unrelated fields', async () => {
    const original = {
      tag: 'office_forward', type: 'forward',
      args: { concurrent: 2, upstreams: [{ addr: '192.0.2.53', enable_pipeline: false }], future: { off: false, zero: 0, empty: '', nothing: null } },
      extension: { untouched: [0, false, null] },
    }
    const schema: OxidnsSchema = { type: 'object', help: '插件', properties: {
      tag: { type: 'string', label: '唯一名称', help: '插件名称' },
      type: { type: 'string', label: '插件类型', help: '执行器类型' },
      args: oxidnsPluginCatalog.find(plugin => plugin.type === 'forward')!.args,
    } }
    const wrapper = render(original, schema)
    expect(wrapper.text()).toContain('唯一名称')
    expect(latest(wrapper)).toBeUndefined()
    expect(error(wrapper)).toBe('')
    expect(dirty(wrapper)).toBe(false)
    await wrapper.get('[aria-label="节点参数.args.concurrent 值"]').setValue('3')
    expect(latest(wrapper)).toEqual({ ...original, args: { ...original.args, concurrent: 3 } })
    expect(original.args.concurrent).toBe(2)
    expect(dirty(wrapper)).toBe(true)
    await mode(wrapper, 'YAML')
    expect(load((yaml(wrapper).element as HTMLTextAreaElement).value, { schema: JSON_SCHEMA })).toEqual(latest(wrapper))
  })

  it.each([
    ['string conditions', { matches: 'qname full:office.test', exec: 'goto office_dns', future: false }],
    ['list conditions', { matches: ['qtype 1'], exec: '$office_dns', future: null }],
    ['unconditional step', { exec: 'return', extension: { keep: true } }],
  ])('retains %s and all step extensions while editing the action', async (_, original) => {
    const schema = oxidnsPluginCatalog.find(plugin => plugin.type === 'sequence')!.args.items!
    const wrapper = render(original, schema)
    await wrapper.get('[aria-label="节点参数.exec 值"]').setValue('$office_forward')
    expect(latest(wrapper)).toEqual({ ...original, exec: '$office_forward' })
    await mode(wrapper, 'YAML')
    await mode(wrapper, '表单')
    expect(latest(wrapper)).toEqual({ ...original, exec: '$office_forward' })
    expect(original.exec).not.toBe('$office_forward')
  })

  it('keeps unknown plugin fields and scalar kinds in the generic form and YAML', async () => {
    const original = { tag: 'future', type: 'future_plugin', args: { zero: 0, off: false, empty: '', nil: null, list: [0, false, '', null], date: '2026-10-25' } }
    const wrapper = render(original)
    expect(wrapper.text()).toContain('通用参数表单')
    await wrapper.get('[aria-label="节点参数.tag 值"]').setValue('future_renamed')
    await mode(wrapper, 'YAML')
    const serialized = (yaml(wrapper).element as HTMLTextAreaElement).value
    expect(load(serialized, { schema: JSON_SCHEMA })).toEqual({ ...original, tag: 'future_renamed' })
    await mode(wrapper, '表单')
    expect(latest(wrapper).args).toEqual(original.args)
  })

  it('does not insert suggested defaults or create omitted args until explicitly added', async () => {
    const original = { tag: 'cache', type: 'cache' }
    const schema: OxidnsSchema = { type: 'object', help: '插件', properties: {
      args: { type: 'object', label: '缓存参数', help: '缓存', default: { size: 1024 } },
    } }
    const wrapper = render(original, schema)
    expect(latest(wrapper)).toBeUndefined()
    await mode(wrapper, 'YAML')
    expect(load((yaml(wrapper).element as HTMLTextAreaElement).value)).toEqual(original)
    await mode(wrapper, '表单')
    const picker = wrapper.findAllComponents(OxidnsParameterPicker).find(picker => picker.props('label') === '节点参数')!
    picker.vm.$emit('select', 'args')
    await flushPromises()
    expect(latest(wrapper)).toEqual({ ...original, args: { size: 1024 } })
    expect(original).not.toHaveProperty('args')
  })

  it('preserves incomplete numeric input and reports it without emitting the previous valid value', async () => {
    const original = { count: 2, extra: false }
    const wrapper = render(original, numeric)
    await wrapper.get('[aria-label="节点参数.count 值"]').setValue('2e')
    expect(error(wrapper)).toContain('有效数字')
    expect(dirty(wrapper)).toBe(true)
    expect(latest(wrapper)).toBeUndefined()
    await mode(wrapper, 'YAML')
    expect(wrapper.find('[aria-label="节点参数 YAML"]').exists()).toBe(false)
    expect(wrapper.get('[aria-label="节点参数.count 值"]').element).toHaveProperty('value', '2e')
    await wrapper.get('[aria-label="节点参数.count 值"]').setValue('2')
    expect(error(wrapper)).toBe('')
    expect(dirty(wrapper)).toBe(false)
    expect(latest(wrapper)).toEqual(original)
  })

  it.each([
    ['invalid syntax', 'args: [unfinished'],
    ['duplicate field', 'count: 1\ncount: 2'],
    ['cyclic alias', '&node\nself: *node'],
    ['nonfinite number', 'count: .inf'],
    ['unsafe integer', 'count: 9007199254740992'],
    ['multiple documents', 'count: 1\n---\ncount: 2'],
    ['empty document', '# no value yet\n'],
  ])('retains %s YAML as an invalid draft without publishing it or falling back to old values', async (_, text) => {
    const wrapper = render({ count: 2 }, numeric)
    await mode(wrapper, 'YAML')
    await yaml(wrapper).setValue(text)
    expect(error(wrapper)).toBeTruthy()
    expect(dirty(wrapper)).toBe(true)
    expect(latest(wrapper)).toBeUndefined()
    expect(yaml(wrapper).element).toHaveProperty('value', text)
    await mode(wrapper, '表单')
    expect(wrapper.findComponent(OxidnsSchemaValueEditor).exists()).toBe(false)
    expect(yaml(wrapper).element).toHaveProperty('value', text)
    await yaml(wrapper).setValue('count: 4\n')
    expect(error(wrapper)).toBe('')
    expect(latest(wrapper)).toEqual({ count: 4 })
    await mode(wrapper, '表单')
    expect(wrapper.get('[aria-label="节点参数.count 值"]').element).toHaveProperty('value', '4')
  })

  it('blocks duplicate field names created in the form while preserving both draft values', async () => {
    const wrapper = render({ first: 1, second: 2 })
    await wrapper.get('[aria-label="节点参数 字段名 2"]').setValue('first')
    expect(error(wrapper)).toContain('重复')
    expect(latest(wrapper)).toBeUndefined()
    expect(dirty(wrapper)).toBe(true)
    expect(wrapper.findAll('[aria-label="节点参数.first 值"]')).toHaveLength(2)
    await wrapper.get('[aria-label="节点参数 字段名 2"]').setValue('other')
    expect(error(wrapper)).toBe('')
    expect(latest(wrapper)).toEqual({ first: 1, other: 2 })
  })

  it('keeps YAML comments and exact numeric input through model echoes and mode switches', async () => {
    const wrapper = render({ count: 2 }, numeric)
    await echoModel(wrapper)
    await wrapper.get('[aria-label="节点参数.count 值"]').setValue('2e3')
    await flushPromises()
    expect(wrapper.get('[aria-label="节点参数.count 值"]').element).toHaveProperty('value', '2e3')
    expect(latest(wrapper)).toEqual({ count: 2000 })
    await mode(wrapper, 'YAML')
    const text = '# retain this draft comment\ncount: 3000\nfuture: 2026-10-25\n'
    await yaml(wrapper).setValue(text)
    await flushPromises()
    expect(yaml(wrapper).element).toHaveProperty('value', text)
    expect(latest(wrapper)).toEqual({ count: 3000, future: '2026-10-25' })
    await mode(wrapper, '表单')
    await mode(wrapper, 'YAML')
    expect(yaml(wrapper).element).toHaveProperty('value', text)
    await yaml(wrapper).setValue('count: [')
    await wrapper.setProps({ modelValue: { count: 3000, future: '2026-10-25' } })
    expect(yaml(wrapper).element).toHaveProperty('value', 'count: [')
    expect(error(wrapper)).toBeTruthy()
  })

  it('resets an invalid draft explicitly even when the parent model value has not changed', async () => {
    const wrapper = render({ count: 2 }, numeric)
    await echoModel(wrapper)
    await wrapper.get('[aria-label="节点参数.count 值"]').setValue('3')
    await flushPromises()
    await wrapper.get('[aria-label="节点参数.count 值"]').setValue('-')
    expect(error(wrapper)).toBeTruthy()
    const count = wrapper.emitted('update:modelValue')!.length
    await wrapper.setProps({ resetKey: 1 })
    expect(wrapper.get('[aria-label="节点参数.count 值"]').element).toHaveProperty('value', '3')
    expect(error(wrapper)).toBe('')
    expect(dirty(wrapper)).toBe(false)
    expect(wrapper.emitted('update:modelValue')).toHaveLength(count)
    await mode(wrapper, 'YAML')
    await yaml(wrapper).setValue('count: [')
    ;(wrapper.vm as unknown as { reset: () => void }).reset()
    await flushPromises()
    expect(yaml(wrapper).element).toHaveProperty('value', 'count: 3\n')
    expect(error(wrapper)).toBe('')
    expect(dirty(wrapper)).toBe(false)
  })

  it('accepts external replacement values and schema updates without leaking previous drafts', async () => {
    const wrapper = render({ count: 2 }, numeric)
    await wrapper.get('[aria-label="节点参数.count 值"]').setValue('-')
    await wrapper.setProps({ modelValue: { exec: 'return', extension: false }, schema: oxidnsPluginCatalog.find(plugin => plugin.type === 'sequence')!.args.items })
    expect(wrapper.get('[aria-label="节点参数.exec 值"]').element).toHaveProperty('value', 'return')
    expect(wrapper.find('[aria-label="节点参数.count 值"]').exists()).toBe(false)
    expect(error(wrapper)).toBe('')
    expect(dirty(wrapper)).toBe(false)
    expect(latest(wrapper)).toBeUndefined()
  })

  it.each([null, [], {}, false, 0, ''].map(value => [value]))('preserves an explicit %j value instead of replacing it with schema defaults', async value => {
    const wrapper = render(value, numeric)
    await mode(wrapper, 'YAML')
    expect(load((yaml(wrapper).element as HTMLTextAreaElement).value, { schema: JSON_SCHEMA })).toEqual(value)
    expect(latest(wrapper)).toBeUndefined()
    expect(error(wrapper)).toBe('')
  })

  it('disables form and YAML editing controls while keeping parameter help available', async () => {
    const wrapper = render({ count: 2 }, numeric)
    await wrapper.setProps({ disabled: true })
    expect(wrapper.get('[aria-label="节点参数.count 值"]').attributes('disabled')).toBeDefined()
    const buttons = wrapper.findAll('button').filter(button => !button.attributes('aria-label')?.endsWith('说明'))
    expect(buttons.every(button => button.attributes('disabled') !== undefined)).toBe(true)
    expect(wrapper.get('[aria-label="节点参数.count说明"]').attributes('disabled')).toBeUndefined()
    await wrapper.setProps({ disabled: false })
    await mode(wrapper, 'YAML')
    await wrapper.setProps({ disabled: true })
    expect(yaml(wrapper).attributes('disabled')).toBeDefined()
    expect(latest(wrapper)).toBeUndefined()
  })
})
