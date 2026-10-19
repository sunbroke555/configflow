import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { defineComponent, h, ref } from 'vue'
import ProxyGroupOrderDialog from '@/components/ProxyGroupOrderDialog.vue'
import type { ProxyGroup } from '@/types'

// Keep Dialog, menu, buttons and Vue's keyed DOM updates real. Only Sortable's
// browser gesture boundary is replaced; the drag test performs its DOM mutation.
const sortable = vi.hoisted(() => ({ create: vi.fn() }))
const feedback = vi.hoisted(() => ({
  notify: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() }
}))
vi.mock('sortablejs', () => ({ default: sortable }))
vi.mock('@/lib/feedback', () => feedback)

type DragEvent = { item: HTMLElement; oldIndex: number; newIndex: number; oldDraggableIndex: number; newDraggableIndex: number; from: HTMLElement; to: HTMLElement }
type SortableOptions = { onStart?: (event: DragEvent) => void; onEnd?: (event: DragEvent) => void }
type SortableInstance = { element: HTMLElement; options: SortableOptions; destroy: ReturnType<typeof vi.fn>; option: ReturnType<typeof vi.fn> }
const wrappers: VueWrapper[] = []
const instances: SortableInstance[] = []
let scrollDescriptor: PropertyDescriptor | undefined

function group(id: string, name: string, type = 'select'): ProxyGroup {
  return { id, name, type, enabled: true, subscriptions: ['source-one'], regex: `${id} filter` }
}
const originalGroups = () => [
  group('alpha', '自建节点'),
  group('beta', '自动选择', 'url-test'),
  group('gamma', '这是一个保留完整名称的较长策略组'),
  group('delta', '备用线路', 'fallback')
]

function deferred() {
  let resolve!: () => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<void>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

beforeEach(() => {
  vi.clearAllMocks()
  instances.splice(0)
  sortable.create.mockImplementation((element: HTMLElement, options: SortableOptions) => {
    const instance = { element, options, destroy: vi.fn(), option: vi.fn() }
    instances.push(instance)
    return instance
  })
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })))
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  scrollDescriptor = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollIntoView')
  Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, writable: true, value: vi.fn() })
})

afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  document.body.innerHTML = ''
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  if (scrollDescriptor) Object.defineProperty(Element.prototype, 'scrollIntoView', scrollDescriptor)
  else Reflect.deleteProperty(Element.prototype, 'scrollIntoView')
})

async function settleUi() {
  await flushPromises()
  await new Promise(resolve => setTimeout(resolve, 25))
  await flushPromises()
}

async function render(saveOrder = vi.fn(async (_ordered: ProxyGroup[]) => {})) {
  const groups = originalGroups()
  const open = ref(false)
  const wrapper = mount(defineComponent({
    setup() {
      return () => h('div', [
        h('button', { onClick: () => { open.value = true } }, '调整顺序'),
        h(ProxyGroupOrderDialog, { open: open.value, groups, saveOrder, 'onUpdate:open': (value: boolean) => { open.value = value } })
      ])
    }
  }), { attachTo: document.body })
  wrappers.push(wrapper)
  await click('调整顺序')
  return { wrapper, component: wrapper.findComponent(ProxyGroupOrderDialog), groups, open, saveOrder }
}

function button(label: string): HTMLButtonElement {
  const result = [...document.querySelectorAll<HTMLButtonElement>('button')].find(element =>
    element.getAttribute('aria-label') === label || element.textContent?.trim() === label
  )
  if (!result) throw new Error(`Missing button: ${label}`)
  return result
}

async function click(label: string) {
  button(label).click()
  await settleUi()
}

async function key(element: Element, value: string) {
  element.dispatchEvent(new KeyboardEvent('keydown', { key: value, bubbles: true, cancelable: true }))
  await settleUi()
}

function order() {
  return [...document.querySelectorAll<HTMLElement>('[role="dialog"] [data-group-id]')].map(element => element.dataset.groupId)
}

async function menuAction(name: string, action: string) {
  const trigger = button(`更多操作 ${name}`)
  trigger.focus()
  await key(trigger, 'Enter')
  const item = [...document.querySelectorAll<HTMLElement>('[role="menuitem"]')].find(element => element.textContent?.trim() === action)
  expect(item, `Menu item ${action} must be available`).toBeTruthy()
  item!.click()
  await settleUi()
}

