import { afterEach, describe, expect, it } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { load } from 'js-yaml'
import OxidnsMatchEditor from '@/components/generate/OxidnsMatchEditor.vue'
import { Select } from '@/components/ui/select'

const wrappers: VueWrapper[] = []
const direct = () => ({ matches: 'qname full:example.test', exec: 'goto china_dns' })
const targets = [
  { tag: 'china_dns', type: 'sequence', exec: 'goto china_dns', source: 'config.yaml' },
  { tag: 'local_dns', type: 'sequence', exec: 'goto local_dns', source: 'config.yaml' },
  { tag: 'proxy_dns_seq', type: 'sequence', exec: 'goto proxy_dns_seq', source: 'plugins/extra.yaml' },
  { tag: 'local_forward', type: 'forward', exec: '$local_forward', source: 'config.yaml' },
]
function render(modelValue: Record<string, any>[] = [], extra: Record<string, any> = {}) {
  const wrapper = mount(OxidnsMatchEditor, { props: { modelValue, targets, targetTags: targets.map(target => target.tag), ...extra } })
  wrappers.push(wrapper)
  return wrapper
}
function latest(wrapper: VueWrapper) {
  return wrapper.emitted('update:modelValue')?.at(-1)?.[0] as Record<string, any>[] | undefined
}
function valid(wrapper: VueWrapper) { return wrapper.emitted('validity')?.at(-1)?.[0] }
async function click(wrapper: VueWrapper, label: string) {
  const button = wrapper.findAll('button').find(button => button.text().trim() === label)
  expect(button, label).toBeDefined()
  await button!.trigger('click')
}
async function yaml(wrapper: VueWrapper, text: string) {
  await click(wrapper, 'YAML')
  await wrapper.get('#oxi-custom-matches').setValue(text)
}
afterEach(() => wrappers.splice(0).forEach(wrapper => wrapper.unmount()))

