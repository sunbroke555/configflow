<template>
  <section class="min-w-0 space-y-3" :aria-label="label">
    <div class="flex flex-wrap items-center justify-between gap-2">
      <div class="flex items-center gap-1">
        <span class="text-xs text-muted-foreground">{{ rows.length }} 个条目</span>
        <FieldHelp :label="`${label}编辑方式`" :text="`${kind === 'upstreams' ? '逐条添加上游地址，展开后可配置官方全部上游参数；也可切换 YAML 批量编辑。' : '逐条添加、删除或调整顺序，也可在 YAML 列表中批量编辑。'} 切换模式保留配置值；条目修改后 YAML 注释和排版可能重新整理。无效 YAML 会保留为草稿，修正后才能切回条目并保存。`" />
      </div>
      <OxidnsEditorModeToggle :model-value="mode" :options="modes" :label="`${label}编辑模式`" @update:model-value="switchMode" />
    </div>
    <template v-if="mode === 'entries'">
      <p v-if="!rows.length" class="rounded-lg border border-dashed border-border px-3 py-5 text-sm text-muted-foreground">{{ emptyHint || '暂无条目，点击下方按钮添加。' }}</p>
      <div v-for="(row, index) in rows" :key="row.id" class="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-x-2 rounded-lg border border-border p-2">
        <div class="col-start-1 row-start-1 flex min-w-0 items-center gap-2" :class="kind === 'upstreams' ? 'max-sm:col-span-2' : ''">
          <span class="w-4 shrink-0 text-center font-mono text-xs text-muted-foreground">{{ index + 1 }}</span>
          <Input :id="index === 0 ? id : undefined" :model-value="kind === 'upstreams' ? upstreamAddress(row.value) : String(row.value)" :aria-label="`${label}第 ${index + 1} 项${kind === 'upstreams' ? '地址' : ''}`" :placeholder="placeholder" :type="kind === 'numbers' ? 'number' : 'text'" :min="kind === 'numbers' ? 1 : undefined" :max="kind === 'numbers' ? 65535 : undefined" class="h-9 min-w-0 flex-1 font-mono text-xs max-sm:min-h-11 pointer-coarse:min-h-11" @update:model-value="editValue(index, String($event))" />
        </div>
        <div class="col-start-2 row-start-1 flex shrink-0 items-center" :class="kind === 'upstreams' ? 'max-sm:row-start-2 max-sm:mt-1' : ''" role="group" :aria-label="`${label}第 ${index + 1} 项操作`">
          <Button type="button" variant="ghost" size="icon" class="size-9 text-muted-foreground max-sm:size-11 pointer-coarse:size-11" :disabled="index === 0" :aria-label="`上移${label}第 ${index + 1} 项`" title="上移" @click="move(index, -1)"><ArrowUp class="size-3.5" /></Button>
          <Button type="button" variant="ghost" size="icon" class="size-9 text-muted-foreground max-sm:size-11 pointer-coarse:size-11" :disabled="index === rows.length - 1" :aria-label="`下移${label}第 ${index + 1} 项`" title="下移" @click="move(index, 1)"><ArrowDown class="size-3.5" /></Button>
          <Button type="button" variant="ghost" size="icon" class="size-9 text-muted-foreground hover:bg-destructive-soft hover:text-destructive-accent max-sm:size-11 pointer-coarse:size-11" :aria-label="`删除${label}第 ${index + 1} 项`" title="删除" @click="remove(index)"><Trash2 class="size-3.5" /></Button>
        </div>
        <Collapsible v-if="kind === 'upstreams'" class="contents">
          <CollapsibleTrigger type="button" class="group col-span-2 col-start-1 row-start-2 mt-1 flex h-8 min-w-0 cursor-pointer items-center gap-2 rounded-md px-2 text-xs text-muted-foreground outline-none hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-primary-accent max-sm:col-span-1 max-sm:h-11 pointer-coarse:min-h-11" :aria-label="`${label}第 ${index + 1} 项上游参数`">
            <span class="shrink-0">上游参数</span>
            <span class="ml-auto truncate text-xs"><template v-if="parameterCount(row.value)"><span class="max-sm:sr-only">已设置 </span>{{ parameterCount(row.value) }} 项</template><template v-else>默认</template></span>
            <ChevronDown class="size-3.5 shrink-0 group-data-[state=open]:rotate-180" aria-hidden="true" />
          </CollapsibleTrigger>
          <CollapsibleContent force-mount class="data-[state=closed]:hidden col-span-2 col-start-1 row-start-3 mt-1 min-w-0 border-t border-border px-2 pt-3 pb-2">
            <div class="grid min-w-0 gap-3 sm:grid-cols-2">
            <FormField v-for="key in ['bootstrap', 'dial_addr']" :key="key" :label="key === 'bootstrap' ? '引导 DNS（bootstrap）' : '连接地址（dial_addr）'" v-bind="upstreamCopy[key]">
              <Input :model-value="upstreamField(row.value, key)" :aria-label="`${label}第 ${index + 1} 项 ${key}`" :placeholder="key === 'bootstrap' ? '例如 223.5.5.5:53' : '例如 1.1.1.1:443'" class="h-9 min-w-0 font-mono text-xs max-sm:min-h-11 pointer-coarse:min-h-11" @update:model-value="editUpstream(index, key, String($event))" />
            </FormField>
            <FormField class="sm:col-span-2" label="连接流水线（enable_pipeline）" hint="上游兼容时，复用同一连接发送多个查询。" :help="optionHelp('仅 TCP / DoT 上游支持；先用 probe upstream 检查兼容性。', '同一连接可同时发送多个查询，减少并发连接开销。UDP 不受影响，核心默认关闭；不兼容的上游开启后可能无法正常查询。')">
              <Select :model-value="pipeline(row.value)" @update:model-value="editUpstream(index, 'enable_pipeline', $event === 'default' ? undefined : $event === 'true')">
                <SelectTrigger class="h-9 w-full sm:w-40 max-sm:min-h-11 pointer-coarse:min-h-11" :aria-label="`${label}第 ${index + 1} 项连接流水线（enable_pipeline）`"><SelectValue /></SelectTrigger>
                <SelectContent><SelectItem value="default">核心默认</SelectItem><SelectItem value="true">启用</SelectItem><SelectItem value="false">关闭</SelectItem></SelectContent>
              </Select>
            </FormField>
            </div>
            <OxidnsUpstreamExtras :model-value="row.value" :label="`${label}第 ${index + 1} 项`" @update:model-value="row.value = $event; send()" @error="setExtraError(row.id, $event)" />
          </CollapsibleContent>
        </Collapsible>
      </div>
      <Button variant="outline" class="min-h-11" @click="add"><Plus class="size-4" />添加{{ kind === 'upstreams' ? '上游' : '条目' }}</Button>
    </template>
    <div v-else class="space-y-2">
      <Textarea :id="`${id}-yaml`" :model-value="yamlDraft" :aria-label="`${label} YAML`" :rows="Math.min(14, Math.max(5, rows.length * 3))" class="min-w-0 font-mono text-xs" spellcheck="false" @update:model-value="editYaml(String($event))" />
    </div>
    <p v-if="error" role="alert" class="break-words text-sm text-destructive-accent">{{ error }}</p>
  </section>
