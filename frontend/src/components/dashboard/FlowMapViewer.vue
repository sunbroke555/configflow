<template>
  <div ref="host" class="min-w-0" tabindex="-1">
    <FlowMap v-if="!isPhone && !open" ref="map" v-bind="mapProps">
      <template v-if="$slots.node" #node="slotProps"><slot name="node" v-bind="slotProps" /></template>
    </FlowMap>

    <div v-if="isPhone" ref="preview" class="min-w-0" data-flow-preview>
      <div class="relative overflow-hidden" :style="{ height: `${previewHeight}px` }">
        <div v-if="!open" ref="plane" class="pointer-events-none absolute left-0 top-0 origin-top-left" :style="previewStyle" inert aria-hidden="true">
          <FlowMap ref="map" v-bind="mapProps">
            <template v-if="$slots.node" #node="slotProps"><slot name="node" v-bind="slotProps" /></template>
          </FlowMap>
        </div>
        <button type="button" class="absolute inset-0 min-h-11 cursor-zoom-in rounded-lg focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring" :aria-label="`全屏查看${label}`" @click="openViewer">
          <span class="sr-only">完整缩略图，打开后可缩放和拖动查看</span>
        </button>
      </div>
      <div class="flex min-h-11 items-center gap-2 border-t border-border/60 px-3">
        <span class="min-w-0 flex-1 text-xs text-muted-foreground">完整预览 · 可缩放查看</span>
        <Button variant="ghost" class="min-h-11 shrink-0 px-2" :aria-label="`全屏查看${label}`" @click="openViewer"><Maximize2 class="size-4" />全屏查看</Button>
      </div>
    </div>

    <Dialog v-model:open="open">
      <DialogContent :show-close-button="false" class="flow-map-dialog" @close-auto-focus="restoreFocus">
        <header class="shrink-0 border-b border-border px-3 pb-2 pt-[max(8px,env(safe-area-inset-top))]">
          <div class="flex min-w-0 items-center gap-2">
            <DialogTitle class="min-w-0 flex-1 truncate text-base" :title="label">{{ label }}</DialogTitle>
            <Button variant="ghost" size="icon" class="size-11 shrink-0" aria-label="关闭全屏图" @click="open = false"><X class="size-4" /></Button>
          </div>
          <DialogDescription class="mb-2 text-xs">拖动查看 · 双指缩放</DialogDescription>
          <div class="flex items-center gap-1" role="group" aria-label="图形缩放">
            <Button variant="outline" size="icon" class="size-11" aria-label="缩小图形" :disabled="zoom <= fitScale + 0.001" @click="setZoom(zoom / 1.25)"><Minus class="size-4" /></Button>
            <Button variant="ghost" class="min-h-11 min-w-16 px-2 font-mono" aria-label="恢复原始大小" title="恢复原始大小" @click="setZoom(1)">{{ Math.round(zoom * 100) }}%</Button>
            <Button variant="outline" size="icon" class="size-11" aria-label="放大图形" :disabled="zoom >= MAX_ZOOM" @click="setZoom(zoom * 1.25)"><Plus class="size-4" /></Button>
            <Button variant="outline" class="ml-auto min-h-11 px-3" aria-label="适应完整图形" @click="fit"><Scan class="size-4" />适应</Button>
          </div>
        </header>
        <div
          ref="viewport"
          class="relative min-h-0 flex-1 touch-none select-none overflow-hidden bg-background focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring"
          data-flow-viewport tabindex="0" role="region" :aria-label="`${label}缩放视图`"
          :style="{ cursor: dragging ? 'grabbing' : 'grab' }"
          @pointerdown="pointerDown"
          @click.capture="guardClick" @wheel.prevent="wheel" @keydown="keyDown"
          @focusin="revealFocusedNode" @scroll="resetNativeScroll"
        >
          <div v-if="open" ref="plane" class="absolute left-0 top-0 origin-top-left" :style="fullStyle" data-flow-plane>
            <FlowMap ref="map" v-bind="mapProps">
              <template v-if="$slots.node" #node="slotProps"><slot name="node" v-bind="slotProps" /></template>
            </FlowMap>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useMediaQuery, useResizeObserver } from '@vueuse/core'
