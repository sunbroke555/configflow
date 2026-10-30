import { afterEach, describe, expect, it } from 'vitest'
import { shallowMount, type VueWrapper } from '@vue/test-utils'
import { parse } from 'yaml'
import MihomoIPv6Settings from '@/components/generate/MihomoIPv6Settings.vue'
import MihomoIPv6Readiness from '@/components/generate/MihomoIPv6Readiness.vue'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { Dialog, DialogContent } from '@/components/ui/dialog'

const wrappers: VueWrapper[] = []
const mount = (modelValue = 'ipv6: true\ndns:\n  ipv6: false\n', extra = {}) => {
  const wrapper = shallowMount(MihomoIPv6Settings, {
    props: { open: true, modelValue, profileId: 'bound', preferredAgentId: 'entry-device', ...extra },
    global: { renderStubDefaultSlot: true, stubs: { Button: false, Primitive: false } },
  })
  wrappers.push(wrapper)
  return wrapper
}
afterEach(() => wrappers.splice(0).forEach(wrapper => wrapper.unmount()))

describe('Mihomo IPv6 settings controls', () => {
  it('reflects handwritten YAML and keeps the two switches independent', async () => {
    const wrapper = mount()
    const controls = wrapper.findAllComponents(Switch)
    expect(controls.map(control => control.props('modelValue'))).toEqual([true, false])
    controls[1].vm.$emit('update:modelValue', true)
    const changed = wrapper.emitted('update:modelValue')?.[0][0] as string
    expect(parse(changed)).toEqual({ ipv6: true, dns: { ipv6: true } })
    await wrapper.setProps({ modelValue: 'ipv6: false\ndns:\n  ipv6: true\n' })
    expect(controls.map(control => control.props('modelValue'))).toEqual([false, true])
  })

  it('fills a missing IPv6 Fake-IP range in the draft and directs route publication to the upstream report', async () => {
    const wrapper = mount('ipv6: true\ndns:\n  enable: true\n  ipv6: false\n  enhanced-mode: fake-ip\ntun:\n  enable: true\n')
    wrapper.findAllComponents(Switch)[1].vm.$emit('update:modelValue', true)
    await wrapper.vm.$nextTick()
    const changed = wrapper.emitted('update:modelValue')?.[0][0] as string
    expect(parse(changed).dns).toMatchObject({ ipv6: true, 'fake-ip-range6': 'fc00::1/64' })
    expect(wrapper.get('[role="status"]').text()).toContain('上级路由')
    expect(wrapper.get('[role="status"]').text()).toContain('查看差异并发布路由')
    expect(wrapper.findComponent(MihomoIPv6Readiness).exists()).toBe(false)
  })

  it('shows the custom range only for enabled IPv6 Fake-IP and reflects handwritten YAML', async () => {
    const source = 'ipv6: true\ndns:\n  enable: true\n  ipv6: true\n  enhanced-mode: fake-ip\n  fake-ip-range6: fd12:3456::1/64 # custom\n'
    const wrapper = mount(source)
    expect(wrapper.getComponent(Input).props('modelValue')).toBe('fd12:3456::1/64')
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
    await wrapper.setProps({ modelValue: source.replace('fd12:3456', 'fd56:789a') })
    expect(wrapper.getComponent(Input).props('modelValue')).toBe('fd56:789a::1/64')
    await wrapper.setProps({ modelValue: source.replace('  ipv6: true', '  ipv6: false') })
    expect(wrapper.findComponent(Input).exists()).toBe(false)
    await wrapper.setProps({ modelValue: source.replace('fake-ip\n', 'redir-host\n') })
    expect(wrapper.findComponent(Input).exists()).toBe(false)
  })

  it('retains invalid input without writing YAML and clears validation when corrected', async () => {
    const source = 'ipv6: true\ndns:\n  enable: true\n  ipv6: true\n  enhanced-mode: fake-ip\n  fake-ip-range6: fc00::1/64 # keep\n'
    const wrapper = mount(source)
    const input = wrapper.getComponent(Input)
    input.vm.$emit('update:modelValue', 'fe80::1/64')
    await wrapper.vm.$nextTick()
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
    expect(input.props('modelValue')).toBe('fe80::1/64')
    expect(wrapper.get('[role="alert"]').text()).toContain('内网网段')
    expect(wrapper.emitted('validation')?.at(-1)).toEqual([{ invalid: true, dirty: true }])
    input.vm.$emit('update:modelValue', 'fd12:3456::1/64')
    await wrapper.vm.$nextTick()
    const changed = wrapper.emitted('update:modelValue')?.at(-1)?.[0] as string
    expect(changed).toContain('fake-ip-range6: fd12:3456::1/64 # keep')
    expect(wrapper.emitted('validation')?.at(-1)).toEqual([{ invalid: false, dirty: false }])
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    await wrapper.setProps({ modelValue: changed })
    input.vm.$emit('update:modelValue', '')
    await wrapper.vm.$nextTick()
    await wrapper.setProps({ open: false })
    expect(wrapper.emitted('validation')?.at(-1)).toEqual([{ invalid: false, dirty: false }])
  })

  it('locks a shared range and explains how to edit its source', () => {
    const source = 'range: &range fd12::1/64\nipv6: true\ndns:\n  enable: true\n  ipv6: true\n  enhanced-mode: fake-ip\n  fake-ip-range6: *range\n'
    const wrapper = mount(source)
    expect(wrapper.getComponent(Input).attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('下方 YAML')
    wrapper.getComponent(Input).vm.$emit('update:modelValue', 'fd56::1/64')
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
  })

  it('does not write merely because a missing field has a default', () => {
    const wrapper = mount('mixed-port: 7890\n')
    expect(wrapper.findAllComponents(Switch).map(control => control.props('modelValue'))).toEqual([true, false])
    expect(wrapper.text()).toContain('未填写，沿用 Mihomo 默认开启')
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
  })

  it('keeps malformed values visible and blocks accidental replacement', () => {
    const wrapper = mount('ipv6: "true"\ndns: false\n')
    expect(wrapper.findAllComponents(Switch).every(control => control.props('disabled'))).toBe(true)
    expect(wrapper.text()).toContain('不要加引号')
    expect(wrapper.text()).toContain('dns 必须是 YAML 对象')
  })

  it('passes the fixed device and scope to the on-demand network workflow', async () => {
    const wrapper = mount(undefined, { dirty: true })
    expect(wrapper.findComponent(MihomoIPv6Readiness).exists()).toBe(false)
    wrapper.getComponent(Dialog).vm.$emit('update:open', true)
    await wrapper.vm.$nextTick()
    expect(wrapper.getComponent(MihomoIPv6Readiness).props()).toMatchObject({ open: true, profileId: 'bound', preferredAgentId: 'entry-device', dirty: true })
    await wrapper.setProps({ profileId: 'other-profile' })
    expect(wrapper.findComponent(MihomoIPv6Readiness).exists()).toBe(false)
  })

  it('keeps the check window open while busy and returns without changing the YAML', async () => {
    const wrapper = mount()
    const dialog = wrapper.getComponent(Dialog)
    dialog.vm.$emit('update:open', true)
    await wrapper.vm.$nextTick()
    const readiness = wrapper.getComponent(MihomoIPv6Readiness)
    readiness.vm.$emit('busy', true)
    await wrapper.vm.$nextTick()
    expect(wrapper.emitted('busy')?.at(-1)).toEqual([true])
    dialog.vm.$emit('update:open', false)
    const escape = new Event('keydown', { cancelable: true })
    const outside = new Event('pointerdown', { cancelable: true })
    const content = wrapper.getComponent(DialogContent)
    content.vm.$emit('escapeKeyDown', escape)
    content.vm.$emit('interactOutside', outside)
    await wrapper.vm.$nextTick()
    expect(escape.defaultPrevented).toBe(true)
    expect(outside.defaultPrevented).toBe(true)
    expect(dialog.props('open')).toBe(true)
    expect(content.props('showCloseButton')).toBe(false)
    readiness.vm.$emit('busy', false)
    await wrapper.vm.$nextTick()
    dialog.vm.$emit('update:open', false)
    await wrapper.vm.$nextTick()
    expect(wrapper.findComponent(MihomoIPv6Readiness).exists()).toBe(false)
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
    expect(wrapper.emitted('busy')?.at(-1)).toEqual([false])
  })

  it('uses the full server default only when the user edits an empty document', () => {
    const defaultConfig = 'mixed-port: 7890\ndns:\n  enable: true\n  nameserver: [223.5.5.5]\n'
    const wrapper = mount('', { defaultConfig })
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
    wrapper.findAllComponents(Switch)[1].vm.$emit('update:modelValue', true)
    const changed = wrapper.emitted('update:modelValue')?.[0][0] as string
    expect(parse(changed)).toEqual({ ...parse(defaultConfig), dns: { ...parse(defaultConfig).dns, ipv6: true } })
  })
})
