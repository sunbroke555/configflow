<script setup lang="ts">
import { computed } from 'vue'
import { CircleCheck, CircleX, Loader2, TriangleAlert } from '@lucide/vue'
import { deploymentStatusLabel, type AgentDeployment } from '@/composables/useAgentDeployments'

const props = defineProps<{ task: AgentDeployment }>()
const label = computed(() => props.task.request_pending ? '提交请求'
  : `${props.task.query_error ? '上次报告：' : ''}${deploymentStatusLabel(props.task)}`)
const tone = computed(() => {
  const task = props.task
  if (task.rollback_rejected) return 'warning'
  if (['failed', 'rollback_failed'].includes(task.status) || (task.status === 'rolled_back' && !task.manual_rollback)) return 'danger'
  if (task.query_error || task.not_recorded || task.pending
    || ['ready', 'unknown', 'rolling_back', 'recovery_pending'].includes(task.status)) return 'warning'
  if (task.status === 'succeeded' || (task.manual_rollback && task.status === 'rolled_back')) return 'success'
  return 'progress'
})
const presentation = computed(() => ({
  progress: { text: 'text-primary-accent', icon: Loader2 },
  success: { text: 'text-success-accent', icon: CircleCheck },
  warning: { text: 'text-warning-accent', icon: TriangleAlert },
  danger: { text: 'text-destructive-accent', icon: CircleX },
})[tone.value])
</script>

<template>
  <p class="m-0 flex min-w-0 items-start gap-2 text-[13px] font-semibold leading-relaxed" :class="presentation.text">
    <component :is="presentation.icon" class="mt-0.5 size-4 shrink-0" :class="{ 'animate-spin': tone === 'progress' }" aria-hidden="true" />
    <span class="min-w-0 break-words"><slot>{{ label }}</slot></span>
  </p>
</template>
