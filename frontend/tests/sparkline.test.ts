import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, type VueWrapper } from '@vue/test-utils'
import { defineComponent, h, nextTick, ref } from 'vue'
import Sparkline from '@/components/common/Sparkline.vue'
import { usePreferences } from '@/stores/preferences'
import { useThemeStore } from '@/stores/theme'

type Point = [number, number]
type Command = { op: 'move' | 'line'; point: Point } | { op: 'arc'; point: Point; radius: number } | { op: 'close' }
type Paint = { path: Command[]; style: unknown; alpha: number; dash: number[] }
type Drawing = { strokes: Paint[]; fills: Paint[] }
type SparklineProps = {
  data: (number | null)[]
  timestamps?: number[]
  timeRange?: [number, number]
  min?: number
  max?: number
  maxGap?: number
  fill?: boolean
  color?: string
}

// Only browser drawing/layout primitives are replaced. Assertions inspect the
// paths actually painted by a mounted component, not its implementation helpers.
function recordingContext() {
  let path: Command[] = []
  let dash: number[] = []
  let drawing: Drawing = { strokes: [], fills: [] }
  const saved: { alpha: number; fill: unknown; stroke: unknown }[] = []
  const ctx = {
    strokeStyle: '' as unknown,
    fillStyle: '' as unknown,
    globalAlpha: 1,
    lineWidth: 1,
    lineJoin: 'miter',
    lineCap: 'butt',
    setTransform: vi.fn(),
    clearRect: vi.fn(() => { drawing = { strokes: [], fills: [] } }),
    beginPath: () => { path = [] },
    moveTo: (x: number, y: number) => { path.push({ op: 'move', point: [x, y] }) },
    lineTo: (x: number, y: number) => { path.push({ op: 'line', point: [x, y] }) },
    arc: (x: number, y: number, radius: number) => { path.push({ op: 'arc', point: [x, y], radius }) },
    closePath: () => { path.push({ op: 'close' }) },
    setLineDash: (value: number[]) => { dash = [...value] },
    stroke: () => { drawing.strokes.push({ path: [...path], style: ctx.strokeStyle, alpha: ctx.globalAlpha, dash: [...dash] }) },
    fill: () => { drawing.fills.push({ path: [...path], style: ctx.fillStyle, alpha: ctx.globalAlpha, dash: [...dash] }) },
    save: () => { saved.push({ alpha: ctx.globalAlpha, fill: ctx.fillStyle, stroke: ctx.strokeStyle }) },
    restore: () => {
      const value = saved.pop()
      if (value) Object.assign(ctx, { globalAlpha: value.alpha, fillStyle: value.fill, strokeStyle: value.stroke })
    },
    createLinearGradient: vi.fn(() => ({ addColorStop: vi.fn() })),
    get drawing() { return drawing }
  }
  return ctx
}

const { prefs } = usePreferences()
const { theme, preference, setTheme } = useThemeStore()
const wrappers: VueWrapper[] = []
let contexts: WeakMap<HTMLCanvasElement, ReturnType<typeof recordingContext>>
let resizeObservers: { callback: ResizeObserverCallback; disconnect: ReturnType<typeof vi.fn> }[]
const frames = new Map<number, FrameRequestCallback>()
let frameId = 0
let previousAccent = prefs.value.accent
let previousTheme = preference.value
let customColor = 'color(srgb 0.2 0.3 0.4)'

function tokenColor(token: string) {
  if (token === '--mixed-accent') return customColor
  return theme.value === 'light' ? 'rgb(35, 45, 55)' : prefs.value.accent === 'sky' ? 'rgb(70, 140, 210)' : 'rgb(210, 120, 80)'
}

