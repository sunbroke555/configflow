<template>
  <section class="mb-3 min-w-0 border-t border-border/70 pt-2" aria-label="网络速率" data-agent-traffic>
    <div class="mb-1 flex min-h-8 items-center justify-between gap-2 text-[11px] text-muted-foreground">
      <span class="inline-flex items-center gap-1">
        网络速率
        <span class="cf-compact-help"><FieldHelp label="网络速率" :text="helpText" /></span>
      </span>
      <span data-traffic-state>{{ stateLabel }}</span>
    </div>
    <div class="grid grid-cols-[44px_minmax(0,1fr)_max-content] items-center gap-x-2.5 gap-y-1.5 text-[11.5px] text-muted-foreground">
      <template v-for="row in rows" :key="row.key">
        <span class="inline-flex items-center gap-1"><component :is="row.icon" class="size-3" aria-hidden="true" />{{ row.label }}</span>
        <Sparkline
          class="h-7 min-w-0 w-full"
          :data="row.values"
          :timestamps="timestamps"
          :time-range="timeRange"
          :min="0"
          :max="scale"
          :max-gap="TRAFFIC_STALE_MS"
          :color="row.color"
          :title="`${row.label} · 本页近 5 分钟峰值 ${rate(row.peak)}`"
        />
        <b class="num whitespace-nowrap text-right font-mono font-medium text-foreground/80" :data-traffic-rate="row.key">{{ rate(row.current) }}</b>
      </template>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ArrowDown, ArrowUp } from '@lucide/vue'
import Sparkline from '@/components/common/Sparkline.vue'
import FieldHelp from '@/components/common/FieldHelp.vue'
import { formatBytes } from '@/lib/format'
import { isTrafficFresh, recordTraffic, trafficSample, TRAFFIC_STALE_MS, TRAFFIC_WINDOW_MS, type TrafficSample } from '@/lib/agentTraffic'
import type { Agent } from '@/types'

const props = defineProps<{ agent: Agent; now: number }>()
const history = ref<TrafficSample[]>([])
watch(() => [props.agent, props.now] as const, ([agent, now], previous) => {
  if (previous && previous[0].id !== agent.id) history.value = []
  history.value = recordTraffic(history.value, agent.status === 'online' ? trafficSample(agent.system_metrics) : null, now)
}, { immediate: true })

const current = computed(() => trafficSample(props.agent.system_metrics))
const fresh = computed(() => props.agent.status === 'online' && isTrafficFresh(current.value, props.now))
const timestamps = computed(() => history.value.map(point => point.at))
const timeRange = computed<[number, number]>(() => [props.now - TRAFFIC_WINDOW_MS, props.now])
const rows = computed(() => ([
  { key: 'upload', label: '上传', icon: ArrowUp, color: '--chart-upload' },
  { key: 'download', label: '下载', icon: ArrowDown, color: '--chart-download' },
] as const).map(row => {
  const values = history.value.map(point => point[row.key])
  const valid = values.filter((value): value is number => value !== null)
  return { ...row, values, current: fresh.value ? current.value?.[row.key] ?? null : null, peak: valid.length ? Math.max(...valid) : null }
}))
// Both directions use the same zero baseline and the recent peak, not a bandwidth percentage.
const scale = computed(() => Math.max(1, ...rows.value.map(row => row.peak ?? 0)))
const stateLabel = computed(() => {
  if (props.agent.status !== 'online') return '离线'
  if (!current.value || (current.value.upload === null && current.value.download === null)) return '暂无数据'
  if (!fresh.value) return '数据未更新'
  if (history.value.filter(point => point.upload !== null || point.download !== null).length < 2) return '采样中'
  return '近 5 分钟'
})
const rate = (value: number | null) => value === null ? '—' : `${formatBytes(value)}/s`
const helpText = computed(() => {
  const sampledAt = current.value ? new Date(current.value.at).toLocaleTimeString('zh-CN', { hour12: false }) : '暂无'
  return `从打开页面起记录最近 5 分钟，随 Agent 上报更新。\n统计 Agent 所在网络环境的全部网卡，并非带宽使用率。\n最近采样：${sampledAt}\n上传峰值：${rate(rows.value[0].peak)} · 下载峰值：${rate(rows.value[1].peak)}`
})
</script>
