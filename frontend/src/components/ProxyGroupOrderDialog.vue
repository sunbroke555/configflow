<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { ArrowDown, ArrowDownToLine, ArrowUp, ArrowUpToLine, Ellipsis, GripVertical, Loader2, X } from '@lucide/vue'
import { isAxiosError } from 'axios'
import Sortable from 'sortablejs'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { notify } from '@/lib/feedback'
import type { ProxyGroup } from '@/types'

const props = defineProps<{
  open: boolean
  groups: ProxyGroup[]
  saveOrder: (ordered: ProxyGroup[]) => Promise<void>
}>()
const emit = defineEmits<{
  'update:open': [value: boolean]
  saved: [ordered: ProxyGroup[]]
}>()

const draft = ref<ProxyGroup[]>([])
const initialOrder = ref<string[]>([])
const list = ref<HTMLOListElement | null>(null)
const saving = ref(false)
const dragging = ref(false)
const grabbedKey = ref<string | null>(null)
const announcement = ref('')
const keyOf = (group: ProxyGroup) => group.id || group.name
const changed = computed(() => draft.value.some((group, index) => keyOf(group) !== initialOrder.value[index]))
const locked = computed(() => saving.value || dragging.value)
const labels: Record<string, string> = {
  chain: '代理链', select: '手动选择', 'url-test': '自动测速', fallback: '故障转移', 'load-balance': '负载均衡'
}

// Sortable exposes these animation helpers at runtime, but omits them from its types.
type AnimatedSortable = ReturnType<typeof Sortable.create> & {
  captureAnimationState?: () => void
  animateAll?: () => void
}
let sortable: AnimatedSortable | null = null
let motionPreference: MediaQueryList | null = null
let motionObserver: MutationObserver | null = null
let session = 0
let disposed = false

const motionEnabled = () => !motionPreference?.matches && document.documentElement.dataset.motion !== 'off'
const updateMotion = () => sortable?.option('animation', motionEnabled() ? 180 : 0)
const position = (index: number) => `第 ${index + 1} 项，共 ${draft.value.length} 项`
const announcePosition = (group: ProxyGroup, index: number) => {
  announcement.value = `${group.name} 已移到${position(index)}`
}

function focusRow(key: string, action = 'handle') {
  const row = Array.from(list.value?.querySelectorAll<HTMLElement>('[data-group-id]') || [])
    .find(element => element.dataset.groupId === key)
  const target = row?.querySelector<HTMLButtonElement>(`[data-order-action="${action}"]:not(:disabled)`)
    || row?.querySelector<HTMLButtonElement>('[data-order-handle]')
  target?.focus({ preventScroll: true })
  row?.scrollIntoView?.({ block: 'nearest', behavior: 'instant' })
}

function move(key: string, target: number, action = 'handle') {
  if (locked.value) return
  const index = draft.value.findIndex(group => keyOf(group) === key)
  if (index < 0 || target < 0 || target >= draft.value.length || target === index) return
  if (action !== 'handle') grabbedKey.value = null
  const instance = sortable
  if (motionEnabled()) instance?.captureAnimationState?.()
  const next = [...draft.value]
  const [group] = next.splice(index, 1)
  next.splice(target, 0, group)
  draft.value = next
  announcePosition(group, target)
  nextTick(() => {
    if (!props.open || disposed) return
    if (instance === sortable && motionEnabled()) instance?.animateAll?.()
    focusRow(key, action)
  })
}

function handleKeydown(event: KeyboardEvent, group: ProxyGroup, index: number) {
  if (locked.value) return
  const key = keyOf(group)
  if (event.key === ' ' || event.key === 'Spacebar') {
    event.preventDefault()
    if (grabbedKey.value === key) {
      grabbedKey.value = null
      announcement.value = `${group.name} 已放下，${position(index)}`
    } else {
      grabbedKey.value = key
      announcement.value = `已抓取 ${group.name}，${position(index)}，使用上下方向键移动，空格放下`
    }
  } else if (event.key === 'Escape' && grabbedKey.value !== null) {
    event.preventDefault()
    event.stopPropagation()
    grabbedKey.value = null
    announcement.value = `${group.name} 已放下，${position(index)}`
  } else if (grabbedKey.value === key && ['ArrowUp', 'ArrowDown'].includes(event.key)) {
    event.preventDefault()
    move(key, index + (event.key === 'ArrowUp' ? -1 : 1))
  }
}

