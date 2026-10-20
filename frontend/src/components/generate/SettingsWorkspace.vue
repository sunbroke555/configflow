<script setup lang="ts">
import { computed, inject, nextTick, ref, watch } from 'vue'
import { useEventListener, useMediaQuery } from '@vueuse/core'
import { matchedRouteKey, onBeforeRouteLeave } from 'vue-router'
import { ArrowLeft, LoaderCircle, Maximize2, Minimize2, Save } from '@lucide/vue'
import FieldHelp from '@/components/common/FieldHelp.vue'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import { confirm } from '@/lib/feedback'

const props = withDefaults(defineProps<{
  open: boolean
  title: string
  description?: string
  dirty?: boolean
  busy?: boolean
  saveDisabled?: boolean
  saveLabel?: string
  backLabel?: string
  size?: 'wide' | 'form' | 'compact'
  inline?: boolean
}>(), {
  description: '',
  dirty: false,
  busy: false,
  saveDisabled: false,
  saveLabel: '保存',
  backLabel: '返回生成工作台',
  size: 'wide',
})

const emit = defineEmits<{
  'update:open': [value: boolean]
  save: []
}>()

const autoFullscreen = useMediaQuery('(max-width: 1023px), (max-height: 760px)')
const expanded = ref(false)
const fullscreen = computed(() => autoFullscreen.value || expanded.value)
const confirmingClose = ref(false)
const toolbar = ref<HTMLElement | null>(null)
let opener: HTMLElement | null = null
let openSession = 0
let pendingClose: Promise<boolean> | null = null

const windowedStyle = computed(() => fullscreen.value ? undefined : {
  maxWidth: `${{ wide: 1400, form: 1100, compact: 680 }[props.size]}px`,
  height: props.size === 'compact' ? 'auto' : 'min(960px, calc(100dvh - 4rem))',
  maxHeight: 'calc(100dvh - 4rem)',
})

watch(() => props.open, open => {
  openSession++
  if (!open) return
  expanded.value = false
  opener = document.activeElement instanceof HTMLElement ? document.activeElement : null
}, { immediate: true, flush: 'sync' })

async function canClose(): Promise<boolean> {
  if (!props.open) return true
  if (props.busy) return false
  if (!props.dirty) return true
  if (pendingClose) return pendingClose

  const session = openSession
  const activeElement = document.activeElement
  const editorFocus = activeElement instanceof HTMLElement && toolbar.value?.parentElement?.contains(activeElement)
    ? activeElement
    : toolbar.value?.querySelector<HTMLButtonElement>('button')
  confirmingClose.value = true
  pendingClose = confirm('尚有未保存的修改。返回后，这些修改将不会保留。', {
    title: '放弃未保存的修改？',
    confirmText: '放弃修改',
    cancelText: '继续编辑',
  }).then(discard => {
    // A save or profile change may have closed/reopened the editor while awaiting confirmation.
    return discard && !props.busy && session === openSession
  }).finally(() => {
    pendingClose = null
    confirmingClose.value = false
    // ConfirmHost is controlled programmatically, so it has no DialogTrigger to
    // return focus to when the user chooses to keep editing.
    void nextTick(() => requestAnimationFrame(() => {
      if (props.open && session === openSession && editorFocus?.isConnected) {
        editorFocus.focus({ preventScroll: true })
      }
    }))
  })
  return pendingClose
}

async function requestClose() {
  if (!props.open || confirmingClose.value) return
  if (await canClose()) emit('update:open', false)
}

// A settings workspace is also mounted independently in previews and tests.
// Register only when Vue Router supplies an active matched page.
if (inject(matchedRouteKey, undefined)?.value) {
  onBeforeRouteLeave(() => canClose())
}

function onDismiss(event: Event) {
  event.preventDefault()
  void requestClose()
}

function onPointerOutside(event: CustomEvent<{ originalEvent: PointerEvent }>) {
  const pointer = event.detail.originalEvent
  // Preserve the modal's existing right-click behavior.
  if (pointer.button !== 0 || pointer.ctrlKey) {
    event.preventDefault()
    return
  }
  onDismiss(event)
}

function restoreFocus(event: Event) {
  event.preventDefault()
  const target = opener
  const session = openSession
  // Let the confirmation dialog finish its own focus restoration first.
  void nextTick(() => requestAnimationFrame(() => {
    if (!props.open && session === openSession && target?.isConnected) {
      target.focus({ preventScroll: true })
    }
  }))
}