import { Maximize2, Minus, Plus, Scan, X } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import FlowMap, { type FlowColumn, type FlowEdge } from './FlowMap.vue'

const props = withDefaults(defineProps<{
  columns: FlowColumn[]; edges: FlowEdge[]; label?: string
  height?: number; minWidth?: number; density?: number; columnsTemplate?: string; gapX?: number
}>(), { label: '配置流向', height: 380, minWidth: 980, density: 1 })
const mapProps = computed(() => ({ columns: props.columns, edges: props.edges, height: props.height, minWidth: props.minWidth, density: props.density, columnsTemplate: props.columnsTemplate, gapX: props.gapX, readableLabels: open.value }))
const isPhone = useMediaQuery('(max-width: 639px)')
const map = ref<InstanceType<typeof FlowMap> | null>(null)
const host = ref<HTMLElement | null>(null)
const preview = ref<HTMLElement | null>(null)
const viewport = ref<HTMLElement | null>(null)
const plane = ref<HTMLElement | null>(null)
const open = ref(false)
const previewWidth = ref(0)
const viewportSize = ref({ width: 0, height: 0 })
const naturalHeight = ref(props.height + 36)
const naturalWidth = computed(() => Math.max(props.minWidth, open.value ? viewportSize.value.width : previewWidth.value))
const previewScale = computed(() => Math.min((previewWidth.value || 320) / naturalWidth.value, 220 / naturalHeight.value, 1))
const previewHeight = computed(() => Math.max(44, Math.ceil(naturalHeight.value * previewScale.value)))
const previewStyle = computed(() => ({ width: `${naturalWidth.value}px`, transform: `translateX(${Math.max(0, (previewWidth.value - naturalWidth.value * previewScale.value) / 2)}px) scale(${previewScale.value})` }))
const MAX_ZOOM = 3
const fitScale = computed(() => Math.min(Math.max(1, viewportSize.value.width - 24) / naturalWidth.value, Math.max(1, viewportSize.value.height - 24) / naturalHeight.value, 1))
const zoom = ref(1)
const pan = ref({ x: 0, y: 0 })
const fitMode = ref(true)
const initialView = ref(false)
const fullStyle = computed(() => ({ width: `${naturalWidth.value}px`, transform: `translate(${pan.value.x}px, ${pan.value.y}px) scale(${zoom.value})` }))
let returnFocus: HTMLElement | null = null

