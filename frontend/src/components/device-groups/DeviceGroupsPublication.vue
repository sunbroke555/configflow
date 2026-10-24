<script setup lang="ts">
import { Button } from '@/components/ui/button'
import { RouterLink } from 'vue-router'
import AgentDeploymentStatus from '@/components/agents/AgentDeploymentStatus.vue'
import AgentStatusRow from '@/components/agents/AgentStatusRow.vue'
import AgentOperationSteps from '@/components/agents/AgentOperationSteps.vue'
import AgentFailureDetails from '@/components/agents/AgentFailureDetails.vue'
import AgentDNSChecks from '@/components/agents/AgentDNSChecks.vue'
import { deploymentNeedsNotice } from '@/composables/useAgentDeployments'
import type { DeviceGroupPublication } from '@/types/deviceGroups'
defineProps<{ publication: DeviceGroupPublication; busy?: boolean; error?: string }>()
const emit = defineEmits<{ action: [action: 'reconcile' | 'retry' | 'activate', agentId?: string] }>()
</script>
<template>
  <section class="min-w-0 space-y-3 border-t border-border pt-4" aria-label="设备组发布进度" aria-live="polite">
    <div class="flex flex-wrap items-center justify-between gap-2"><h2 class="m-0 text-sm font-semibold">发布进度</h2><span class="text-xs text-muted-foreground">先 Mihomo，再 OxiDNS</span></div>
    <p class="m-0 break-words text-sm" :class="publication.state === 'failed' ? 'text-destructive-accent' : publication.state === 'paused' ? 'text-warning-accent' : 'text-muted-foreground'">{{ publication.message }}</p>
    <div class="divide-y divide-border">
      <div v-for="target in publication.targets" :key="target.agent_id" class="min-w-0" :data-group-publish-target="target.agent_id">
        <AgentDeploymentStatus v-if="target.deployment" :label="target.name" :task="target.deployment" compact>
          <template v-if="deploymentNeedsNotice(target.deployment)" #notice><AgentOperationSteps :task="target.deployment" :show-current="false" :show-steps="false" /><AgentFailureDetails :task="target.deployment" :show-progress-message="false" :showDNSChecks="false" /></template>
          <template #details><AgentOperationSteps :task="target.deployment" :show-current="false" :show-message="!deploymentNeedsNotice(target.deployment)" embedded /></template>
        </AgentDeploymentStatus>
        <AgentStatusRow v-else :label="target.name" :status="target.state === 'queued' ? '等待发布' : target.state === 'paused' ? '已暂停' : target.state === 'failed' ? '未发布' : target.state === 'succeeded' ? '成功' : '正在准备'" :tone="target.state === 'failed' ? 'danger' : target.state === 'paused' ? 'warning' : target.state === 'succeeded' ? 'success' : target.state === 'queued' ? 'neutral' : 'progress'"><template v-if="target.message" #notice>{{ target.message }}</template></AgentStatusRow>
        <AgentDNSChecks :checks="target.deployment?.dns_checks" :checked-at="target.deployment?.dns_checked_at" compact />
        <Button v-if="publication.actions.activate_agent_ids.includes(target.agent_id)" variant="outline" class="mb-2 min-h-11" :disabled="busy" @click="emit('action', 'activate', target.agent_id)">应用并启动 {{ target.name }}</Button>
      </div>
    </div>
    <p v-if="error" role="alert" class="m-0 break-words text-sm text-destructive-accent">{{ error }}</p>
    <p v-if="publication.actions.requires_new_preview" class="m-0 text-sm text-warning-accent">配置或发布目标已变化，请重新预览。已成功的设备保留本次结果。</p>
    <div class="flex flex-wrap gap-2"><Button v-if="publication.actions.can_reconcile" variant="outline" class="min-h-11" :disabled="busy" @click="emit('action', 'reconcile')">重新确认结果</Button><Button v-if="publication.actions.can_retry" variant="outline" class="min-h-11" :disabled="busy" @click="emit('action', 'retry')">重试未完成设备</Button><RouterLink to="/agents" class="inline-flex min-h-11 items-center text-xs text-primary-accent underline underline-offset-4">前往 Agent 查看发布记录</RouterLink></div>
    <p v-if="publication.state === 'running' || publication.state === 'preparing'" class="m-0 text-xs text-muted-foreground">可以离开页面；已登记的批次会继续执行。</p>
  </section>
</template>