</template>

<script setup lang="ts">
import { onUnmounted, ref, watch } from 'vue'
import { ArrowDown, ArrowUp, ChevronDown, Plus, Trash2 } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import FormField from '@/components/common/FormField.vue'
import FieldHelp from '@/components/common/FieldHelp.vue'
import OxidnsEditorModeToggle from './OxidnsEditorModeToggle.vue'
import OxidnsUpstreamExtras from './OxidnsUpstreamExtras.vue'
import { encodeEntries, entryYaml, parseEntryModel, parseEntryYaml, type EntryKind, type EntryValue } from '@/lib/oxidnsEntries'
import { optionCopy, optionHelp } from '@/lib/optionCopy'

const upstreamCopy: Record<string, ReturnType<typeof optionCopy>> = {
  bootstrap: optionCopy('上游使用域名时，先用此 DNS 找到它的 IP。', '上游地址包含域名，需要避免依赖自身 DNS 形成循环查询。', '填写 IP:端口，如 223.5.5.5:53。上游已用 IP 时无需填写；同时指定连接地址 dial_addr 时，优先使用连接地址。'),
  dial_addr: optionCopy('直接指定上游 IP，同时保留域名进行身份验证。', '已知上游真实 IP，希望跳过域名解析或避免解析循环。', '连接指定 IP，但仍使用原域名核对 TLS 证书和 HTTP Host。此项优先于 bootstrap，不会将加密连接中的域名身份改成 IP。'),
}

