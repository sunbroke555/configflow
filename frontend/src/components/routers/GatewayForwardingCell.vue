<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import { Loader2 } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import StatusDot from '@/components/common/StatusDot.vue'
import { gatewayForwardingView, type GatewayForwardingEntry } from '@/lib/gatewayForwarding'

const props = defineProps<{ entry?: GatewayForwardingEntry; loading: boolean; busy: boolean; failed?: boolean; disabled?: boolean }>()
const emit = defineEmits<{ persist: [] }>()
const view = computed(() => gatewayForwardingView(props.entry, props.loading, props.failed))
</script>

<template>
  <div class="flex min-w-0 flex-col items-start gap-1" data-gateway-forwarding>
    <StatusDot :tone="view.tone" :label="view.label" />
    <p v-if="view.note" :role="view.tone === 'danger' ? 'alert' : undefined"
      :class="['m-0 max-w-[260px] break-words text-[11.5px] leading-relaxed', view.tone === 'danger' ? 'text-destructive-accent' : 'text-muted-foreground']">
      {{ view.note }}
    </p>
    <Button v-if="view.action === 'persist'" size="sm" variant="outline" class="max-md:min-h-11" :disabled="busy || disabled" @click="emit('persist')">
      <Loader2 v-if="busy" class="size-3.5 animate-spin" />
      开启并持久化 IPv4 转发
    </Button>
    <RouterLink v-else-if="view.action === 'upgrade'" to="/agents"
      class="inline-flex items-center text-[12px] text-primary-accent underline underline-offset-4 max-md:min-h-11">
      前往 Agent 页更新
    </RouterLink>
  </div>
</template>
