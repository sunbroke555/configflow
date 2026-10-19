<template>
  <canvas ref="canvas" class="block" :style="canvasStyle" aria-hidden="true" />
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useThemeStore } from '@/stores/theme'
import { usePreferences } from '@/stores/preferences'

/**
 * 迷你趋势线：默认等距绘制；提供 timestamps 后按真实时间绘制，缺失样本断线。
 * 颜色取语义 token 名（如 --primary-accent），主题与强调色切换后自动重画。
 */
const props = withDefaults(
  defineProps<{
    data: (number | null)[]
    timestamps?: number[]
    timeRange?: [number, number]
    min?: number
    max?: number
    maxGap?: number
    color?: string
    fill?: boolean
  }>(),
  { color: '--primary-accent', fill: true }
)

const canvas = ref<HTMLCanvasElement | null>(null)
const { theme } = useThemeStore()
const { prefs } = usePreferences()
// Let CSS resolve derived color-mix tokens before passing colors to Canvas.
const canvasStyle = computed(() => ({
  color: props.color.startsWith('--') ? `var(${props.color})` : props.color,
  borderColor: 'var(--border-strong)'
}))
const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value)
type Point = { x: number; value: number }

const draw = () => {
  const cv = canvas.value
  if (!cv) return
  const rect = cv.getBoundingClientRect()
  if (!rect.width || !rect.height) return
  const dpr = Math.min(window.devicePixelRatio || 1, 2)
  cv.width = Math.round(rect.width * dpr)
  cv.height = Math.round(rect.height * dpr)
  const ctx = cv.getContext('2d')
  if (!ctx) return
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  ctx.clearRect(0, 0, rect.width, rect.height)

  const cs = getComputedStyle(cv)
  const color = cs.color
  const W = rect.width
  const H = rect.height - 2
  const timed = props.timestamps !== undefined
  const times = props.timestamps?.slice(0, props.data.length).filter(finite) || []
  const explicitRange = props.timeRange
  const [start, end] = explicitRange && finite(explicitRange[0]) && finite(explicitRange[1]) && explicitRange[1] > explicitRange[0]
    ? explicitRange
    : times.length ? [Math.min(...times), Math.max(...times)] : [0, 0]
  const maxGap = finite(props.maxGap) && props.maxGap >= 0 ? props.maxGap : Infinity
  const segments: Point[][] = []
  let segment: Point[] = []
  let previousTime: number | undefined
  const split = () => { segment = []; previousTime = undefined }

  props.data.forEach((value, index) => {
    const time = props.timestamps?.[index]
    if (!finite(value) || (timed && (!finite(time) || time < start || time > end))) {
      split()
      return
    }
    if (timed && finite(time) && previousTime !== undefined && (time <= previousTime || time - previousTime > maxGap)) split()
    if (!segment.length) segments.push(segment)
    segment.push({
      x: timed && finite(time) ? (end > start ? (time - start) / (end - start) * W : W / 2)
        : index / Math.max(1, props.data.length - 1) * W,
      value
    })
    previousTime = time
  })

  // Keep the original one-sample baseline for callers without timestamps.
  if (!segments.length || (!timed && props.data.length < 2)) {
    ctx.strokeStyle = cs.borderTopColor
    ctx.lineWidth = 1
    ctx.setLineDash([3, 4])
    ctx.beginPath()
    ctx.moveTo(0, H - 1)
    ctx.lineTo(W, H - 1)
    ctx.stroke()
    return
  }

  const values = segments.flatMap(points => points.map(point => point.value))
  const min = finite(props.min) ? props.min : Math.min(...values)
  const max = Math.max(min, finite(props.max) ? props.max : Math.max(...values))
  const y = (value: number) => H - Math.max(0, Math.min(1, (value - min) / (max - min || 1))) * Math.max(0, H - 4) + 1
  const path = (points: Point[]) => {
    ctx.beginPath()
    points.forEach((point, index) => {
      if (index) ctx.lineTo(point.x, y(point.value))
      else ctx.moveTo(point.x, y(point.value))
    })
  }
  ctx.setLineDash([])
  ctx.fillStyle = color
  ctx.strokeStyle = color
  ctx.lineWidth = 1.5
  ctx.lineJoin = 'round'
  segments.forEach((points, index) => {
    const last = points[points.length - 1]
    if (points.length > 1) {
      if (props.fill) {
        path(points)
        ctx.lineTo(last.x, H + 2)
        ctx.lineTo(points[0].x, H + 2)
        ctx.closePath()
        ctx.globalAlpha = 0.14
        ctx.fill()
        ctx.globalAlpha = 1
      }
      path(points)
      ctx.stroke()
    }
    if (points.length === 1 || index === segments.length - 1) {
      ctx.beginPath()
      ctx.arc(last.x - (!timed && points.length > 1 ? 2 : 0), y(last.value), 2.5, 0, Math.PI * 2)
      ctx.fill()
    }
  })
}

let observer: ResizeObserver | null = null
let themeFrame: number | undefined

onMounted(() => {
  draw()
  if (typeof ResizeObserver !== 'undefined') {
    observer = new ResizeObserver(draw)
    if (canvas.value) observer.observe(canvas.value)
  }
})

onUnmounted(() => {
  observer?.disconnect()
  if (themeFrame !== undefined) cancelAnimationFrame(themeFrame)
})

watch(() => [props.data, props.timestamps, props.timeRange, props.min, props.max, props.maxGap, props.color, props.fill], draw, { deep: true, flush: 'post' })
watch([theme, () => prefs.value.accent], () => {
  if (themeFrame !== undefined) cancelAnimationFrame(themeFrame)
  themeFrame = requestAnimationFrame(() => { themeFrame = undefined; draw() })
}, { flush: 'post' })
</script>