describe('OxiDNS custom match editor', () => {
  it('defaults to entries and keeps a newly added incomplete entry local until valid', async () => {
    const wrapper = render()
    expect(wrapper.text()).toContain('尚未添加')
    expect(valid(wrapper)).toBe(true)
    await click(wrapper, '添加匹配项')
    expect(valid(wrapper)).toBe(false)
    expect(latest(wrapper)).toBeUndefined()
    await wrapper.get('[aria-label="匹配项 1 条件 1"]').setValue('qname full:new.test')
    expect(valid(wrapper)).toBe(false)
    expect(latest(wrapper)).toBeUndefined()
    expect(wrapper.get('[aria-label="匹配项 1 目标流程"]').text()).not.toContain('china_dns')
    wrapper.findAllComponents(Select)[1].vm.$emit('update:modelValue', 'goto local_dns')
    await flushPromises()
    expect(valid(wrapper)).toBe(true)
    expect(latest(wrapper)).toEqual([{ enabled: true, matches: ['qname full:new.test'], exec: 'goto local_dns' }])
    await wrapper.get('[aria-label="删除匹配项 1"]').trigger('click')
    expect(latest(wrapper)).toEqual([])
  })

  it('retains an invalid YAML draft, reports errors and blocks entry mode without replacing valid data', async () => {
    const original = [direct()]
    const wrapper = render(original)
    await yaml(wrapper, '- matches: [unterminated')
    expect(valid(wrapper)).toBe(false)
    expect(wrapper.emitted('error')?.at(-1)?.[0]).toBeTruthy()
    expect(latest(wrapper)).toBeUndefined()
    await click(wrapper, '条目')
    expect(wrapper.find('#oxi-custom-matches').exists()).toBe(true)
    expect((wrapper.get('#oxi-custom-matches').element as HTMLTextAreaElement).value).toBe('- matches: [unterminated')
    expect(original).toEqual([direct()])
    await wrapper.get('#oxi-custom-matches').setValue('[]')
    expect(valid(wrapper)).toBe(true)
    expect(wrapper.emitted('error')?.at(-1)?.[0]).toBe('')
    expect(latest(wrapper)).toEqual([])
    await click(wrapper, '条目')
    expect(wrapper.text()).toContain('尚未添加')
  })

  it.each([
    ['mapping', 'matches: qname full:x.test\nexec: accept', 'YAML 列表'],
    ['empty document', '', 'YAML 列表'],
    ['scalar entry', '- text', '必须是对象'],
    ['matches mapping', '- matches: {domain: example.test}\n  exec: accept', '字符串或字符串列表'],
    ['non-string condition', '- matches: [qtype 1, 65]\n  exec: accept', '字符串或字符串列表'],
    ['non-string action', '- matches: qtype 1\n  exec: [accept]', 'exec 必须为字符串'],
    ['non-boolean enable', '- matches: qtype 1\n  exec: accept\n  enabled: "false"', 'enabled 必须为布尔值'],
    ['empty condition', '- matches: []\n  exec: accept', '至少一个非空'],
    ['missing action', '- matches: qtype 1', '填写执行动作'],
    ['cyclic extension', '- &entry\n  matches: qtype 1\n  exec: accept\n  extra: *entry', '循环 YAML 引用'],
  ])('rejects %s while preserving the current model', async (_, text, message) => {
    const wrapper = render([direct()])
    await yaml(wrapper, text)
    expect(valid(wrapper)).toBe(false)
    expect(wrapper.get('[role="alert"]').text()).toContain(message)
    expect(latest(wrapper)).toBeUndefined()
  })

  it('round-trips string/list semantics, absent/false enable flags, order and unknown fields', async () => {
    const original = [
      { ...direct(), extra: { date: '2026-10-16', nullable: null, list: [1, true, { key: 'value' }] } },
      { matches: ['qtype 1', 'qname full:list.test\nqname full:line.test'], exec: '$plugin', enabled: false, id: 'saved-id' },
    ]
    const wrapper = render(original)
    expect(wrapper.text()).toContain('扩展字段，已保留')
    await click(wrapper, 'YAML')
    const text = (wrapper.get('#oxi-custom-matches').element as HTMLTextAreaElement).value
    await click(wrapper, '条目')
    expect(latest(wrapper)).toEqual(original)
    expect(typeof latest(wrapper)![0].matches).toBe('string')
    expect(latest(wrapper)![0]).not.toHaveProperty('enabled')
    expect(Array.isArray(latest(wrapper)![1].matches)).toBe(true)
    expect(latest(wrapper)![0].extra.date).toBe('2026-10-16')
    await click(wrapper, 'YAML')
    expect((wrapper.get('#oxi-custom-matches').element as HTMLTextAreaElement).value).toBe(text)
  })

  it('keeps strings as strings and one-element lists as lists when editing or adding conditions', async () => {
    const wrapper = render([direct(), { matches: ['qtype 1'], exec: 'accept' }])
    await wrapper.get('[aria-label="为匹配项 1 添加条件"]').trigger('click')
    await wrapper.get('[aria-label="匹配项 1 条件 2"]').setValue('qtype 28')
    expect(latest(wrapper)![0].matches).toBe('qname full:example.test\nqtype 28')
    await wrapper.get('[aria-label="匹配项 2 条件 1"]').setValue('qtype 65')
    expect(latest(wrapper)![1].matches).toEqual(['qtype 65'])
    await wrapper.get('[aria-label="删除匹配项 1 条件 1"]').trigger('click')
    expect(latest(wrapper)![0].matches).toBe('qtype 28')
  })

  it('preserves order, enabled flags and extensions through reordering and toggling', async () => {
    const first = { ...direct(), note: { keep: true } }
    const second = { matches: ['qtype 1'], exec: 'accept', enabled: false, future: 123 }
    const original = [first, second]
    const wrapper = render(original)
    await wrapper.get('[aria-label="上移匹配项 2"]').trigger('click')
    expect(latest(wrapper)).toEqual([second, first])
    await wrapper.get('[aria-label="启用匹配项 1"]').trigger('click')
    expect(latest(wrapper)).toEqual([{ ...second, enabled: true }, first])
    expect(original).toEqual([first, second])
    expect(second.enabled).toBe(false)
    await click(wrapper, 'YAML')
    expect(load((wrapper.get('#oxi-custom-matches').element as HTMLTextAreaElement).value)).toEqual(latest(wrapper))
  })

  it('supports preset and custom actions while preserving unedited fields', async () => {
    const wrapper = render([{ ...direct(), exec: '$existing', extension: { keep: 'yes' } }])
    expect((wrapper.get('[aria-label="匹配项 1 自定义动作"]').element as HTMLInputElement).value).toBe('$existing')
    wrapper.findComponent(Select).vm.$emit('update:modelValue', 'sequence')
    await flushPromises()
    expect(valid(wrapper)).toBe(false)
    expect(latest(wrapper)).toBeUndefined()
    wrapper.findAllComponents(Select)[1].vm.$emit('update:modelValue', 'goto proxy_dns_seq')
    await flushPromises()
    expect(latest(wrapper)![0].exec).toBe('goto proxy_dns_seq')
    wrapper.findComponent(Select).vm.$emit('update:modelValue', 'custom')
    await flushPromises()
    await wrapper.get('[aria-label="匹配项 1 自定义动作"]').setValue('goto my_sequence')
    expect(latest(wrapper)![0]).toEqual({ ...direct(), exec: 'goto my_sequence', extension: { keep: 'yes' } })
    await wrapper.get('[aria-label="匹配项 1 自定义动作"]').setValue('')
    expect(valid(wrapper)).toBe(false)
    expect(latest(wrapper)![0].exec).toBe('goto my_sequence')
  })


  it('switches between explicit workflow and plugin choices without choosing a default', async () => {
    const wrapper = render([{ ...direct(), extra: { retained: true } }])
    wrapper.findComponent(Select).vm.$emit('update:modelValue', 'plugin')
    await flushPromises()
    expect(valid(wrapper)).toBe(false)
    expect(latest(wrapper)).toBeUndefined()
    expect(wrapper.get('[aria-label="匹配项 1 目标插件"]').text()).not.toContain('local_forward')
    // An event for a workflow is not a valid selection in the plugin picker.
    wrapper.findAllComponents(Select)[1].vm.$emit('update:modelValue', 'goto local_dns')
    await flushPromises()
    expect(latest(wrapper)).toBeUndefined()
    wrapper.findAllComponents(Select)[1].vm.$emit('update:modelValue', '$local_forward')
    await flushPromises()
    expect(latest(wrapper)![0]).toEqual({ ...direct(), exec: '$local_forward', extra: { retained: true } })
    expect(valid(wrapper)).toBe(true)
  })

  it('keeps a removed target visible and never changes an existing action on catalog refresh', async () => {
    const original = [{ ...direct(), exec: 'goto local_dns', future: [false, null] }]
    const wrapper = render(original)
    expect(latest(wrapper)).toBeUndefined()
    await wrapper.setProps({ targets: targets.filter(target => target.tag !== 'local_dns'), targetTags: ['china_dns', 'proxy_dns_seq', 'local_forward'] })
    expect(wrapper.get('[aria-label="匹配项 1 目标流程"]').text()).toContain('local_dns')
    expect(wrapper.get('[role="status"]').text()).toContain('目标「local_dns」不在当前配置中')
    expect(latest(wrapper)).toBeUndefined()
    expect(valid(wrapper)).toBe(true)
    await wrapper.get('[aria-label="匹配项 1 条件 1"]').setValue('qtype 28')
    expect(latest(wrapper)![0]).toEqual({ ...original[0], matches: 'qtype 28' })
    await click(wrapper, 'YAML')
    expect(wrapper.get('[role="status"]').text()).toContain('local_dns')
    expect(load((wrapper.get('#oxi-custom-matches').element as HTMLTextAreaElement).value)).toEqual(latest(wrapper))
    expect(original[0].exec).toBe('goto local_dns')
  })

  it.each(['$china_dns', 'jump local_dns', 'goto   local_dns', 'forward 192.0.2.53', '$future_plugin'])('preserves the call semantics and spelling of %s', async exec => {
    const original = [{ ...direct(), exec, future: { keep: true } }]
    const wrapper = render(original, { targetTags: [...targets.map(target => target.tag), 'future_plugin'] })
    expect((wrapper.get('[aria-label="匹配项 1 自定义动作"]').element as HTMLInputElement).value).toBe(exec)
    expect(latest(wrapper)).toBeUndefined()
    await wrapper.get('[aria-label="匹配项 1 条件 1"]').setValue('qtype 28')
    expect(latest(wrapper)![0]).toEqual({ ...original[0], matches: 'qtype 28' })
    await click(wrapper, 'YAML')
    await click(wrapper, '条目')
    expect(latest(wrapper)![0].exec).toBe(exec)
    expect(original[0].exec).toBe(exec)
  })

  it('distinguishes an existing unavailable target from a deleted target', async () => {
    const wrapper = render([{ ...direct(), exec: 'goto local_forward' }])
    expect(wrapper.get('[role="status"]').text()).toContain('不在当前可选目标中')
    expect(wrapper.get('[role="status"]').text()).not.toContain('不在当前配置中')
    expect(latest(wrapper)).toBeUndefined()
    await wrapper.setProps({ modelValue: [{ ...direct(), exec: '$missing' }] })
    expect(wrapper.get('[role="status"]').text()).toContain('目标「missing」不在当前配置中')
    expect((wrapper.get('[aria-label="匹配项 1 自定义动作"]').element as HTMLInputElement).value).toBe('$missing')
    expect(latest(wrapper)).toBeUndefined()
  })

  it('offers no fabricated target for an empty configuration and still supports direct responses', async () => {
    const wrapper = render([], { targets: [], targetTags: [] })
    await click(wrapper, '添加匹配项')
    await wrapper.get('[aria-label="匹配项 1 条件 1"]').setValue('qtype 65')
    expect(wrapper.text()).toContain('当前没有可选流程')
    expect(wrapper.text()).not.toContain('china_dns')
    expect(latest(wrapper)).toBeUndefined()
    wrapper.findComponent(Select).vm.$emit('update:modelValue', 'reject 0')
    await flushPromises()
    expect(latest(wrapper)![0].exec).toBe('reject 0')
    expect(valid(wrapper)).toBe(true)
  })

  it('does not overwrite an invalid YAML draft when the target catalog changes', async () => {
    const wrapper = render([direct()])
    await yaml(wrapper, '- matches: [unfinished')
    await wrapper.setProps({ targets: [], targetTags: [] })
    expect((wrapper.get('#oxi-custom-matches').element as HTMLTextAreaElement).value).toBe('- matches: [unfinished')
    expect(valid(wrapper)).toBe(false)
    expect(latest(wrapper)).toBeUndefined()
  })

  it('names the three reject responses after the DNS handling labels used by policy rules', async () => {
    const wrapper = mount(OxidnsMatchEditor, { props: { modelValue: ['reject', 'reject 3', 'reject 0'].map(exec => ({ ...direct(), exec })) }, attachTo: document.body })
    wrappers.push(wrapper)
    expect(wrapper.find('[aria-label$="自定义动作"]').exists()).toBe(false)
    const trigger = wrapper.get('[aria-label="匹配项 1 执行动作"]')
    await trigger.trigger('keydown', { key: 'ArrowDown' })
    await flushPromises()
    const labels = Array.from(document.querySelectorAll('[role="option"]')).map(option => option.textContent?.trim())
    expect(labels).toEqual(expect.arrayContaining(['拒绝（REFUSED）', '不存在（NXDOMAIN）', '空应答（NOERROR）']))
    expect(labels.indexOf('拒绝（REFUSED）')).toBeLessThan(labels.indexOf('不存在（NXDOMAIN）'))
    // Recognised presets, not custom actions: each trigger shows its preset label.
    expect(['1', '2', '3'].map(index => wrapper.get(`[aria-label="匹配项 ${index} 执行动作"]`).text())).toEqual(['拒绝（REFUSED）', '不存在（NXDOMAIN）', '空应答（NOERROR）'])
  })

  it('allows a paused incomplete entry and validates it when enabled', async () => {
    const wrapper = render([{ enabled: false, extension: 'retained' }])
    expect(valid(wrapper)).toBe(true)
    await wrapper.get('[aria-label="启用匹配项 1"]').trigger('click')
    expect(valid(wrapper)).toBe(false)
    expect(latest(wrapper)).toBeUndefined()
    await wrapper.get('[aria-label="启用匹配项 1"]').trigger('click')
    expect(latest(wrapper)).toEqual([{ enabled: false, extension: 'retained' }])
  })

  it('keeps YAML drafts through model echoes and accepts new external values', async () => {
    const wrapper = render([direct()])
    await wrapper.setProps({ 'onUpdate:modelValue': (value: Record<string, any>[]) => wrapper.setProps({ modelValue: value }) })
    const text = '# keep my draft comment\n- matches: qtype 1\n  exec: accept\n  extra: 2026-10-16\n'
    await yaml(wrapper, text)
    await flushPromises()
    expect((wrapper.get('#oxi-custom-matches').element as HTMLTextAreaElement).value).toBe(text)
    await wrapper.get('#oxi-custom-matches').setValue('- matches: [')
    await flushPromises()
    expect(valid(wrapper)).toBe(false)
    expect(latest(wrapper)![0].extra).toBe('2026-10-16')
    await wrapper.setProps({ modelValue: [{ matches: ['qtype 28'], exec: 'accept' }] })
    expect(valid(wrapper)).toBe(true)
    expect((wrapper.get('#oxi-custom-matches').element as HTMLTextAreaElement).value).toContain('qtype 28')
  })

  it('disables editing controls while keeping help available when the parent form is busy', async () => {
    const wrapper = render([direct()])
    await wrapper.setProps({ disabled: true })
    const editingButtons = wrapper.findAll('button').filter(button => !button.attributes('aria-label')?.endsWith('说明'))
    expect(editingButtons.every(button => button.attributes('disabled') !== undefined)).toBe(true)
    expect(wrapper.get('[aria-label="自定义匹配说明"]').attributes('disabled')).toBeUndefined()
    expect(wrapper.get('textarea').attributes('disabled')).toBeDefined()
  })
})