beforeEach(async () => {
  previousAccent = prefs.value.accent
  previousTheme = preference.value
  prefs.value.accent = 'clay'
  setTheme('dark')
  await nextTick()
  contexts = new WeakMap()
  resizeObservers = []
  frames.clear()
  frameId = 0
  customColor = 'color(srgb 0.2 0.3 0.4)'
  vi.stubGlobal('devicePixelRatio', 2)
  vi.stubGlobal('requestAnimationFrame', vi.fn((callback: FrameRequestCallback) => {
    const id = ++frameId
    frames.set(id, callback)
    return id
  }))
  vi.stubGlobal('cancelAnimationFrame', vi.fn((id: number) => { frames.delete(id) }))
  vi.stubGlobal('ResizeObserver', class {
    record: (typeof resizeObservers)[number]
    constructor(callback: ResizeObserverCallback) {
      this.record = { callback, disconnect: vi.fn() }
      resizeObservers.push(this.record)
    }
    observe() {}
    disconnect() { this.record.disconnect() }
  })
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(function (this: HTMLCanvasElement) {
    if (!contexts.has(this)) contexts.set(this, recordingContext())
    return contexts.get(this)! as unknown as CanvasRenderingContext2D
  })
  vi.spyOn(HTMLCanvasElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLCanvasElement) {
    return new DOMRect(0, 0, Number.parseFloat(this.style.width) || 200, Number.parseFloat(this.style.height) || 40)
  })
  vi.stubGlobal('getComputedStyle', vi.fn((element: Element) => {
    const token = (element as HTMLElement).style.color.match(/var\((--[^,)]+)/)?.[1] || '--primary-accent'
    return {
      color: tokenColor(token),
      borderTopColor: 'rgb(100, 100, 100)',
      getPropertyValue: (name: string) => name === '--mixed-accent'
        ? 'color-mix(in srgb, #123456 50%, #abcdef)'
        : name === '--border-strong' ? 'rgb(100, 100, 100)' : tokenColor(name)
    } as CSSStyleDeclaration
  }))
})

