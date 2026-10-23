<script setup lang="ts">
import { computed, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { Loader2 } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import StatusDot from '@/components/common/StatusDot.vue'
import ForwardFilterNotice from '@/components/agents/ForwardFilterNotice.vue'
import { gatewayForwardingView, type GatewayForwardingEntry } from '@/lib/gatewayForwarding'
import { forwardFilterWarning } from '@/lib/forwardFilter'

const props = defineProps<{ entry?: GatewayForwardingEntry; loading: boolean; busy: boolean; failed?: boolean; disabled?: boolean }>()
const emit = defineEmits<{ persist: [] }>()
const view = computed(() => gatewayForwardingView(props.entry, props.loading, props.failed))
// 转发过滤只作提醒：不改变上面的状态、说明与操作。
const filterWarning = computed(() => forwardFilterWarning(props.entry?.forward_filter))
const filterOpen = ref(false)
const agentName = computed(() => props.entry?.agent_name || props.entry?.agent_id || '')
</script>

<template>
  <div class="flex min-w-0 flex-col items-start gap-1" data-gateway-forwarding>
    <!-- 桌面上文字不宽于按钮：8 字以上的状态均衡折成两行，圆点对齐首行；说明随之换行，不撑宽整列。 -->
    <StatusDot :tone="view.tone" :label="view.label" class="items-start [text-wrap:balance] sm:max-w-[102px] [&>span:first-child]:mt-[5px]" />
    <p v-if="view.note" :role="view.tone === 'danger' ? 'alert' : undefined"
      :class="['m-0 max-w-[260px] break-words text-[11.5px] sm:max-w-[102px] leading-relaxed', view.tone === 'danger' ? 'text-destructive-accent' : 'text-muted-foreground']">
      {{ view.note }}
    </p>
    <!-- 列标题已写明「IPv4 转发」，按钮只写动作，完整名称留给读屏与悬停提示；执行中换成更短的文字，按钮不变宽。 -->
    <Button v-if="view.action === 'persist'" size="sm" variant="outline" class="px-2 max-md:min-h-11"
      aria-label="开启并持久化 IPv4 转发" title="开启并持久化 IPv4 转发" :aria-busy="busy || undefined"
      :disabled="busy || disabled" @click="emit('persist')">
      <Loader2 v-if="busy" class="size-3.5 animate-spin" />
      {{ busy ? '开启中…' : '开启并持久化' }}
    </Button>
    <RouterLink v-else-if="view.action === 'upgrade'" to="/agents"
      class="inline-flex items-center text-[12px] text-primary-accent underline underline-offset-4 max-md:min-h-11">
      前往 Agent 页更新
    </RouterLink>
    <template v-if="filterWarning">
      <!-- 短句最长 6 个字宽，在 102px 列内一行放下。 -->
      <p class="m-0 max-w-[260px] whitespace-nowrap text-[11.5px] leading-relaxed text-warning-accent sm:max-w-[102px]" data-forward-filter-short>{{ filterWarning.short }}</p>
      <Button size="sm" variant="ghost" class="px-2 text-xs max-md:min-h-11" aria-label="查看放行 TUN 转发的命令" title="查看放行 TUN 转发的命令" @click="filterOpen = true">放行命令</Button>
      <Dialog v-model:open="filterOpen">
        <DialogContent class="sm:max-w-[560px]">
          <DialogHeader>
            <DialogTitle class="pr-8 [overflow-wrap:anywhere]">放行「{{ agentName }}」的 TUN 转发</DialogTitle>
            <DialogDescription>这台设备的防火墙检查结果，只作提醒，不影响发布和 IPv4 转发。</DialogDescription>
          </DialogHeader>
          <ForwardFilterNotice :filter="entry?.forward_filter" />
        </DialogContent>
      </Dialog>
    </template>
  </div>
</template>
