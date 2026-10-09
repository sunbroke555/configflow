<template>
  <div
    class="h-full min-w-0 rounded-xl border border-border bg-card p-5 max-sm:p-4"
  >
    <div class="flex items-start justify-between gap-2">
      <div class="min-w-0">
        <p class="m-0 break-words text-[13px] leading-5 font-medium text-muted-foreground">
          {{ label }}
        </p>
        <p class="mt-3 mb-0 flex flex-wrap items-baseline gap-1 break-all text-[30px] leading-none font-semibold tracking-[-0.03em] text-foreground">
          <AnimatedNumber :value="value" :precision="precision" instant />
          <span v-if="unit" class="text-[13px] font-medium text-muted-foreground">{{ unit }}</span>
        </p>
      </div>

      <div
        v-if="icon"
        class="flex size-8 shrink-0 items-center justify-center rounded-lg bg-muted"
        :class="iconClass"
      >
        <component :is="icon" class="size-4.5" :stroke-width="2" aria-hidden="true" />
      </div>
    </div>

    <div v-if="hint || $slots.default" class="mt-4 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
      <slot>{{ hint }}</slot>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, type Component } from 'vue'
import AnimatedNumber from './AnimatedNumber.vue'

type Tone = 'primary' | 'success' | 'warning' | 'danger' | 'info'

const props = withDefaults(
  defineProps<{
    label: string
    value: number
    unit?: string
    hint?: string
    icon?: Component
    tone?: Tone
    precision?: number
  }>(),
  { tone: 'primary', precision: 0 }
)


const iconClass = computed(
  () =>
    ({
      primary: 'text-primary-accent',
      success: 'text-success-accent',
      warning: 'text-warning-accent',
      danger: 'text-destructive-accent',
      info: 'text-info-accent'
    })[props.tone]
)
</script>
