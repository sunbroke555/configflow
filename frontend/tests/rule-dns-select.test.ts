import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import RuleDnsSelect from '@/components/rules/RuleDnsSelect.vue'
import type { RuleDnsUpstream } from '@/types'

const upstreams: RuleDnsUpstream[] = [
  { key: 'b2e55ef47454', source: 'local', addr: 'udp://223.5.5.5', label: '直连 · 223.5.5.5', linked: false },
  { key: '6f01aa93c2d0', source: 'remote', addr: 'udp://127.0.0.1:1053', label: '代理 · 127.0.0.1:1053', linked: true }
]
const wrappers: ReturnType<typeof mount>[] = []
const scrollDescriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollIntoView')

beforeEach(() => {
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() })
})
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  document.body.innerHTML = ''
  vi.unstubAllGlobals()
  if (scrollDescriptor) Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', scrollDescriptor)
  else delete (HTMLElement.prototype as any).scrollIntoView
})

async function render(props: Record<string, unknown> = {}) {
  const wrapper = mount(RuleDnsSelect, {
    props: { modelValue: 'follow', label: 'DNS（OxiDNS）', upstreams, policyRoute: 'proxy', defaultForward: 'forward_remote', ...props },
    attachTo: document.body
  })
  wrappers.push(wrapper)
  await wrapper.get('button').trigger('click')
  await flushPromises()
  return wrapper
}
const options = () => Array.from(document.querySelectorAll<HTMLElement>('[role="option"]'))
const option = (text: string) => options().find(item => item.querySelector('.text-sm, .font-mono')?.textContent?.includes(text))!
async function search(value: string) {
  const input = document.querySelector<HTMLInputElement>('[aria-label="DNS（OxiDNS） 搜索"]')!
  input.value = value
  input.dispatchEvent(new Event('input', { bubbles: true }))
  await flushPromises()
}

describe('rule DNS selector', () => {
  it('groups follow and default, resolving, pinned upstreams and blocking actions', async () => {
    const wrapper = await render()
    expect(wrapper.get('button').text()).toBe('跟随策略 · 代理 DNS')
    const headings = Array.from(document.querySelectorAll('[data-slot="command-group-heading"]')).map(item => item.textContent?.trim())
    expect(headings).toEqual(['跟随与默认', '解析', '指定上游', '拦截'])
    expect(option('跟随策略').textContent).toContain('按当前配置：代理 DNS')
    expect(option('默认上游').textContent).toContain('未匹配时：代理 DNS')
    expect(option('不存在').textContent).toContain('告诉客户端这个域名不存在')
    expect(options().map(item => item.querySelector('.text-sm, .font-mono')?.textContent?.trim())).toEqual([
      '跟随策略', '默认上游', '直连 DNS', '代理 DNS', '直连 · 223.5.5.5', '代理 · 127.0.0.1:1053', '拒绝', '不存在', '空应答', '黑洞'
    ])
  })

  it('selects an option and closes', async () => {
    const wrapper = await render()
    option('黑洞').click()
    await flushPromises()
    expect(wrapper.emitted('update:modelValue')).toEqual([['blackhole']])
    expect(options()).toHaveLength(0)
  })

  it('opens layered help above the menu without selecting the option', async () => {
    const wrapper = await render()
    const help = option('空应答').querySelector<HTMLButtonElement>('button[aria-label="空应答说明"]')!
    help.click()
    await flushPromises()
    const content = document.querySelector('[data-field-help-content]')!
    expect(content.textContent).toContain('【适用条件】')
    expect(content.textContent).toContain('只想关 IPv6 请用「屏蔽查询类型」')
    expect(content.classList.contains('z-[80]')).toBe(true)
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
    expect(options().length).toBeGreaterThan(0)
  })

  it('searches labels, hints and hidden details', async () => {
    await render()
    await search('NXDOMAIN')
    expect(options().map(item => item.textContent)).toEqual([expect.stringContaining('不存在'), expect.stringContaining('黑洞')])
    await search('223.5.5.5')
    expect(options()).toHaveLength(1)
    await search('不存在的处理方式')
    expect(document.body.textContent).toContain('没有匹配的 DNS 处理')
  })

  it('disables an upstream bound to DNS follow and explains why', async () => {
    const wrapper = await render()
    const linked = option('127.0.0.1:1053')
    expect(linked.hasAttribute('data-disabled')).toBe(true)
    expect(linked.textContent).toContain('已绑定 DNS 联动，请改选直连 DNS')
    linked.click()
    await flushPromises()
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
  })

  it('says when there is no upstream to pin and keeps the current pinned address that left the list', async () => {
    await render({ upstreams: [] })
    expect(document.body.textContent).toContain('没有可固定的上游')
    wrappers.splice(0).forEach(wrapper => wrapper.unmount())
    document.body.innerHTML = ''
    const wrapper = await render({ upstreams: [], modelValue: 'upstream:0123456789ab', upstreamLabel: '直连 · 10.0.0.53' })
    expect(wrapper.get('button').text()).toContain('指定上游 · 10.0.0.53（已不在上游列表）')
    expect(option('10.0.0.53（已不在上游列表）')).toBeTruthy()
    expect(document.body.textContent).not.toContain('没有可固定的上游')
  })

  it('renders the cell variant as a labelled text selector with a 44px hit area', async () => {
    const wrapper = mount(RuleDnsSelect, {
      props: { modelValue: 'blackhole', label: 'ads.test 的 DNS：黑洞，点击修改', variant: 'cell', upstreams },
      slots: { default: '<span>黑洞</span>' },
      attachTo: document.body
    })
    wrappers.push(wrapper)
    const trigger = wrapper.get('button')
    expect(trigger.attributes('aria-label')).toBe('ads.test 的 DNS：黑洞，点击修改')
    expect(trigger.classes()).toEqual(expect.arrayContaining(['rule-dns-cell-trigger', 'h-[30px]', 'text-left']))
    await wrapper.setProps({ disabled: true })
    await trigger.trigger('click')
    await flushPromises()
    expect(options()).toHaveLength(0)
  })
})
