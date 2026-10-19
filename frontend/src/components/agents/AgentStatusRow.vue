<script setup lang="ts">
import { computed, ref, useId } from 'vue'
import { ChevronRight, CircleCheck, CircleDashed, CircleX, Loader2, TriangleAlert } from '@lucide/vue'

const props = defineProps<{
  label: string
  status: string
  tone: 'success' | 'warning' | 'danger' | 'progress' | 'neutral'
}>()
const expanded = ref(false)
const detailsId = `agent-status-${useId()}`
const presentation = computed(() => ({
  success: { icon: CircleCheck, text: 'text-success-accent' },
  warning: { icon: TriangleAlert, text: 'text-warning-accent' },
  danger: { icon: CircleX, text: 'text-destructive-accent' },
  progress: { icon: Loader2, text: 'text-primary-accent' },
  neutral: { icon: CircleDashed, text: 'text-muted-foreground' },
})[props.tone])
</script>

<template>
  <section class="min-w-0 py-1" :aria-label="label" :data-agent-status-row="label" :data-tone="tone">
    <div class="flex min-w-0 items-start gap-1">
      <component
        :is="$slots.default ? 'button' : 'div'"
        :type="$slots.default ? 'button' : undefined"
        :aria-expanded="$slots.default ? expanded : undefined"
        :aria-controls="$slots.default ? detailsId : undefined"
        :aria-label="$slots.default ? `${label}：${status}` : undefined"
        class="flex min-h-11 min-w-0 flex-1 items-center gap-2 rounded-md py-2 text-left text-[13px] font-semibold leading-5 outline-none"
        :class="$slots.default && 'cursor-pointer hover:bg-accent/40 focus-visible:ring-2 focus-visible:ring-ring'"
        @click="$slots.default && (expanded = !expanded)"
      >
        <component :is="presentation.icon" class="size-4 shrink-0" :class="[presentation.text, tone === 'progress' && 'animate-spin motion-reduce:animate-none']" aria-hidden="true" />
        <span class="min-w-0 text-foreground [overflow-wrap:anywhere]">{{ label }}</span>
        <span class="ml-auto max-w-[50%] shrink-0 break-words text-right" :class="presentation.text" aria-live="polite" aria-atomic="true" :aria-label="`${label}：${status}`">{{ status }}</span>
        <ChevronRight v-if="$slots.default" class="size-3.5 shrink-0 text-muted-foreground" :class="expanded && 'rotate-90'" aria-hidden="true" />
      </component>
      <slot name="actions" />
    </div>
    <div v-if="$slots.notice" class="min-w-0 space-y-2 pb-2 text-xs leading-relaxed [overflow-wrap:anywhere]" aria-live="polite" data-status-notice>
      <slot name="notice" />
    </div>
    <div v-if="$slots.default" v-show="expanded" :id="detailsId" class="min-w-0 space-y-3 pb-3 pt-1 text-xs leading-relaxed text-muted-foreground [overflow-wrap:anywhere]" data-status-details>
      <slot />
    </div>
  </section>
</template>
