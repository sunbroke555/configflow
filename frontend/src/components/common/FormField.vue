<template>
  <div class="cf-form-field flex min-w-0 flex-col gap-1.5">
    <div v-if="label" class="cf-field-heading flex min-w-0 flex-wrap items-center gap-1">
      <Label :for="htmlFor" class="min-w-0 break-words">{{ label }}</Label>
      <span v-if="helpText" class="cf-compact-help">
        <FieldHelp :label="label" :text="helpText" />
      </span>
      <a v-if="documentation" :href="documentation.href" target="_blank" rel="noopener noreferrer" class="inline-flex min-h-6 items-center gap-1 rounded-sm px-1 text-xs text-primary-accent underline-offset-4 outline-none hover:underline focus-visible:ring-2 focus-visible:ring-primary-accent max-sm:min-h-11 pointer-coarse:min-h-11" :aria-label="`${documentation.label}（官方文档，新窗口打开）`">
        {{ documentation.label }}<ExternalLink class="size-3" aria-hidden="true" />
      </a>
    </div>
    <slot />
    <p v-if="hint && !hintInHelp" class="m-0 text-[12px] leading-relaxed whitespace-pre-line text-muted-foreground">
      {{ hint }}
    </p>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useMediaQuery } from '@vueuse/core'
import { Label } from '@/components/ui/label'
import { ExternalLink } from '@lucide/vue'
import FieldHelp from './FieldHelp.vue'

const props = defineProps<{
  label?: string
  hint?: string
  help?: string
  /** 警告、当前状态等必须直接可见的提示。无标题时也始终显示 hint。 */
  persistentHint?: boolean
  htmlFor?: string
  documentation?: { href: string; label: string }
}>()

const mobile = useMediaQuery('(max-width: 767px)')
const hintInHelp = computed(() => mobile.value && !!props.label && !props.persistentHint)
const helpText = computed(() => [...new Set([props.help, hintInHelp.value ? props.hint : ''].filter(Boolean))].join('\n\n'))
</script>

<style>
@media (max-width: 767px) {
  /* 同一网格有问号时才对齐标签行，普通表单不额外占用高度。 */
  .grid:has(> .cf-form-field > .cf-field-heading > .cf-compact-help) > .cf-form-field > .cf-field-heading {
    min-height: 32px;
  }
}
</style>
