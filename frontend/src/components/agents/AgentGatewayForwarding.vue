<script setup lang="ts">
import { computed } from 'vue'
import { Loader2 } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import AgentStatusRow from '@/components/agents/AgentStatusRow.vue'
import ForwardFilterNotice from '@/components/agents/ForwardFilterNotice.vue'
import { forwardFilterWarning } from '@/lib/forwardFilter'
import {
  FORWARDING_CONFIG_FILE, gatewayForwardingReason, gatewayForwardingView, systemPersistenceText, type GatewayForwardingEntry,
} from '@/lib/gatewayForwarding'

// Agent 卡片上的代理网关 IPv4 转发：只为被路由器用作代理网关的设备显示。
const props = defineProps<{ entry: GatewayForwardingEntry; busy: boolean; disabled?: boolean }>()
const emit = defineEmits<{ persist: [] }>()
const view = computed(() => gatewayForwardingView(props.entry))
const reason = computed(() => gatewayForwardingReason(props.entry))
const systemPersistence = computed(() => systemPersistenceText(props.entry))
// 转发过滤只作提醒：不改变上面的状态、说明与操作。
const filterWarning = computed(() => forwardFilterWarning(props.entry.forward_filter))
</script>

<template>
  <AgentStatusRow label="IPv4 转发" :status="view.label" :tone="view.tone" data-gateway-forwarding>
    <template v-if="view.note || view.action" #notice>
      <p v-if="view.note" :role="view.tone === 'danger' ? 'alert' : undefined"
        :class="['m-0', view.tone === 'danger' ? 'text-destructive-accent' : 'text-muted-foreground']">
        {{ view.note }}
      </p>
      <Button v-if="view.action === 'persist'" size="sm" variant="outline" class="min-h-11"
        :aria-busy="busy || undefined" :disabled="busy || disabled" @click="emit('persist')">
        <Loader2 v-if="busy" class="size-3.5 animate-spin" />
        {{ busy ? '开启中…' : '开启并持久化' }}
      </Button>
    </template>
    <p class="m-0">{{ reason }}</p>
    <p v-if="systemPersistence" class="m-0">{{ systemPersistence }}</p>
    <p v-else-if="entry.state === 'persisted'" class="m-0">已写入 <span class="font-mono">{{ FORWARDING_CONFIG_FILE }}</span>，Agent 启动前会检查并保持开启。</p>
  </AgentStatusRow>
  <AgentStatusRow v-if="filterWarning" label="转发过滤" :status="filterWarning.status" tone="warning" data-forward-filter-row>
    <p class="m-0">这台设备的防火墙检查结果，只作提醒，不影响发布和 IPv4 转发。</p>
    <ForwardFilterNotice :filter="entry.forward_filter" />
  </AgentStatusRow>
</template>
