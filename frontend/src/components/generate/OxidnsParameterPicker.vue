<script setup lang="ts">
import { ref, watch } from 'vue'
import { Plus } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from '@/components/ui/command'
import type { OxidnsSchema } from '@/lib/oxidnsSchema'
import FieldHelp from '@/components/common/FieldHelp.vue'

const props = withDefaults(defineProps<{
  fields: Array<{ key: string; schema: OxidnsSchema }>
  label: string
  disabled?: boolean
  buttonText?: string
  scopeTitle?: string
  compactMobile?: boolean
  contentClass?: string
}>(), { buttonText: '添加参数' })

const emit = defineEmits<{ select: [key: string] }>()
const open = ref(false)

watch(() => [props.disabled, props.fields.length], () => {
  if (props.disabled || !props.fields.length) open.value = false
})

function select(key: string) {
  if (!open.value || props.disabled || !props.fields.some(field => field.key === key)) return
  open.value = false
  emit('select', key)
}
</script>

<template>
  <Popover v-model:open="open">
    <PopoverTrigger as-child>
      <Button
        type="button"
        variant="outline"
        size="sm"
        class="min-h-11 gap-1.5 md:min-h-9 [@media(pointer:coarse)]:min-h-11"
        :class="compactMobile ? 'max-md:size-11 max-md:p-0' : ''"
        :title="compactMobile ? buttonText : undefined"
        :aria-label="`${label} ${buttonText}`"
        :disabled="disabled || !fields.length"
      >
        <Plus class="size-3.5" aria-hidden="true" />
        <span :class="compactMobile ? 'max-md:hidden' : ''">{{ buttonText }}</span>
      </Button>
    </PopoverTrigger>
    <PopoverContent
      align="start"
      :collision-padding="12"
      class="w-[28rem] max-w-[min(calc(100vw-1.5rem),var(--reka-popover-content-available-width))] overflow-hidden p-0 shadow-overlay"
      :class="contentClass"
      :aria-label="`${label} 参数选择`"
    >
      <div v-if="scopeTitle" class="flex h-9 min-w-0 items-center gap-1 border-b border-border px-3 text-xs text-muted-foreground">
        <span class="shrink-0">添加到</span>
        <span class="truncate font-medium text-foreground" :title="scopeTitle">{{ scopeTitle }}</span>
      </div>
      <Command v-if="open" class="[&_[data-slot=command-input-wrapper]]:min-h-11 md:[&_[data-slot=command-input-wrapper]]:min-h-9 [@media(pointer:coarse)]:[&_[data-slot=command-input-wrapper]]:min-h-11">
        <CommandInput :aria-label="`${label} 搜索参数`" placeholder="搜索名称、参数或用途…" />
        <CommandList
          :aria-label="`${label} 可添加参数`"
          class="p-1"
          :class="scopeTitle
            ? 'max-h-[min(20rem,calc(var(--reka-popover-content-available-height)-5.25rem))]'
            : 'max-h-[min(20rem,calc(var(--reka-popover-content-available-height)-3rem))]'"
        >
          <CommandEmpty class="px-3 py-6 text-muted-foreground">没有匹配的参数</CommandEmpty>
          <CommandGroup class="p-0">
            <CommandItem
              v-for="field in fields"
              :key="field.key"
              :value="field.key"
              :disabled="disabled"
              class="min-h-11 cursor-pointer flex-col items-stretch gap-1 px-3 py-2.5"
              @select="select(field.key)"
            >
              <span class="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-0.5">
                <span class="text-sm font-medium">{{ field.schema.label || field.key }}</span>
                <code v-if="field.schema.label && !field.schema.label.includes(field.key)" class="break-all text-xs text-muted-foreground">{{ field.key }}</code>
                <FieldHelp v-if="field.schema.hint" :label="`${field.schema.label || field.key}用途`" :text="field.schema.help" content-class="z-[80]" />
              </span>
              <span class="whitespace-pre-line break-words text-xs leading-relaxed text-muted-foreground">{{ field.schema.hint || field.schema.help }}</span>
              <span v-if="field.schema.hint" class="hidden" aria-hidden="true">{{ field.schema.help }}</span>
            </CommandItem>
          </CommandGroup>
        </CommandList>
      </Command>
    </PopoverContent>
  </Popover>
</template>
