<template>
  <div class="grid min-w-0 grid-cols-1 gap-4 sm:grid-cols-2">
    <FormField v-for="field in visibleFields" :key="field.path" :label="field.label" :html-for="`oxi-${field.path}`" :hint="field.help ? field.hint : undefined" :help="field.help || field.hint" :documentation="field.documentation" :class="field.wide && 'sm:col-span-2'">
      <div v-if="capability(field.path)?.editable === false" class="flex min-h-11 min-w-0 items-center gap-1 text-xs text-muted-foreground">
        <span>暂不可编辑</span><FieldHelp :label="`${field.label}配置状态`" :text="capability(field.path)?.reason || '当前配置使用自定义结构，请在完整参数中编辑。'" />
        <Button variant="ghost" type="button" class="ml-auto min-h-11 shrink-0 text-xs" :disabled="disabled || navigationDisabled" @click="emit('navigate', capability(field.path)?.target || field.path)">完整参数<ChevronRight class="size-3.5" /></Button>
      </div>
      <fieldset v-else class="m-0 min-w-0 space-y-2 border-0 p-0" :disabled="disabled">
      <OxidnsEntryEditor v-if="['list', 'numbers', 'upstreams', 'lines'].includes(field.type || '')" :id="`oxi-${field.path}`" :model-value="read(field.path)" :label="field.label" :kind="field.type === 'list' ? 'strings' : field.type as 'numbers' | 'upstreams' | 'lines'" :placeholder="field.placeholder" :empty-hint="field.emptyHint" @update:model-value="write(field.path, $event)" @error="report(field.path, $event ? `${field.label}：${$event}` : '')" />
      <div v-else-if="field.type === 'switch' || field.type === 'tls'" class="flex min-h-11 items-center">
        <span v-if="read(field.path) == null" class="mr-3 text-xs text-muted-foreground">未配置 · 核心默认</span>
        <Switch :id="`oxi-${field.path}`" :disabled="disabled" :model-value="Boolean(read(field.path))" @update:model-value="write(field.path, field.type === 'tls' ? ($event ? {} : undefined) : $event)" />
      </div>
      <Select v-else-if="field.options" :disabled="disabled" :model-value="String(read(field.path) ?? '')" @update:model-value="write(field.path, $event)">
        <SelectTrigger :id="`oxi-${field.path}`" class="min-h-11"><SelectValue :placeholder="read(field.path) == null ? '未配置（使用核心默认）' : '请选择'" /></SelectTrigger>
        <SelectContent><SelectItem v-for="option in field.options" :key="option.value" :value="option.value">{{ option.label }}</SelectItem></SelectContent>
      </Select>
      <Textarea v-else-if="field.type === 'textarea'" :id="`oxi-${field.path}`" :disabled="disabled" :model-value="display(field)" :rows="field.rows ?? 3" :placeholder="field.placeholder" class="min-w-0 font-mono text-xs" @update:model-value="updateText(field, String($event))" />
      <Input v-else :id="`oxi-${field.path}`" :disabled="disabled" :model-value="inputValue(field)" :type="field.type === 'number' ? 'text' : field.type ?? 'text'" :inputmode="field.type === 'number' ? 'numeric' : undefined" :min="capabilities ? undefined : field.min" :max="capabilities ? undefined : field.max" :aria-invalid="Boolean(numberErrors[field.path])" :placeholder="field.placeholder || (read(field.path) == null ? (field.type === 'number' ? '未配置（使用核心默认）' : '未配置') : undefined)" :autocomplete="field.type === 'password' ? 'new-password' : 'off'" class="min-h-11 min-w-0 font-mono" @update:model-value="field.type === 'number' ? updateNumber(field, String($event)) : write(field.path, $event)" />
      <p v-if="numberErrors[field.path]" role="alert" class="text-xs text-destructive-accent">{{ numberErrors[field.path] }}</p>
      </fieldset>
    </FormField>
  </div>
</template>

