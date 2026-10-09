<template>
  <SectionCard :padded="false" role="region" aria-label="配置健康" :aria-busy="loading">
    <header class="flex flex-wrap items-start justify-between gap-3 px-5 pt-5 pb-4 max-md:px-4">
      <div>
        <h2 class="m-0 text-sm font-semibold text-foreground">配置健康</h2>
        <p class="mt-1 mb-0 text-[13px] text-muted-foreground">
          <span class="tabular-nums">{{ okCount }} / {{ rows.length }}</span> 项正常
        </p>
        <p class="mt-1 mb-0 text-xs text-muted-foreground">
          健康评分 <span class="tabular-nums">{{ score }}</span>
        </p>
      </div>
      <Badge :variant="overall.variant">{{ overall.text }}</Badge>
    </header>

    <Separator />

    <ul class="m-0 list-none px-5 py-1 max-md:px-4">
      <li
        v-for="row in rows"
        :key="row.label"
        class="flex flex-wrap items-center gap-x-2.5 gap-y-1 border-b border-border py-3 last:border-b-0"
      >
        <StatusDot :tone="toneOf(row.level)" />
        <span class="min-w-0 flex-1 text-[13px] text-muted-foreground">{{ row.label }}</span>
        <span class="num ml-auto text-right text-[13px] font-medium" :class="textTone(row.level)">
          {{ row.value }}
        </span>
      </li>
    </ul>

    <Separator />

    <footer class="flex flex-wrap items-center gap-2 px-5 py-3 max-md:px-4">
      <span class="text-xs text-muted-foreground">上次检测 <span class="tabular-nums">{{ checkedAt || '—' }}</span></span>
      <Button variant="ghost" size="sm" class="ml-auto" :disabled="loading" @click="$emit('refresh')">
        <RefreshCw class="size-3.5" :class="loading && 'animate-spin motion-reduce:animate-none'" aria-hidden="true" />
        {{ loading ? '检测中…' : '立即检测' }}
      </Button>
    </footer>
  </SectionCard>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { RefreshCw } from '@lucide/vue'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Separator } from '@/components/ui/separator'
import SectionCard from '@/components/common/SectionCard.vue'
import StatusDot from '@/components/common/StatusDot.vue'

export type HealthLevel = 'ok' | 'warn' | 'err'

export interface HealthRow {
  label: string
  value: string
  level: HealthLevel
}

const props = defineProps<{
  rows: HealthRow[]
  checkedAt?: string
  loading?: boolean
}>()

defineEmits<{ (e: 'refresh'): void }>()

const okCount = computed(() => props.rows.filter(r => r.level === 'ok').length)

// 正常项计一分，告警项计半分，异常项不计分。
const score = computed(() => {
  if (!props.rows.length) return 0
  const points = props.rows.reduce(
    (sum, r) => sum + (r.level === 'ok' ? 1 : r.level === 'warn' ? 0.5 : 0),
    0
  )
  return Math.round((points / props.rows.length) * 100)
})

// 总体状态取最差的一项，避免出现「有告警但总体良好」的自相矛盾
const overall = computed(() => {
  if (props.rows.some(r => r.level === 'err')) return { variant: 'danger' as const, text: '异常' }
  if (props.rows.some(r => r.level === 'warn')) return { variant: 'warning' as const, text: '需关注' }
  return { variant: 'success' as const, text: '良好' }
})

const toneOf = (level: HealthLevel) =>
  level === 'ok' ? ('success' as const) : level === 'warn' ? ('warning' as const) : ('danger' as const)

const textTone = (level: HealthLevel): string =>
  level === 'ok'
    ? 'text-foreground'
    : level === 'warn'
      ? 'text-warning-accent'
      : 'text-destructive-accent'
</script>