useEventListener('beforeunload', (event: BeforeUnloadEvent) => {
  if (!props.open || !props.dirty) return
  event.preventDefault()
  event.returnValue = ''
})
</script>

<template>
  <div v-if="inline && open" class="min-w-0"><slot /><div v-if="$slots.footer" class="mt-5 border-t border-border pt-4"><slot name="footer" :disabled="busy || saveDisabled" /></div></div>
  <Dialog v-else :open="open" @update:open="value => !value && requestClose()">
    <DialogContent
      :show-close-button="false"
      v-bind="description ? {} : { 'aria-describedby': undefined }"
      :data-fullscreen="fullscreen"
      :data-confirming-close="confirmingClose"
      data-settings-workspace
      class="flex min-h-0 min-w-0 flex-col gap-0 overflow-hidden p-0 max-sm:p-0"
      :class="fullscreen
        ? 'inset-0 top-0 left-0 h-dvh max-h-dvh w-full max-w-none translate-x-0 translate-y-0 rounded-none border-0'
        : 'w-[calc(100%-4rem)]'"
      :style="windowedStyle"
      @escape-key-down="onDismiss"
      @pointer-down-outside="onPointerOutside"
      @close-auto-focus="restoreFocus"
    >
      <header ref="toolbar" class="flex min-w-0 shrink-0 items-center gap-2 border-b border-border px-3 py-2 sm:px-5">
        <Button
          variant="ghost"
          class="size-11 shrink-0"
          :aria-label="backLabel"
          :title="backLabel"
          :disabled="busy || confirmingClose"
          @click="requestClose"
        >
          <ArrowLeft class="size-4" aria-hidden="true" />
        </Button>
        <DialogTitle class="min-w-0 truncate text-base font-semibold" :title="title">{{ title }}</DialogTitle>
        <div v-if="description" class="workspace-help shrink-0">
          <FieldHelp :label="title" :text="description" />
          <DialogDescription class="sr-only">{{ description }}</DialogDescription>
        </div>
        <span v-if="dirty" class="hidden shrink-0 text-xs text-muted-foreground sm:block">未保存</span>
        <div class="ml-auto flex shrink-0 items-center gap-2">
          <Button
            v-if="!autoFullscreen"
            variant="ghost"
            class="size-11"
            :aria-label="expanded ? '还原窗口' : '全屏编辑'"
            :title="expanded ? '还原窗口' : '全屏编辑'"
            :aria-pressed="expanded"
            @click="expanded = !expanded"
          >
            <Minimize2 v-if="expanded" class="size-4" aria-hidden="true" />
            <Maximize2 v-else class="size-4" aria-hidden="true" />
          </Button>
          <Button v-if="!$slots.footer" class="min-h-11 gap-2 px-4" :disabled="busy || saveDisabled || confirmingClose" @click="emit('save')">
            <LoaderCircle v-if="busy" class="size-4 animate-spin" aria-hidden="true" />
            <Save v-else class="size-4" aria-hidden="true" />
            {{ busy ? '保存中…' : saveLabel }}
          </Button>
        </div>
      </header>
      <div class="workspace-content flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
        <slot />
      </div>
      <div v-if="$slots.status" class="workspace-status min-w-0 shrink-0 border-t border-border bg-background/30 px-4 py-2 sm:px-5">
        <slot name="status" />
      </div>
      <div v-if="$slots.footer" class="min-w-0 shrink-0 border-t border-border bg-card px-4 py-3 sm:px-5">
        <slot name="footer" :disabled="busy || saveDisabled || confirmingClose" />
      </div>
    </DialogContent>
  </Dialog>
</template>

<style scoped>
.workspace-help :deep(.field-help-trigger) {
  width: var(--cf-touch, 44px);
  height: var(--cf-touch, 44px);
}

@media (max-height: 600px) {
  .workspace-content {
    overflow-y: auto;
    overscroll-behavior: contain;
  }

  /* Let nested editor navigation scroll out of view instead of crushing its fields. */
  .workspace-content > :deep(*) {
    min-height: 26rem;
    flex-shrink: 0;
  }

  .workspace-status {
    max-height: 15dvh;
    overflow-y: auto;
    overscroll-behavior: contain;
  }
}

/* The shared host may teleport before this editor. Elevate both confirmation
   layers only while this workspace is awaiting a discard decision. */
:global(body:has([data-settings-workspace][data-confirming-close='true']) [data-slot='alert-dialog-overlay']),
:global(body:has([data-settings-workspace][data-confirming-close='true']) [data-slot='alert-dialog-content']) {
  z-index: 60;
}
</style>
