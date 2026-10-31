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

  it('lets only the longest option wrap when asked, keeping the others on one line', () => {
    const wrapper = mount(Segmented, { props: { modelValue: 'b', label: '模式', wrap: true,
      options: [{ value: 'a', label: '关闭' }, { value: 'b', label: 'Mihomo 用 IPv6，客户端走 IPv4' }, { value: 'c', label: '客户端也走 IPv6' }] } })
    wrappers.push(wrapper)
    const segments = wrapper.findAll('[data-segment]')
    expect(segments.map(item => item.attributes('data-wrap') !== undefined)).toEqual([false, true, false])
    expect(segments[1].classes()).toEqual(expect.arrayContaining(['whitespace-normal', 'h-auto', 'min-w-0']))
    expect(segments[0].classes()).toEqual(expect.arrayContaining(['whitespace-nowrap', 'shrink-0']))
    expect(render('a').find('[data-wrap]').exists()).toBe(false)
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
    // Its 1px ring must not linger as a stray line.
    expect(thumb(wrapper)).toContain('opacity: 0')
    await wrapper.setProps({ modelValue: 'c' })
    await nextTick()
    await nextTick()
    expect(thumb(wrapper)).toContain('opacity: 1')
  })

  it('keeps the thumb in place while its panel is hidden', async () => {
    let resize!: () => void
    vi.stubGlobal('ResizeObserver', class { constructor(callback: () => void) { resize = callback } observe() {} unobserve() {} disconnect() {} })
    let width = 40
    const original = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetWidth')!
    Object.defineProperty(HTMLElement.prototype, 'offsetWidth', { configurable: true, get: () => width })
    onTestFinished(() => { Object.defineProperty(HTMLElement.prototype, 'offsetWidth', original) })
    const wrapper = render('a')
    await nextTick()
    await nextTick()
    expect(thumb(wrapper)).toContain('width: 40px')
    // 所在标签隐藏时量到 0 宽：保留原位置，切回时滑块不会从 0 宽重新滑出
    width = 0
    resize()
    await nextTick()
    expect(thumb(wrapper)).toContain('width: 40px')
    width = 48
    resize()
    await nextTick()
    expect(thumb(wrapper)).toContain('width: 48px')
  })
})
