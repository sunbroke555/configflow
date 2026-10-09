<template>
  <div
    class="flex h-9 shrink-0 items-center gap-0.5 rounded-lg border border-border bg-muted p-0.5"
    role="group"
    aria-label="视图切换"
  >
    <button
      v-for="option in OPTIONS"
      :key="option.value"
      type="button"
      :aria-pressed="modelValue === option.value"
      :aria-label="option.label"
      :title="option.label"
      :class="[
        'grid size-8 cursor-pointer place-items-center rounded-md border-0 p-0 transition-colors focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none motion-reduce:transition-none',
        modelValue === option.value ? 'bg-card text-foreground' : 'bg-transparent text-muted-foreground hover:bg-accent hover:text-foreground'
      ]"
      @click="$emit('update:modelValue', option.value)"
    >
      <component :is="option.icon" class="size-4" :stroke-width="2" aria-hidden="true" />
    </button>
  </div>
</template>

<script setup lang="ts">
import { LayoutGrid, List } from '@lucide/vue'

export type ViewMode = 'list' | 'card'

defineProps<{ modelValue: ViewMode }>()
defineEmits<{ (e: 'update:modelValue', value: ViewMode): void }>()

const OPTIONS = [
  { value: 'list' as const, label: '列表视图', icon: List },
  { value: 'card' as const, label: '卡片视图', icon: LayoutGrid }
]
</script>
