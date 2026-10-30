<script setup lang="ts">
import { computed } from 'vue'
import AgentStatusRow from '@/components/agents/AgentStatusRow.vue'
import MihomoIPv6CheckSummary from './MihomoIPv6CheckSummary.vue'
import { buildMihomoIPv6Summary } from '@/lib/mihomoIPv6Summary'
import type { IPv6Verification } from '@/types/mihomoIPv6'

const props = defineProps<{ verification: IPv6Verification; compact?: boolean }>()
const scopedChecks = computed(() => props.verification.scope === 'mihomo_runtime'
  ? props.verification.checks.filter(check => check.id !== 'client_not_configured') : props.verification.checks)
const complete = computed(() => props.verification.status === 'ok'
  && scopedChecks.value.length > 0 && scopedChecks.value.every(check => check.status === 'ok')
  && buildMihomoIPv6Summary({ verification: props.verification })
    .filter(group => props.verification.scope !== 'mihomo_runtime' || group.id !== 'client')
    .every(group => group.tone === 'success'))
const failed = computed(() => props.verification.status === 'error'
  || props.verification.checks.some(check => check.status === 'error'))
const checkedAt = computed(() => {
  if (!props.verification.checked_at) return ''
  const date = new Date(props.verification.checked_at)
  return Number.isNaN(date.getTime()) ? props.verification.checked_at : date.toLocaleString('zh-CN')
})
</script>

<template>
  <div class="min-w-0 divide-y divide-border/70" data-mihomo-ipv6-verification>
    <AgentStatusRow :label="verification.scope === 'mihomo_runtime' ? '主机自检' : '客户端全链路验收'" :status="complete ? '已通过' : failed ? '有项目未通过' : '尚未全部验证'" :tone="complete ? 'success' : failed ? 'danger' : 'warning'">
      <template v-if="!complete" #notice>
        <p class="m-0 break-words">{{ verification.message || '仍有项目未完成验证，展开查看原因。' }}</p>
      </template>
      <template v-if="checkedAt || compact" #default>
        <p v-if="checkedAt" class="m-0 break-words text-muted-foreground">检查时间：{{ checkedAt }}</p>
        <p v-if="complete && verification.message" class="m-0 break-words">{{ verification.message }}</p>
        <div v-if="compact" class="max-h-80 overflow-y-auto overscroll-contain">
          <MihomoIPv6CheckSummary :verification="verification" />
        </div>
      </template>
    </AgentStatusRow>
    <MihomoIPv6CheckSummary v-if="!compact" :verification="verification" />
  </div>
</template>
