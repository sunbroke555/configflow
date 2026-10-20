<template>
  <section aria-label="基础配置扩展" class="min-w-0 space-y-4">
    <div class="flex min-w-0 flex-wrap items-center justify-between gap-3">
      <div class="flex items-center gap-1">
        <h3 class="text-sm font-semibold">基础配置扩展</h3>
        <FieldHelp label="基础配置扩展" :text="optionHelp('模板设置之外，还需要添加全局参数或自定义插件。', '支持逐项和 YAML 编辑，模板字段以页面设置为准。切换保留值，表单修改会整理注释排版；无效文本保留为草稿，修正后才能保存和切回表单。')" />
      </div>
      <OxidnsEditorModeToggle :model-value="mode" :options="[{ value: 'form', label: '逐项编辑' }, { value: 'yaml', label: 'YAML', disabled: mode === 'form' && Boolean(error) }]" label="基础配置编辑模式" @update:model-value="switchMode" />
    </div>

    <div v-if="mode === 'yaml'" class="space-y-2">
      <Textarea id="oxi-custom-config" :model-value="draft" :rows="12" aria-label="基础配置扩展 YAML" :aria-invalid="Boolean(error)" class="min-w-0 font-mono text-xs" placeholder="# 额外的 OxiDNS 全局参数或插件" @update:model-value="editYaml(String($event))" />
    </div>
    <div v-else class="min-w-0 space-y-5">
      <div class="min-w-0 space-y-3">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <div class="flex items-center gap-1">
            <h4 class="text-sm font-medium">全局参数 <span class="text-muted-foreground num">{{ globals.length }}</span></h4>
            <FieldHelp label="全局参数" :text="optionHelp('核心文档提供了模板表单中没有的顶层参数时使用。', '按官方字段名添加参数，对象可继续添加子字段，列表可逐条填写。由模板管理的核心字段应回到对应设置页修改，避免生成时被模板设置覆盖。')" />
          </div>
          <Button type="button" variant="outline" class="min-h-11" @click="appendConfigEntry(tree); commit()"><Plus class="size-3.5" />添加全局参数</Button>
        </div>
        <p v-if="!globals.length" class="text-xs text-muted-foreground">尚无额外全局参数。</p>
        <div v-for="entry in globals" :key="entry.id" class="min-w-0 space-y-2 border-b border-border pb-3">
          <div class="flex min-w-0 items-center gap-2">
            <Input :model-value="entry.key" aria-label="全局参数名称" placeholder="参数名称" class="min-h-11 min-w-0 flex-1 font-mono text-xs" @update:model-value="entry.key = String($event); commit()" />
            <Button type="button" variant="ghost" size="icon" class="size-11 shrink-0" :aria-label="`删除全局参数 ${entry.key}`" @click="tree.entries.splice(tree.entries.indexOf(entry), 1); commit()"><Trash2 class="size-4" /></Button>
          </div>
          <OxidnsValueEditor :node="entry.value" :label="`全局参数 ${entry.key}`" @change="commit" />
        </div>
      </div>

      <div class="min-w-0 space-y-3">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <div class="flex items-center gap-1">
            <h4 class="text-sm font-medium">自定义插件 <span class="text-muted-foreground num">{{ plugins?.value.items.length ?? 0 }}</span></h4>
            <FieldHelp label="自定义插件" :text="optionHelp('需要添加模板之外的处理动作，且知道对应插件参数。', '填写 tag 唯一名称、type 类型和 args 参数。仅定义插件不会自动参与解析，执行器还需在自定义匹配中用 $tag 引用，参数按插件文档填写。')" />
          </div>
          <Button type="button" variant="outline" class="min-h-11" :disabled="nonListPlugins" @click="addPlugin"><Plus class="size-3.5" />添加插件</Button>
        </div>
        <p v-if="nonListPlugins" class="text-xs text-warning-accent">现有 plugins 不是列表，已完整显示在全局参数中。请先修改其类型再添加插件。</p>
        <p v-else-if="!plugins?.value.items.length" class="text-xs text-muted-foreground">尚无额外插件。新增插件包含 tag、type 和 args，可继续添加扩展字段。</p>
        <div v-for="(plugin, index) in plugins?.value.items ?? []" :key="plugin.id" class="min-w-0 space-y-2 rounded-lg border border-border p-3">
          <div class="flex min-w-0 items-center gap-1">
            <h5 class="mr-auto text-sm font-medium num">插件 {{ index + 1 }}</h5>
            <Button type="button" variant="ghost" size="icon" class="size-11 shrink-0" :disabled="index === 0" :aria-label="`上移插件 ${index + 1}`" @click="movePlugin(index, -1)"><ArrowUp class="size-4" /></Button>
            <Button type="button" variant="ghost" size="icon" class="size-11 shrink-0" :disabled="index === (plugins?.value.items.length ?? 0) - 1" :aria-label="`下移插件 ${index + 1}`" @click="movePlugin(index, 1)"><ArrowDown class="size-4" /></Button>
            <Button type="button" variant="ghost" size="icon" class="size-11 shrink-0" :aria-label="`删除插件 ${index + 1}`" @click="plugins!.value.items.splice(index, 1); commit()"><Trash2 class="size-4" /></Button>
          </div>
          <OxidnsValueEditor :node="plugin" :label="`插件 ${index + 1}`" @change="commit" />
        </div>
      </div>
    </div>
    <p v-if="error" role="alert" class="break-words text-sm text-destructive-accent">{{ error }}</p>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ArrowDown, ArrowUp, Plus, Trash2 } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import OxidnsValueEditor from './OxidnsValueEditor.vue'
