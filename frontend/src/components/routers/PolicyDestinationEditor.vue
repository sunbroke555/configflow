<template>
  <Dialog :open="true" @update:open="value => { if (!value) close() }">
    <DialogContent class="sm:max-w-2xl" :show-close-button="false">
      <DialogHeader><DialogTitle>编辑目的规则集</DialogTitle><DialogDescription>在原规则内容中新增、修改或删除 IP，再回到面板预览发布。</DialogDescription></DialogHeader>
      <div class="grid min-w-0 gap-4">
        <div class="space-y-2"><Label for="destination-edit-set">规则集</Label>
          <Select :model-value="selected || undefined" :disabled="saving" @update:model-value="value => choose(String(value))">
            <SelectTrigger id="destination-edit-set" class="min-h-11 w-full"><SelectValue placeholder="选择要编辑的规则集" /></SelectTrigger>
            <SelectContent><SelectItem v-for="rule in rules" :key="rule.id" :value="rule.id">{{ rule.name }}{{ selectedIds.includes(rule.id) ? ' · 策略路由' : '' }}</SelectItem></SelectContent>
          </Select>
        </div>
        <p v-if="address" class="m-0 break-words text-xs text-muted-foreground">已下发网段 {{ address }}。ROS 中的网段可能合并自多个条目，请核对原规则集后修改。</p>
        <p v-if="selected && !selectedIds.includes(selected)" class="m-0 text-xs text-muted-foreground">此规则集当前未选为策略路由。编辑内容不会自动勾选；请到「规则集与网关」选择。</p>
        <LoadingRows v-if="loading" :rows="3" />
        <template v-else-if="data">
          <div class="flex items-start gap-2 rounded-md bg-info-soft p-3 text-xs leading-relaxed text-info-accent">
            <Info class="mt-0.5 size-4 shrink-0" aria-hidden="true" />
            <div class="grid min-w-0 gap-1">
              <p class="m-0 font-medium">共享规则内容 · {{ data.usage_count }} 处引用</p>
              <p class="m-0">保存会影响所有引用它的配置空间。保存后仍需预览发布，才会下发到设备。</p>
            </div>
          </div>
          <template v-if="data.rule.source_type === 'content'">
            <div class="space-y-2"><Label for="destination-edit-content">规则内容 · {{ data.rule.behavior || '原格式' }}</Label><Textarea id="destination-edit-content" v-model="content" :disabled="saving" class="min-h-64 w-full font-mono text-xs" spellcheck="false" /></div>
            <p class="m-0 text-xs text-muted-foreground">保留原格式；删除原行即移除该条目。若其他规则集也包含同一 IP，发布后仍可能保留。</p>
          </template>
          <div v-else class="space-y-2 text-sm"><p class="m-0">此规则集由订阅源更新，不能在这里修改下载内容。手动添加 IP 请选用内容型规则集。</p><p class="m-0 break-all font-mono text-xs text-muted-foreground">{{ data.rule.url }}</p><Button as-child variant="outline" class="min-h-11"><a href="/rule-library" target="_blank" rel="noopener noreferrer">打开规则仓库<ExternalLink class="size-4" /></a></Button></div>
        </template>
        <p v-else-if="!rules.length" class="m-0 text-sm text-muted-foreground">当前配置空间还没有规则集，请先在规则配置中添加。</p>
        <p v-if="error" role="alert" class="m-0 break-words text-sm text-destructive-accent">{{ error }}</p>
      </div>
      <DialogFooter class="gap-2 sm:justify-between">
        <Button v-if="selected" variant="ghost" class="min-h-11" :disabled="loading || saving" @click="reload">重新读取</Button>
        <div class="flex flex-wrap justify-end gap-2"><Button variant="outline" class="min-h-11" :disabled="saving" @click="close">关闭</Button><Button v-if="data?.rule.source_type === 'content'" class="min-h-11" :disabled="loading || saving || !dirty" @click="save"><Loader2 v-if="saving" class="size-4 animate-spin" />保存规则内容</Button></div>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { ExternalLink, Info, Loader2 } from '@lucide/vue'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import LoadingRows from '@/components/common/LoadingRows.vue'
import { ruleContentApi, type PolicyWorkspace, type RuleContentDraft } from '@/api'
import { confirm, notify } from '@/lib/feedback'
import { prepareRuleContent } from '@/lib/ruleset-content'
import { beginScopedRequest, endScopedRequest } from '@/profileContext'
const props = defineProps<{ rules: PolicyWorkspace['rule_sets']; selectedIds: string[]; address?: string }>()
const emit = defineEmits<{ close: []; saved: [] }>()
const initialIds = props.selectedIds.filter(id => props.rules.some(rule => rule.id === id))
const selected = ref(initialIds.length === 1 ? initialIds[0]! : props.rules.length === 1 ? props.rules[0]!.id : '')
const data = ref<RuleContentDraft | null>(null), content = ref(''), error = ref('')
const loading = ref(false), saving = ref(false)
const dirty = computed(() => !!data.value && data.value.rule.source_type === 'content' && content.value !== data.value.rule.content)
const message = (failure: any) => failure?.response?.data?.message || failure?.message || '操作失败，请重试。'
let epoch = 0
async function canLeave() { return !saving.value && (!dirty.value || await confirm('放弃尚未保存的规则内容？', { confirmText: '放弃修改' })) }
async function close() { if (await canLeave()) emit('close') }
async function choose(id: string) { if (id !== selected.value && await canLeave()) { selected.value = id; await load() } }
async function reload() { if (await canLeave()) await load() }
async function load() {
  const current = ++epoch
  data.value = null; content.value = ''; error.value = ''; loading.value = false
  const library = props.rules.find(rule => rule.id === selected.value)?.library_rule_id
  if (!selected.value) return
  if (!library) { error.value = '找不到此规则集的共享来源，请重新载入面板。'; return }
  loading.value = true
  try {
    const response = await ruleContentApi.read(library)
    if (current !== epoch) return
    if (!response.data.success) throw new Error('读取规则内容失败。')
    data.value = response.data; content.value = response.data.rule.content
  } catch (failure) { if (current === epoch) error.value = message(failure) }
  finally { if (current === epoch) loading.value = false }
}
async function save() {
  if (!data.value || !dirty.value || saving.value || loading.value) return
  const current = epoch, id = data.value.rule.id
  saving.value = true; error.value = ''; beginScopedRequest()
  try {
    const checked = await prepareRuleContent({ ...data.value.rule, content: content.value })
    if (current !== epoch || checked === null) return
    content.value = checked
    const response = await ruleContentApi.save(id, data.value.revision, checked)
    if (current !== epoch) return
    if (!response.data.success) throw new Error('保存失败，请重新读取核对。')
    data.value = { ...data.value!, revision: response.data.revision, rule: { ...data.value!.rule, content: content.value } }
    notify.success('规则内容已保存，请重新预览后发布。'); emit('saved')
  } catch (failure) { if (current === epoch) error.value = message(failure) }
  finally { endScopedRequest(); if (current === epoch) saving.value = false }
}
void load()
onBeforeUnmount(() => { epoch++ })
defineExpose({ canLeave })
</script>
