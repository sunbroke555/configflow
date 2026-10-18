<template>
  <Dialog :open="!!row" @update:open="value => { if (!value) emit('close') }">
    <DialogContent class="max-h-[90dvh] max-w-[720px] overflow-y-auto max-md:[&_[data-slot=dialog-close]]:min-h-11 max-md:[&_[data-slot=dialog-close]]:min-w-11">
      <DialogHeader>
        <DialogTitle class="flex flex-wrap items-center gap-2">
          连接详情
          <span v-if="row?.closedAt" class="chip">已关闭</span>
          <span v-else class="chip chip-ok">活跃</span>
        </DialogTitle>
        <DialogDescription class="break-all font-mono">{{ row?.target }}</DialogDescription>
      </DialogHeader>
      <Tabs v-if="row" v-model="tab" class="min-w-0">
        <TabsList class="grid min-h-11 w-full grid-cols-3">
          <TabsTrigger value="overview" class="min-h-11">概览</TabsTrigger>
          <TabsTrigger value="raw" class="min-h-11">原始数据</TabsTrigger>
          <TabsTrigger value="proxy" class="min-h-11">代理</TabsTrigger>
        </TabsList>
        <TabsContent value="overview" class="mt-4 space-y-3">
          <section v-for="section in sections" :key="section.title" class="rounded-xl border border-border bg-background p-4">
            <h3 class="m-0 mb-2 text-[13px] font-semibold text-foreground">{{ section.title }}</h3>
            <dl class="m-0 grid grid-cols-[6.5rem_minmax(0,1fr)] gap-x-3 gap-y-1.5 text-[13px]">
              <template v-for="item in section.items" :key="item.label">
                <dt class="text-muted-foreground">{{ item.label }}</dt>
                <dd class="m-0 break-all" :class="{ 'font-mono text-xs leading-5': item.mono }">{{ item.value }}</dd>
              </template>
            </dl>
          </section>
        </TabsContent>
        <TabsContent value="raw" class="mt-4">
          <pre class="m-0 max-h-[60dvh] overflow-auto rounded-xl border border-border bg-background p-4 font-mono text-xs leading-5 text-foreground">{{ rawText }}</pre>
        </TabsContent>
        <TabsContent value="proxy" class="mt-4">
          <ol class="m-0 list-none space-y-2 p-0">
            <li v-for="(hop, index) in hops" :key="index" class="flex min-h-11 items-center gap-3 rounded-xl border border-border bg-background px-4 py-2">
              <span class="num w-5 shrink-0 text-xs text-muted-foreground">{{ index + 1 }}</span>
              <span class="min-w-0 flex-1 break-all text-[13px] text-foreground">{{ hop }}</span>
              <span class="shrink-0 text-xs text-muted-foreground">{{ index === 0 ? '命中策略' : index === hops.length - 1 ? '出站节点' : '策略组' }}</span>
            </li>
          </ol>
          <p class="mt-3 mb-0 text-xs text-muted-foreground">由规则 {{ ruleText }} 命中，按顺序逐级选择到最终出站节点。</p>
        </TabsContent>
      </Tabs>
    </DialogContent>
  </Dialog>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { formatBytes } from '@/lib/format'
import { sourceLabel, type MihomoConnection } from '@/types/mihomo'

export interface ConnectionRow extends MihomoConnection {
  raw: MihomoConnection
  target: string
  chain: string
  upSpeed: number
  downSpeed: number
  /** 前端发现连接消失的时间（毫秒）；活跃连接为空 */
  closedAt?: number
}

const props = defineProps<{ row: ConnectionRow | null }>()
const emit = defineEmits<{ close: [] }>()
const tab = ref('overview')
watch(() => props.row?.id, () => { tab.value = 'overview' })

const speedText = (value: number) => formatBytes(value) + '/s'
const timeText = (value: number) => Number.isFinite(value) ? new Date(value).toLocaleString() : '—'
const dash = (value: string | undefined | null) => value || '—'

const ruleText = computed(() => props.row ? (props.row.rulePayload ? `${props.row.rule}: ${props.row.rulePayload}` : props.row.rule) : '')
const hops = computed(() => props.row ? [...props.row.chains].reverse() : [])
const rawText = computed(() => props.row ? JSON.stringify(props.row.raw, null, 2) : '')

const sections = computed(() => {
  const row = props.row
  if (!row) return []
  const meta = row.metadata
  const started = Date.parse(row.start)
  const ended = row.closedAt ?? Date.now()
  const seconds = Math.max(0, Math.round((ended - started) / 1000))
  const inbound = [meta.inboundName, meta.inboundUser].filter(Boolean).join(' · ')
  return [
    {
      title: '基本',
      items: [
        { label: 'ID', value: row.id, mono: true },
        { label: '类型', value: `${meta.type} | ${meta.network}` },
        { label: '连接时间', value: timeText(started) },
        { label: row.closedAt ? '关闭时间' : '已持续', value: row.closedAt ? timeText(row.closedAt) : `${seconds} 秒` },
        { label: '规则', value: ruleText.value },
        { label: '入站', value: dash(inbound) }
      ]
    },
    {
      title: '来源与目的',
      items: [
        { label: '源 IP', value: sourceLabel(row), mono: true },
        { label: '源端口', value: dash(meta.sourcePort), mono: true },
        { label: '主机', value: row.target, mono: true },
        { label: '嗅探域名', value: dash(meta.sniffHost), mono: true },
        { label: '解析地址', value: dash(meta.destinationIP), mono: true },
        { label: '解析类型', value: meta.destinationIP ? (meta.destinationIP.includes(':') ? 'IPv6' : 'IPv4') : '—' },
        { label: '远端地址', value: dash(meta.remoteDestination), mono: true },
        { label: '进程', value: dash(meta.processPath || meta.process), mono: true }
      ]
    },
    {
      title: '流量',
      items: [
        { label: '下载', value: formatBytes(row.download) },
        { label: '上传', value: formatBytes(row.upload) },
        { label: '下载速度', value: row.closedAt ? '—' : speedText(row.downSpeed) },
        { label: '上传速度', value: row.closedAt ? '—' : speedText(row.upSpeed) }
      ]
    },
    {
      title: '出站节点',
      items: [
        { label: '代理链', value: dash(row.chain) },
        { label: '出站节点', value: dash(row.chains[0]) }
      ]
    }
  ]
})
</script>
