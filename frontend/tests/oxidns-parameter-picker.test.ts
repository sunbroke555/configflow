import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import OxidnsParameterPicker from '@/components/generate/OxidnsParameterPicker.vue'
import type { OxidnsSchema } from '@/lib/oxidnsSchema'

const fields: Array<{ key: string; schema: OxidnsSchema }> = [
  { key: 'enable_pipeline', schema: { type: 'boolean', label: '连接流水线（enable_pipeline）', help: '让 TCP / DoT 单连接同时承载多个查询；需上游支持。' } },
  { key: 'bootstrap', schema: { type: 'string', label: '引导 DNS', help: '解析上游域名的 DNS 服务器，必须填写 IP 地址，避免循环解析。' } },
  { key: 'max_conns', schema: { type: 'number', label: '最大连接数', help: '限制连接池上限。' } },
]
const wrappers: ReturnType<typeof mount>[] = []
const originalScrollIntoView = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollIntoView')

beforeEach(() => {
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() })
})
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  document.body.innerHTML = ''
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  if (originalScrollIntoView) Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', originalScrollIntoView)
  else delete (HTMLElement.prototype as any).scrollIntoView
})

function render(disabled = false) {
  const wrapper = mount(OxidnsParameterPicker, {
    props: { fields, label: '转发上游', disabled },
    attachTo: document.body,
  })
  wrappers.push(wrapper)
  return wrapper
}

async function open(wrapper: ReturnType<typeof render>) {
  await wrapper.get('button').trigger('click')
  await flushPromises()
}

function options() {
  return Array.from(document.querySelectorAll<HTMLElement>('[role="option"]'))
}

async function search(value: string) {
  const input = document.querySelector<HTMLInputElement>('[aria-label="转发上游 搜索参数"]')!
  input.value = value
  input.dispatchEvent(new Event('input', { bubbles: true }))
  await flushPromises()
}

describe('OxiDNS parameter picker', () => {
  it('supports a local popover layer above a containing sheet', async () => {
    const wrapper = render()
    await wrapper.setProps({contentClass: 'z-[70]'})
    await open(wrapper)
    const popover = document.querySelector('[aria-label="转发上游 参数选择"]')!
    expect(popover.classList.contains('z-[70]')).toBe(true)
    expect(popover.classList.contains('z-50')).toBe(false)
    expect(options()).toHaveLength(3)
  })
  it('identifies the destination scope without affecting search or parameter selection', async () => {
    const wrapper = render()
    await wrapper.setProps({ scopeTitle: '日志轮转' })
    await open(wrapper)
    const popover = document.querySelector('[aria-label="转发上游 参数选择"]')!
    expect(popover.textContent).toContain('添加到日志轮转')
    expect(popover.querySelector('[title="日志轮转"]')?.textContent).toBe('日志轮转')
    expect(options()).toHaveLength(3)
    await search('连接池')
    expect(options()).toHaveLength(1)
    options()[0].click()
    await flushPromises()
    expect(wrapper.emitted('select')).toEqual([['max_conns']])
    expect(document.querySelector('[aria-label="转发上游 搜索参数"]')).toBeNull()
  })

  it('supports a plugin addition label and disables the trigger when no choices remain', async () => {
    const wrapper = render()
    await wrapper.setProps({ label: '原生插件', buttonText: '添加插件' })
    expect(wrapper.get('button').attributes('aria-label')).toBe('原生插件 添加插件')
    expect(wrapper.get('button').text()).toBe('添加插件')
    await open(wrapper)
    expect(options()).toHaveLength(3)
    await wrapper.setProps({ fields: [] })
    await flushPromises()
    expect(wrapper.get('button').attributes('disabled')).toBeDefined()
    expect(options()).toHaveLength(0)
    expect(wrapper.emitted('select')).toBeUndefined()
  })

  it('shows parameter explanations before selection and searches labels, keys and help', async () => {
    const wrapper = render()
    await open(wrapper)
    expect(options()).toHaveLength(3)
    expect(options()[0].textContent).toContain('需上游支持')
    expect(options()[0].textContent?.match(/enable_pipeline/g)).toHaveLength(1)
    expect(options()[1].querySelector('code')?.textContent).toBe('bootstrap')
    for (const [query, key] of [['流水线', 'enable_pipeline'], ['ENABLE_PIPELINE', 'enable_pipeline'], ['BOOTSTRAP', 'bootstrap'], ['循环解析', 'bootstrap']]) {
      await search(query)
      expect(options()).toHaveLength(1)
      expect(options()[0].textContent).toContain(key)
    }
    await search('不存在的参数')
    expect(options()).toHaveLength(0)
    expect(document.body.textContent).toContain('没有匹配的参数')
    expect(wrapper.emitted('select')).toBeUndefined()
  })

  it('adds the chosen parameter immediately, closes, and resets search when reopened', async () => {
    const wrapper = render()
    await open(wrapper)
    await search('连接池')
    options()[0].click()
    await flushPromises()
    expect(wrapper.emitted('select')).toEqual([['max_conns']])
    expect(document.querySelector('[aria-label="转发上游 搜索参数"]')).toBeNull()
    await open(wrapper)
    expect(options()).toHaveLength(3)
    expect(document.querySelector<HTMLInputElement>('[aria-label="转发上游 搜索参数"]')?.value).toBe('')
  })

  it('supports choosing a searched result from the keyboard', async () => {
    const wrapper = render()
    await open(wrapper)
    await search('引导')
    const input = document.querySelector<HTMLInputElement>('[aria-label="转发上游 搜索参数"]')!
    input.focus()
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))
    await flushPromises()
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    await flushPromises()
    expect(wrapper.emitted('select')).toEqual([['bootstrap']])
  })

  it('prevents choosing parameters while disabled, including after opening', async () => {
    const wrapper = render(true)
    expect(wrapper.get('button').attributes('disabled')).toBeDefined()
    await open(wrapper)
    expect(options()).toHaveLength(0)
    expect(wrapper.emitted('select')).toBeUndefined()

    await wrapper.setProps({ disabled: false })
    await open(wrapper)
    expect(options()).toHaveLength(3)
    await wrapper.setProps({ disabled: true })
    await flushPromises()
    expect(options()).toHaveLength(0)
    expect(wrapper.emitted('select')).toBeUndefined()
  })
})
