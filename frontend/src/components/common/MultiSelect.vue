<template>
  <Popover v-model:open="open">
    <PopoverTrigger as-child>
      <button
        :id="triggerId"
        type="button"
        role="combobox"
        :aria-expanded="open"
        class="flex min-h-9 w-full cursor-pointer items-center gap-2 rounded-lg border border-input bg-background px-3 py-1.5 text-left text-[13px] transition-colors hover:border-border-strong focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none motion-reduce:transition-none"
      >
        <span v-if="!selected.length" class="min-w-0 flex-1 whitespace-normal break-words text-muted-foreground">{{ placeholder }}</span>
        <span v-else class="flex min-w-0 flex-1 flex-wrap gap-1">
          <!-- 选中项超过 maxVisible 时折叠成计数，避免触发器高度失控 -->
          <Badge
            v-for="option in visibleSelected"
            :key="option.value"
            variant="secondary"
            class="max-w-full gap-1 whitespace-normal break-all text-left"
          >
            {{ option.label }}
          </Badge>
          <Badge v-if="hiddenCount" variant="outline">+{{ hiddenCount }}</Badge>
        </span>
        <ChevronsUpDown class="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
      </button>
    </PopoverTrigger>

    <PopoverContent class="w-(--reka-popper-anchor-width) p-0" align="start">
      <Command :filter-function="filterOptions">
        <CommandInput :placeholder="searchPlaceholder" />
        <!-- 批量操作：无搜索词时作用于全部选项，有搜索词时只作用于匹配项 -->
        <CommandSearchState v-if="options.length > 1" v-slot="{ search }">
          <div class="flex flex-wrap items-center gap-1 border-b px-2 py-1.5">
            <Button
              type="button"
              variant="ghost"
              size="xs"
              :disabled="!matchedOptions(search).length"
              @click="toggleMatched(search)"
            >
              {{ bulkLabel(search) }}
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="xs"
              class="ml-auto"
              :disabled="!modelValue.length"
              @click="emit('update:modelValue', [])"
            >
              清空已选
            </Button>
          </div>
        </CommandSearchState>
        <CommandList>
          <CommandEmpty>没有找到可选项，请调整搜索条件或先添加对应资源。</CommandEmpty>
          <CommandGroup>
            <CommandItem
              v-for="option in options"
              :key="option.value"
              :value="option"
              @select="toggle(option.value)"
            >
              <Check
                class="size-4"
                :class="modelValue.includes(option.value) ? 'opacity-100 text-primary-accent' : 'opacity-0'"
                aria-hidden="true"
              />
              <span class="min-w-0 whitespace-normal break-all">{{ option.label }}</span>
            </CommandItem>
          </CommandGroup>
        </CommandList>
      </Command>
    </PopoverContent>
  </Popover>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { Check, ChevronsUpDown } from '@lucide/vue'
import { useFilter } from 'reka-ui'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import CommandSearchState from '@/components/common/CommandSearchState.vue'
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList
} from '@/components/ui/command'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'

export interface MultiSelectOption {
  value: string
  label: string
}

const props = withDefaults(
  defineProps<{
    modelValue: string[]
    options: MultiSelectOption[]
    triggerId?: string
    placeholder?: string
    searchPlaceholder?: string
    maxVisible?: number
  }>(),
  { placeholder: '请选择', searchPlaceholder: '搜索…', maxVisible: 6 }
)

const emit = defineEmits<{ (e: 'update:modelValue', value: string[]): void }>()

const open = ref(false)

const selected = computed(() =>
  props.modelValue
    .map(value => props.options.find(option => option.value === value))
    .filter((option): option is MultiSelectOption => Boolean(option))
)

const visibleSelected = computed(() => selected.value.slice(0, props.maxVisible))
const hiddenCount = computed(() => Math.max(0, selected.value.length - props.maxVisible))

const toggle = (value: string): void => {
  const next = props.modelValue.includes(value)
    ? props.modelValue.filter(item => item !== value)
    : [...props.modelValue, value]
  emit('update:modelValue', next)
}

/* 与 Command 内部的过滤保持一致：按显示文字包含匹配，忽略大小写和变音符号 */
const { contains } = useFilter({ sensitivity: 'base' })

const matchedOptions = (search: string): MultiSelectOption[] =>
  search ? props.options.filter(option => contains(option.label, search)) : props.options

const allMatchedSelected = (search: string): boolean => {
  const matched = matchedOptions(search)
  return matched.length > 0 && matched.every(option => props.modelValue.includes(option.value))
}

const bulkLabel = (search: string): string => {
  const count = matchedOptions(search).length
  if (!search) return allMatchedSelected(search) ? '取消全选' : `全选 ${count} 项`
  return allMatchedSelected(search) ? `取消选中 ${count} 个结果` : `选中 ${count} 个结果`
}

/* 全部已选时整体取消，否则补选缺少的项；保留不在匹配范围内的已选项及原有顺序 */
const toggleMatched = (search: string): void => {
  const matchedValues = matchedOptions(search).map(option => option.value)
  if (allMatchedSelected(search)) {
    emit('update:modelValue', props.modelValue.filter(value => !matchedValues.includes(value)))
    return
  }
  const missing = matchedValues.filter(value => !props.modelValue.includes(value))
  emit('update:modelValue', [...props.modelValue, ...missing])
}

/* Command 默认按 value 的字符串形式过滤；这里传的是对象，改为按 label 匹配 */
const filterOptions = (list: unknown[], term: string) => {
  const q = term.trim().toLowerCase()
  if (!q) return list
  return (list as MultiSelectOption[]).filter(option =>
    option.label.toLowerCase().includes(q)
  )
}
</script>
