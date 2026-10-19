<template>
  <div
    class="cf-view-toggle flex h-9 max-md:h-auto shrink-0 items-center gap-0.5 rounded-lg border border-border bg-muted p-0.5"
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
        'grid size-8 max-md:size-11 cursor-pointer place-items-center rounded-md border p-0 transition-colors focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none motion-reduce:transition-none dark:border-0',
        modelValue === option.value ? 'border-primary-accent bg-primary-soft text-primary-accent dark:bg-card dark:text-foreground' : 'border-transparent bg-transparent text-muted-foreground hover:bg-accent hover:text-foreground'
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

<style scoped>
@media (width < 640px) {
  .cf-view-toggle {
    position: relative;
    isolation: isolate;
    height: var(--cf-touch);
    border: 0;
    padding-block: 0;
    background: transparent;
    overflow: visible;
  }

  .cf-view-toggle::before {
    content: '';
    position: absolute;
    z-index: -1;
    inset-inline: 0;
    top: calc((var(--cf-touch) - var(--cf-choice-track)) / 2);
    height: var(--cf-choice-track);
    border: 1px solid var(--border);
    border-radius: inherit;
    background: var(--muted);
    pointer-events: none;
  }

  /* The button retains its full hit area around the shorter visual surface. */
  .cf-view-toggle button {
    position: relative;
    isolation: isolate;
    width: var(--cf-touch);
    height: var(--cf-touch);
    border: 0;
    background: transparent;
  }

  .cf-view-toggle button::before {
    content: '';
    position: absolute;
    z-index: -1;
    inset-inline: 0;
    top: calc((var(--cf-touch) - var(--cf-choice-selected)) / 2);
    height: var(--cf-choice-selected);
    border: 1px solid transparent;
    border-radius: inherit;
    transition: inherit;
    pointer-events: none;
  }

  .cf-view-toggle button[aria-pressed='true']::before {
    border-color: var(--primary-accent);
    background: var(--primary-soft);
  }

  .cf-view-toggle button[aria-pressed='false']:hover::before {
    background: var(--accent);
  }

  [data-theme='dark'] .cf-view-toggle button[aria-pressed='true']::before {
    border-color: transparent;
    background: var(--card);
  }
}
</style>
