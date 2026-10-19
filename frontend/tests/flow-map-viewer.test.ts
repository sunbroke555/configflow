import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { nextTick } from 'vue'
import FlowMapViewer from '@/components/dashboard/FlowMapViewer.vue'
import FlowMap from '@/components/dashboard/FlowMap.vue'
import { usePreferences } from '@/stores/preferences'

const { push } = vi.hoisted(() => ({ push: vi.fn() }))
vi.mock('vue-router', () => ({ useRouter: () => ({ push }) }))

// Keep the real map, Dialog, focus handling and gestures. Only browser layout,
// media queries and canvas are supplied, since jsdom does not render them.
const wrappers: VueWrapper[] = []
const observers = new Set<SyntheticResizeObserver>()
const queries = new Set<SyntheticMediaQuery>()
let mobile = true
let previewWidth = 296
let viewportWidth = 320
let viewportHeight = 480
let mapHeight = 236

function geometry(element: Element) {
  if (element.hasAttribute('data-flow-preview')) return new DOMRect(0, 0, previewWidth, 160)
  if (element.hasAttribute('data-flow-viewport')) return new DOMRect(0, 0, viewportWidth, viewportHeight)
  const id = element.getAttribute('data-fid')
  if (id) return new DOMRect(id === 'a' ? 20 : 380, 50, 200, 52)
  if (element instanceof HTMLButtonElement) return new DOMRect(0, 0, 100, 44)
  return new DOMRect(0, 0, 640, mapHeight)
}

class SyntheticResizeObserver {
  private targets = new Set<Element>()
  constructor(private callback: ResizeObserverCallback) { observers.add(this) }
  observe(target: Element) { this.targets.add(target); queueMicrotask(() => this.notify()) }
  unobserve(target: Element) { this.targets.delete(target) }
  disconnect() { this.targets.clear(); observers.delete(this) }
  notify() {
    const entries = [...this.targets].map(target => ({ target, contentRect: geometry(target) } as ResizeObserverEntry))
    if (entries.length) this.callback(entries, this as unknown as ResizeObserver)
  }
}

class SyntheticMediaQuery extends EventTarget {
  constructor(readonly media: string) { super(); queries.add(this) }
  get matches() { return this.media.includes('max-width: 639px') && mobile }
  addListener(listener: EventListener) { this.addEventListener('change', listener) }
  removeListener(listener: EventListener) { this.removeEventListener('change', listener) }
  notify() { this.dispatchEvent(Object.assign(new Event('change'), { matches: this.matches, media: this.media })) }
}

beforeEach(() => {
  mobile = true; previewWidth = 296; viewportWidth = 320; viewportHeight = 480; mapHeight = 236
  push.mockClear()
  usePreferences().prefs.value.motion = false
  vi.stubGlobal('matchMedia', (query: string) => new SyntheticMediaQuery(query))
  vi.stubGlobal('ResizeObserver', SyntheticResizeObserver)
  vi.stubGlobal('IntersectionObserver', class { observe() {} disconnect() {} })
  vi.stubGlobal('CSS', { escape: (value: string) => value })
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null)
  vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function () { return geometry(this) })
  vi.spyOn(Element.prototype, 'getClientRects').mockImplementation(function () {
    const hiddenInDetails = this.tagName !== 'SUMMARY' && this.closest('details:not([open])')
    return (hiddenInDetails ? [] : [geometry(this)]) as unknown as DOMRectList
  })
  for (const [property, dimension] of [['clientWidth', 'width'], ['clientHeight', 'height'], ['offsetWidth', 'width'], ['offsetHeight', 'height']] as const) {
    vi.spyOn(HTMLElement.prototype, property, 'get').mockImplementation(function () { return geometry(this)[dimension] })
  }
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => window.setTimeout(() => callback(performance.now()), 0))
  vi.stubGlobal('cancelAnimationFrame', (id: number) => window.clearTimeout(id))
})