function bounded(x: number, y: number, scale = zoom.value) {
  const clamp = (value: number, extent: number, available: number) => extent <= available
    ? (available - extent) / 2
    : Math.min(12, Math.max(available - extent - 12, value))
  return { x: clamp(x, naturalWidth.value * scale, viewportSize.value.width), y: clamp(y, naturalHeight.value * scale, viewportSize.value.height) }
}
function fit() {
  initialView.value = false
  fitMode.value = true
  zoom.value = fitScale.value
  pan.value = bounded(0, 0)
}
function setZoom(value: number, focal = { x: viewportSize.value.width / 2, y: viewportSize.value.height / 2 }) {
  initialView.value = false
  fitMode.value = false
  const next = Math.max(fitScale.value, Math.min(MAX_ZOOM, value))
  const ratio = next / zoom.value
  pan.value = bounded(focal.x - (focal.x - pan.value.x) * ratio, focal.y - (focal.y - pan.value.y) * ratio, next)
  zoom.value = next
}
async function openViewer(event: MouseEvent) {
  returnFocus = event.currentTarget as HTMLElement
  clearGesture()
  initialView.value = true
  fitMode.value = false
  zoom.value = 1
  pan.value = { x: 12, y: 12 }
  open.value = true
  await nextTick()
  measureViewport()
}
function restoreFocus(event: Event) {
  event.preventDefault()
  nextTick(() => {
    const visible = (element: HTMLElement | null | undefined) => element?.isConnected && element.getClientRects().length
    if (visible(returnFocus)) return returnFocus!.focus({ preventScroll: true })
    if (visible(host.value)) return host.value!.focus({ preventScroll: true })
    // A responsive details section may collapse while the fullscreen dialog is
    // open. Return to its visible summary rather than a now-hidden trigger.
    for (let details = host.value?.closest('details:not([open])'); details; details = details.parentElement?.closest('details:not([open])')) {
      const summary = details.querySelector<HTMLElement>(':scope > summary')
      if (visible(summary)) return summary!.focus({ preventScroll: true })
    }
  })
}
function measureViewport() {
  if (!viewport.value) return
  viewportSize.value = { width: viewport.value.clientWidth, height: viewport.value.clientHeight }
  positionInitialView()
}
function positionInitialView() {
  // The first column's hub is centered even when many targets make a tall map.
  // Reapply until the first interaction, including the initial content measure.
  pan.value = bounded(12, (viewportSize.value.height - naturalHeight.value) / 2)
}
async function revealFocusedNode() {
  // Tab can scroll overflow:hidden containers itself. Keep one coordinate
  // system, then bring the newly focused node into view using the same pan.
  await nextTick()
  const area = viewport.value
  const node = document.activeElement?.closest<HTMLElement>('[data-fid]')
  if (!open.value || !area || !node || !plane.value?.contains(node)) return
  area.scrollLeft = 0
  area.scrollTop = 0
  const bounds = area.getBoundingClientRect()
  const rect = node.getBoundingClientRect()
  const reveal = (start: number, end: number, low: number, high: number) =>
    start < low || end - start > high - low ? low - start : end > high ? high - end : 0
  const dx = reveal(rect.left, rect.right, bounds.left + 12, bounds.right - 12)
  const dy = reveal(rect.top, rect.bottom, bounds.top + 12, bounds.bottom - 12)
  if (dx || dy) {
    initialView.value = false
    fitMode.value = false
    pan.value = bounded(pan.value.x + dx, pan.value.y + dy)
  }
}
function resetNativeScroll() {
  if (!viewport.value || (!viewport.value.scrollLeft && !viewport.value.scrollTop)) return
  viewport.value.scrollLeft = 0
  viewport.value.scrollTop = 0
  void revealFocusedNode()
}
useResizeObserver(preview, entries => { if (entries[0]?.contentRect.width) previewWidth.value = entries[0].contentRect.width })
useResizeObserver(viewport, entries => {
  if (!entries[0]) return
  viewportSize.value = { width: entries[0].contentRect.width, height: entries[0].contentRect.height }
})
useResizeObserver(plane, entries => {
  if (entries[0]?.contentRect.height) naturalHeight.value = entries[0].contentRect.height
})
watch([fitScale, naturalWidth, naturalHeight, viewportSize], () => {
  if (!open.value) return
  clearGesture()
  if (initialView.value) positionInitialView()
  else if (fitMode.value) fit()
  else { zoom.value = Math.max(fitScale.value, zoom.value); pan.value = bounded(pan.value.x, pan.value.y) }
}, { flush: 'post' })
watch([previewScale, zoom, open, isPhone], () => nextTick(() => map.value?.refreshLayout()), { flush: 'post' })
watch(open, value => { if (!value) clearGesture() })

