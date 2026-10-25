<template>
  <section class="min-w-0 space-y-4" aria-label="入口流程可视化">
    <div class="flex flex-wrap items-center gap-2">
      <div class="mr-auto flex items-center gap-1"><h3 class="text-sm font-semibold">入口与处理流程</h3><FieldHelp label="入口与处理流程" text="从监听入口查看实际处理步骤，点击节点编辑参数。连线按配置关系生成，步骤按原顺序保存。goto 转入其他流程，jump 调用后可返回；插件是否继续执行由自身行为决定。" /></div>
      <Button variant="outline" type="button" class="min-h-11 text-xs" :disabled="disabled || navigationLocked" @click="editGlobals">全局参数</Button>
    </div>
    <FormField label="监听入口流程" html-for="oxi-entry-flow" hint="多个监听入口可以共用同一个流程。">
      <Select :model-value="selectedEntry" :disabled="disabled || navigationLocked" @update:model-value="chooseEntry(String($event))">
        <SelectTrigger id="oxi-entry-flow" class="min-h-11 w-full" aria-label="监听入口流程"><SelectValue placeholder="选择要编辑的入口流程" /></SelectTrigger>
        <SelectContent class="max-w-[calc(100vw-2rem)]"><SelectItem v-for="group in entryGroups" :key="group.tag" :value="group.tag" class="min-h-11"><span class="break-all whitespace-normal">{{ group.tag || '未设置入口' }} · {{ group.entries.length }} 个监听入口</span></SelectItem></SelectContent>
      </Select>
    </FormField>
    <p v-if="!entryGroups.length" role="status" class="m-0 text-xs text-muted-foreground">当前没有监听入口。可从下方选择已有插件编辑，或在完整参数中添加监听服务。</p>
    <p v-else-if="!rootGroup" role="status" class="m-0 text-xs text-muted-foreground">请选择入口流程，再查看它的处理顺序。</p>
    <div v-if="rootGroup" class="space-y-2">
      <div class="flex flex-wrap gap-2" aria-label="关联监听入口">
        <button v-for="entry in rootGroup.entries" :key="entry.id" type="button" class="min-h-11 min-w-0 rounded-lg border border-border bg-muted/30 px-3 py-2 text-left text-xs hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-accent disabled:opacity-50" :disabled="disabled || navigationLocked || !pluginById(entry.listener_id)" :aria-label="`编辑监听 ${entry.listener_tag}${entry.path ? ` ${entry.path}` : ''}`" @click="openPlugin(entry.listener_id)"><Network class="mr-1 inline size-3.5" aria-hidden="true" /><span class="break-all">{{ flowEntryLabel(entry) }}</span></button>
      </div>
      <p v-if="rootGroup.entries.length > 1" role="status" class="m-0 text-xs text-info-accent">以上 {{ rootGroup.entries.length }} 个监听入口共用 {{ rootGroup.tag }}，修改此流程会同时影响它们。</p>
      <p v-if="rootGroup.entries.some(entry => entry.dynamic)" role="status" class="m-0 break-words text-xs text-info-accent">入口由环境变量决定，实际目标在 OxiDNS 运行时解析；可从下方选择已定义的流程编辑。</p>
      <p v-else-if="!rootGroup.plugin" role="status" class="m-0 break-words text-xs text-warning-accent">此入口引用的插件不存在或名称重复，请在完整参数中修正。</p>
    </div>
    <div class="flex flex-wrap items-center gap-2">
      <Select :model-value="activeId" :disabled="disabled || navigationLocked" @update:model-value="openPlugin(String($event))">
        <SelectTrigger class="min-h-11 min-w-0 flex-1" aria-label="查看流程或插件"><SelectValue placeholder="查看任意流程或插件" /></SelectTrigger>
        <SelectContent class="max-w-[calc(100vw-2rem)]"><SelectItem v-for="plugin in flow.plugins" :key="plugin.id" :value="plugin.id" class="min-h-11"><span class="break-all whitespace-normal">{{ plugin.tag }} · {{ plugin.type }} · {{ plugin.source }}</span></SelectItem></SelectContent>
      </Select>
      <Button v-if="rootGroup?.plugin && activeId !== rootGroup.plugin.id" variant="ghost" type="button" class="min-h-11 text-xs" :disabled="disabled || navigationLocked" @click="openPlugin(rootGroup.plugin.id)"><CornerUpLeft class="size-4" />返回入口</Button>
    </div>
    <OxidnsEditorModeToggle class="sm:h-[50px] sm:min-h-[50px]" v-if="simpleAvailable" v-model="displayMode" :options="[{ value: 'graph', label: '流程图' }, { value: 'simple', label: '简洁设置' }]" label="流程编辑方式" :disabled="disabled || navigationLocked" />
    <div v-if="displayMode === 'simple' && simpleAvailable" class="space-y-4"><slot name="simple" /></div>
    <template v-else>
      <p v-if="activePlugin && !activePlugin.editable" role="status" class="m-0 break-words text-xs text-warning-accent">{{ activePlugin.reason || '当前节点只能查看，请在完整参数中修正配置。' }}</p>
      <div v-if="activePlugin || edit" class="grid min-w-0 gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <div v-if="activePlugin" class="min-w-0 space-y-3" aria-label="处理流程图">
          <button type="button" class="flex min-h-14 w-full min-w-0 items-center gap-2 rounded-lg border border-border-strong bg-card p-3 text-left hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-accent disabled:opacity-50" :disabled="disabled || navigationLocked || !activePlugin.editable" :aria-label="`编辑流程参数 ${activePlugin.tag}`" @click="editPlugin(activePlugin)">
            <Workflow class="size-5 shrink-0 text-primary-accent" aria-hidden="true" /><span class="min-w-0 flex-1"><strong class="block break-all text-sm font-medium">{{ activePlugin.tag }}</strong><span class="break-all text-xs text-muted-foreground">{{ activePlugin.type }} · {{ activePlugin.source }}</span></span><Settings2 class="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
          </button>
          <p v-if="affectedEntries.length > 1" role="status" class="m-0 break-words text-xs text-info-accent">此节点影响 {{ affectedEntries.length }} 个监听入口：{{ affectedEntries.map(flowEntryLabel).join('；') }}</p>
          <template v-if="steps">
            <p class="m-0 text-xs text-muted-foreground">按实际配置顺序展示。点击步骤编辑条件与动作；未命中的条件会跳过该步骤。</p>
            <ol class="m-0 list-none space-y-0 p-0" aria-label="流程步骤">
              <li v-for="(step, index) in steps" :key="`${activePlugin.id}:${index}`" class="min-w-0">
                <div class="flex min-h-8 items-center justify-center gap-1 py-1 text-center text-[11px] text-muted-foreground"><ArrowDown class="size-3.5 shrink-0" aria-hidden="true" /><span>{{ index === 0 ? '开始处理' : flowNextLabel(steps[index - 1]) }}</span></div>
                <div class="min-w-0 rounded-lg border bg-card" :class="edit?.kind === 'step' && edit.index === index ? 'border-primary-accent' : 'border-border'">
                  <button type="button" class="flex min-h-14 w-full min-w-0 gap-2 rounded-t-lg p-3 text-left hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-accent disabled:opacity-50" :disabled="disabled || navigationLocked || !activePlugin.editable" :aria-label="`编辑步骤 ${index + 1}`" @click="editStep(index)">
                    <span class="num shrink-0 text-xs text-muted-foreground">{{ index + 1 }}</span><span class="min-w-0"><span class="block break-words text-xs text-muted-foreground">{{ flowStepSummary(step).condition }}</span><code class="mt-1 block break-all text-xs">{{ flowStepSummary(step).action }}</code></span>
                  </button>
                  <div v-if="flowStepSummary(step).reference" class="mx-3 mb-2 flex min-w-0 items-center gap-1 border-l border-info-accent pl-2">
                    <ArrowRight class="size-3.5 shrink-0 text-info-accent" aria-hidden="true" /><button type="button" class="min-h-11 min-w-0 rounded px-2 text-left text-xs text-info-accent hover:bg-info-soft focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-accent disabled:opacity-50" :disabled="disabled || navigationLocked || !pluginForTag(flowStepSummary(step).reference!.tag)" :aria-label="`查看步骤 ${index + 1} 目标 ${flowStepSummary(step).reference!.tag}`" @click="openTag(flowStepSummary(step).reference!.tag)"><span class="break-all">{{ flowStepSummary(step).reference!.label }} → {{ flowStepSummary(step).reference!.tag }}</span></button>
                    <span v-if="!pluginForTag(flowStepSummary(step).reference!.tag)" class="text-xs text-warning-accent">目标不可用</span>
                  </div>
                  <div class="flex flex-wrap items-center gap-1 border-t border-border px-1" :aria-label="`步骤 ${index + 1} 操作`">
                    <Button variant="ghost" type="button" class="min-h-11 px-2 text-xs" :disabled="disabled || navigationLocked || !activePlugin.editable" :aria-label="`在步骤 ${index + 1} 前插入`" @click="insertStep(index)">前插</Button>
                    <Button variant="ghost" type="button" class="min-h-11 px-2 text-xs" :disabled="disabled || navigationLocked || !activePlugin.editable" :aria-label="`在步骤 ${index + 1} 后插入`" @click="insertStep(index + 1)">后插</Button>
                    <Button variant="ghost" type="button" size="icon" class="size-11" :disabled="disabled || navigationLocked || !activePlugin.editable || index === 0" :aria-label="`上移步骤 ${index + 1}`" @click="moveStep(index, -1)"><ArrowUp class="size-4" /></Button>
                    <Button variant="ghost" type="button" size="icon" class="size-11" :disabled="disabled || navigationLocked || !activePlugin.editable || index === steps.length - 1" :aria-label="`下移步骤 ${index + 1}`" @click="moveStep(index, 1)"><ArrowDown class="size-4" /></Button>
                    <Button variant="ghost" type="button" size="icon" class="ml-auto size-11 text-muted-foreground hover:text-destructive-accent" :disabled="disabled || navigationLocked || !activePlugin.editable" :aria-label="`删除步骤 ${index + 1}`" @click="deleteStep(index)"><Trash2 class="size-4" /></Button>
                  </div>
                </div>
              </li>
            </ol>
            <p v-if="!steps.length" class="text-xs text-muted-foreground">此流程尚无步骤。</p>
            <Button variant="outline" type="button" class="min-h-11 w-full" :disabled="disabled || navigationLocked || !activePlugin.editable" @click="insertStep(steps.length)"><Plus class="size-4" />添加流程步骤</Button>
          </template>
          <div v-if="dependencies.length" class="space-y-1 border-t border-border pt-3" aria-label="节点引用关系"><p class="m-0 text-xs text-muted-foreground">此节点引用</p><button v-for="edge in dependencies" :key="`${edge.target}:${JSON.stringify(edge.path)}`" type="button" class="flex min-h-11 w-full min-w-0 items-center gap-2 rounded px-2 text-left text-xs hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-accent disabled:opacity-50" :disabled="disabled || navigationLocked || !edge.to" @click="edge.to && openPlugin(edge.to)"><ArrowRight class="size-3.5 shrink-0 text-info-accent" aria-hidden="true" /><span class="break-all">{{ edge.target }}<span v-if="!edge.to" class="text-warning-accent"> · 目标不存在或不唯一</span></span></button></div>
        </div>
        <section v-if="edit" ref="formPanel" class="min-w-0 space-y-3 rounded-lg border border-border-strong bg-card p-3" aria-label="节点参数编辑">
          <div class="flex flex-wrap items-center gap-2"><h4 class="mr-auto break-all text-sm font-semibold">{{ edit.label }}</h4><span class="text-xs text-muted-foreground">{{ edit.source }}</span></div>
          <p class="m-0 text-xs text-muted-foreground">修改后点击「应用节点修改」，最后统一保存草稿。</p>
          <FormField v-if="edit.kind === 'step'" label="动作快捷选择" hint="可选现有目标；特殊表达式可直接在下方参数中填写。">
            <Select :disabled="disabled || Boolean(formError)" :model-value="shortcutValue" @update:model-value="setShortcut(String($event))"><SelectTrigger class="min-h-11 w-full" aria-label="步骤动作快捷选择"><SelectValue placeholder="选择动作或保留当前写法" /></SelectTrigger><SelectContent class="max-w-[calc(100vw-2rem)]"><SelectItem v-for="action in shortcuts" :key="action.value" :value="action.value" class="min-h-11"><span class="break-all whitespace-normal">{{ action.label }}</span></SelectItem></SelectContent></Select>
          </FormField>
          <OxidnsFlowNodeEditor :key="edit.key" v-model="nodeValue" :schema="edit.schema" :label="edit.label" :disabled="disabled" @error="formError = $event" @dirty="formDirty = $event" />
          <p v-if="nodeIssue && !formError" role="alert" class="m-0 break-words text-xs text-destructive-accent">{{ nodeIssue }}</p>
          <div class="flex flex-wrap items-center gap-2 border-t border-border pt-3"><Button type="button" class="min-h-11" :disabled="disabled || !localDirty || Boolean(nodeIssue)" @click="applyNode">应用节点修改</Button><Button type="button" variant="outline" class="min-h-11" :disabled="disabled" @click="cancelEdit">取消节点修改</Button></div>
        </section>
        <p v-else-if="activePlugin" class="m-0 rounded-lg border border-dashed border-border p-4 text-xs text-muted-foreground">点击流程、步骤或插件节点，在这里编辑参数。已收录字段使用专用控件，扩展字段仍可逐项或通过 YAML 编辑。</p>
      </div>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { ArrowDown, ArrowRight, ArrowUp, CornerUpLeft, Network, Plus, Settings2, Trash2, Workflow } from '@lucide/vue'