describe('proxy group order dialog', () => {
  it('moves with the visible up/down buttons without changing parent data, and disables save after undoing the change', async () => {
    const { groups, saveOrder } = await render()
    const snapshot = structuredClone(groups)
    expect(order()).toEqual(['alpha', 'beta', 'gamma', 'delta'])
    expect(button('上移 自建节点').disabled).toBe(true)
    expect(button('下移 备用线路').disabled).toBe(true)
    expect(button('保存顺序').disabled).toBe(true)

    await click('下移 自建节点')
    expect(order()).toEqual(['beta', 'alpha', 'gamma', 'delta'])
    expect(button('保存顺序').disabled).toBe(false)
    expect(groups).toEqual(snapshot)
    expect(saveOrder).not.toHaveBeenCalled()

    await click('上移 自建节点')
    expect(order()).toEqual(['alpha', 'beta', 'gamma', 'delta'])
    expect(button('保存顺序').disabled).toBe(true)
  })

  it('moves to either end from the menu, then saves that exact order and closes only after success', async () => {
    const request = deferred()
    const saveOrder = vi.fn((_ordered: ProxyGroup[]) => request.promise)
    const { groups, component, open } = await render(saveOrder)
    await menuAction('这是一个保留完整名称的较长策略组', '置顶')
    expect(order()).toEqual(['gamma', 'alpha', 'beta', 'delta'])
    await menuAction('自建节点', '置底')
    expect(order()).toEqual(['gamma', 'beta', 'delta', 'alpha'])
    await click('保存顺序')
    expect(saveOrder).toHaveBeenCalledExactlyOnceWith([groups[2], groups[1], groups[3], groups[0]])
    expect(open.value).toBe(true)
    expect(component.emitted('saved')).toBeUndefined()

    request.resolve()
    await settleUi()
    expect(component.emitted('saved')).toHaveLength(1)
    expect(open.value).toBe(false)
    expect(document.querySelector('[role="dialog"]')).toBeNull()
  })

  it('discards a cancelled draft and starts the next opening from the parent order', async () => {
    const { groups, saveOrder, open } = await render()
    await click('上移 自动选择')
    await click('取消')
    expect(open.value).toBe(false)
    expect(saveOrder).not.toHaveBeenCalled()
    expect(groups.map(item => item.id)).toEqual(['alpha', 'beta', 'gamma', 'delta'])

    await click('调整顺序')
    expect(order()).toEqual(['alpha', 'beta', 'gamma', 'delta'])
    expect(button('保存顺序').disabled).toBe(true)
  })

  it('retains the draft after failure, prevents changes and dismissal while saving, and permits retry', async () => {
    const request = deferred()
    const saveOrder = vi.fn(async (_ordered: ProxyGroup[]) => {}).mockImplementationOnce(() => request.promise)
    const { open, component } = await render(saveOrder)
    await click('下移 自建节点')
    await click('保存顺序')
    expect(button('取消').disabled).toBe(true)
    expect(button('关闭排序').disabled).toBe(true)
    expect(button('上移 自建节点').disabled).toBe(true)
    expect(button('下移 自建节点').disabled).toBe(true)
    expect(button('更多操作 自建节点').disabled).toBe(true)
    await click('关闭排序')
    await click('上移 自建节点')
    await key(document.activeElement!, 'Escape')
    document.querySelector('[data-slot="dialog-overlay"]')!.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true, button: 0, cancelable: true }))
    await settleUi()
    expect(open.value).toBe(true)
    expect(order()).toEqual(['beta', 'alpha', 'gamma', 'delta'])

    request.reject(new Error('暂时无法保存'))
    await settleUi()
    expect(open.value).toBe(true)
    expect(order()).toEqual(['beta', 'alpha', 'gamma', 'delta'])
    expect(component.emitted('saved')).toBeUndefined()
    expect(button('保存顺序').disabled).toBe(false)
    expect(feedback.notify.error).toHaveBeenCalled()

    await click('保存顺序')
    expect(saveOrder).toHaveBeenCalledTimes(2)
    expect(saveOrder.mock.calls[1]![0].map(item => item.id)).toEqual(['beta', 'alpha', 'gamma', 'delta'])
    expect(component.emitted('saved')).toHaveLength(1)
    expect(open.value).toBe(false)
  })

  it('moves a keyboard-grabbed row and keeps focus on the moved handle', async () => {
    await render()
    const handle = document.querySelector<HTMLButtonElement>('[aria-label^="拖动 自建节点，"]')!
    expect(handle).toBeTruthy()
    handle.focus()
    await key(handle, ' ')
    await key(handle, 'ArrowDown')
    expect(order()).toEqual(['beta', 'alpha', 'gamma', 'delta'])
    expect(document.activeElement).toBe(handle)
    await key(handle, 'ArrowDown')
    expect(order()).toEqual(['beta', 'gamma', 'alpha', 'delta'])
    expect(document.activeElement).toBe(handle)
    await key(handle, ' ')
    await key(handle, 'ArrowDown')
    expect(order()).toEqual(['beta', 'gamma', 'alpha', 'delta'])
  })

  it('reconciles Sortable DOM movement with Vue before another move and submission', async () => {
    const { saveOrder } = await render()
    const instance = instances.at(-1)!
    expect(instance).toBeTruthy()
    const row = instance.element.querySelector<HTMLElement>('[data-group-id="alpha"]')!
    const event = { item: row, oldIndex: 0, newIndex: 2, oldDraggableIndex: 0, newDraggableIndex: 2, from: instance.element, to: instance.element }
    instance.options.onStart?.(event)
    // Sortable puts Alpha after Gamma before notifying the data layer.
    const delta = instance.element.querySelector('[data-group-id="delta"]')!
    instance.element.insertBefore(row, delta)
    instance.options.onEnd?.(event)
    await settleUi()
    expect(order()).toEqual(['beta', 'gamma', 'alpha', 'delta'])

    await click('上移 备用线路')
    expect(order()).toEqual(['beta', 'gamma', 'delta', 'alpha'])
    await click('保存顺序')
    expect(saveOrder.mock.calls[0]![0].map(item => item.id)).toEqual(['beta', 'gamma', 'delta', 'alpha'])
  })
})
