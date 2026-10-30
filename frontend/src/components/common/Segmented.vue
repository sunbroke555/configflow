<template>
  <div
    ref="root"
    role="radiogroup"
    :aria-label="label"
    :class="cn('cf-segmented relative inline-flex rounded-xl border border-border bg-secondary p-[3px]', block && 'flex w-full', props.class)"
  >
    <div class="cf-segmented-options relative inline-flex w-max min-w-full shrink-0">
      <span
        class="cf-segmented-thumb pointer-events-none absolute inset-y-0 rounded-[9px] bg-card shadow-[0_2px_10px_-4px_rgb(0_0_0/40%),0_0_0_1px_var(--border-strong)] transition-[left,width] duration-350 ease-(--ease-flow)"
        :style="thumb"
        aria-hidden="true"
      />
      <button
        v-for="option in options"
        :key="option.value"
        data-segment
        type="button"
        role="radio"
        :aria-checked="option.value === modelValue"
        :disabled="disabled || option.disabled"
        :class="cn(
          'relative z-1 h-[30px] cursor-pointer rounded-[9px] px-3.5 text-[13px] whitespace-nowrap transition-colors duration-250 focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-50 max-sm:px-3',
          block && 'flex-1 px-2',
          option.value === modelValue ? 'text-foreground' : 'text-muted-foreground hover:text-foreground'
        )"
        @click="emit('update:modelValue', option.value)"
      >
        {{ option.label }}
      </button>
    </div>
  </div>
</template>

<script setup lang="ts" generic="T extends string">
import { nextTick, onMounted, onUnmounted, ref, watch, type HTMLAttributes } from 'vue'
import { cn } from '@/lib/utils'

/** 分段控件：选中项下方有一块滑动的底，用于「定一个参数」的单选 */
const props = defineProps<{
  modelValue: T
  options: Array<{ value: T; label: string; disabled?: boolean }>
  label?: string
  block?: boolean
  disabled?: boolean
  class?: HTMLAttributes['class']
}>()
const emit = defineEmits<{ (e: 'update:modelValue', value: T): void }>()

const root = ref<HTMLElement | null>(null)
const thumb = ref<Record<string, string>>({ left: '0px', width: '0px' })

const place = () => {
  const index = props.options.findIndex(o => o.value === props.modelValue)
  // v-for 收集的模板 ref 数组不保证与选项同序，按 DOM 顺序取对应按钮
  const el = index < 0 ? undefined : root.value?.querySelectorAll<HTMLButtonElement>('[data-segment]')[index]
  // A value outside the options (e.g. a handwritten custom state) selects nothing.
  if (!el) {
    thumb.value = { left: '0px', width: '0px', opacity: '0' }
    return
  }
  thumb.value = { left: `${el.offsetLeft}px`, width: `${el.offsetWidth}px`, opacity: '1' }
}

let observer: ResizeObserver | null = null
onMounted(() => {
  nextTick(place)
  observer = new ResizeObserver(place)
  if (root.value) observer.observe(root.value)
})
onUnmounted(() => observer?.disconnect())
watch(() => [props.modelValue, props.options], () => nextTick(place), { deep: true })
</script>

<style scoped>
@media (width < 640px) {
  .cf-segmented {
    border: 0;
    background: transparent;
    padding: calc((var(--cf-touch) - var(--cf-choice-track)) / 2) 0;
  }

  .cf-segmented-options {
    height: var(--cf-choice-track);
    padding-inline: 3px;
    border-radius: var(--radius-xl);
    background: var(--secondary);
    box-shadow: inset 0 0 0 1px var(--border);
  }

  /* 完整按钮仍占 44px；外层留出透明区域，横滑时也不会裁掉触区。 */
  [data-segment] {
    flex-shrink: 0;
    height: var(--cf-touch);
    min-width: var(--cf-touch);
    margin-block: calc((var(--cf-choice-track) - var(--cf-touch)) / 2);
  }

  [data-segment]:focus-visible {
    outline: 2px solid var(--ring);
    outline-offset: -2px;
    box-shadow: none;
  }

  .cf-segmented-thumb {
    top: calc((var(--cf-choice-track) - var(--cf-choice-selected)) / 2);
    bottom: calc((var(--cf-choice-track) - var(--cf-choice-selected)) / 2);
  }
}
</style>
