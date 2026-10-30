<script setup lang="ts">
import { computed } from 'vue'
import AgentStatusRow from '@/components/agents/AgentStatusRow.vue'
import type { IPv6ReadinessCheck, IPv6Verification } from '@/types/mihomoIPv6'

const props = defineProps<{ verification: IPv6Verification; compact?: boolean }>()
const scopedChecks = computed(() => props.verification.scope === 'mihomo_runtime'
  ? props.verification.checks.filter(check => check.id !== 'client_not_configured') : props.verification.checks)
const complete = computed(() => props.verification.status === 'ok'
  && scopedChecks.value.length > 0 && scopedChecks.value.every(check => check.status === 'ok'))
const failed = computed(() => props.verification.status === 'error'
  || props.verification.checks.some(check => check.status === 'error'))
const labels: Record<IPv6ReadinessCheck['status'], string> = {
  ok: '已通过', warning: '需留意', error: '未通过', unknown: '未验证',
}
const tones = { ok: 'success', warning: 'warning', error: 'danger', unknown: 'neutral' } as const
const checkedAt = computed(() => {
  if (!props.verification.checked_at) return ''
  const date = new Date(props.verification.checked_at)
  return Number.isNaN(date.getTime()) ? props.verification.checked_at : date.toLocaleString('zh-CN')
})
</script>

<template>
  <div class="min-w-0 divide-y divide-border/70" data-mihomo-ipv6-verification>
    <AgentStatusRow :label="verification.scope === 'mihomo_runtime' ? '主机自检' : '客户端全链路验收'" :status="complete ? '已通过' : failed ? '有项目未通过' : '尚未全部验证'" :tone="complete ? 'success' : failed ? 'danger' : 'warning'">
      <template #notice>
        <p class="m-0 break-words">{{ verification.message || (complete ? verification.scope === 'mihomo_runtime' ? '主机自检已通过；客户端实际流量的验证结果另列。' : '本次客户端全链路检查已通过。' : '请查看下面各项结果；未验证的项目不代表已通过。') }}</p>
      </template>
      <template v-if="checkedAt || compact" #default>
        <p v-if="checkedAt" class="m-0 break-words text-muted-foreground">检查时间：{{ checkedAt }}</p>
        <div v-if="compact" class="max-h-64 divide-y divide-border/70 overflow-y-auto overscroll-contain">
          <AgentStatusRow v-for="check in verification.checks" :key="check.id" :label="check.label" :status="labels[check.status]" :tone="tones[check.status]">
            <template v-if="check.status !== 'ok'" #notice><p class="m-0 break-words">{{ check.message }}</p></template>
            <template v-if="check.status === 'ok' || check.details?.length" #default>
              <p v-if="check.status === 'ok'" class="m-0 break-words">{{ check.message }}</p>
              <p v-for="detail in check.details || []" :key="detail" class="m-0 break-words">{{ detail }}</p>
            </template>
          </AgentStatusRow>
        </div>
      </template>
    </AgentStatusRow>
    <AgentStatusRow v-for="check in compact ? [] : verification.checks" :key="check.id" :label="check.label" :status="labels[check.status]" :tone="tones[check.status]">
      <template v-if="check.status !== 'ok'" #notice><p class="m-0 break-words" :role="check.status === 'error' ? 'alert' : undefined">{{ check.message }}</p></template>
      <template v-if="check.status === 'ok' || check.details?.length" #default>
        <p v-if="check.status === 'ok'" class="m-0 break-words">{{ check.message }}</p>
        <p v-for="detail in check.details || []" :key="detail" class="m-0 break-words">{{ detail }}</p>
      </template>
    </AgentStatusRow>
  </div>
</template>
