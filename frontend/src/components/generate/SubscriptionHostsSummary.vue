<script setup lang="ts">
import { computed, ref } from 'vue'
import { ChevronDown } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import FieldHelp from '@/components/common/FieldHelp.vue'
import type { SubscriptionHostsReport } from '@/types'

const props = defineProps<{ report: SubscriptionHostsReport }>()

const open = ref(false)
const entriesOpen = ref(false)
const HELP = '订阅自带的 hosts 只合并本配置所用节点自己的 server 域名，其他域名一律忽略，避免订阅改写别的网站。'
  + 'Mihomo 基础配置里手写的 hosts 优先，其次是推送给 Agent 时同步的 MosDNS 自定义 Hosts，多个订阅冲突时按订阅列表顺序采用排在前面的。'
  + '「域名 → 域名」条目只有节点域名经 Mihomo 本机 DNS 监听解析时才生效：ConfigFlow 用 proxy-server-nameserver-policy 只把这些节点域名交给 dns.listen'
  + '（需要 Mihomo v1.19.20 或更新，并且写了 proxy-server-nameserver），其他节点照旧用你的 proxy-server-nameserver；Fake-IP 模式下只把映射目标加入 fake-ip-filter。'
  + '订阅更新后 hosts 有变化，需重新推送 Mihomo 才会生效。'
const hidden = computed(() => Math.max(0, props.report.merged + props.report.ignored - props.report.items.length))
const value = (item: SubscriptionHostsReport['items'][number]) =>
  Array.isArray(item.value) ? item.value.join(', ') : item.value || ''
const dns = computed(() => props.report.dns ?? null)
const listener = computed(() => (dns.value?.listener ?? '').replace(/^udp:\/\//, ''))
/** Fake-IP blacklist or rule mode: targets were (or already are) kept out of the fake pool. */
const filtering = computed(() => dns.value?.fake_ip === 'blacklist' || dns.value?.fake_ip === 'rule')
const ownPolicy = computed(() => dns.value?.policy.filter(item => item.source === 'user').length ?? 0)
const outdated = computed(() => dns.value?.outdated_agents ?? [])
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
    <template v-if="dns">
      <div v-if="dns.applied" class="flex min-w-0 flex-wrap items-center gap-x-2 pb-1" data-subscription-hosts-dns>
        <p class="m-0 min-w-0 flex-1 text-muted-foreground [overflow-wrap:anywhere]">
          <span class="cf-num text-foreground">{{ dns.policy.length }}</span> 个被映射的节点域名改由本机 DNS 监听
          <span class="font-mono text-foreground">{{ listener }}</span> 解析<template v-if="filtering && dns.targets_filtered">，<span class="cf-num text-foreground">{{ dns.targets_filtered }}</span> 个映射目标已加入 Fake-IP 过滤</template><template v-if="filtering && dns.targets_covered">，<span class="cf-num text-foreground">{{ dns.targets_covered }}</span> 个映射目标已在 fake-ip-filter 里</template>。<template v-if="ownPolicy">其中 {{ ownPolicy }} 个是你写的 proxy-server-nameserver-policy，已指向这个监听，保留原值。</template>
        </p>
        <Button
          variant="ghost"
          size="sm"
          class="min-h-11"
          :aria-expanded="entriesOpen"
          aria-controls="subscription-hosts-dns-entries"
          @click="entriesOpen = !entriesOpen"
        >
          {{ entriesOpen ? '收起条目' : '查看条目' }}
          <ChevronDown :class="['size-3.5 transition-transform', entriesOpen && 'rotate-180']" aria-hidden="true" />
        </Button>
      </div>
      <p v-else class="m-0 pb-1 text-muted-foreground" data-subscription-hosts-dns>
        其中 {{ dns.mapped }} 条「域名 → 域名」映射对节点连接不生效，节点仍连接左侧域名自己的地址：
      </p>
      <p v-for="warning in dns.warnings" :key="warning" class="m-0 pb-1 text-warning-accent [overflow-wrap:anywhere]" data-subscription-hosts-dns-warning>
        {{ warning }}
      </p>
      <p v-for="agent in outdated" :key="agent.name" class="m-0 pb-1 text-warning-accent [overflow-wrap:anywhere]" data-subscription-hosts-dns-warning>
        Agent「{{ agent.name }}」上报的 Mihomo 内核为 <span class="font-mono">{{ agent.version }}</span>，早于 {{ dns.min_version }}：推送给它的配置不加 proxy-server-nameserver-policy，映射在这台设备上不生效，升级内核后重新推送。
      </p>
      <p v-for="note in dns.notes" :key="note" class="m-0 pb-1 text-muted-foreground [overflow-wrap:anywhere]">{{ note }}</p>
      <div v-if="dns.applied && entriesOpen" id="subscription-hosts-dns-entries" class="flex min-w-0 flex-col gap-1.5 pb-1.5" aria-label="本机 DNS 监听解析的节点域名">
        <div class="min-w-0" data-dns-group="policy">
          <p class="m-0 text-muted-foreground">proxy-server-nameserver-policy：交给本机 DNS 监听解析</p>
          <ul class="m-0 flex list-none flex-col gap-0.5 p-0 [overflow-wrap:anywhere]">
            <li v-for="item in dns.policy" :key="item.domain" class="min-w-0">
              <span class="font-mono text-foreground">{{ item.domain }}</span>
              <span class="text-muted-foreground" aria-hidden="true"> → </span>
              <span class="font-mono text-foreground">{{ item.target }}</span>
              <span v-if="item.source === 'user'" class="text-muted-foreground">（你写的）</span>
            </li>
          </ul>
        </div>
        <div v-if="dns.filter.length" class="min-w-0" data-dns-group="filter">
          <p class="m-0 text-muted-foreground">{{ dns.fake_ip === 'rule' ? 'fake-ip-filter：新增 real-ip 规则' : 'fake-ip-filter：新增条目' }}</p>
          <ul class="m-0 flex list-none flex-wrap gap-x-3 gap-y-0.5 p-0 font-mono text-foreground [overflow-wrap:anywhere]">
            <li v-for="entry in dns.filter" :key="entry">{{ entry }}</li>
          </ul>
        </div>
      </div>
    </template>
    <p v-else-if="report.domain_mapped" class="m-0 pb-1 text-muted-foreground" data-subscription-hosts-domain-note>
      其中 {{ report.domain_mapped }} 条是「域名 → 域名」，在 hosts 内最终指向 IP，Mihomo 直接连接这个 IP。
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
