<template>
  <div class="flex min-w-0 flex-col gap-3" data-testid="oxidns-match-editor">
    <div class="flex flex-wrap items-center justify-between gap-2">
      <div class="flex w-full items-center gap-1 sm:w-auto">
        <span class="text-sm font-medium">自定义匹配</span>
        <FieldHelp label="自定义匹配" :text="allowDisable ? '按条目顺序执行；同一条目的多个条件需要同时满足。暂停的条目不参与生成。扩展字段可在 YAML 中编辑，切换模式保留其值；无效 YAML 会保留为草稿，修正后才能保存。' : '按条目顺序执行；同一条目的多个条件需要同时满足。这里编辑实际执行的匹配项，不包含暂停状态；删除条目可移除规则。扩展字段可在 YAML 中编辑，无效输入会保留为草稿，修正后才能保存。'" />
      </div>
      <div class="flex items-center gap-2">
        <OxidnsEditorModeToggle :model-value="mode" :options="[{ value: 'entries', label: '条目' }, { value: 'yaml', label: 'YAML' }]" label="自定义匹配编辑模式" :disabled="disabled" @update:model-value="changeMode" />
        <Button v-if="mode === 'entries'" type="button" variant="outline" size="sm" class="h-9 text-xs max-sm:h-11 sm:pointer-coarse:h-[50px]" :disabled="disabled" @click="addItem">
          <Plus class="size-3.5" />添加匹配项
        </Button>
      </div>
    </div>

    <template v-if="mode === 'entries'">
      <p v-if="items.length === 0" class="m-0 rounded-lg border border-border bg-background/40 p-3 text-xs text-muted-foreground">尚未添加自定义匹配项，点击「添加匹配项」设置条件和动作。</p>
      <section v-for="(item, index) in items" :key="item.key" class="flex min-w-0 flex-col gap-3 rounded-lg border border-border bg-background/40 p-3" :aria-label="`匹配项 ${index + 1}`" data-testid="match-entry">
        <div class="flex flex-wrap items-center gap-2">
          <span class="text-sm font-medium">匹配项 {{ index + 1 }}</span>
          <label v-if="allowDisable" class="flex min-h-11 items-center gap-2 text-xs text-muted-foreground">
            <Switch :model-value="item.data.enabled !== false" :disabled="disabled" :aria-label="`启用匹配项 ${index + 1}`" @update:model-value="setEnabled(item, $event)" />
            {{ item.data.enabled === false ? '已暂停' : '启用' }}
          </label>
          <div class="ml-auto flex items-center">
            <Button type="button" variant="ghost" size="icon" class="size-11" :aria-label="`上移匹配项 ${index + 1}`" :disabled="disabled || index === 0" @click="moveItem(index, -1)"><ChevronUp class="size-4" /></Button>
            <Button type="button" variant="ghost" size="icon" class="size-11" :aria-label="`下移匹配项 ${index + 1}`" :disabled="disabled || index === items.length - 1" @click="moveItem(index, 1)"><ChevronDown class="size-4" /></Button>
            <Button type="button" variant="ghost" size="icon" class="size-11 text-destructive-accent hover:bg-destructive-soft" :aria-label="`删除匹配项 ${index + 1}`" :disabled="disabled" @click="removeItem(index)"><Trash2 class="size-4" /></Button>
          </div>
        </div>
        <div class="flex min-w-0 flex-col gap-2">
          <div class="flex flex-wrap items-center justify-between gap-2">
            <div class="flex items-center gap-1">
              <span class="text-xs font-medium">匹配条件</span>
              <FieldHelp :label="`匹配项 ${index + 1} 条件`" :text="optionHelp('需要限制某条动作在哪些请求上生效时填写。', '每条一个表达式，如 qname full:example.com 匹配域名，qtype 1 匹配 A 查询。多条件须全部满足；不填则对所有到达此处的请求生效。')" />
            </div>
            <Button type="button" variant="ghost" size="sm" class="min-h-11" :aria-label="`为匹配项 ${index + 1} 添加条件`" :disabled="disabled" @click="addCondition(item)"><Plus class="size-3.5" />添加条件</Button>
          </div>
          <div v-for="(condition, conditionIndex) in conditions(item)" :key="conditionIndex" class="flex min-w-0 items-start gap-1">
            <Textarea :model-value="condition" :rows="1" class="min-h-11 min-w-0 flex-1 font-mono text-xs" :aria-label="`匹配项 ${index + 1} 条件 ${conditionIndex + 1}`" :disabled="disabled" placeholder="例如 qname full:example.com 或 qtype 1" @update:model-value="setCondition(item, conditionIndex, String($event))" />
            <Button type="button" variant="ghost" size="icon" class="size-11 shrink-0 text-muted-foreground" :aria-label="`删除匹配项 ${index + 1} 条件 ${conditionIndex + 1}`" :disabled="disabled" @click="removeCondition(item, conditionIndex)"><Trash2 class="size-4" /></Button>
          </div>
        </div>
        <FormField label="执行动作" :html-for="`${item.key}-action`" hint="条件全部满足时，使用所选 DNS 或返回指定应答。" :help="optionHelp('同一匹配项的条件全部满足后，执行所选动作。', '直连、代理、备用进入对应 DNS 流程；拒绝返回 REFUSED，不存在返回 NXDOMAIN，空应答返回无答案的 NOERROR。自定义可用 $插件名 或 goto 序列名。')">
          <Select :model-value="item.customAction ? 'custom' : String(item.data.exec)" :disabled="disabled" @update:model-value="setAction(item, String($event))">
            <SelectTrigger :id="`${item.key}-action`" class="min-h-11 w-full" :aria-label="`匹配项 ${index + 1} 执行动作`"><SelectValue placeholder="选择执行动作" /></SelectTrigger>
            <SelectContent>
              <SelectItem v-for="action in actions" :key="action.value" :value="action.value">{{ action.label }}</SelectItem>
              <SelectItem value="custom">自定义动作</SelectItem>
            </SelectContent>
          </Select>
          <Input v-if="item.customAction" :model-value="item.data.exec ?? ''" class="min-h-11 min-w-0 font-mono text-xs" :aria-label="`匹配项 ${index + 1} 自定义动作`" :disabled="disabled" placeholder="例如 $my_plugin 或 goto my_sequence" @update:model-value="setCustomAction(item, String($event))" />
        </FormField>
        <p v-if="hasExtensions(item)" class="m-0 text-xs text-muted-foreground">此条目还有扩展字段，已保留；可切换到 YAML 查看。</p>
      </section>
    </template>

    <FormField v-else label="自定义匹配 YAML" html-for="oxi-custom-matches" :help="allowDisable ? '使用 YAML 列表。每项的 matches 可为字符串或字符串列表，exec 为执行动作，enabled: false 表示暂停。清空所有匹配项可填写 []。' : '使用实际执行的 YAML 列表。每项的 matches 可为字符串或字符串列表，exec 为执行动作，不支持 enabled 暂停字段。清空所有匹配项可填写 []。'">
      <Textarea id="oxi-custom-matches" :model-value="yamlDraft" :rows="12" class="min-w-0 font-mono text-xs" :disabled="disabled" :aria-invalid="Boolean(error)" :aria-describedby="error ? 'oxi-match-error' : undefined" spellcheck="false" placeholder="- matches: qname full:example.com&#10;  exec: goto china_dns" @update:model-value="updateYaml(String($event))" />
    </FormField>
    <p v-if="error" id="oxi-match-error" role="alert" class="m-0 whitespace-pre-wrap break-words rounded-lg bg-destructive-soft p-3 text-xs text-destructive-accent">{{ error }}</p>
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { dump, JSON_SCHEMA, load } from 'js-yaml'
import { ChevronDown, ChevronUp, Plus, Trash2 } from '@lucide/vue'
import FormField from '@/components/common/FormField.vue'
import FieldHelp from '@/components/common/FieldHelp.vue'
import { optionHelp } from '@/lib/optionCopy'
import OxidnsEditorModeToggle from './OxidnsEditorModeToggle.vue'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { Textarea } from '@/components/ui/textarea'