function handleEscape(event: KeyboardEvent) {
  if (locked.value || grabbedKey.value !== null) {
    event.preventDefault()
    if (grabbedKey.value !== null) {
      const index = draft.value.findIndex(group => keyOf(group) === grabbedKey.value)
      grabbedKey.value = null
      if (index >= 0) announcement.value = `${draft.value[index].name} 已放下，${position(index)}`
    } else if (dragging.value) {
      announcement.value = '正在拖动，请先松开手柄'
    }
  }
}

function requestOpen(open: boolean) {
  if (!locked.value) emit('update:open', open)
}

function keepMenuFocus(event: Event, key: string) {
  event.preventDefault()
  nextTick(() => focusRow(key, 'more'))
}

function destroySortable() {
  const container = list.value
  if (sortable && container) {
    // Sortable.destroy() does not remove fallback nodes when a dialog is
    // forcibly closed mid-gesture. Keep any subsequent Vue render consistent.
    for (const clone of [Sortable.ghost, Sortable.clone] as Array<HTMLElement | undefined>) {
      if (clone && container.contains(clone)) clone.remove()
    }
    if (dragging.value) {
      const rows = new Map(Array.from(container.children)
        .filter((row): row is HTMLElement => row instanceof HTMLElement && row.hasAttribute('data-group-id'))
        .map(row => [row.dataset.groupId, row]))
      draft.value.forEach(group => {
        const row = rows.get(keyOf(group))
        if (!row) return
        row.classList.remove('proxy-group-order-placeholder', 'proxy-group-order-chosen')
        container.appendChild(row)
      })
      syncVisiblePositions()
    }
  }
  sortable?.destroy()
  sortable = null
  dragging.value = false
  motionPreference?.removeEventListener('change', updateMotion)
  motionPreference = null
  motionObserver?.disconnect()
  motionObserver = null
}

type OrderDragEvent = {
  item: HTMLElement
  from: HTMLElement
  oldIndex?: number
  newIndex?: number
  oldDraggableIndex?: number
  newDraggableIndex?: number
}

function syncVisiblePositions() {
  const rows = Array.from(list.value?.children || [])
    .filter((element): element is HTMLElement => element instanceof HTMLElement && element.hasAttribute('data-group-id'))
  rows.forEach((row, index) => {
    const number = row.querySelector<HTMLElement>('[data-order-position]')
    if (number) number.textContent = String(index + 1).padStart(2, '0')
  })
}

