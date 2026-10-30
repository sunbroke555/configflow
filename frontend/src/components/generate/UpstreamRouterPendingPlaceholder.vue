<script setup lang="ts">
// TEMPORARY stand-in for components/routers/UpstreamRouterPending.vue, which
// another branch provides. Same props and `status` event; it does not read
// anything, so it always reports `unknown`. Delete once the real one lands.
import { onMounted, watch } from 'vue'
import { RouterLink } from 'vue-router'

const props = defineProps<{ agentId: string; refreshKey?: number | string; compact?: boolean }>()
const emit = defineEmits<{ status: [value: { state: 'none' | 'synced' | 'pending' | 'unknown'; pending: number }] }>()
const report = () => emit('status', { state: 'unknown', pending: 0 })
onMounted(report)
watch(() => [props.agentId, props.refreshKey], report)
</script>

<template>
  <p class="m-0 text-xs leading-relaxed" data-upstream-router-pending-placeholder>
    到<RouterLink class="inline-flex min-h-11 items-center px-1 text-primary-accent underline underline-offset-4" to="/routers">上级路由</RouterLink>预览并发布对应路由。
  </p>
</template>