type Match = Record<string, any>
type Item = { key: string; data: Match; customAction: boolean }
const props = withDefaults(defineProps<{ modelValue: Match[]; disabled?: boolean; allowDisable?: boolean }>(), { disabled: false, allowDisable: true })
const emit = defineEmits<{
  (event: 'update:modelValue', value: Match[]): void
  (event: 'validity', valid: boolean): void
  (event: 'error', message: string): void
}>()
const mode = ref<'entries' | 'yaml'>('entries')
const items = ref<Item[]>([])
const yamlDraft = ref('[]\n')
const error = ref('')
let sequence = 0
let lastEmitted: string | undefined
const actions = [
  { value: 'goto china_dns', label: '直连 DNS' },
  { value: 'goto proxy_dns_seq', label: '代理 DNS' },
  { value: 'goto fallback_dns', label: '备用 DNS' },
  { value: 'reject', label: '拒绝（REFUSED）' },
  { value: 'reject 3', label: '不存在（NXDOMAIN）' },
  { value: 'reject 0', label: '空应答（NOERROR）' },
  { value: 'accept', label: '接受响应并结束' },
  { value: 'return', label: '返回上级序列' },
]
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value))
const serialize = (value: unknown) => dump(value, { schema: JSON_SCHEMA, noRefs: true, lineWidth: -1 })
const makeItems = (value: Match[]) => value.map(data => ({
  key: `oxi-match-${++sequence}`, data: clone(data), customAction: !actions.some(action => action.value === data.exec),
}))
const report = (message: string) => {
  error.value = message
  emit('validity', !message)
  emit('error', message)
}