import FieldHelp from '@/components/common/FieldHelp.vue'
import { optionHelp } from '@/lib/optionCopy'
import OxidnsEditorModeToggle from './OxidnsEditorModeToggle.vue'
import { appendConfigEntry, parseConfigDocument, serializeConfigDocument, valueToConfigNode } from '@/lib/oxidnsConfigEditor'

const props = defineProps<{ modelValue: string }>()
const emit = defineEmits<{
  (event: 'update:modelValue', value: string): void
  (event: 'validity', valid: boolean): void
  (event: 'error', message: string): void
}>()
const mode = ref<'form' | 'yaml'>('form')
const tree = ref(valueToConfigNode({}))
const draft = ref('')
const error = ref('')
let lastEmitted: string | undefined
const plugins = computed(() => tree.value.entries.find(entry => entry.key === 'plugins' && entry.value.type === 'array'))
const globals = computed(() => tree.value.entries.filter(entry => entry !== plugins.value))
const nonListPlugins = computed(() => tree.value.entries.some(entry => entry.key === 'plugins' && entry.value.type !== 'array'))

function report(message = '') {
  error.value = message
  emit('validity', !message)
  emit('error', message)
}
function publish(text: string) {
  lastEmitted = text
  emit('update:modelValue', text)
}
function parseDraft() {
  try {
    tree.value = parseConfigDocument(draft.value)
    report()
    return true
  } catch (failure: any) {
    report(failure.message || '无法读取 YAML，请检查格式。')
    return false
  }
}
function editYaml(text: string) {
  draft.value = text
  publish(text)
  parseDraft()
}
function commit() {
  try {
    draft.value = serializeConfigDocument(tree.value)
    report()
    publish(draft.value)
  } catch (failure: any) {
    report(failure.message || '请修正配置内容。')
  }
}
function switchMode(next: 'form' | 'yaml') {
  if (next === mode.value) return
  if (next === 'form' && !parseDraft()) return
  if (next === 'yaml' && error.value) return
  mode.value = next
}
function addPlugin() {
  const target = plugins.value ?? appendConfigEntry(tree.value, 'plugins', [])
  target.value.items.push(valueToConfigNode({ tag: '', type: '', args: {} }))
  commit()
}
function movePlugin(index: number, offset: number) {
  const items = plugins.value!.value.items
  const [item] = items.splice(index, 1)
  items.splice(index + offset, 0, item)
  commit()
}
watch(() => props.modelValue, text => {
  if (text === lastEmitted) {
    lastEmitted = undefined
    return
  }
  lastEmitted = undefined
  draft.value = text ?? ''
  if (!parseDraft()) mode.value = 'yaml'
}, { immediate: true })
</script>
