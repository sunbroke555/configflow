<template>
  <StatTile v-if="monitorNumber(value)" :label="label" :value="value" :unit="unit" :precision="precision" :hint="hint" />
  <SectionCard v-else :padded="false" class="min-w-0 p-4 max-sm:p-3">
    <p class="m-0 text-[12px] font-medium text-muted-foreground">{{ label }}</p>
    <p class="font-display mt-2.5 mb-0 text-[38px] leading-none text-muted-foreground max-md:text-[30px] max-sm:text-[26px]">—</p>
    <p class="mt-3 mb-0 text-[11.5px] text-muted-foreground">{{ hint || '暂无数据' }}</p>
  </SectionCard>
</template>
<script setup lang="ts">
import StatTile from '@/components/common/StatTile.vue'
import SectionCard from '@/components/common/SectionCard.vue'
import { monitorNumber } from '@/types/oxidnsMonitor'
withDefaults(defineProps<{ label: string; value: number | null; unit?: string; precision?: number; hint?: string }>(), { precision: 0 })
</script>
