<template>
  <div
    class="mb-4 flex flex-wrap items-center gap-3 rounded-xl border border-border bg-card p-3"
  >
    <div v-if="searchable" class="relative min-w-0 flex-[1_1_240px]">
      <Search
        class="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground"
        aria-hidden="true"
      />
      <Input
        :model-value="search"
        :placeholder="placeholder"
        :aria-label="placeholder"
        class="h-9 bg-background pl-8 text-[13px]"
        @update:model-value="value => emit('update:search', String(value))"
      />
    </div>

    <slot name="filters" />

    <div v-if="$slots.actions" class="ml-auto flex min-w-0 max-w-full flex-wrap items-center gap-2">
      <slot name="actions" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { Search } from '@lucide/vue'
import { Input } from '@/components/ui/input'

withDefaults(
  defineProps<{ search?: string; placeholder?: string; searchable?: boolean }>(),
  { search: '', placeholder: '搜索…', searchable: true }
)

const emit = defineEmits<{ 'update:search': [value: string] }>()
</script>