const props = defineProps<{ modelValue: unknown; kind: EntryKind; label: string; id: string; placeholder?: string; emptyHint?: string }>()
const emit = defineEmits<{ 'update:modelValue': [value: string | EntryValue[]]; validity: [valid: boolean]; error: [message: string] }>()
const mode = ref<'entries' | 'yaml'>('entries')
const modes = [{ value: 'entries' as const, label: '条目' }, { value: 'yaml' as const, label: 'YAML' }]
const rows = ref<Array<{ id: number; value: EntryValue }>>([])
const yamlDraft = ref('')
const error = ref('')
let sequence = 0
let expected: string | undefined
const extraErrors = new Map<number, string>()
let baseError = ''
const reportErrors = () => { const message = baseError || [...extraErrors.values()].find(Boolean) || ''; error.value = message; emit('validity', !message); emit('error', message) }
const setError = (message: string) => { baseError = message; reportErrors() }
const setExtraError = (id: number, message: string) => { if (message) extraErrors.set(id, message); else extraErrors.delete(id); reportErrors() }
onUnmounted(() => { emit('validity', true); emit('error', '') })
const values = () => rows.value.map(row => row.value)
const replaceRows = (entries: EntryValue[]) => { rows.value = entries.map(value => ({ id: ++sequence, value })) }
const send = () => {
  try {
    const value = encodeEntries(values(), props.kind)
    expected = JSON.stringify(value)
    emit('update:modelValue', value)
    setError('')
    return true
  } catch (cause: any) { setError(cause.message); return false }
}
watch(() => props.modelValue, value => {
  if (JSON.stringify(value) === expected) return
  try { replaceRows(parseEntryModel(value, props.kind)); yamlDraft.value = entryYaml(values()); setError('') }
  catch (cause: any) { mode.value = 'yaml'; yamlDraft.value = typeof value === 'string' ? value : entryYaml(value); setError(cause.message) }
}, { immediate: true, deep: true })
const switchMode = (next: 'entries' | 'yaml') => {
  if (next === mode.value) return
  if (next === 'yaml' && error.value) return
  if (next === 'entries') {
    try { replaceRows(parseEntryYaml(yamlDraft.value, props.kind)); if (!send()) return }
    catch (cause: any) { setError(cause.message); return }
  } else { yamlDraft.value = entryYaml(values()) }
  mode.value = next
}
const editYaml = (text: string) => {
  yamlDraft.value = text
  try { replaceRows(parseEntryYaml(text, props.kind)); send() }
  catch (cause: any) { setError(cause.message) }
}
const add = () => { rows.value.push({ id: ++sequence, value: props.kind === 'upstreams' ? { addr: '' } : '' }); send() }
const remove = (index: number) => { rows.value.splice(index, 1); send() }
const move = (index: number, step: number) => { const [row] = rows.value.splice(index, 1); rows.value.splice(index + step, 0, row); send() }
const upstreamAddress = (value: EntryValue) => typeof value === 'string' ? value : typeof value === 'object' ? value.addr : ''
const upstreamField = (value: EntryValue, key: string) => typeof value === 'object' ? value[key] ?? '' : ''
const pipeline = (value: EntryValue) => typeof value === 'object' && typeof value.enable_pipeline === 'boolean' ? String(value.enable_pipeline) : 'default'
const parameterCount = (value: EntryValue) => typeof value === 'object' ? Object.keys(value).filter(key => key !== 'addr' && value[key] !== undefined).length : 0
const editValue = (index: number, value: string) => {
  if (props.kind === 'upstreams') {
    const before = rows.value[index].value
    rows.value[index].value = typeof before === 'string' ? value : { ...before as Record<string, any>, addr: value }
  } else rows.value[index].value = props.kind === 'numbers' && value !== '' ? Number(value) : value
  send()
}
const editUpstream = (index: number, key: string, value: unknown) => {
  const before = rows.value[index].value
  const entry: Record<string, any> = typeof before === 'object' ? { ...before } : { addr: before }
  if (value === undefined || value === '') delete entry[key]
  else entry[key] = value
  rows.value[index].value = entry
  send()
}
</script>
