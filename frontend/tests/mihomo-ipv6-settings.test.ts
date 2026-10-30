import { afterEach, describe, expect, it } from 'vitest'
import { shallowMount, type VueWrapper } from '@vue/test-utils'
import { parse } from 'yaml'
import MihomoIPv6Settings from '@/components/generate/MihomoIPv6Settings.vue'
import MihomoIPv6Readiness from '@/components/generate/MihomoIPv6Readiness.vue'
import { Switch } from '@/components/ui/switch'

const wrappers: VueWrapper[] = []
const mount = (modelValue = 'ipv6: true\ndns:\n  ipv6: false\n', extra = {}) => {
  const wrapper = shallowMount(MihomoIPv6Settings, {
    props: { open: true, modelValue, profileId: 'bound', preferredAgentId: 'entry-device', ...extra },
    global: { stubs: { Button: false, Primitive: false } },
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
    await wrapper.get('button').trigger('click')
    expect(wrapper.getComponent(MihomoIPv6Readiness).props()).toMatchObject({ open: true, profileId: 'bound', preferredAgentId: 'entry-device', dirty: true })
    await wrapper.setProps({ profileId: 'other-profile' })
    expect(wrapper.findComponent(MihomoIPv6Readiness).exists()).toBe(false)
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