afterEach(async () => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  await flushPromises()
  observers.clear(); queries.clear()
  vi.restoreAllMocks(); vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

async function settle() { await nextTick(); await flushPromises(); await nextTick() }
async function render() {
  const wrapper = mount(FlowMapViewer, {
    attachTo: document.body,
    props: {
      label: '下发拓扑', minWidth: 640, height: 200, density: 0,
      columns: [
        { key: 'source', title: '配置中心', nodes: [{ id: 'a', title: '配置中心', to: '/profiles' }] },
        { key: 'target', title: '设备', nodes: [{ id: 'b', title: '家庭网络与办公设备的完整长名称', meta: '2001:db8:1234:5678:90ab:cdef:1234:5678', status: 'offline', to: '/agents' }] }
      ],
      edges: [{ from: 'a', to: 'b', dead: true }]
    }
  })
  wrappers.push(wrapper)
  await settle()
  return wrapper
}
const button = (label: string) => document.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`)!
const viewport = () => document.querySelector<HTMLElement>('[data-flow-viewport]')!
const plane = () => document.querySelector<HTMLElement>('[data-flow-plane]')!
async function click(label: string) { button(label).click(); await settle() }
async function open() { await click('全屏查看下发拓扑') }
function pointer(target: EventTarget, type: string, id: number, x: number, y: number) {
  const event = Object.assign(new MouseEvent(type, { bubbles: true, cancelable: true, clientX: x, clientY: y, button: 0 }), { pointerId: id, pointerType: 'touch' })
  target.dispatchEvent(event)
}
function physicalClick(target: Element) { target.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, detail: 1 })) }

describe('FlowMapViewer mobile interaction', () => {
  it('returns focus to a visible summary if its details section collapsed while fullscreen', async () => {
    const wrapper = await render()
    const details = document.createElement('details')
    details.open = true
    const summary = document.createElement('summary')
    summary.textContent = '下发拓扑'
    details.append(summary, wrapper.element)
    document.body.append(details)
    await open()
    details.open = false
    await click('关闭全屏图')
    expect(document.activeElement).toBe(summary)
    details.open = true
    await open()
    expect(document.querySelectorAll('canvas')).toHaveLength(1)
    expect(document.querySelector('[role="dialog"]')).not.toBeNull()
  })

  it('opens one accessible full map, retains nodes and restores trigger focus on Escape', async () => {
    const wrapper = await render()
    expect(document.querySelectorAll('canvas')).toHaveLength(1)
    expect(wrapper.get('[inert]').findAll('[data-fid]')).toHaveLength(2)
    const trigger = button('全屏查看下发拓扑')
    trigger.focus()
    await open()
    expect(document.querySelectorAll('canvas')).toHaveLength(1)
    expect(document.querySelector('[role="dialog"]')?.textContent).toContain('下发拓扑')
    expect(plane().querySelectorAll('[data-fid]')).toHaveLength(2)
    expect(plane().querySelector('[inert]')).toBeNull()
    expect(wrapper.getComponent(FlowMap).props('readableLabels')).toBe(true)
    expect(button('恢复原始大小').textContent).toBe('100%')
    button('关闭全屏图').dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
    await settle()
    expect(document.querySelector('[role="dialog"]')).toBeNull()
    expect(document.querySelectorAll('canvas')).toHaveLength(1)
    expect(document.activeElement).toBe(trigger)
    expect(wrapper.getComponent(FlowMap).props('readableLabels')).toBe(false)
  })

  it('zooms, pans without navigating, and still follows a deliberate node tap', async () => {
    await render(); await open(); await click('恢复原始大小')
    expect(button('恢复原始大小').textContent).toBe('100%')
    const node = plane().querySelector('[data-fid="a"]')!
    const before = plane().style.transform
    pointer(node, 'pointerdown', 1, 100, 100)
    pointer(window, 'pointermove', 1, 50, 100)
    // Touch capture transfers from the pressed node to the pan viewport.
    pointer(node, 'lostpointercapture', 1, 50, 100)
    pointer(window, 'pointermove', 1, 20, 100)
    pointer(window, 'pointerup', 1, 20, 100)
    physicalClick(node)
    await settle()
    expect(plane().style.transform).not.toBe(before)
    expect(push).not.toHaveBeenCalled()
    pointer(node, 'pointerdown', 2, 100, 100)
    pointer(window, 'pointerup', 2, 100, 100)
    physicalClick(node)
    expect(push).toHaveBeenCalledWith('/profiles')
    await click('放大图形')
    expect(button('恢复原始大小').textContent).toBe('125%')
    await click('适应完整图形')
    expect(button('恢复原始大小').textContent).toBe('46%')
  })

  it('pinches around the touch midpoint and clears outside/canceled fingers', async () => {
    await render(); await open(); await click('恢复原始大小')
    const area = viewport()
    pointer(area, 'pointerdown', 1, 100, 120)
    // Ending just outside the viewport before capture must not create a ghost finger.
    pointer(window, 'pointerup', 1, 321, 120)
    pointer(area, 'pointerdown', 2, 100, 120)
    pointer(area, 'pointerdown', 3, 200, 120)
    pointer(window, 'pointermove', 3, 250, 120)
    await settle()
    expect(button('恢复原始大小').textContent).toBe('150%')
    pointer(window, 'pointercancel', 2, 100, 120)
    pointer(window, 'pointercancel', 3, 250, 120)
    const node = plane().querySelector('[data-fid="a"]')!
    pointer(node, 'pointerdown', 4, 100, 120)
    pointer(window, 'pointerup', 4, 100, 120)
    physicalClick(node)
    expect(push).toHaveBeenCalledOnce()
  })

  it('reveals a keyboard-focused node through pan without retaining native scroll offsets', async () => {
    await render(); await open()
    const area = viewport()
    const before = plane().style.transform
    area.scrollLeft = 242
    area.scrollTop = 80
    plane().querySelector<HTMLButtonElement>('[data-fid="b"]')!.focus()
    await settle()
    expect(area.scrollLeft).toBe(0)
    expect(area.scrollTop).toBe(0)
    expect(plane().style.transform).not.toBe(before)
    expect(document.activeElement?.getAttribute('data-fid')).toBe('b')
    expect(push).not.toHaveBeenCalled()
  })

  it('centers a late-measured tall map once and preserves the user pan during data updates', async () => {
    await render(); await open()
    mapHeight = 1500
    observers.forEach(observer => observer.notify())
    await settle()
    expect(plane().style.transform).toBe('translate(12px, -510px) scale(1)')
    pointer(viewport(), 'pointerdown', 1, 100, 120)
    pointer(window, 'pointermove', 1, 50, 70)
    pointer(window, 'pointerup', 1, 50, 70)
    await settle()
    const panned = plane().style.transform
    mapHeight = 1700
    observers.forEach(observer => observer.notify())
    await settle()
    expect(plane().style.transform).toBe(panned)
    await click('关闭全屏图'); await open()
    expect(plane().style.transform).toBe('translate(12px, -610px) scale(1)')
  })

  it('refits measured content and rotations, then returns to an unchanged desktop map', async () => {
    const wrapper = await render(); await open(); await click('适应完整图形')
    mapHeight = 1500
    observers.forEach(observer => observer.notify())
    await settle()
    expect(button('恢复原始大小').textContent).toBe('30%')
    viewportWidth = 600; viewportHeight = 320
    observers.forEach(observer => observer.notify())
    await settle()
    expect(button('恢复原始大小').textContent).toBe('20%')
    await click('关闭全屏图')
    expect(wrapper.get('[data-flow-preview] > div').attributes('style')).toContain('height: 220px')
    mobile = false
    queries.forEach(query => query.notify())
    await settle()
    expect(wrapper.find('[data-flow-preview]').exists()).toBe(false)
    expect(document.querySelectorAll('canvas')).toHaveLength(1)
    const map = wrapper.getComponent(FlowMap)
    expect(map.props()).toMatchObject({ minWidth: 640, height: 200, readableLabels: false })
    const burst = vi.spyOn(map.vm.$.exposed!, 'burst')
    ;(wrapper.vm as unknown as { burst(to: string, count: number): void }).burst('b', 5)
    expect(burst).toHaveBeenCalledWith('b', 5)
  })
})
