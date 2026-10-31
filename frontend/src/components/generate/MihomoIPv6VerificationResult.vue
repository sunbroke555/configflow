<script setup lang="ts">
import { computed } from 'vue'
import MihomoIPv6CheckSummary from './MihomoIPv6CheckSummary.vue'
import type { IPv6Verification } from '@/types/mihomoIPv6'

const props = defineProps<{ verification: IPv6Verification; compact?: boolean }>()
const checkedAt = computed(() => {
  if (!props.verification.checked_at) return ''
  const date = new Date(props.verification.checked_at)
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('zh-CN')
})
</script>

<template>
  <div class="min-w-0" data-mihomo-ipv6-verification>
    <MihomoIPv6CheckSummary :verification="verification" />
    <p v-if="checkedAt" class="m-0 pt-1 text-xs text-muted-foreground">验证时间：{{ checkedAt }}</p>
  </div>
</template>