interface Point { x: number; y: number }
const pointers = new Map<number, Point>()
type Gesture = { kind: 'pan'; point: Point; pan: Point } | { kind: 'pinch'; distance: number; zoom: number; anchor: Point }
let gesture: Gesture | null = null
let suppressClick = false
const dragging = ref(false)
function point(event: PointerEvent | WheelEvent): Point {
  const rect = viewport.value!.getBoundingClientRect()
  return { x: event.clientX - rect.left, y: event.clientY - rect.top }
}
function capture(id: number) {
  try { viewport.value?.setPointerCapture(id) } catch { /* A canceled touch may already be gone. */ }
}
function pinch() {
  const [a, b] = [...pointers.values()]
  return { midpoint: { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 }, distance: Math.max(1, Math.hypot(b.x - a.x, b.y - a.y)) }
}
function beginGesture() {
  if (pointers.size >= 2) {
    const { midpoint, distance } = pinch()
    gesture = { kind: 'pinch', distance, zoom: zoom.value, anchor: { x: (midpoint.x - pan.value.x) / zoom.value, y: (midpoint.y - pan.value.y) / zoom.value } }
    suppressClick = true
    dragging.value = true
    for (const id of pointers.keys()) capture(id)
  } else if (pointers.size) {
    gesture = { kind: 'pan', point: [...pointers.values()][0], pan: { ...pan.value } }
  } else { gesture = null; dragging.value = false }
}
function pointerDown(event: PointerEvent) {
  if (event.pointerType === 'mouse' && event.button !== 0) return
  initialView.value = false
  if (!pointers.size) {
    suppressClick = false
    // A tap keeps its original node target. Before the drag threshold, listen
    // outside the viewport too so an edge gesture cannot leave a stale finger.
    window.addEventListener('pointermove', pointerMove, { passive: false })
    window.addEventListener('pointerup', pointerEnd)
    window.addEventListener('pointercancel', pointerEnd)
  }
  pointers.set(event.pointerId, point(event))
  beginGesture()
}
function pointerMove(event: PointerEvent) {
  if (!pointers.has(event.pointerId) || !gesture) return
  const current = point(event)
  pointers.set(event.pointerId, current)
  if (gesture.kind === 'pinch' && pointers.size >= 2) {
    const { midpoint, distance } = pinch()
    const next = Math.max(fitScale.value, Math.min(MAX_ZOOM, gesture.zoom * distance / gesture.distance))
    fitMode.value = false
    zoom.value = next
    pan.value = bounded(midpoint.x - gesture.anchor.x * next, midpoint.y - gesture.anchor.y * next)
  } else if (gesture.kind === 'pan') {
    const dx = current.x - gesture.point.x, dy = current.y - gesture.point.y
    if (!dragging.value && Math.hypot(dx, dy) < 6) return
    suppressClick = true
    dragging.value = true
    fitMode.value = false
    capture(event.pointerId)
    pan.value = bounded(gesture.pan.x + dx, gesture.pan.y + dy)
  }
  event.preventDefault()
}
function pointerEnd(event: PointerEvent) {
  if (!pointers.delete(event.pointerId)) return
  beginGesture()
  if (!pointers.size) removePointerListeners()
}
function removePointerListeners() {
  window.removeEventListener('pointermove', pointerMove)
  window.removeEventListener('pointerup', pointerEnd)
  window.removeEventListener('pointercancel', pointerEnd)
}
function clearGesture() {
  const ids = [...pointers.keys()]
  pointers.clear()
  gesture = null
  dragging.value = false
  removePointerListeners()
  for (const id of ids) {
    try { if (viewport.value?.hasPointerCapture(id)) viewport.value.releasePointerCapture(id) } catch { /* Viewport may have unmounted. */ }
  }
}
function guardClick(event: MouseEvent) {
  if (suppressClick && event.detail !== 0) { event.preventDefault(); event.stopPropagation() }
}
function wheel(event: WheelEvent) {
  initialView.value = false
  if (event.ctrlKey || event.metaKey) setZoom(zoom.value * Math.exp(-event.deltaY * 0.002), point(event))
  else { fitMode.value = false; pan.value = bounded(pan.value.x - event.deltaX, pan.value.y - event.deltaY) }
}
function keyDown(event: KeyboardEvent) {
  if (event.target !== event.currentTarget) return
  const directions: Record<string, Point> = { ArrowLeft: { x: 48, y: 0 }, ArrowRight: { x: -48, y: 0 }, ArrowUp: { x: 0, y: 48 }, ArrowDown: { x: 0, y: -48 } }
  if (event.key in directions) {
    initialView.value = false
    event.preventDefault(); fitMode.value = false
    const delta = directions[event.key]
    pan.value = bounded(pan.value.x + delta.x, pan.value.y + delta.y)
  } else if (event.key === '+' || event.key === '=') { event.preventDefault(); setZoom(zoom.value * 1.25) }
  else if (event.key === '-') { event.preventDefault(); setZoom(zoom.value / 1.25) }
  else if (event.key === 'Home') { event.preventDefault(); fit() }
}
onBeforeUnmount(clearGesture)
defineExpose({ burst: (to: string, count?: number) => map.value?.burst(to, count) })
</script>

<!-- DialogContent is teleported and does not inherit this wrapper's scope id. -->
<style>
.flow-map-dialog {
  inset: 0;
  display: flex;
  width: 100vw;
  max-width: none;
  height: 100dvh;
  max-height: none;
  translate: none;
  flex-direction: column;
  gap: 0;
  overflow: hidden;
  border: 0;
  border-radius: 0;
  padding: 0;
  animation: none;
}
</style>
