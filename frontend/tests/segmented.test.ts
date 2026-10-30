import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from 'vitest'
import { mount, type VueWrapper } from '@vue/test-utils'
import { nextTick } from 'vue'
import Segmented from '@/components/common/Segmented.vue'

const wrappers: VueWrapper[] = []
beforeEach(() => { vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} }) })
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); vi.unstubAllGlobals() })
const options = [{ value: 'a', label: 'A' }, { value: 'b', label: 'B', disabled: true }, { value: 'c', label: 'C' }]
const render = (modelValue: string) => {
  const wrapper = mount(Segmented, { props: { modelValue, options, label: '模式' }, attachTo: document.body })
  wrappers.push(wrapper)
  return wrapper
}
const thumb = (wrapper: VueWrapper) => wrapper.get('.cf-segmented-thumb').attributes('style') || ''

describe('Segmented', () => {
  it('disables a single option without disabling the others', () => {
    const wrapper = render('a')
    expect(wrapper.findAll('[data-segment]').map(item => item.attributes('disabled') !== undefined)).toEqual([false, true, false])
  })

  it('selects nothing and hides the thumb for a value outside the options', async () => {
    // jsdom has no layout; give every segment a width so the thumb is measurable.
    const width = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetWidth')!
    Object.defineProperty(HTMLElement.prototype, 'offsetWidth', { configurable: true, get: () => 40 })
    onTestFinished(() => { Object.defineProperty(HTMLElement.prototype, 'offsetWidth', width) })
    const wrapper = render('a')
    await nextTick()
    await nextTick()
    expect(thumb(wrapper)).toContain('width: 40px')
    await wrapper.setProps({ modelValue: 'custom' })
    await nextTick()
    await nextTick()
    expect(wrapper.findAll('[role="radio"]').every(item => item.attributes('aria-checked') === 'false')).toBe(true)
    expect(thumb(wrapper)).toContain('width: 0px')
  })
})
