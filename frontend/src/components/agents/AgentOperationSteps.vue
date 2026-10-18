<script setup lang="ts">
import { computed } from 'vue'
import { Loader2 } from '@lucide/vue'
import { deploymentStatusLabel, type AgentDeployment } from '@/composables/useAgentDeployments'
import { upgradeLabels, upgradeStepMessage } from '@/composables/useAgentUpgrades'
import type { AgentOperationEvent } from '@/types/agentOperation'

const props = withDefaults(defineProps<{
  task: Pick<Partial<AgentDeployment>, 'status' | 'message' | 'events' | 'query_error' | 'request_pending' | 'manual_rollback' | 'rollback_rejected'> & { target_version?: string }
  kind?: 'deployment' | 'upgrade'
  busy?: boolean
  showCurrent?: boolean
}>(), { kind: 'deployment', showCurrent: true })
const label = computed(() => props.task.request_pending ? '提交请求' : props.kind === 'upgrade'
  ? upgradeLabels[props.task.status || 'unknown'] || props.task.status
  : deploymentStatusLabel({ ...props.task, deployment_id: '', status: props.task.status || 'unknown' }))
const message = computed(() => {
  if (props.kind === 'deployment' && (['failed', 'rollback_failed'].includes(props.task.status || '') || (props.task.status === 'rolled_back' && !props.task.manual_rollback))) return ''
  return props.task.message || (props.kind === 'upgrade'
    ? upgradeStepMessage(props.task.status || 'unknown', props.task.target_version) : '')
})
const events = computed(() => props.task.events || [])
const connectionMessage = computed(() => {
  if (!props.task.query_error) return ''
  if (props.kind === 'upgrade' && ['restarting', 'checking'].includes(props.task.status || '')) {
    return 'Agent 重启期间暂时无法连接，正在等待重新连通；尚未确认更新成功。'
  }
  return '暂时无法取得最新状态，正在继续查询；下方保留最近已确认的步骤。'
})
const eventLabel = (event: AgentOperationEvent) => props.kind === 'upgrade'
  ? upgradeLabels[event.status] || event.status
  : deploymentStatusLabel({ deployment_id: '', status: event.status, manual_rollback: props.task.manual_rollback })
const eventTime = (value: string) => {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '时间未记录' : date.toLocaleTimeString()
}
</script>

<template>
  <div class="min-w-0 space-y-1 text-xs leading-relaxed" data-agent-operation-steps>
    <div aria-live="polite" role="status" class="min-w-0 space-y-1">
      <p v-if="showCurrent" class="m-0 flex min-w-0 items-center gap-2 font-medium text-foreground">
        <Loader2 v-if="busy" class="size-3.5 shrink-0 animate-spin text-primary-accent" aria-hidden="true" />
        <span class="min-w-0 break-words">{{ task.query_error ? '上次报告：' : '' }}{{ label }}</span>
      </p>
      <p v-if="message && message !== label" class="m-0 whitespace-pre-wrap text-muted-foreground [overflow-wrap:anywhere]">{{ message }}</p>
      <p v-if="connectionMessage" class="m-0 text-warning-accent [overflow-wrap:anywhere]">{{ connectionMessage }}</p>
    </div>
    <details class="min-w-0 rounded-md border border-border bg-background/50 text-foreground">
      <summary class="min-h-11 cursor-pointer px-3 py-3 text-xs font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-accent">步骤详情<span v-if="events.length" class="ml-1 font-normal text-muted-foreground">（{{ events.length }}）</span></summary>
      <div class="max-h-64 min-w-0 overflow-y-auto border-t border-border p-3" tabindex="0">
        <ol v-if="events.length" class="m-0 list-none space-y-3 p-0">
          <li v-for="(event, index) in events" :key="`${index}:${event.at}:${event.status}`" class="min-w-0 border-l-2 border-border-strong pl-3">
            <p class="m-0 flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-1"><span class="font-medium">{{ eventLabel(event) }}</span><time class="font-mono text-[11px] text-muted-foreground" :datetime="event.at">{{ eventTime(event.at) }}</time></p>
            <p v-if="event.message" class="mb-0 mt-1 whitespace-pre-wrap text-muted-foreground [overflow-wrap:anywhere]">{{ event.message }}</p>
          </li>
        </ol>
        <p v-else class="m-0 text-muted-foreground">{{ task.request_pending ? '请求已提交，等待服务端返回步骤记录。' : '此任务未提供步骤记录，仅能显示已报告的状态；旧版 Agent 更新后可记录后续任务的详细步骤。' }}</p>
      </div>
    </details>
  </div>
</template>
