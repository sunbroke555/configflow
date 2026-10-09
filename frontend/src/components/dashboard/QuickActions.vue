<template>
  <SectionCard title="快速操作" role="region" aria-label="快速操作">
    <div class="flex flex-col gap-2">
      <Button
        v-for="action in actions"
        :key="action.label"
        :variant="action.primary ? 'default' : 'outline'"
        class="h-auto min-h-10 w-full justify-start px-3 py-2 text-[13px]"
        :disabled="action.disabled"
        @click="$emit('run', action)"
      >
        <component :is="iconOf(action.icon)" class="size-4 shrink-0" :stroke-width="2" aria-hidden="true" />
        <span class="min-w-0 flex-1 whitespace-normal text-left">{{ action.label }}</span>
        <ChevronRight class="size-4 shrink-0 text-current opacity-50" aria-hidden="true" />
      </Button>
    </div>
  </SectionCard>
</template>

<script setup lang="ts">
import { ChevronRight } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import SectionCard from '@/components/common/SectionCard.vue'
import { iconOf } from '@/lib/icons'

export interface QuickAction {
  label: string
  icon: string
  /** 一个视图只有一个主操作 */
  primary?: boolean
  disabled?: boolean
  route?: string
}

defineProps<{ actions: QuickAction[] }>()
defineEmits<{ (e: 'run', action: QuickAction): void }>()
</script>
