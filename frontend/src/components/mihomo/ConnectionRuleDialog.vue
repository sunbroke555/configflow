<template>
  <Dialog :open="true" @update:open="value => { if (!value && !saving) emit('close') }">
    <DialogContent class="max-h-[90dvh] max-w-lg overflow-y-auto" @escape-key-down="event => { if (saving) event.preventDefault() }" @interact-outside="event => { if (saving) event.preventDefault() }">
      <DialogHeader>
        <DialogTitle>加入{{ target === 'proxy' ? '代理' : '直连' }}规则集</DialogTitle>
        <DialogDescription>使用此 Mihomo 所属配置空间在「域名发现」中设置的默认规则集。</DialogDescription>
      </DialogHeader>
      <p v-if="loading" role="status" class="text-sm text-muted-foreground">正在读取默认规则集…</p>
      <p v-if="error" role="alert" class="m-0 text-sm text-destructive-accent">{{ error }}</p>
      <template v-if="state">
        <div class="rounded-lg border bg-muted/40 p-3 text-sm">
          <p class="m-0 break-words">{{ state.profile_name }} · {{ selectedTarget?.name || '未设置默认规则集' }}</p>
          <p v-if="!selectedTarget || selectedTarget.problem" role="alert" class="mb-0 mt-2 text-xs text-warning-accent">请先在此配置空间的「域名发现」设置可编辑的默认{{ target === 'proxy' ? '代理' : '直连' }}规则集。</p>
          <p v-else-if="!selectedTarget.active" class="mb-0 mt-2 text-xs text-warning-accent">此规则集尚未在当前配置中启用，保存后还需启用并发布。</p>
        </div>
        <FormField label="连接域名" html-for="connection-rule-domain">
          <Select v-model="domain" :disabled="saving || !!result"><SelectTrigger id="connection-rule-domain" class="min-h-11 w-full min-w-0 font-mono"><SelectValue /></SelectTrigger>
            <SelectContent><SelectItem v-for="host in domains" :key="host" :value="host">{{ host }}</SelectItem></SelectContent>
          </Select>
        </FormField>
        <FormField label="匹配范围" html-for="connection-rule-type">
          <Select v-model="ruleType" :disabled="saving || !!result"><SelectTrigger id="connection-rule-type" class="min-h-11 w-full"><SelectValue /></SelectTrigger>
            <SelectContent><SelectItem value="DOMAIN">仅此域名</SelectItem><SelectItem value="DOMAIN-SUFFIX">此域名及其子域名</SelectItem></SelectContent>
          </Select>
        </FormField>
        <p class="m-0 break-all rounded-lg bg-muted/40 p-3 font-mono text-xs">{{ ruleType }},{{ domain }}</p>
        <p class="m-0 text-xs leading-relaxed text-muted-foreground">将保存到共享规则集，其他引用它的配置也会更新。设备按现有规则同步机制生效，已有连接不会被断开。</p>
        <div v-if="result" role="status" class="space-y-2 text-sm">
          <p class="m-0 text-success-accent">{{ result.added.length ? '已加入规则集。' : '规则集已包含此域名，无需重复添加。' }}</p>
          <p v-for="warning in result.warnings" :key="warning" class="m-0 text-xs text-warning-accent">{{ warning }}</p>
        </div>
      </template>
      <DialogFooter>
        <Button variant="outline" class="min-h-11" :disabled="saving" @click="emit('close')">{{ result ? '完成' : '取消' }}</Button>
        <Button v-if="error && !state" variant="outline" class="min-h-11" @click="load">重试</Button>
        <Button v-if="!result" class="min-h-11" :disabled="loading || saving || !selectedTarget || !!selectedTarget.problem || !domain" @click="save"><Loader2 v-if="saving" class="size-4 animate-spin" />确认添加</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { Loader2 } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import FormField from '@/components/common/FormField.vue'
import { clientToolsApi } from '@/api'
import type { ConnectionRuleTargets } from '@/lib/device-notes'
import { panelError } from '@/types/mihomo'
const props = defineProps<{ agentId: string; domains: string[]; target: 'direct' | 'proxy' }>()
const emit = defineEmits<{ close: [] }>()
const state = ref<ConnectionRuleTargets | null>(null)
const selectedTarget = computed(() => state.value?.[props.target])
const domain = ref(props.domains[0] || '')
const ruleType = ref<'DOMAIN' | 'DOMAIN-SUFFIX'>('DOMAIN')
const result = ref<{ added: unknown[]; skipped: unknown[]; warnings: string[] } | null>(null)
const error = ref('')
const loading = ref(true)
const saving = ref(false)
let disposed = false
async function load() {
  loading.value = true; error.value = ''
  try {
    const { data } = await clientToolsApi.ruleTargets(props.agentId)
    if (disposed) return
    if (!data?.success) throw new Error('读取默认规则集失败。')
    state.value = data
  } catch (failure) { if (!disposed) error.value = panelError(failure) }
  finally { if (!disposed) loading.value = false }
}
async function save() {
  if (!state.value || !selectedTarget.value || selectedTarget.value.problem || saving.value || result.value) return
  saving.value = true; error.value = ''
  try {
    const { data } = await clientToolsApi.addRule(props.agentId, { profile_id: state.value.profile_id, ruleset_id: selectedTarget.value.id, target: props.target, value: domain.value, rule_type: ruleType.value })
    if (disposed) return
    if (!data?.success) throw new Error('保存规则失败。')
    result.value = data
  } catch (failure) { if (!disposed) error.value = panelError(failure) }
  finally { if (!disposed) saving.value = false }
}
onMounted(load)
onUnmounted(() => { disposed = true })
</script>