function createSortable() {
  if (!props.open || !list.value || sortable || disposed) return
  motionPreference = window.matchMedia('(prefers-reduced-motion: reduce)')
  motionPreference.addEventListener('change', updateMotion)
  motionObserver = new MutationObserver(updateMotion)
  motionObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-motion'] })
  sortable = Sortable.create(list.value, {
    animation: motionEnabled() ? 180 : 0,
    easing: 'cubic-bezier(0.22, 1, 0.36, 1)',
    handle: '[data-order-handle]',
    draggable: '[data-order-item]',
    dataIdAttr: 'data-group-id',
    ghostClass: 'proxy-group-order-placeholder',
    chosenClass: 'proxy-group-order-chosen',
    fallbackClass: 'proxy-group-order-floating',
    forceFallback: true,
    // Keep the visual clone inside the dialog and its scroll container.
    fallbackOnBody: false,
    fallbackTolerance: 4,
    direction: 'vertical',
    scroll: true,
    scrollSensitivity: 64,
    scrollSpeed: 12,
    onStart: (event: OrderDragEvent) => {
      dragging.value = true
      grabbedKey.value = null
      const ghost = Sortable.ghost as HTMLElement | undefined
      if (ghost) {
        ghost.setAttribute('aria-hidden', 'true')
        ghost.removeAttribute('data-group-id')
        ghost.inert = true
      }
      const group = draft.value.find(item => keyOf(item) === event.item.dataset.groupId)
      if (group) announcement.value = `已抓取 ${group.name}，拖动到新位置后松开`
    },
    onChange: (event: OrderDragEvent) => {
      const index = event.newDraggableIndex ?? event.newIndex
      const group = draft.value.find(item => keyOf(item) === event.item.dataset.groupId)
      syncVisiblePositions()
      if (group && index !== undefined) {
        const ghost = Sortable.ghost as HTMLElement | undefined
        const ghostNumber = ghost?.querySelector<HTMLElement>('[data-order-position]')
        if (ghostNumber) ghostNumber.textContent = String(index + 1).padStart(2, '0')
        announcement.value = `${group.name}，移到${position(index)}`
      }
    },
    onEnd: (event: OrderDragEvent) => {
      const oldIndex = event.oldDraggableIndex ?? event.oldIndex
      const newIndex = event.newDraggableIndex ?? event.newIndex
      dragging.value = false
      if (oldIndex === undefined || newIndex === undefined) return
      const group = draft.value[oldIndex]
      if (!group) return
      if (oldIndex !== newIndex) {
        // Sortable has moved the node. Restore Vue's previous DOM before letting
        // its keyed render apply the new order, otherwise it can move twice.
        event.item.remove()
        event.from.insertBefore(event.item, event.from.children[oldIndex] || null)
        // Restore the live drag counters too, so Vue starts from its own values.
        syncVisiblePositions()
        const next = [...draft.value]
        next.splice(oldIndex, 1)
        next.splice(newIndex, 0, group)
        draft.value = next
      } else syncVisiblePositions()
      announcePosition(group, newIndex)
      nextTick(() => focusRow(keyOf(group)))
    }
  })
}

async function save() {
  if (!changed.value || locked.value) return
  const activeSession = session
  const ordered = [...draft.value]
  saving.value = true
  grabbedKey.value = null
  sortable?.option('disabled', true)
  try {
    await props.saveOrder(ordered)
    if (disposed || !props.open || activeSession !== session) return
    emit('saved', ordered)
    emit('update:open', false)
  } catch (error) {
    if (disposed || !props.open || activeSession !== session) return
    const message = isAxiosError(error) ? error.response?.data?.message
      : error instanceof Error ? error.message : undefined
    notify.error('顺序保存失败', message || '调整已保留，请重试。')
    announcement.value = '顺序保存失败，调整已保留，可以重试'
  } finally {
    if (!disposed && activeSession === session) {
      saving.value = false
      sortable?.option('disabled', false)
    }
  }
}

watch(() => props.open, (open) => {
  session += 1
  saving.value = false
  grabbedKey.value = null
  if (open) {
    draft.value = [...props.groups]
    initialOrder.value = draft.value.map(keyOf)
    announcement.value = `共 ${draft.value.length} 个策略组，拖动或使用上下按钮调整顺序`
    nextTick(createSortable)
  } else {
    destroySortable()
  }
}, { immediate: true })
watch(list, (element) => { if (element) createSortable() }, { flush: 'post' })
onBeforeUnmount(() => {
  disposed = true
  session += 1
  destroySortable()
})
</script>

