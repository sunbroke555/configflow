<template>
  <div class="oxi-mode-toggle inline-flex h-9 shrink-0 items-stretch rounded-lg border border-border bg-secondary p-0.5 max-sm:h-[50px] pointer-coarse:h-[50px]" role="group" :aria-label="label">
    <div class="oxi-mode-options relative inline-flex w-max min-w-full shrink-0 items-stretch">
      <Button
        v-for="option in options"
        :key="option.value"
        type="button"
        size="sm"
        variant="ghost"
        class="h-full min-w-11 rounded-md border px-2.5 text-xs shadow-none"
        :class="modelValue === option.value
          ? 'border-primary-accent bg-primary-soft text-primary-accent hover:bg-primary-soft dark:border-border-strong dark:bg-card dark:text-foreground dark:hover:bg-card'
          : 'border-transparent text-muted-foreground'"
        :aria-pressed="modelValue === option.value"
        :disabled="disabled || option.disabled"
        @click="emit('update:modelValue', option.value)"
      >{{ option.label }}</Button>
    </div>
  </div>
</template>

<script setup lang="ts" generic="T extends string">
import { Button } from '@/components/ui/button'

defineProps<{
  modelValue: T
  options: Array<{ value: T; label: string; disabled?: boolean }>
  label: string
  disabled?: boolean
}>()
const emit = defineEmits<{ 'update:modelValue': [value: T] }>()
</script>

<style scoped>
@media (width < 640px) {
  .oxi-mode-toggle {
    height: var(--cf-touch);
    max-width: 100%;
    overflow-x: auto;
    border: 0;
    padding: 0;
    background: transparent;
  }

  .oxi-mode-options {
    isolation: isolate;
    height: var(--cf-touch);
    padding-inline: 2px;
  }

  .oxi-mode-options::before {
    content: '';
    position: absolute;
    z-index: -1;
    inset-inline: 0;
    top: calc((var(--cf-touch) - var(--cf-choice-track)) / 2);
    height: var(--cf-choice-track);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    background: var(--secondary);
    pointer-events: none;
  }

  .oxi-mode-options button {
    position: relative;
    isolation: isolate;
    height: var(--cf-touch);
    border: 0;
    background: transparent;
  }

  .oxi-mode-options button::before {
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

  .oxi-mode-options button[aria-pressed='true']::before {
    border-color: var(--primary-accent);
    background: var(--primary-soft);
  }

  .oxi-mode-options button[aria-pressed='false']:hover::before {
    background: var(--accent);
  }

  [data-theme='dark'] .oxi-mode-options button[aria-pressed='true']::before {
    border-color: var(--border-strong);
    background: var(--card);
  }

  .oxi-mode-options button:focus-visible {
    outline: 2px solid var(--ring);
    outline-offset: -2px;
    box-shadow: none;
  }
}
</style>
