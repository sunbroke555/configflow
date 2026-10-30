<script setup lang="ts">
import { computed } from 'vue'
import AgentStatusRow from '@/components/agents/AgentStatusRow.vue'
import { buildMihomoIPv6Summary } from '@/lib/mihomoIPv6Summary'
import type { IPv6ReadinessCheck, IPv6Verification, MihomoIPv6Readiness } from '@/types/mihomoIPv6'

const props = defineProps<{ readiness?: MihomoIPv6Readiness; verification?: IPv6Verification; preferVerificationSnapshot?: boolean }>()
const groups = computed(() => buildMihomoIPv6Summary({ readiness: props.readiness, verification: props.verification, preferVerificationSnapshot: props.preferVerificationSnapshot }))
const checkLabels: Record<IPv6ReadinessCheck['status'], string> = {
  ok: '已通过', warning: '需留意', error: '未通过', unknown: '未验证',
}
const checkColors = {
  success: 'text-success-accent', warning: 'text-warning-accent',
  danger: 'text-destructive-accent', neutral: 'text-muted-foreground', progress: 'text-primary-accent',
}
const checkTones = { ok: 'success', warning: 'warning', error: 'danger', unknown: 'neutral' } as const
</script>

<template>
  <div class="min-w-0 divide-y divide-border/70" data-ipv6-check-summary>
    <div v-for="group in groups" :key="group.id" class="min-w-0" :data-ipv6-check-group="group.id">
      <AgentStatusRow :label="group.label" :status="group.status" :tone="group.tone">
        <template v-if="group.issues.length" #notice>
          <p v-for="issue in group.issues.slice(0, 2)" :key="issue" class="m-0 [overflow-wrap:anywhere]" :role="group.tone === 'danger' ? 'alert' : undefined">{{ issue }}</p>
          <p v-if="group.issues.length > 2" class="m-0 text-muted-foreground">另有 {{ group.issues.length - 2 }} 项需查看，展开本项了解详情。</p>
        </template>
        <template v-if="group.note || group.checks.length" #default>
          <p v-if="group.note" class="m-0 [overflow-wrap:anywhere]">{{ group.note }}</p>
          <ul v-if="group.checks.length" class="m-0 list-none divide-y divide-border/50 p-0" :aria-label="`${group.label}检查详情`">
            <li v-for="check in group.checks" :key="check.id" class="min-w-0 space-y-1 py-2 first:pt-0 last:pb-0" :data-ipv6-check-id="check.id">
              <div class="flex min-w-0 items-start gap-2">
                <span class="min-w-0 flex-1 font-medium text-foreground [overflow-wrap:anywhere]">{{ check.displayLabel || check.label }}</span>
                <span class="shrink-0" :class="checkColors[check.displayTone || checkTones[check.status]]">{{ check.displayStatus || checkLabels[check.status] }}</span>
              </div>
              <p class="m-0 [overflow-wrap:anywhere]">{{ check.message }}</p>
              <p v-for="(detail, index) in check.details || []" :key="index" class="m-0 whitespace-pre-wrap [overflow-wrap:anywhere]">{{ detail }}</p>
            </li>
          </ul>
        </template>
      </AgentStatusRow>
    </div>
  </div>
</template>