<template>
  <Dialog :open="open" @update:open="requestOpen">
    <DialogContent
      class="proxy-group-order-dialog"
      :show-close-button="false"
      :aria-busy="saving"
      @escape-key-down="handleEscape"
      @interact-outside="event => { if (locked) event.preventDefault() }"
    >
      <header class="proxy-group-order-header">
        <div class="min-w-0">
          <DialogTitle class="font-display text-2xl font-medium">调整策略组顺序</DialogTitle>
          <DialogDescription class="mt-2 text-xs">拖动快速调整，↑ ↓ 逐项微调。</DialogDescription>
        </div>
        <Button type="button" variant="ghost" size="icon" class="proxy-group-order-icon text-muted-foreground" aria-label="关闭排序" :disabled="locked" @click="requestOpen(false)"><X class="size-4" /></Button>
      </header>

      <div class="proxy-group-order-scroll">
        <p id="proxy-group-order-keyboard-hint" class="cf-sr">在拖动手柄上按空格抓取，使用上下方向键移动，再按空格或 Esc 放下。</p>
        <ol ref="list" class="proxy-group-order-list" data-order-list aria-label="策略组顺序">
          <li
            v-for="(group, index) in draft"
            :key="keyOf(group)"
            :data-group-id="keyOf(group)"
            data-order-item
            class="proxy-group-order-row"
            :class="{ 'proxy-group-order-grabbed': grabbedKey === keyOf(group) }"
          >
            <Button
              type="button" variant="ghost" size="icon"
              class="proxy-group-order-icon proxy-group-order-handle"
              data-order-handle data-order-action="handle"
              :aria-label="`拖动 ${group.name}，${position(index)}`"
              aria-describedby="proxy-group-order-keyboard-hint"
              :aria-pressed="grabbedKey === keyOf(group)"
              :disabled="saving"
              @keydown="handleKeydown($event, group, index)"
            ><GripVertical class="size-4" /></Button>
            <span class="proxy-group-order-position num" data-order-position aria-hidden="true">{{ String(index + 1).padStart(2, '0') }}</span>
            <span class="proxy-group-order-name">
              {{ group.name }}
              <span class="proxy-group-order-type">{{ labels[group.type] || group.type }}<span v-if="group.enabled === false"> · 已停用</span></span>
            </span>
            <div class="proxy-group-order-actions">
              <Button type="button" variant="ghost" size="icon" class="proxy-group-order-icon" data-order-action="up" :aria-label="`上移 ${group.name}`" title="上移" :disabled="locked || index === 0" @click="move(keyOf(group), index - 1, 'up')"><ArrowUp class="size-4" /></Button>
              <Button type="button" variant="ghost" size="icon" class="proxy-group-order-icon" data-order-action="down" :aria-label="`下移 ${group.name}`" title="下移" :disabled="locked || index === draft.length - 1" @click="move(keyOf(group), index + 1, 'down')"><ArrowDown class="size-4" /></Button>
              <DropdownMenu>
                <DropdownMenuTrigger as-child>
                  <Button type="button" variant="ghost" size="icon" class="proxy-group-order-icon" data-order-action="more" :aria-label="`更多操作 ${group.name}`" title="更多操作" :disabled="locked"><Ellipsis class="size-4" /></Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" @close-auto-focus="keepMenuFocus($event, keyOf(group))">
                  <DropdownMenuItem class="min-h-11" :disabled="locked || index === 0" @select="move(keyOf(group), 0, 'more')"><ArrowUpToLine class="size-4" />置顶</DropdownMenuItem>
                  <DropdownMenuItem class="min-h-11" :disabled="locked || index === draft.length - 1" @select="move(keyOf(group), draft.length - 1, 'more')"><ArrowDownToLine class="size-4" />置底</DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            </div>
          </li>
        </ol>
        <p v-if="!draft.length" class="py-10 text-center text-sm text-muted-foreground">暂无可排序的策略组</p>
      </div>

      <footer class="proxy-group-order-footer">
        <span class="text-xs text-muted-foreground">共 <span class="num">{{ draft.length }}</span> 个策略组<span v-if="changed" class="hidden sm:inline"> · 顺序已调整</span></span>
        <div class="flex shrink-0 gap-2">
          <Button type="button" variant="outline" class="h-11" :disabled="locked" @click="requestOpen(false)">取消</Button>
          <Button type="button" class="h-11" :disabled="!changed || locked" @click="save"><Loader2 v-if="saving" class="size-4 animate-spin" />{{ saving ? '保存中…' : '保存顺序' }}</Button>
        </div>
      </footer>
      <p class="cf-sr" aria-live="polite" aria-atomic="true">{{ announcement }}</p>
    </DialogContent>
  </Dialog>
