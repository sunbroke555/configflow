<script setup lang="ts">
import { computed } from 'vue'
import { Loader2 } from '@lucide/vue'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { deviceGroupModeLabel, type DeviceGroupsPreview } from '@/types/deviceGroups'
const props = defineProps<{ preview: DeviceGroupsPreview; migration?: boolean; confirmLabel: string; busy: boolean; error?: string }>()
const emit = defineEmits<{ close: []; confirm: [] }>()
const migrationSources = computed(() => {
  const before = props.preview.migration?.sources_before || {}, after = props.preview.migration?.sources_after || {}
  return [...new Set([...Object.keys(before), ...Object.keys(after)])].map(id => ({ id, before: before[id] || [], after: after[id] || [] }))
})
</script>
<template>
  <Dialog :open="true" @update:open="value => { if (!value && !busy) emit('close') }">
    <DialogContent :show-close-button="false" class="flex h-[min(880px,90dvh)] max-h-dvh w-[calc(100%-2rem)] max-w-[960px] flex-col gap-0 overflow-hidden p-0 max-sm:inset-0 max-sm:h-dvh max-sm:w-full max-sm:max-w-none max-sm:translate-x-0 max-sm:translate-y-0 max-sm:rounded-none" @escape-key-down="event => { if (busy) event.preventDefault() }" @pointer-down-outside="event => { if (busy) event.preventDefault() }">
      <DialogHeader class="shrink-0 border-b border-border p-3 sm:p-5"><DialogTitle>{{ migration ? '升级为设备组' : '设备组变更预览' }}</DialogTitle><DialogDescription>核对实际生成差异与受影响设备。{{ migration ? '升级只保存草稿，之后仍需发布。' : '保存草稿与发布到设备是独立操作。' }}</DialogDescription></DialogHeader>
      <div class="min-h-0 flex-1 space-y-5 overflow-y-auto p-3 sm:p-5">
        <section v-if="migration" class="space-y-2"><h3 class="m-0 text-sm font-semibold">将创建的设备组</h3><ul class="m-0 list-none space-y-2 p-0"><li v-for="group in preview.draft.device_groups.groups" :key="group.id" class="break-words text-sm"><strong>{{ group.name }}</strong> · {{ group.id === 'default' ? '全局规则' : deviceGroupModeLabel(group.mode) }}<span class="ml-2 text-xs text-muted-foreground">{{ group.members.length }} 项成员</span><span class="mt-1 block break-all font-mono text-xs text-muted-foreground">{{ group.members.join('、') || '未归组设备兜底' }}</span></li></ul><p class="m-0 text-xs text-muted-foreground">直连 DNS 保持原有无缓存、无路由器联动的行为；Mihomo 新增连接层 DIRECT。落在直连范围的基础设施地址改按全局规则处理。</p></section>
        <section class="space-y-2"><h3 class="m-0 text-sm font-semibold">策略路由来源</h3><p class="m-0 break-all font-mono text-xs">包含：{{ preview.draft.policy_routing_sources.include.join('、') || '无' }}</p><p class="m-0 break-words text-xs">排除设备组：{{ preview.draft.policy_routing_sources.exclude_groups.map(id => preview.draft.device_groups.groups.find(group => group.id === id)?.name || id).join('、') || '无' }}</p><p v-if="preview.routing_changes.removed.length" class="m-0 break-words text-sm text-warning-accent" role="alert">以下来源将不再经过 RouterOS 策略路由：<span class="font-mono">{{ preview.routing_changes.removed.join('、') }}</span></p><p v-if="preview.routing_changes.added.length" class="m-0 break-words text-xs">新增有效来源：<span class="font-mono">{{ preview.routing_changes.added.join('、') }}</span></p></section>
        <p v-if="preview.routing_changes.affected_clients?.length" class="m-0 break-words text-sm text-warning-accent">查询样本中将离开策略路由的客户端：<span class="break-all font-mono">{{ preview.routing_changes.affected_clients.join('、') }}</span>。样本不代表全部受影响设备。</p>
        <details v-if="migration && migrationSources.length" class="min-w-0 rounded-md border border-border" data-migration-sources>
          <summary class="min-h-11 cursor-pointer px-3 py-3 text-xs">逐条核对迁移前后来源（{{ migrationSources.length }} 台 RouterOS）</summary>
          <div class="min-w-0 space-y-4 border-t border-border p-3">
            <p class="m-0 text-xs text-muted-foreground">每条来源均按实际生成顺序显示，请核对前后是否逐项一致。</p>
            <section v-for="router in migrationSources" :key="router.id" class="min-w-0 space-y-2" :data-router-source-comparison="router.id">
              <h3 class="m-0 break-all text-sm font-semibold">路由器 {{ router.id }}</h3>
              <div class="grid min-w-0 gap-3 sm:grid-cols-2">
                <div class="min-w-0" data-source-phase="before"><h4 class="mb-2 mt-0 text-xs font-semibold">升级前（{{ router.before.length }} 条）</h4><ol v-if="router.before.length" class="m-0 list-decimal space-y-1 pl-5 font-mono text-xs"><li v-for="(source, index) in router.before" :key="index" class="break-all">{{ source }}</li></ol><p v-else class="m-0 text-xs text-muted-foreground">升级前没有有效来源。</p></div>
                <div class="min-w-0" data-source-phase="after"><h4 class="mb-2 mt-0 text-xs font-semibold">升级后（{{ router.after.length }} 条）</h4><ol v-if="router.after.length" class="m-0 list-decimal space-y-1 pl-5 font-mono text-xs"><li v-for="(source, index) in router.after" :key="index" class="break-all">{{ source }}</li></ol><p v-else class="m-0 text-xs text-muted-foreground">升级后没有有效来源。</p></div>
              </div>
            </section>
          </div>
        </details>
        <section v-if="preview.copied_blocking_rules.length || preview.excluded_after_match_rules.length" class="space-y-2"><h3 class="m-0 text-sm font-semibold">仍执行 DNS 拦截</h3><p class="m-0 text-xs leading-relaxed">拦截优先：复制到组分段的规则，不再被它前面的普通允许规则遮挡。只复制第一条已启用 MATCH 之前的拦截规则。</p><ul class="m-0 list-inside list-disc space-y-1 p-0 text-xs"><li v-for="rule in preview.copied_blocking_rules" :key="rule.id">{{ rule.name }}<span class="ml-2 text-muted-foreground">{{ [rule.dns_action, rule.policy].filter(Boolean).join(' · ') }}</span></li></ul><p v-if="preview.excluded_after_match_rules.length" class="m-0 text-xs text-warning-accent">排在 MATCH 之后，未复制：{{ preview.excluded_after_match_rules.map(rule => rule.name).join('、') }}</p></section>
        <ul v-if="preview.warnings.length" class="m-0 list-none space-y-2 rounded-lg bg-warning-soft p-3 text-sm" role="status"><li v-for="(warning, index) in preview.warnings" :key="`${warning.code}:${index}`" class="break-words">{{ warning.message }}</li></ul>
        <p v-for="sample in preview.infrastructure_samples || []" :key="sample.ip" class="m-0 break-words text-xs text-warning-accent">基础设施 {{ sample.ip }}：样本内 {{ sample.count }} 次查询，占 {{ (sample.share * 100).toFixed(1) }}%。这些查询改按全局规则处理。</p>
        <section v-for="change in preview.generated_changes" :key="change.service_type" class="min-w-0 space-y-2"><h3 class="m-0 text-sm font-semibold">{{ change.service_type === 'oxidns' ? 'OxiDNS' : 'Mihomo' }} 生成差异</h3><p v-if="change.before === change.after" class="m-0 text-xs text-muted-foreground">生成结果没有变化。</p><details v-else class="rounded-md border border-border"><summary class="min-h-11 cursor-pointer px-3 py-3 text-xs">查看当前与变更后配置</summary><div class="grid min-w-0 gap-3 border-t border-border p-3 sm:grid-cols-2"><div class="min-w-0"><p class="mt-0 text-xs text-muted-foreground">当前</p><pre class="m-0 max-h-72 overflow-auto whitespace-pre font-mono text-xs leading-relaxed">{{ change.before }}</pre></div><div class="min-w-0"><p class="mt-0 text-xs text-primary-accent">变更后</p><pre class="m-0 max-h-72 overflow-auto whitespace-pre font-mono text-xs leading-relaxed">{{ change.after }}</pre></div></div></details></section>
        <section class="space-y-2"><h3 class="m-0 text-sm font-semibold">需要重新发布的 Agent</h3><ul class="m-0 list-none divide-y divide-border p-0"><li v-for="agent in preview.affected_agents" :key="agent.id" class="flex min-w-0 flex-wrap gap-x-2 gap-y-1 py-2 text-sm"><span class="min-w-0 break-words">{{ agent.name }}</span><span class="text-xs text-muted-foreground">{{ agent.service_type === 'mihomo' ? 'Mihomo' : 'OxiDNS' }} · {{ agent.status === 'online' && agent.enabled !== false ? '在线' : '离线或已禁用' }}</span><span v-if="agent.version_state === 'unsupported'" class="text-xs text-destructive-accent">内核版本不支持设备组</span><span v-else-if="agent.service_type === 'mihomo' && agent.version_state === 'unknown'" class="text-xs text-warning-accent">尚未验证内核版本</span></li></ul><p v-if="!preview.affected_agents.length" class="m-0 text-xs text-muted-foreground">当前没有受影响的 Agent，可先保存草稿。</p><p v-else class="m-0 text-xs text-muted-foreground">发布全部受影响的 Mihomo 后，再发布 OxiDNS；失败或结果待确认时暂停后续设备。发布包含该配置空间的完整已保存草稿。</p></section>
        <ul v-if="preview.blockers.length" class="m-0 list-none space-y-1 rounded-lg bg-destructive-soft p-3 text-sm text-destructive-accent" role="alert"><li v-for="blocker in preview.blockers" :key="blocker">{{ blocker }}</li></ul>
        <p v-if="error" role="alert" class="m-0 break-words text-sm text-destructive-accent">{{ error }}</p>
      </div>
      <DialogFooter class="shrink-0 border-t border-border p-3 pb-[max(.75rem,env(safe-area-inset-bottom))] sm:p-5"><Button variant="outline" class="min-h-11" :disabled="busy" @click="emit('close')">返回编辑</Button><Button class="min-h-11" :disabled="busy || !!preview.blockers.length" @click="emit('confirm')"><Loader2 v-if="busy" class="size-4 animate-spin" />{{ busy ? '正在处理…' : confirmLabel }}</Button></DialogFooter>
    </DialogContent>
  </Dialog>
</template>
