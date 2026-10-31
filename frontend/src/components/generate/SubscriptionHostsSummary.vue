<script setup lang="ts">
import { computed, ref } from 'vue'
import { ChevronDown } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import FieldHelp from '@/components/common/FieldHelp.vue'
import type { SubscriptionHostsReport } from '@/types'

const props = defineProps<{ report: SubscriptionHostsReport }>()

const open = ref(false)
const HELP = '订阅自带的 hosts 只合并本配置所用节点自己的 server 域名，其他域名一律忽略，避免订阅改写别的网站。'
  + 'Mihomo 基础配置里手写的 hosts 优先，其次是推送给 Agent 时同步的 MosDNS 自定义 Hosts，多个订阅冲突时按订阅列表顺序采用排在前面的。'
  + '订阅更新后 hosts 有变化，需重新推送 Mihomo 才会生效。'
const hidden = computed(() => Math.max(0, props.report.merged + props.report.ignored - props.report.items.length))
const value = (item: SubscriptionHostsReport['items'][number]) =>
  Array.isArray(item.value) ? item.value.join(', ') : item.value || ''
</script>

<template>
  <div class="min-w-0 border-b border-border px-5 py-2 text-xs max-sm:px-3" role="group" aria-label="订阅 hosts" data-subscription-hosts>
    <div class="flex min-h-11 flex-wrap items-center gap-x-2 gap-y-1">
      <span class="font-semibold text-foreground">订阅 hosts</span>
      <span class="text-muted-foreground" data-subscription-hosts-counts>
        已合并 <span class="cf-num text-foreground">{{ report.merged }}</span> 条，忽略
        <span class="cf-num text-foreground">{{ report.ignored }}</span> 条
      </span>
      <FieldHelp label="订阅 hosts" :text="HELP" />
      <Button
        v-if="report.items.length"
        variant="ghost"
        size="sm"
        class="ml-auto min-h-11"
        :aria-expanded="open"
        aria-controls="subscription-hosts-items"
        @click="open = !open"
      >
        {{ open ? '收起' : '详情' }}
        <ChevronDown :class="['size-3.5 transition-transform', open && 'rotate-180']" aria-hidden="true" />
      </Button>
    </div>
    <p v-if="report.domain_mapped" class="m-0 pb-1 text-muted-foreground" data-subscription-hosts-domain-note>
      其中 {{ report.domain_mapped }} 条是「域名 → 域名」：Mihomo 连接节点时不采用这类映射，节点仍连接原域名解析到的地址。
    </p>
    <ul v-if="open" id="subscription-hosts-items" class="m-0 flex list-none flex-col divide-y divide-border/70 p-0 pb-1">
      <li v-for="(item, i) in report.items" :key="i" class="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 py-1.5 [overflow-wrap:anywhere]">
        <span :class="['chip', item.status === 'merged' && 'chip-ok']">{{ item.status === 'merged' ? '已合并' : '已忽略' }}</span>
        <span class="min-w-0 font-mono text-foreground">{{ item.domain || 'hosts' }}</span>
        <template v-if="value(item)">
          <span class="text-muted-foreground" aria-hidden="true">→</span>
          <span class="min-w-0 font-mono text-foreground">{{ value(item) }}</span>
        </template>
        <span class="text-muted-foreground">· {{ item.subscription }}</span>
        <span v-if="item.status === 'ignored'" class="w-full text-muted-foreground">{{ item.detail }}</span>
      </li>
      <li v-if="hidden" class="py-1.5 text-muted-foreground">另有 {{ hidden }} 条未列出，原因见服务日志。</li>
    </ul>
  </div>
</template>