afterEach(async () => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  prefs.value.accent = previousAccent
  setTheme(previousTheme)
  await nextTick()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

async function render(props: SparklineProps) {
  const wrapper = mount(Sparkline, { props, attrs: { style: 'width: 200px; height: 40px' } })
  wrappers.push(wrapper)
  await settle()
  return wrapper
}

async function settle() {
  await nextTick()
  const callbacks = [...frames.values()]
  frames.clear()
  callbacks.forEach(callback => callback(16))
  await nextTick()
}

function contextOf(wrapper: VueWrapper) {
  return contexts.get(wrapper.get('canvas').element as HTMLCanvasElement)!
}

function subpaths(path: Command[]) {
  const result: Point[][] = []
  for (const command of path) {
    if (command.op === 'move') result.push([command.point])
    if (command.op === 'line') result[result.length - 1]?.push(command.point)
  }
  return result
}

function lines(wrapper: VueWrapper) {
  return contextOf(wrapper).drawing.strokes.flatMap(paint => subpaths(paint.path))
}

function areas(wrapper: VueWrapper) {
  return contextOf(wrapper).drawing.fills
    .filter(paint => !paint.path.some(command => command.op === 'arc'))
    .flatMap(paint => subpaths(paint.path))
}

function dots(wrapper: VueWrapper) {
  return contextOf(wrapper).drawing.fills.flatMap(paint => paint.path.filter(command => command.op === 'arc'))
}

function expectPoints(actual: Point[], expected: Point[]) {
  expect(actual).toHaveLength(expected.length)
  actual.forEach((point, index) => {
    expect(point[0]).toBeCloseTo(expected[index]![0], 6)
    expect(point[1]).toBeCloseTo(expected[index]![1], 6)
  })
}

describe('Sparkline painted series', () => {
  it('keeps legacy numeric data evenly spaced and scales its own finite min/max', async () => {
    const wrapper = await render({ data: [20, 50, 30] })
    const [points] = lines(wrapper)
    expect(points!.map(point => point[0])).toEqual([0, 100, 200])
    const [low, high, middle] = points!
    expect(low![1]).toBeGreaterThan(high![1])
    expect((low![1] - middle![1]) / (low![1] - high![1])).toBeCloseTo(1 / 3)
    await wrapper.setProps({ data: [200, 500, 300] })
    await settle()
    expectPoints(lines(wrapper)[0]!, points!)
  })

  it('positions samples by timestamps within the complete time range', async () => {
    const wrapper = await render({ data: [1, 2, 3], timestamps: [0, 30_000, 90_000], timeRange: [0, 120_000] })
    expect(lines(wrapper)[0]!.map(point => point[0])).toEqual([0, 50, 150])
    expect(dots(wrapper).at(-1)?.point[0]).toBe(150)
    await wrapper.setProps({ timeRange: [-120_000, 120_000] })
    await settle()
    expect(lines(wrapper)[0]!.map(point => point[0])).toEqual([100, 125, 175])
  })

  it.each([null, Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY])('breaks both line and fill at missing or invalid value %s', async missing => {
    const wrapper = await render({ data: [1, 2, missing, 3, 4], timestamps: [0, 30, 60, 90, 120], timeRange: [0, 120] })
    expect(lines(wrapper).map(points => points.map(point => point[0]))).toEqual([[0, 50], [150, 200]])
    const paintedAreas = areas(wrapper)
    expect(paintedAreas).toHaveLength(2)
    expect(paintedAreas.map(points => [Math.min(...points.map(point => point[0])), Math.max(...points.map(point => point[0]))]))
      .toEqual([[0, 50], [150, 200]])
    expect([...lines(wrapper), ...paintedAreas].flat().every(point => point.every(Number.isFinite))).toBe(true)
  })

  it('breaks at a long elapsed-time gap without filling across the unobserved interval', async () => {
    const wrapper = await render({ data: [1, 2, 3, 4], timestamps: [0, 30_000, 210_000, 240_000], timeRange: [0, 240_000], maxGap: 120_000 })
    expect(lines(wrapper).map(points => points.map(point => point[0]))).toEqual([[0, 25], [175, 200]])
    expect(areas(wrapper).map(points => [Math.min(...points.map(point => point[0])), Math.max(...points.map(point => point[0]))]))
      .toEqual([[0, 25], [175, 200]])
    await wrapper.setProps({ maxGap: 180_000 })
    await settle()
    expect(lines(wrapper)[0]!.map(point => point[0])).toEqual([0, 25, 175, 200])
  })

  it.each([Number.NaN, Number.POSITIVE_INFINITY, 180])('breaks at invalid or out-of-window timestamp %s', async timestamp => {
    const wrapper = await render({ data: [1, 2, 3, 4, 5], timestamps: [0, 30, timestamp, 90, 120], timeRange: [0, 120] })
    expect(lines(wrapper).map(points => points.map(point => point[0]))).toEqual([[0, 50], [150, 200]])
    expect(areas(wrapper).map(points => [Math.min(...points.map(point => point[0])), Math.max(...points.map(point => point[0]))]))
      .toEqual([[0, 50], [150, 200]])
  })

  it('starts a separate line when timestamps move backwards', async () => {
    const wrapper = await render({ data: [1, 2, 3, 4], timestamps: [0, 60, 30, 90], timeRange: [0, 120] })
    expect(lines(wrapper).map(points => points.map(point => point[0]))).toEqual([[0, 100], [50, 150]])
  })

  it('keeps zero as a real sample and includes it in the scale', async () => {
    const wrapper = await render({ data: [10, 0, 20], timestamps: [0, 60, 120], timeRange: [0, 120] })
    const [points] = lines(wrapper)
    expect(points).toHaveLength(3)
    expect(points![1]![0]).toBe(100)
    expect(points![1]![1]).toBeGreaterThan(points![0]![1])
    expect(points![0]![1]).toBeGreaterThan(points![2]![1])
    expect(points![0]![1]).toBeCloseTo((points![1]![1] + points![2]![1]) / 2)
  })

  it('paints a lone time sample at its real position without fabricating a line', async () => {
    const wrapper = await render({ data: [5], timestamps: [90], timeRange: [0, 120], min: 0, max: 10 })
    expect(lines(wrapper).filter(points => points.length > 1)).toHaveLength(0)
    expect(areas(wrapper)).toHaveLength(0)
    expect(dots(wrapper)).toHaveLength(1)
    expect(dots(wrapper)[0]!.point[0]).toBe(150)
    expect(dots(wrapper)[0]!.radius).toBeGreaterThan(0)
  })

  it.each([{ data: [] }, { data: [5] }])('retains the baseline for an empty or single-item legacy series $data', async ({ data }) => {
    const wrapper = await render({ data })
    expect(lines(wrapper)).toHaveLength(1)
    const [start, end] = lines(wrapper)[0]!
    expect(start![0]).toBe(0)
    expect(end![0]).toBe(200)
    expect(start![1]).toBe(end![1])
    expect(dots(wrapper)).toHaveLength(0)
    expect(areas(wrapper)).toHaveLength(0)
  })

  it('uses the empty baseline when a time series has no finite samples', async () => {
    const wrapper = await render({ data: [null, Number.NaN], timestamps: [0, 60], timeRange: [0, 120] })
    expect(lines(wrapper)[0]!.map(point => point[0])).toEqual([0, 200])
    expect(dots(wrapper)).toHaveLength(0)
    expect(areas(wrapper)).toHaveLength(0)
  })

  it('uses the supplied min/max to share a vertical scale and reacts when it changes', async () => {
    const upload = await render({ data: [0, 50, 100], min: 0, max: 100 })
    const download = await render({ data: [50, 75], min: 0, max: 100 })
    const uploadPoints = lines(upload)[0]!
    const downloadPoints = lines(download)[0]!
    expect(downloadPoints[0]![1]).toBeCloseTo(uploadPoints[1]![1])
    expect(downloadPoints[1]![1]).toBeCloseTo((uploadPoints[1]![1] + uploadPoints[2]![1]) / 2)
    await upload.setProps({ max: 200 })
    await settle()
    expect(lines(upload)[0]![2]![1]).toBeCloseTo(uploadPoints[1]![1])
    await upload.setProps({ min: -100, max: 100 })
    await settle()
    expect(lines(upload)[0]![0]![1]).toBeCloseTo(uploadPoints[1]![1])
  })

  it('updates fill and timestamp props without losing the real endpoint', async () => {
    const wrapper = await render({ data: [1, 2], timestamps: [0, 120], timeRange: [0, 120] })
    expect(areas(wrapper)).toHaveLength(1)
    await wrapper.setProps({ fill: false, timestamps: [30, 90] })
    await settle()
    expect(lines(wrapper)[0]!.map(point => point[0])).toEqual([50, 150])
    expect(areas(wrapper)).toHaveLength(0)
    expect(dots(wrapper).at(-1)?.point[0]).toBe(150)
  })

  it('measures updated DOM dimensions after a data change and redraws on resize', async () => {
    const values = ref([0, 1])
    const width = ref(200)
    const wrapper = mount(defineComponent({
      setup: () => () => h(Sparkline, { data: values.value, style: { width: `${width.value}px`, height: '40px' } })
    }))
    wrappers.push(wrapper)
    await settle()
    values.value = [0, 2, 1]
    width.value = 320
    await settle()
    expect(lines(wrapper)[0]!.map(point => point[0])).toEqual([0, 160, 320])
    const canvas = wrapper.get('canvas').element as HTMLCanvasElement
    expect(canvas.width).toBe(640)
    canvas.style.width = '400px'
    resizeObservers[0]!.callback([], {} as ResizeObserver)
    await settle()
    expect(lines(wrapper)[0]!.at(-1)?.[0]).toBe(400)
    expect(canvas.width).toBe(800)
    wrapper.unmount()
    expect(resizeObservers[0]!.disconnect).toHaveBeenCalledOnce()
  })

  it('redraws with the resolved theme and accent colors', async () => {
    const wrapper = await render({ data: [1, 2] })
    expect(contextOf(wrapper).drawing.strokes[0]!.style).toBe('rgb(210, 120, 80)')
    prefs.value.accent = 'sky'
    await settle()
    expect(contextOf(wrapper).drawing.strokes[0]!.style).toBe('rgb(70, 140, 210)')
    setTheme('light')
    await settle()
    expect(contextOf(wrapper).drawing.strokes[0]!.style).toBe('rgb(35, 45, 55)')
  })

  it('uses resolved color-mix tokens without appending hex transparency suffixes', async () => {
    const wrapper = await render({ data: [1, 2], color: '--mixed-accent' })
    expect(wrapper.get('canvas').element.style.color).toBe('var(--mixed-accent)')
    const paints = [...contextOf(wrapper).drawing.strokes, ...contextOf(wrapper).drawing.fills]
    expect(paints.length).toBeGreaterThan(1)
    expect(paints.every(paint => paint.style === customColor)).toBe(true)
    expect(paints.some(paint => paint.alpha > 0 && paint.alpha < 1)).toBe(true)
    expect(contextOf(wrapper).drawing.strokes[0]!.alpha).toBe(1)
    expect(contextOf(wrapper).drawing.fills.at(-1)!.alpha).toBe(1)
    await wrapper.setProps({ color: '--primary-accent' })
    await settle()
    expect(contextOf(wrapper).drawing.strokes[0]!.style).toBe('rgb(210, 120, 80)')
  })
})
