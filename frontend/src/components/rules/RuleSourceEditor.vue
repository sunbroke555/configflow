<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { isAxiosError } from 'axios'
import api from '@/api'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import FormField from '@/components/common/FormField.vue'
import Segmented from '@/components/common/Segmented.vue'
import RuleContentIssues from '@/components/rules/RuleContentIssues.vue'
import { CLASSICAL_CONTENT_HINT, prepareRuleContent, type RuleContentCheck } from '@/lib/ruleset-content'
import { detectRuleContentFormat } from '@/lib/ruleset-format'

const props = defineProps<{ source: Record<string, any> }>()
const emit = defineEmits<{
  saved: [source: Record<string, any>]
  busy: [value: boolean]
  dirty: [value: boolean]
}>()

// This editor receives a shared library entry, never a hydrated profile reference.
const sourceId = String(props.source.id)
const sourceEnabled = props.source.enabled !== false
const draft = reactive({
  name: String(props.source.name || ''),
  source_type: (props.source.source_type === 'content' ? 'content' : 'url') as 'url' | 'content',
  url: String(props.source.url || ''),
  content: String(props.source.content || ''),
  behavior: String(props.source.behavior || 'domain'),
  format: String(props.source.format || 'auto')
})
const saving = ref(false)
const saveError = ref('')
const contentCheck = ref<RuleContentCheck | null>(null)
let initial = JSON.stringify(draft)
watch(() => JSON.stringify(draft), value => emit('dirty', value !== initial), { immediate: true, flush: 'sync' })
watch(() => [draft.content, draft.behavior, draft.format, draft.source_type], () => { contentCheck.value = null })
watch(() => draft.source_type, value => { if (value === 'content' && draft.format === 'mrs') draft.format = 'auto' }, { flush: 'sync' })

const sourceOptions: Array<{ value: 'url' | 'content'; label: string }> = [
  { value: 'url', label: '远程地址' }, { value: 'content', label: '粘贴内容' }
]
const contentHint = computed(() => draft.behavior === 'domain'
  ? 'example.com 仅匹配此域名；+.example.com 包含它的所有子域名。保留现有写法，每行一条，不加 DOMAIN 等前缀。'
  : draft.behavior === 'ipcidr' ? '每行一个 IPv4 或 IPv6 网段，例如 192.0.2.0/24。' : CLASSICAL_CONTENT_HINT)

function validUrl(value: string) {
  if (!/^https?:\/\//i.test(value)) return false
  try {
    const parsed = new URL(value)
    return ['http:', 'https:'].includes(parsed.protocol) && !!parsed.hostname
  } catch { return false }
}

async function save(): Promise<boolean> {
  if (saving.value) return false
  saveError.value = ''
  if (!draft.name.trim()) {
    saveError.value = '请输入规则名称'
    return false
  }
  if (draft.source_type === 'url' && !validUrl(draft.url.trim())) {
    saveError.value = '请输入有效的 http:// 或 https:// 规则地址'
    return false
  }
  if (draft.source_type === 'content' && !draft.content.trim()) {
    saveError.value = '请输入规则内容'
    return false
  }

  saving.value = true
  emit('busy', true)
  let saved: Record<string, any> | null = null
  try {
    // The server keeps scheduling metadata; do not send profile/DNS fields or a
    // stale update_interval from when the dialog was opened.
    const payload: Record<string, any> = {
      id: sourceId, name: draft.name.trim(), source_type: draft.source_type,
      behavior: draft.behavior, enabled: sourceEnabled
    }
    if (draft.source_type === 'url') {
      payload.url = draft.url.trim()
      if (draft.format !== 'auto') payload.format = draft.format
    } else {
      payload.format = draft.format === 'auto' ? detectRuleContentFormat(draft.content) : draft.format
      const content = await prepareRuleContent({ behavior: draft.behavior, content: draft.content, format: payload.format }, undefined, check => { contentCheck.value = check })
      if (content === null) return false
      payload.content = content
    }
    const { data } = await api.put(`/rule-library/${encodeURIComponent(sourceId)}`, payload)
    const updated: Record<string, any> = { ...(data.data || payload) }
    delete updated.has_ip_part
    saved = updated
    initial = JSON.stringify(draft)
    emit('dirty', false)
  } catch (error) {
    saveError.value = isAxiosError<{ message?: string }>(error)
      ? error.response?.data?.message || error.message || '保存来源失败'
      : error instanceof Error ? error.message : '保存来源失败'
    return false
  } finally {
    saving.value = false
    emit('busy', false)
  }
  emit('saved', saved!)
  return true
}

defineExpose({ save })
</script>

<template>
  <fieldset :disabled="saving" class="m-0 flex min-w-0 flex-col gap-4 border-0 p-0" data-rule-source-editor>
    <FormField label="规则名称" html-for="source-name">
      <Input id="source-name" v-model="draft.name" class="bg-background/50" placeholder="例如 Epic" />
    </FormField>
    <FormField label="来源">
      <Segmented v-model="draft.source_type" :options="sourceOptions" :disabled="saving" label="规则集来源" block />
    </FormField>
    <FormField v-if="draft.source_type === 'url'" label="规则地址" html-for="source-url">
      <Input id="source-url" v-model="draft.url" class="bg-background/50 font-mono" placeholder="https://…" />
    </FormField>
    <FormField v-else label="规则内容" html-for="source-content" :hint="contentHint" persistent-hint>
      <Textarea id="source-content" v-model="draft.content" class="min-h-[180px] bg-background/50 font-mono text-[12.5px]" :rows="8"
        :placeholder="draft.behavior === 'domain' ? '+.example.com（每行一条）' : draft.behavior === 'ipcidr' ? '192.0.2.0/24（每行一条）' : 'DOMAIN-SUFFIX,example.com（每行一条）'" />
      <RuleContentIssues :check="contentCheck" />
    </FormField>
    <div class="grid grid-cols-2 gap-3 max-sm:grid-cols-1">
      <FormField label="内容类型">
        <Select v-model="draft.behavior" :disabled="saving">
          <SelectTrigger class="w-full bg-background/50" aria-label="内容类型"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="domain">域名 domain</SelectItem>
            <SelectItem value="ipcidr">IP 网段 ipcidr</SelectItem>
            <SelectItem value="classical">混合 classical</SelectItem>
          </SelectContent>
        </Select>
      </FormField>
      <FormField label="内容格式" :hint="draft.source_type === 'content' ? 'Text 每行一条；YAML 使用 payload 列表。自动识别会按内容保存格式。' : '自动按地址后缀识别；地址与实际内容格式不同时，可手动指定。'">
        <Select v-model="draft.format" :disabled="saving">
          <SelectTrigger class="w-full bg-background/50" aria-label="内容格式"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="auto">自动识别</SelectItem>
            <SelectItem value="text">Text · 每行一条</SelectItem>
            <SelectItem value="yaml">YAML · payload 列表</SelectItem>
            <SelectItem v-if="draft.source_type === 'url'" value="mrs">MRS · 二进制规则</SelectItem>
          </SelectContent>
        </Select>
      </FormField>
    </div>
    <p v-if="draft.source_type === 'url' && draft.behavior === 'classical'" class="m-0 text-xs leading-relaxed text-muted-foreground">{{ CLASSICAL_CONTENT_HINT }}</p>
    <Alert v-if="saveError" class="border-destructive-accent/30" data-source-save-error>
      <AlertDescription role="alert" class="whitespace-pre-wrap break-all text-destructive-accent">{{ saveError }}</AlertDescription>
    </Alert>
  </fieldset>
</template>
