<script setup lang="ts">
import { computed } from 'vue'
import { CircleCheck, CircleX, Loader2, TriangleAlert } from '@lucide/vue'
import { deploymentNeedsNotice, deploymentStatusLabel, type AgentDeployment } from '@/composables/useAgentDeployments'
import AgentStatusRow from './AgentStatusRow.vue'
import AgentHAPublication from './AgentHAPublication.vue'

const props = defineProps<{ task: AgentDeployment; compact?: boolean; label?: string }>()
const groupNotice = computed(() => !!props.task.ha_publication && deploymentNeedsNotice(props.task))
const statusLabel = computed(() => props.task.request_pending ? '提交请求'
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
const compactLabel = computed(() => {
  const task = props.task
  if (task.request_pending) return '提交中'
  if (task.rollback_rejected) return '未执行'
  if (task.not_recorded || task.query_error || task.pending) return '待确认'
  if (task.manual_rollback) {
    if (task.status === 'rolled_back') return '成功'
    if (task.status === 'rolling_back') return '恢复中'
  }
  return ({ succeeded: '成功', failed: '失败', rolled_back: '失败，已回滚',
    rollback_failed: '回滚失败', recovery_pending: '等待恢复检查',
    ready: '待激活', unknown: '待确认',
  } as Record<string, string>)[task.status] || deploymentStatusLabel(task)
})
</script>

<template>
  <AgentStatusRow v-if="compact" :label="props.label || (task.manual_rollback || task.rollback_rejected ? '配置回滚' : '配置推送')" :status="compactLabel" :tone="tone">
    <template v-if="$slots.notice || groupNotice" #notice>
      <AgentHAPublication v-if="task.ha_publication && groupNotice" :publication="task.ha_publication" />
      <slot name="notice" />
    </template>
    <AgentHAPublication v-if="task.ha_publication && !groupNotice" :publication="task.ha_publication" />
    <slot name="details" />
  </AgentStatusRow>
  <template v-else>
    <p class="m-0 flex min-w-0 items-start gap-2 text-[13px] font-semibold leading-relaxed" :class="presentation.text">
      <component :is="presentation.icon" class="mt-0.5 size-4 shrink-0" :class="{ 'animate-spin': tone === 'progress' }" aria-hidden="true" />
      <span class="min-w-0 break-words"><slot>{{ statusLabel }}</slot></span>
    </p>
    <AgentHAPublication v-if="task.ha_publication" :publication="task.ha_publication" />
  </template>
</template>
