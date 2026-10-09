<template>
  <div
    role="region"
    aria-label="关键指标"
    class="mb-5 grid grid-cols-4 gap-4 max-[900px]:grid-cols-2 max-sm:gap-3"
  >
    <component
      v-for="item in items"
      :key="item.label"
      :is="item.route ? 'button' : 'div'"
      :type="item.route ? 'button' : undefined"
      class="m-0 min-w-0 appearance-none rounded-xl border-0 bg-transparent p-0 text-left"
      :class="item.route && 'group cursor-pointer transition-colors hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background focus-visible:outline-none motion-reduce:transition-none'"
      @click="item.route && $router.push(item.route)"
    >
      <StatTile
        :label="item.label"
        :value="Number(item.value) || 0"
        :icon="iconOf(item.icon)"
        tone="primary"
      >
        <span v-if="item.route" class="inline-flex items-center gap-1 group-hover:text-primary group-focus-visible:text-primary">
          查看详情
          <ChevronRight class="size-3" aria-hidden="true" />
        </span>
      </StatTile>
    </component>
  </div>
</template>

<script setup lang="ts">
import { ChevronRight } from '@lucide/vue'
import StatTile from '@/components/common/StatTile.vue'
import { iconOf } from '@/lib/icons'

export interface KpiItem {
  label: string
  value: number | string
  icon: string
  scope: 'resource' | 'profile' | 'system'
  route?: string
}

defineProps<{ items: KpiItem[] }>()

</script>