<script setup lang="ts">
import { computed, onUnmounted, reactive, watch } from 'vue'
import { ChevronRight } from '@lucide/vue'
import FormField from '@/components/common/FormField.vue'
import FieldHelp from '@/components/common/FieldHelp.vue'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Switch } from '@/components/ui/switch'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import type { OxidnsField } from '@/lib/oxidns'
import OxidnsEntryEditor from './OxidnsEntryEditor.vue'

export interface OxidnsFieldCapability { editable: boolean; reason?: string; target?: string; absent?: boolean }
const props = withDefaults(defineProps<{ settings: Record<string, any>; fields: OxidnsField[]; disabled?: boolean; navigationDisabled?: boolean; capabilities?: Record<string, OxidnsFieldCapability> }>(), { disabled: false, navigationDisabled: false })
const emit = defineEmits<{ error: [path: string, message: string]; change: [path: string, value: unknown]; navigate: [target: string] }>()
const textDrafts = reactive<Record<string, string>>({})
const numberDrafts = reactive<Record<string, string>>({})
const numberErrors = reactive<Record<string, string>>({})
const lastValues = new Map<string, string | undefined>()
const read = (path: string): unknown => path.split('.').reduce<unknown>((value, key) =>
  value !== null && typeof value === 'object' ? (value as Record<string, unknown>)[key] : undefined, props.settings)
const inputValue = (field: OxidnsField): string | number => {
  const value = field.type === 'number' ? numberDrafts[field.path] ?? read(field.path) : read(field.path)
  return typeof value === 'number' ? value : String(value ?? '')
}
const visibleFields = computed(() => props.fields.filter(field => !field.when || Boolean(read(field.when))))
function capability(path: string): OxidnsFieldCapability | undefined {
  const parts = path.split('.')
  while (parts.length) {
    const value = props.capabilities?.[parts.join('.')]
    if (value) return value
    parts.pop()
  }
}
const report = (path: string, message: string) => emit('error', path, message)
watch(() => props.settings, () => {
  for (const field of props.fields) {
    const value = JSON.stringify(read(field.path))
    if (lastValues.has(field.path) && lastValues.get(field.path) !== value && !numberErrors[field.path]) {
      delete textDrafts[field.path]
      delete numberDrafts[field.path]
    }
    lastValues.set(field.path, value)
  }
}, { immediate: true, deep: true })
onUnmounted(() => props.fields.forEach(field => report(field.path, '')))
const write = (path: string, value: unknown) => {
  if (props.disabled || capability(path)?.editable === false) return
  const keys = path.split('.')
  const key = keys.pop()!
  const target = keys.reduce((value, part) => (value[part] ??= {}), props.settings)
  if (value === undefined) delete target[key]
  else target[key] = value
  lastValues.set(path, JSON.stringify(value))
  emit('change', path, value)
}
function updateNumber(field: OxidnsField, raw: string) {
  numberDrafts[field.path] = raw
  const value = Number(raw)
  const message = !raw.trim() || !/^[+-]?\d+$/.test(raw.trim()) || !Number.isSafeInteger(value)
    ? `${field.label}：请填写整数。`
    : !props.capabilities && field.min !== undefined && value < field.min ? `${field.label}：不能小于 ${field.min}。`
      : !props.capabilities && field.max !== undefined && value > field.max ? `${field.label}：不能大于 ${field.max}。` : ''
  numberErrors[field.path] = message
  report(field.path, message)
  if (!message) write(field.path, value)
}
const display = (field: OxidnsField) => {
  if (field.path in textDrafts) return textDrafts[field.path]
  const value = read(field.path)
  return Array.isArray(value) ? value.join('\n') : String(value ?? '')
}
const updateText = (field: OxidnsField, text: string) => {
  textDrafts[field.path] = text
  if (field.type === 'list') write(field.path, text.split('\n').map(line => line.trim()).filter(Boolean))
  else if (field.type === 'numbers') write(field.path, text.split(/[\s,]+/).filter(Boolean).map(Number))
  else write(field.path, text)
}
</script>