import FormField from '@/components/common/FormField.vue'
import FieldHelp from '@/components/common/FieldHelp.vue'
import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import OxidnsEditorModeToggle from './OxidnsEditorModeToggle.vue'
import OxidnsFlowNodeEditor from './OxidnsFlowNodeEditor.vue'
import { oxidnsMatchBuiltinActions } from '@/lib/oxidnsMatchActions'
import { flowAffectedEntries, flowClone, flowEntryGroups, flowEntryLabel, flowNextLabel, flowObject, flowParameters, flowPluginSchema, flowStepSchema, flowStepSummary, oxidnsGlobalSchema, type OxidnsEntryFlow, type OxidnsFlowPatch, type OxidnsFlowPlugin } from '@/lib/oxidnsEntryFlows'
import type { OxidnsSchema } from '@/lib/oxidnsSchema'

const props = defineProps<{ flow: OxidnsEntryFlow; disabled?: boolean; simpleTarget?: string; simpleEnabled?: boolean; simpleInvalid?: boolean }>()
const emit = defineEmits<{ change: [patch: OxidnsFlowPatch]; pending: [value: boolean]; error: [message: string] }>()
const selectedEntry = ref('')
const activeId = ref('')
const displayMode = ref('graph')
const entryGroups = computed(() => flowEntryGroups(props.flow))
const rootGroup = computed(() => entryGroups.value.find(group => group.tag === selectedEntry.value))
const pluginById = (id: string) => props.flow.plugins.find(plugin => plugin.id === id)
const pluginForTag = (tag: string) => props.flow.plugins.filter(plugin => plugin.tag === tag).length === 1 ? props.flow.plugins.find(plugin => plugin.tag === tag) : undefined
const activePlugin = computed(() => pluginById(activeId.value))
const steps = computed(() => activePlugin.value?.type === 'sequence' && Array.isArray(activePlugin.value.value.args) ? activePlugin.value.value.args : undefined)
const dependencies = computed(() => props.flow.edges.filter(edge => edge.from === activeId.value))
const affectedEntries = computed(() => flowAffectedEntries(props.flow, activeId.value))
const simpleAvailable = computed(() => props.simpleEnabled && rootGroup.value?.plugin?.tag === props.simpleTarget && activeId.value === rootGroup.value?.plugin?.id)
type Edit = { key: number; kind: 'step' | 'plugin' | 'globals'; label: string; source: string; schema: OxidnsSchema; original: unknown; before: Record<string, unknown>; plugin?: OxidnsFlowPlugin; index?: number; insert?: boolean }
const edit = ref<Edit>()
const nodeValue = ref<unknown>({})
const formError = ref('')
const formDirty = ref(false)
const formPanel = ref<HTMLElement>()
let editKey = 0
const localDirty = computed(() => Boolean(edit.value && (formDirty.value || JSON.stringify(nodeValue.value) !== JSON.stringify(edit.value.original))))
const navigationLocked = computed(() => localDirty.value || (displayMode.value === 'simple' && props.simpleInvalid))
const nodeIssue = computed(() => {
  if (!edit.value) return ''
  if (formError.value) return formError.value
  if (!flowObject(nodeValue.value)) return '节点参数需要是一个对象。'
  if (edit.value.kind === 'step' && (typeof nodeValue.value.exec !== 'string' || !nodeValue.value.exec.trim())) return '请填写执行动作。'
  if (edit.value.kind === 'plugin' && ('tag' in nodeValue.value || 'type' in nodeValue.value)) return '名称和类型请在完整参数中修改。'
  if (edit.value.kind === 'globals' && 'plugins' in nodeValue.value) return '插件请通过对应节点编辑。'
  return ''
})
const shortcuts = computed(() => [...(pluginById(edit.value?.plugin?.id ?? '')?.targets ?? []).map(target => ({ value: target.exec, label: `${target.type === 'sequence' ? '转入流程' : '调用插件'} · ${target.tag} · ${target.type}` })), ...oxidnsMatchBuiltinActions])
const shortcutValue = computed(() => { const value = nodeValue.value; return flowObject(value) && shortcuts.value.some(action => action.value === value.exec) ? String(value.exec) : '' })
watch([localDirty, nodeIssue], () => { emit('pending', localDirty.value); emit('error', localDirty.value ? nodeIssue.value : '') }, { immediate: true })
onUnmounted(() => { emit('pending', false); emit('error', '') })
watch(entryGroups, groups => {
  if (localDirty.value) return
  if (!groups.some(group => group.tag === selectedEntry.value)) selectedEntry.value = groups.length === 1 ? groups[0]!.tag : ''
  if (!pluginById(activeId.value)) activeId.value = rootGroup.value?.plugin?.id ?? ''
}, { immediate: true })
watch(simpleAvailable, available => { if (!available) displayMode.value = 'graph' })
function cancelEdit() { edit.value = undefined; formError.value = ''; formDirty.value = false }
function chooseEntry(tag: string) { if (props.disabled || navigationLocked.value) return; cancelEdit(); selectedEntry.value = tag; activeId.value = rootGroup.value?.plugin?.id ?? ''; displayMode.value = 'graph' }
function openPlugin(id: string) { if (props.disabled || navigationLocked.value) return; cancelEdit(); activeId.value = id; displayMode.value = 'graph'; const plugin = pluginById(id); if (plugin?.type !== 'sequence' && plugin?.editable) editPlugin(plugin) }
function openTag(tag: string) { const plugin = pluginForTag(tag); if (plugin) openPlugin(plugin.id) }
async function begin(value: Omit<Edit, 'key'>, draft: unknown) {
  cancelEdit(); nodeValue.value = flowClone(draft); edit.value = { ...value, key: ++editKey }; await nextTick(); formPanel.value?.scrollIntoView?.({ block: 'nearest', behavior: 'instant' })
}
function editPlugin(plugin: OxidnsFlowPlugin) { const value = flowParameters(plugin); void begin({ kind: 'plugin', label: `${plugin.tag} 参数`, source: plugin.source, schema: flowPluginSchema(plugin), original: flowClone(value), before: flowClone(plugin.value), plugin }, value) }
function editGlobals() { displayMode.value = 'graph'; const value = flowClone(props.flow.globals.value); void begin({ kind: 'globals', label: '全局参数', source: props.flow.globals.source, schema: oxidnsGlobalSchema, original: flowClone(value), before: value }, value) }
function editStep(index: number) { if (!activePlugin.value || !steps.value) return; const value = flowClone(steps.value[index]); void begin({ kind: 'step', label: `步骤 ${index + 1}`, source: activePlugin.value.source, schema: flowStepSchema, original: flowClone(value), before: flowClone(activePlugin.value.value), plugin: activePlugin.value, index }, value) }
function insertStep(index: number) { if (!activePlugin.value) return; void begin({ kind: 'step', label: `插入步骤 ${index + 1}`, source: activePlugin.value.source, schema: flowStepSchema, original: null, before: flowClone(activePlugin.value.value), plugin: activePlugin.value, index, insert: true }, { exec: '' }) }
function setShortcut(exec: string) { if (!props.disabled && !formError.value && flowObject(nodeValue.value)) nodeValue.value = { ...nodeValue.value, exec } }
function submitPlugin(plugin: OxidnsFlowPlugin, before: Record<string, unknown>, value: Record<string, unknown>) { emit('change', { source: plugin.source, index: plugin.index, before: flowClone(before), value: flowClone(value) }) }
function applyNode() {
  const current = edit.value
  if (!current || props.disabled || !localDirty.value || nodeIssue.value || !flowObject(nodeValue.value)) return
  const value = flowClone(nodeValue.value)
  cancelEdit()
  if (current.kind === 'globals') { emit('change', { kind: 'globals', before: current.before, value }); return }
  const plugin = current.plugin!
  if (current.kind === 'plugin') { submitPlugin(plugin, current.before, { tag: plugin.tag, type: plugin.type, ...value }); return }
  const args = flowClone(current.before.args as unknown[])
  args.splice(current.index!, current.insert ? 0 : 1, value)
  submitPlugin(plugin, current.before, { ...current.before, args })
}
function moveStep(index: number, delta: number) { if (!activePlugin.value || !steps.value || props.disabled || localDirty.value) return; const args = flowClone(steps.value); const [item] = args.splice(index, 1); args.splice(index + delta, 0, item); cancelEdit(); submitPlugin(activePlugin.value, activePlugin.value.value, { ...activePlugin.value.value, args }) }
function deleteStep(index: number) { if (!activePlugin.value || !steps.value || props.disabled || localDirty.value) return; const args = flowClone(steps.value); args.splice(index, 1); cancelEdit(); submitPlugin(activePlugin.value, activePlugin.value.value, { ...activePlugin.value.value, args }) }
</script>
