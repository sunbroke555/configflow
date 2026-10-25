<template>
  <section class="oxi-flow-node-editor min-w-0 space-y-3" :aria-label="`${label} 编辑器`">
    <div class="flex min-w-0 flex-wrap items-center justify-between gap-2">
      <p class="m-0 min-w-0 flex-1 break-words text-xs text-muted-foreground">{{ schema ? '按参数填写；未展示的扩展字段也会保留。' : '当前使用通用参数表单，已有字段和值会保留。' }}</p>
      <OxidnsEditorModeToggle :model-value="mode" :options="modes" :label="`${label} 编辑方式`" :disabled="disabled" @update:model-value="changeMode" />
    </div>
    <OxidnsSchemaValueEditor v-if="mode === 'form' && tree" :node="tree" :schema="schema" :label="label" :scope-title="schema?.label || label" :auto-expand-depth="2" :disabled="disabled" @change="changeForm" />
    <Textarea v-else :model-value="yamlDraft" :rows="12" :aria-label="`${label} YAML`" :aria-invalid="Boolean(error)" :aria-describedby="error ? errorId : undefined" :disabled="disabled" class="min-h-64 min-w-0 font-mono text-xs" spellcheck="false" @update:model-value="changeYaml(String($event))" />
    <p v-if="error" :id="errorId" role="alert" class="m-0 break-words whitespace-pre-wrap text-xs text-destructive-accent">{{ error }}</p>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, useId, watch } from 'vue'
import { dump, JSON_SCHEMA, load } from 'js-yaml'
import { Textarea } from '@/components/ui/textarea'
import { configNodeToValue, valueToConfigNode, type ConfigValueNode } from '@/lib/oxidnsConfigEditor'
import type { OxidnsSchema } from '@/lib/oxidnsSchema'
import OxidnsEditorModeToggle from './OxidnsEditorModeToggle.vue'
import OxidnsSchemaValueEditor from './OxidnsSchemaValueEditor.vue'

const props = withDefaults(defineProps<{
  modelValue: unknown
  schema?: OxidnsSchema
  label: string
  disabled?: boolean
  /** Increment to discard local text drafts even when modelValue is unchanged. */
  resetKey?: string | number
}>(), { disabled: false })
const emit = defineEmits<{
  'update:modelValue': [value: unknown]
  error: [message: string]
  dirty: [value: boolean]
}>()
const mode = ref<'form' | 'yaml'>('form')
const tree = ref<ConfigValueNode | null>(null)
const yamlDraft = ref('')
const error = ref('')
const errorId = `oxi-flow-node-error-${useId()}`
const modes = computed(() => [
  { value: 'form' as const, label: '表单', disabled: mode.value === 'yaml' && Boolean(error.value) },
  { value: 'yaml' as const, label: 'YAML', disabled: mode.value === 'form' && Boolean(error.value) },
])
let baseline = ''
let baselineYaml = ''
let lastEmitted: string | undefined
const serialize = (value: unknown) => dump(value, { schema: JSON_SCHEMA, noRefs: true, lineWidth: -1, sortKeys: false })
function report(message: string) { error.value = message; emit('error', message) }
function failure(reason: unknown) { report(reason instanceof Error ? reason.message : '参数格式错误，请修正后应用。'); emit('dirty', true) }

/** Parent v-model echoes preserve lexical drafts; explicit reset starts a new baseline. */
function reset() {
  lastEmitted = undefined
  try {
    const next = valueToConfigNode(props.modelValue)
    const value = configNodeToValue(next)
    tree.value = next
    baseline = JSON.stringify(value)
    baselineYaml = serialize(value)
    yamlDraft.value = baselineYaml
    report('')
    emit('dirty', false)
  } catch (reason) {
    tree.value = null
    yamlDraft.value = ''
    baseline = ''
    baselineYaml = ''
    mode.value = 'yaml'
    failure(reason)
  }
}
function publish(value: unknown, textChanged = false) {
  lastEmitted = JSON.stringify(value)
  report('')
  emit('dirty', lastEmitted !== baseline || textChanged)
  emit('update:modelValue', value)
}
function changeForm() {
  if (props.disabled || !tree.value) return
  try {
    const value = configNodeToValue(tree.value)
    yamlDraft.value = serialize(value)
    publish(value)
  } catch (reason) { failure(reason) }
}
function changeYaml(text: string) {
  if (props.disabled) return
  yamlDraft.value = text
  try {
    const empty = text.split('\n').every(line => !line.trim() || line.trim().startsWith('#') || /^(?:---|\.\.\.)(?:\s+#.*)?$/.test(line.trim()))
    const value = empty ? undefined : load(text, { schema: JSON_SCHEMA })
    if (value === undefined) throw new Error('请填写 YAML 值；空对象用 {}，空列表用 []，空值用 null。')
    const next = valueToConfigNode(value)
    // Convert through the same bounded JSON tree as the form; this preserves
    // unknown fields and rejects cycles, unsupported values and unsafe numbers.
    const nextValue = configNodeToValue(next)
    tree.value = next
    publish(nextValue, text !== baselineYaml)
  } catch (reason) { failure(reason) }
}
function changeMode(next: 'form' | 'yaml') {
  if (props.disabled || next === mode.value || error.value) return
  mode.value = next
}
watch(() => [props.modelValue, props.resetKey] as const, (current, previous) => {
  let signature: string | undefined
  try { signature = JSON.stringify(current[0]) } catch { /* reset reports the precise value error */ }
  if (previous && current[1] === previous[1] && signature !== undefined && signature === lastEmitted) return
  reset()
}, { immediate: true, deep: true })
defineExpose({ reset })
</script>

<style scoped>
.oxi-flow-node-editor :deep(button),
.oxi-flow-node-editor :deep(.oxi-native-control) { min-height: 44px; }
.oxi-flow-node-editor :deep(.oxi-native-icon),
.oxi-flow-node-editor :deep(.field-help-trigger) { width: 44px; height: 44px; }
.oxi-flow-node-editor :deep(.oxi-mode-toggle) { height: 50px; }
.oxi-flow-node-editor :deep(.oxi-mode-options) { min-height: 44px; }
</style>
