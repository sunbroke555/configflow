<script setup lang="ts">
import { ref } from 'vue'
import { CircleHelp } from '@lucide/vue'
import { TooltipContent, TooltipPortal, TooltipProvider, TooltipRoot, TooltipTrigger } from 'reka-ui'
import { cn } from '@/lib/utils'

defineProps<{ label: string; text: string; contentClass?: string }>()

const open = ref(false)

function closeOnEscape(event: KeyboardEvent) {
  if (!open.value) return
  event.preventDefault()
  event.stopPropagation()
  open.value = false
}
</script>

<template>
  <TooltipProvider :delay-duration="250" :skip-delay-duration="100">
    <TooltipRoot v-model:open="open" :disable-closing-trigger="true">
      <TooltipTrigger as-child>
        <button
          type="button"
          class="field-help-trigger inline-flex size-6 shrink-0 items-center justify-center rounded-sm text-muted-foreground outline-none hover:bg-accent hover:text-primary-accent focus-visible:ring-2 focus-visible:ring-primary-accent focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          :aria-label="`${label}说明`"
          :aria-expanded="open"
          @click.stop.prevent="open = !open"
          @pointerdown.stop
          @keydown.esc="closeOnEscape"
          @keydown.enter.stop
          @keydown.space.stop
        >
          <CircleHelp class="size-3.5" aria-hidden="true" />
        </button>
      </TooltipTrigger>
      <TooltipPortal>
        <TooltipContent
          side="top"
          align="start"
          :side-offset="6"
          :collision-padding="16"
          :hide-when-detached="true"
          :aria-label="text"
          data-field-help-content
          :class="cn('field-help-content z-[60] w-max overflow-y-auto overscroll-contain rounded-md border border-border-strong bg-popover px-3 py-2 text-xs font-normal leading-relaxed whitespace-pre-line text-popover-foreground shadow-overlay', contentClass)"
          @escape-key-down="closeOnEscape"
          @click.stop
          @pointerdown.stop
        >
          <span aria-hidden="true">{{ text }}</span>
        </TooltipContent>
      </TooltipPortal>
    </TooltipRoot>
  </TooltipProvider>
</template>

<style>
/* TooltipPortal does not forward this component's scoped attribute to its content. */
.field-help-content {
  max-width: min(20rem, calc(100vw - 2rem), var(--reka-tooltip-content-available-width));
  max-height: min(20rem, var(--reka-tooltip-content-available-height));
  overflow-wrap: anywhere;
}

.cf-compact-help {
  display: inline-flex;
  flex-shrink: 0;
  align-items: center;
}

@media (max-width: 767px) {
  /* 32px 标签行，保留 44px 点击区；相邻控件至少留出 6px 间距。 */
  .cf-compact-help {
    position: relative;
    width: var(--cf-touch, 44px);
    height: 32px;
  }

  .cf-compact-help .field-help-trigger {
    position: absolute;
    top: 50%;
    transform: translateY(-50%);
  }
}

@media (max-width: 767px), (pointer: coarse) {
  .field-help-trigger {
    width: var(--cf-touch, 44px);
    height: var(--cf-touch, 44px);
  }
}
</style>