function validate(value: unknown): asserts value is Match[] {
  if (!Array.isArray(value)) throw new Error('自定义匹配必须是 YAML 列表；清空时请填写 []。')
  // Unknown fields are retained, but must be representable by the JSON settings API.
  const seen = new Set<object>()
  const inspect = (part: unknown) => {
    if (typeof part === 'number' && !Number.isFinite(part)) throw new Error('自定义匹配不能包含非有限数值。')
    if (part && typeof part === 'object') {
      if (seen.has(part)) throw new Error('自定义匹配不能包含循环 YAML 引用。')
      seen.add(part)
      Object.values(part).forEach(inspect)
      seen.delete(part)
    }
  }
  inspect(value)
  value.forEach((entry, index) => {
    const title = `匹配项 ${index + 1}`
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)) throw new Error(`${title} 必须是对象。`)
    if (!props.allowDisable && 'enabled' in entry) throw new Error(`${title} 是实际执行规则，不支持 enabled 暂停字段；请删除该字段，或删除整条规则。`)
    if ('enabled' in entry && typeof entry.enabled !== 'boolean') throw new Error(`${title} 的 enabled 必须为布尔值。`)
    if ('matches' in entry && typeof entry.matches !== 'string' && !(Array.isArray(entry.matches) && entry.matches.every((condition: unknown) => typeof condition === 'string'))) throw new Error(`${title} 的 matches 必须为字符串或字符串列表。`)
    if ('exec' in entry && typeof entry.exec !== 'string') throw new Error(`${title} 的 exec 必须为字符串。`)
    if (entry.enabled !== false) {
      const list = typeof entry.matches === 'string' ? entry.matches.split('\n') : entry.matches ?? []
      if (!list.some((condition: string) => condition.trim())) throw new Error(`${title} 需要至少一个非空匹配条件。`)
      if (!entry.exec?.trim()) throw new Error(`${title} 需要填写执行动作。`)
    }
  })
}

const publish = (value: unknown): value is Match[] => {
  try {
    validate(value)
    report('')
    lastEmitted = JSON.stringify(value)
    emit('update:modelValue', clone(value))
    return true
  } catch (reason) {
    report(reason instanceof Error ? reason.message : '自定义匹配格式错误。')
    return false
  }
}
const publishItems = () => publish(items.value.map(item => item.data))

watch(() => props.modelValue, value => {
  if (JSON.stringify(value) === lastEmitted) return
  try {
    validate(value)
    items.value = makeItems(value)
    yamlDraft.value = serialize(value)
    report('')
  } catch (reason) {
    mode.value = 'yaml'
    yamlDraft.value = serialize(value)
    report(reason instanceof Error ? reason.message : '自定义匹配格式错误。')
  }
}, { immediate: true, deep: true })

function updateYaml(text: string) {
  yamlDraft.value = text
  try {
    const value = load(text, { schema: JSON_SCHEMA })
    if (publish(value)) items.value = makeItems(value)
  } catch (reason) {
    report(reason instanceof Error ? reason.message : 'YAML 语法错误。')
  }
}
function changeMode(next: 'entries' | 'yaml') {
  if (next === mode.value) return
  if (next === 'entries') {
    updateYaml(yamlDraft.value)
    if (error.value) return
  } else {
    yamlDraft.value = serialize(items.value.map(item => item.data))
  }
  mode.value = next
}
function addItem() {
  items.value.push(...makeItems([{ ...(props.allowDisable ? { enabled: true } : {}), matches: [''], exec: 'goto china_dns' }]))
  publishItems()
}
function removeItem(index: number) {
  items.value.splice(index, 1)
  publishItems()
}
function moveItem(index: number, direction: number) {
  const next = index + direction
  if (next < 0 || next >= items.value.length) return
  const [item] = items.value.splice(index, 1)
  items.value.splice(next, 0, item!)
  publishItems()
}
const conditions = (item: Item): string[] => typeof item.data.matches === 'string' ? item.data.matches.split('\n') : item.data.matches ?? []
function writeConditions(item: Item, value: string[]) {
  item.data.matches = typeof item.data.matches === 'string' ? value.join('\n') : value
  publishItems()
}
function addCondition(item: Item) { writeConditions(item, [...conditions(item), '']) }
function setCondition(item: Item, index: number, text: string) {
  const value = [...conditions(item)]
  value[index] = text
  writeConditions(item, value)
}
function removeCondition(item: Item, index: number) { writeConditions(item, conditions(item).filter((_, position) => position !== index)) }
function setEnabled(item: Item, enabled: boolean) { item.data.enabled = enabled; publishItems() }
function setAction(item: Item, action: string) {
  item.customAction = action === 'custom'
  if (!item.customAction) { item.data.exec = action; publishItems() }
}
function setCustomAction(item: Item, action: string) { item.data.exec = action; publishItems() }
const hasExtensions = (item: Item) => Object.keys(item.data).some(key => !['enabled', 'matches', 'exec'].includes(key))
</script>