</template>

<style scoped>
:global(.proxy-group-order-dialog) {
  display: flex;
  flex-direction: column;
  width: min(640px, calc(100vw - 32px));
  max-width: 640px;
  max-height: min(760px, calc(100dvh - 48px));
  overflow: hidden;
  gap: 0;
  padding: 0;
  /* Sortable detects transformed containing blocks, but not CSS translate. */
  transform: translateZ(0);
}
.proxy-group-order-header {
  display: flex;
  flex: none;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  padding: 24px;
  border-bottom: 1px solid var(--border);
}
.proxy-group-order-scroll { min-height: 0; overflow-y: auto; overscroll-behavior: contain; padding: 8px 12px; }
.proxy-group-order-list { margin: 0; padding: 0; list-style: none; }
.proxy-group-order-row {
  position: relative;
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 68px;
  padding: 8px 4px;
  border-radius: var(--radius-md);
  background: var(--card);
}
.proxy-group-order-row:hover { background: var(--secondary); }
.proxy-group-order-icon { width: var(--cf-touch); height: var(--cf-touch); flex: 0 0 var(--cf-touch); color: var(--muted-foreground); }
.proxy-group-order-icon:focus-visible { outline: 2px solid var(--primary-accent); outline-offset: -2px; box-shadow: none; }
.proxy-group-order-icon:disabled { opacity: 0.35; }
.proxy-group-order-handle { cursor: grab; touch-action: none; }
.proxy-group-order-handle:active { cursor: grabbing; transform: none; }
.proxy-group-order-position { width: 24px; flex: 0 0 24px; color: var(--muted-foreground); font-family: var(--font-mono); font-size: 12px; }
.proxy-group-order-name { flex: 1; min-width: 0; overflow-wrap: anywhere; font-size: 14px; font-weight: 500; }
.proxy-group-order-type { display: block; margin-top: 2px; color: var(--muted-foreground); font-size: 12px; font-weight: 400; }
.proxy-group-order-actions { display: flex; flex: none; align-items: center; }
.proxy-group-order-grabbed { background: var(--primary-soft); box-shadow: inset 3px 0 var(--primary-accent); }
.proxy-group-order-grabbed .proxy-group-order-handle { color: var(--primary-accent); cursor: grabbing; }
.proxy-group-order-placeholder { background: var(--secondary); outline: 1px dashed var(--border-strong); outline-offset: -3px; }
.proxy-group-order-placeholder > * { visibility: hidden; }
.proxy-group-order-floating { background: var(--card); box-shadow: var(--shadow-overlay); outline: 1px solid var(--primary-accent); opacity: 1 !important; }
.proxy-group-order-floating .proxy-group-order-handle { color: var(--primary-accent); }
.proxy-group-order-footer { display: flex; flex: none; align-items: center; justify-content: space-between; gap: 12px; padding: 16px 24px; border-top: 1px solid var(--border); }
@media (max-width: 639px) {
  :global(.proxy-group-order-dialog) { top: 0; left: 0; width: 100%; height: 100dvh; max-height: 100dvh; max-width: none; translate: none; border: 0; border-radius: 0; }
  .proxy-group-order-header { padding: max(16px, env(safe-area-inset-top)) 16px 16px; }
  .proxy-group-order-scroll { flex: 1; padding: 8px; }
  .proxy-group-order-row { gap: 4px; padding: 8px 0; }
  .proxy-group-order-position { width: 20px; flex-basis: 20px; }
  .proxy-group-order-footer { gap: 8px; padding: 12px 16px max(12px, env(safe-area-inset-bottom)); }
}
@media (prefers-reduced-motion: reduce) {
  .proxy-group-order-row { transition-duration: 0.01ms !important; }
}
:global(html[data-motion='off'] .proxy-group-order-row) { transition-duration: 0.01ms !important; }
</style>
