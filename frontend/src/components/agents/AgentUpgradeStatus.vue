<script setup lang="ts">
import { computed } from 'vue'
import { upgradeLabels, type AgentUpgrade } from '@/composables/useAgentUpgrades'
import AgentStatusRow from './AgentStatusRow.vue'
import AgentOperationSteps from './AgentOperationSteps.vue'

const props = defineProps<{ task: AgentUpgrade; busy?: boolean }>()
const failed = computed(() => ['failed', 'rolled_back', 'rollback_failed'].includes(props.task.status))
const attention = computed(() => props.task.query_error || props.task.status === 'unknown')
const tone = computed(() => failed.value ? 'danger' : attention.value ? 'warning' : props.task.status === 'succeeded' ? 'success' : 'progress')
const label = computed(() => attention.value ? '待确认' : ({
  succeeded: '成功', failed: '失败', rolled_back: '失败，已回滚', rollback_failed: '回滚失败',
} as Record<string, string>)[props.task.status] || upgradeLabels[props.task.status] || props.task.status)
const showNotice = computed(() => props.busy || failed.value || attention.value || !!props.task.error)
</script>

<template>
  <AgentStatusRow label="Agent 更新" :status="label" :tone="tone">
    <template v-if="showNotice" #notice>
      <AgentOperationSteps :task="task" kind="upgrade" :busy="busy" :show-current="false" :show-steps="false" />
      <p v-if="task.error" class="m-0 whitespace-pre-wrap text-destructive-accent">{{ task.error }}</p>
      <slot name="actions" />
    </template>
    <p v-if="task.target_version" class="m-0">目标版本：<span class="font-mono">{{ task.target_version }}</span></p>
    <AgentOperationSteps :task="task" kind="upgrade" :busy="busy" :show-current="false" :show-message="!showNotice" embedded />
  </AgentStatusRow>
</template>
