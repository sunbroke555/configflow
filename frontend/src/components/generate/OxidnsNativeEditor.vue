<template>
  <section aria-label="OxiDNS 配置" class="flex min-h-0 min-w-0 flex-1 flex-col gap-3">
    <div class="flex shrink-0 flex-wrap items-center justify-between gap-2">
      <div class="flex items-center gap-1">
        <Button v-if="mode === 'full'" variant="outline" class="min-h-11 max-sm:w-11 max-sm:px-0 md:hidden" aria-label="目录" @click="directoryOpen = true"><PanelLeft class="size-4" /><span class="max-sm:sr-only">目录</span></Button>
        <slot name="navigation"><h3 class="text-sm font-semibold max-sm:sr-only">配置编辑</h3></slot>
        <FieldHelp label="完整配置" text="常用设置、完整参数和 YAML 编辑同一份配置，共用一次保存。切换入口或分组不会改写配置，也不会丢失未完成的输入。只有主动添加或修改的参数才写入配置；未知字段和原生值类型均保留。表单修改后会重新整理 YAML 注释和排版。" />
      </div>
      <OxidnsEditorModeToggle :model-value="mode" :disabled="disabled || syncing" :options="[{ value: 'common', label: '常用设置', disabled: Boolean(slots.common) && (formDraftError || syncError) }, { value: 'full', label: '完整参数' }, { value: 'yaml', label: 'YAML', disabled: mode !== 'yaml' && (formDraftError || syncError) }]" label="配置编辑方式" @update:model-value="switchMode" />
    </div>
    <Textarea v-if="mode === 'yaml'" :model-value="draft" :disabled="disabled" :rows="20" aria-label="OxiDNS 完整配置 YAML" :aria-invalid="Boolean(error)" class="min-h-0 min-w-0 flex-1 resize-none font-mono text-xs" @update:model-value="editYaml(String($event))" />
    <div v-show="mode === 'common'" class="flex min-h-0 min-w-0 flex-1 flex-col">
      <slot name="common"><OxidnsCommonEditor v-if="mode === 'common'" v-model:section-id="commonSection" :global-node="globalTree" :plugin-nodes="plugins?.value.items ?? []" :disabled="disabled" :non-list-plugins="nonListPlugins" @change="$event ? commitGlobals() : commit()" @navigate="selectScope" @add-global="addGlobalFeature" @add-plugin="addPlugin" @add-args="addPluginArgs" /></slot>
    </div>
    <div v-if="mode === 'full'" class="grid min-h-0 min-w-0 flex-1 overflow-hidden rounded-lg border border-border md:grid-cols-[256px_minmax(0,1fr)]">
      <aside class="hidden min-h-0 min-w-0 flex-col border-r border-border bg-muted/30 md:flex">
        <OxidnsConfigNavigation :items="navigationItems" :selected-id="selectedId" :disabled="disabled" class="min-h-0 flex-1 overflow-y-auto p-2" @select="selectScope" />
        <div class="flex shrink-0 items-center gap-1 border-t border-border p-2">
          <OxidnsParameterPicker :fields="pluginChoices" label="原生插件" button-text="添加插件" :disabled="disabled || nonListPlugins" @select="addPlugin" />
          <FieldHelp label="添加原生插件" text="选择功能后直接进入表单；只创建 tag、type 和空参数。填写参数后，还需在监听入口或执行链中引用插件，才会参与 DNS 处理。" />
        </div>
      </aside>
      <div ref="formScroll" class="min-h-0 min-w-0 overflow-y-auto p-3 md:p-5" aria-label="当前配置分组">
        <nav class="mb-3 flex min-w-0 flex-wrap items-center gap-x-1 text-xs text-muted-foreground" aria-label="当前配置路径">
          <template v-for="(crumb, index) in breadcrumbs" :key="crumb.id">
            <ChevronRight v-if="index" class="size-3 shrink-0" />
            <button type="button" class="min-h-9 max-w-full truncate rounded px-1 text-left hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-accent max-md:min-h-11" :title="crumb.title" :disabled="disabled" :aria-current="index === breadcrumbs.length - 1 ? 'location' : undefined" @click="selectScope(crumb.id)">{{ crumb.title }}</button>
          </template>
        </nav>
        <div v-if="selectedCategory" class="space-y-4">
          <div class="flex flex-wrap items-center gap-1.5">
            <h4 class="text-sm font-semibold">{{ selectedCategory.label }}</h4>
            <FieldHelp :label="selectedCategory.label" :text="selectedCategory.help" />
            <OxidnsParameterPicker v-if="categoryPluginChoices.length" :fields="categoryPluginChoices" :label="selectedCategory.label" button-text="添加实例" :disabled="disabled || nonListPlugins" class="ml-auto" @select="addPlugin" />
          </div>
          <p class="text-xs text-muted-foreground">选择已配置项继续编辑，或点击带加号的功能添加配置。</p>
          <div class="divide-y divide-border rounded-md border border-border">
            <div v-for="item in categoryItems" :key="item.id" class="flex min-w-0 items-center gap-1 px-2">
              <button type="button" class="flex min-h-14 min-w-0 flex-1 items-center gap-3 rounded-md px-2 py-2 text-left hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-accent disabled:opacity-50" :aria-label="`${item.available ? '添加' : '编辑'} ${item.label}`" :disabled="disabled || item.disabled" @click="selectScope(item.id)">
                <Plus v-if="item.available" class="size-4 shrink-0 text-primary-accent" />
                <span class="min-w-0 flex-1"><span class="block break-words text-xs font-medium">{{ item.label }}</span><code v-if="item.code" class="mt-0.5 block break-all text-[11px] text-muted-foreground">{{ item.code }}</code></span>
                <span class="shrink-0 text-[11px] text-muted-foreground">{{ item.available ? '未配置' : '查看' }}</span><ChevronRight v-if="!item.available" class="size-3.5 shrink-0" />
              </button>
              <FieldHelp v-if="item.help" :label="`${item.label}功能`" :text="item.help" />
            </div>
          </div>
          <p v-if="nonListPlugins && categoryPluginChoices.length" class="text-xs text-warning-accent">plugins 当前不是列表，原值已保留；请在扩展配置中修正类型后添加插件。</p>
        </div>
        <OxidnsSchemaValueEditor v-else-if="selectedScope" :key="selectedScope.id" :node="selectedScope.node" :schema="selectedScope.schema" :label="selectedScope.label" :scope-title="selectedScope.title" :layout="selectedScope.pluginRoot ? 'plugin' : 'default'" :focused="true" :type-locked="selectedId === 'globals'" :removable="Boolean(selectedScope.parentId && !selectedScope.pluginRoot)" :disabled="disabled" @change="selectedScope.global ? commitGlobals() : commit()" @navigate="selectScope" @remove="removeSelectedScope">
          <template #header>
            <div class="flex min-w-0 flex-1 flex-wrap items-center gap-1.5">
            <h4 class="min-w-0 flex-1 basis-24 break-all text-sm font-semibold">{{ selectedScope.title }}</h4>
            <span v-if="usesReverseLookupDefaults" class="text-[11px] font-normal text-muted-foreground">使用默认参数</span><FieldHelp v-if="usesReverseLookupDefaults" label="反向查询缓存默认参数" text="args: {} 合法。OxiDNS 会使用容量 65535、TTL 7200 秒、handle_ptr: false。需要直接回答 PTR 查询时添加并开启 handle_ptr；插件还需在执行链中引用才参与处理。" />
            <FieldHelp :label="`${selectedScope.title}分组`" :text="selectedScope.schema?.help || '扩展分组，所有原生字段和值类型均保留。'" />
            <a :href="selectedScope.documentation" target="_blank" rel="noopener noreferrer" class="ml-auto inline-flex min-h-9 shrink-0 items-center gap-1 text-xs text-primary-accent underline-offset-4 hover:underline max-md:min-h-11">文档<ExternalLink class="size-3" /></a>
        <div v-if="selectedPluginIndex >= 0" class="flex shrink-0 items-center gap-0.5">
          <Button variant="ghost" size="icon" class="oxi-native-icon" :disabled="disabled || selectedPluginIndex === 0" :aria-label="`上移原生插件 ${selectedPluginIndex + 1}`" @click="movePlugin(selectedPluginIndex, -1)"><ArrowUp class="size-3.5" /></Button>
          <Button variant="ghost" size="icon" class="oxi-native-icon" :disabled="disabled || selectedPluginIndex === (plugins?.value.items.length ?? 0) - 1" :aria-label="`下移原生插件 ${selectedPluginIndex + 1}`" @click="movePlugin(selectedPluginIndex, 1)"><ArrowDown class="size-3.5" /></Button>
          <Button variant="ghost" size="icon" class="oxi-native-icon text-muted-foreground hover:text-destructive-accent" :disabled="disabled" :aria-label="`删除原生插件 ${selectedPluginIndex + 1}`" @click="removePlugin(selectedPluginIndex)"><Trash2 class="size-3.5" /></Button>
        </div>
            </div>
          </template>
        </OxidnsSchemaValueEditor>
        <div v-else class="space-y-3">
          <p v-if="nonListPlugins" class="text-xs text-warning-accent">plugins 不是列表，已保留在全局参数中；请修正类型后添加插件。</p>
          <p v-else-if="!plugins?.value.items.length" class="text-xs text-muted-foreground">尚无插件。选择类型添加，或切换 YAML 粘贴完整配置。</p>
          <button v-for="(plugin, index) in plugins?.value.items ?? []" :key="plugin.id" type="button" class="flex min-h-11 w-full min-w-0 items-center gap-3 rounded-md border border-border px-3 py-2 text-left hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-accent disabled:opacity-50" :disabled="disabled" @click="selectScope(plugin.id)"><span class="num text-xs text-muted-foreground">{{ index + 1 }}</span><span class="min-w-0 flex-1"><span class="block truncate font-mono text-xs">{{ scalarField(plugin, 'tag') || `插件 ${index + 1}` }}</span><span class="block text-xs text-muted-foreground">{{ scalarField(plugin, 'type') || '未指定类型' }}</span></span><ChevronRight class="size-4 shrink-0" /></button>
          <OxidnsParameterPicker :fields="pluginChoices" label="插件总览" button-text="添加插件" :disabled="disabled || nonListPlugins" @select="addPlugin" />
        </div>
      </div>
    </div>
    <Sheet v-model:open="directoryOpen">
      <SheetContent side="left" class="z-[60] w-[min(320px,calc(100vw-2rem))] gap-2" aria-describedby="oxi-directory-description">
        <SheetHeader><SheetTitle>配置目录</SheetTitle><SheetDescription id="oxi-directory-description">选择分组后直接编辑，当前草稿会保留。</SheetDescription></SheetHeader>
        <OxidnsConfigNavigation :items="navigationItems" :selected-id="selectedId" :disabled="disabled" class="min-h-0 flex-1 overflow-y-auto px-2" @select="selectScope" />
        <div class="border-t border-border p-3"><OxidnsParameterPicker :fields="pluginChoices" label="移动端原生插件" button-text="添加插件" :disabled="disabled || nonListPlugins" @select="addPlugin" /></div>
      </SheetContent>
    </Sheet>
    <p v-if="syncing" role="status" class="shrink-0 text-sm text-muted-foreground">正在同步配置依赖…</p>
    <p v-else-if="linkedChange && !error" role="status" class="shrink-0 break-words text-sm text-muted-foreground">{{ linkedChange }}</p>
    <div v-if="error" class="flex shrink-0 flex-wrap items-center gap-2">
      <p role="alert" class="min-w-0 break-words text-sm text-destructive-accent">{{ error }}</p>
      <Button v-if="formDraftError && invalidScopeId && mode !== 'yaml'" variant="outline" size="sm" @click="selectScope(invalidScopeId)">返回未完成输入</Button>
      <template v-if="syncError">
        <Button v-for="(reference, index) in references" :key="index" variant="outline" size="sm" class="min-h-11" :disabled="disabled" @click="locateIssue(reference.path)">查看引用 {{ index + 1 }}</Button>
        <Button variant="outline" size="sm" class="min-h-11" :disabled="disabled" @click="commit">重试同步</Button>
        <Button variant="ghost" size="sm" class="min-h-11" :disabled="disabled" @click="discardNativeChanges">撤销未同步修改</Button>
      </template>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, useSlots, watch } from 'vue'
import { ArrowDown, ArrowUp, ChevronRight, ExternalLink, PanelLeft, Plus, Trash2 } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import FieldHelp from '@/components/common/FieldHelp.vue'
import OxidnsCommonEditor, { type OxidnsCommonSection } from './OxidnsCommonEditor.vue'
import OxidnsEditorModeToggle from './OxidnsEditorModeToggle.vue'
import OxidnsSchemaValueEditor from './OxidnsSchemaValueEditor.vue'
import OxidnsParameterPicker from './OxidnsParameterPicker.vue'
import OxidnsConfigNavigation, { type OxidnsNavigationItem } from './OxidnsConfigNavigation.vue'
import { oxidnsFeatureCategories, getOxidnsGlobalCategory, getOxidnsPluginCategory } from '@/lib/oxidnsFeatureNavigation'
import { appendConfigEntry, configNodeToValue, defaultConfigNode, parseConfigDocument, reconcileConfigNode, serializeConfigDocument, valueToConfigNode, type ConfigValueNode } from '@/lib/oxidnsConfigEditor'
import { oxidnsGlobalSchema, oxidnsPluginCatalog, type OxidnsSchema } from '@/lib/oxidnsSchema'
import { needsOxidnsReconciliation, type OxidnsNativeChangeResult, type OxidnsNativeReference } from '@/lib/oxidnsNativeChanges'

type EditorView = 'common' | 'full' | 'yaml'
const props = withDefaults(defineProps<{ modelValue: string; disabled?: boolean; initialView?: EditorView; reconcile?: (before: string, after: string, renames?: Record<string, string>) => Promise<OxidnsNativeChangeResult> }>(), { disabled: false, initialView: 'common' })
const emit = defineEmits<{ (event: 'update:modelValue', value: string): void; (event: 'error', message: string): void; (event: 'view', value: EditorView): void; (event: 'pending', value: boolean): void }>()
const slots = useSlots()
const tree = ref(valueToConfigNode({}))
const globalTree = ref(valueToConfigNode({}))
const draft = ref('')
const mode = ref<EditorView>(props.initialView)
const commonSection = ref<OxidnsCommonSection>('runtime')
const error = ref('')
const linkedChange = ref('')
const syncing = ref(false)
const syncError = ref(false)
const references = ref<OxidnsNativeReference[]>([])
let syncVersion = 0
function setSyncing(value: boolean) { syncing.value = value; emit('pending', value) }
const formDraftError = ref(false)
const invalidScopeId = ref('')
const selectedId = ref('category:runtime')
const directoryOpen = ref(false)
const formScroll = ref<HTMLElement>()
let lastEmitted: string | undefined
let acceptedTags = new Map<string, string>()
const plugins = computed(() => tree.value.entries.find(entry => entry.key === 'plugins' && entry.value.type === 'array'))
const nonListPlugins = computed(() => tree.value.entries.some(entry => entry.key === 'plugins' && entry.value.type !== 'array'))
const scalarField = (node: ConfigValueNode, key: string) => node.entries.find(entry => entry.key === key)?.value.scalar ?? ''
function rememberTags() { acceptedTags = new Map((plugins.value?.value.items ?? []).map(node => [node.id, scalarField(node, 'tag')])) }
function renamedTags() {
  return Object.fromEntries((plugins.value?.value.items ?? []).flatMap(node => {
    const previous = acceptedTags.get(node.id)
    const current = scalarField(node, 'tag')
    return previous && current && previous !== current ? [[previous, current]] : []
  }))
}
const pluginDefinition = (node: ConfigValueNode) => oxidnsPluginCatalog.find(plugin => plugin.type === scalarField(node, 'type'))
const pluginChoices = [...oxidnsPluginCatalog.map(plugin => ({ key: plugin.type, schema: { type: 'string' as const, label: plugin.label, help: plugin.help } })), { key: '__custom__', schema: { type: 'string' as const, label: '自定义类型', help: '用于更新版本或第三方插件，添加后手动填写类型及参数。' } }]
function pluginSchema(node: ConfigValueNode): OxidnsSchema {
  return { type: 'object', help: pluginDefinition(node)?.help || '自定义插件类型。全部原生字段都会保留，请按对应文档设置参数。', properties: {
    tag: { type: 'string', label: '唯一标识', help: '其他插件通过 $tag 引用此插件。标识不得与其他插件重复。' },
    type: { type: 'string', label: '插件类型', help: '选择官方插件类型，或更改值类型后填写自定义类型。更改插件类型会保留已有参数，请按新插件文档调整。', enum: oxidnsPluginCatalog.map(plugin => plugin.type) },
    args: pluginDefinition(node)?.args ?? { type: 'object', label: '参数', help: '按插件定义添加参数。支持对象、列表、文本等全部原生值类型。' },
  } }
}
type NavigationItem = OxidnsNavigationItem
type AddAction = { kind: 'global'; path: string[] } | { kind: 'plugin'; type: string }
interface Scope { id: string; node: ConfigValueNode; schema?: OxidnsSchema; label: string; title: string; nativePath: string; parentId?: string; ownerId?: string; global: boolean; pluginRoot?: boolean; documentation: string; identity: string }
// Navigation and forms share nodes, including unfinished inputs. Navigation never serializes them.
const directory = computed(() => {
  const scopes = new Map<string, Scope>()
  const additions = new Map<string, AddAction>()
  const root: Scope = { id: 'globals', node: globalTree.value, schema: oxidnsGlobalSchema, label: '全局参数', title: '全局参数', nativePath: '', global: true, documentation: 'https://oxidns.org/configuration/global/', identity: 'globals' }
  scopes.set(root.id, root)
  function descend(parent: Scope): NavigationItem[] {
    const children: NavigationItem[] = []
    const entries = parent.node.type === 'object' ? parent.node.entries.map(entry => ({ key: entry.key, node: entry.value, schema: parent.schema?.properties?.[entry.key] ?? parent.schema?.additionalProperties }))
      : parent.node.type === 'array' ? parent.node.items.map((node, index) => ({ key: String(index + 1), node, schema: parent.schema?.items })) : []
    for (const entry of entries) {
      const isArray = parent.node.type === 'array'
      const summary = isArray ? ['addr', 'tag', 'exec', 'path', 'name', 'domain'].map(key => scalarField(entry.node, key)).find(Boolean) : ''
      const title = isArray ? `${entry.key} · ${summary || entry.schema?.label || '条目'}` : entry.schema?.label || entry.key
      const path = isArray ? `${parent.nativePath}[${entry.key}]` : [parent.nativePath, entry.key].filter(Boolean).join('.')
      const scope: Scope = { id: entry.node.id, node: entry.node, schema: entry.schema, label: isArray ? `${parent.label}[${entry.key}]` : `${parent.label}.${entry.key}`, title, nativePath: path, parentId: parent.id, ownerId: parent.id, global: parent.global, documentation: parent.documentation, identity: `${parent.identity}/${JSON.stringify(entry.key)}` }
      scopes.set(scope.id, scope)
      // Keep scalar scopes addressable after an object is changed to another native value type.
      if (entry.node.type !== 'object' && entry.node.type !== 'array' && parent.id !== 'globals' && !['object', 'array'].includes(entry.schema?.type || '')) continue
      const nested = descend(scope)
      // args is an implementation wrapper; put its meaningful groups below the plugin.
      if (parent.pluginRoot && entry.key === 'args') {
        for (const child of nested) scopes.get(child.id)!.parentId = parent.id
        children.push(...nested)
      } else children.push({ id: scope.id, label: title, code: path, help: entry.schema?.help, defaultExpanded: false, children: nested })
    }
    return children
  }
  const globalItems = descend(root)
  // Absent schema groups are navigation actions, never configuration nodes.
  function includeAvailableGlobals(items: NavigationItem[], node: ConfigValueNode | undefined, schema: OxidnsSchema, path: string[] = []) {
    if (node && node.type !== 'object') return
    for (const [key, field] of Object.entries(schema.properties ?? {})) {
      if (!['object', 'array'].includes(field.type)) continue
      const childPath = [...path, key]
      const existing = node?.entries.find(entry => entry.key === key)?.value
      let item = existing ? items.find(item => item.id === existing.id) : undefined
      if (!existing) {
        const id = `add:global:${JSON.stringify(childPath)}`
        additions.set(id, { kind: 'global', path: childPath })
        item = { id, label: field.label || key, code: childPath.join('.'), help: field.help, available: true, defaultExpanded: false, children: [] }
        items.push(item)
      }
      if (item && field.type === 'object') {
        item.children ??= []
        includeAvailableGlobals(item.children, existing, field, childPath)
      }
    }
  }
  includeAvailableGlobals(globalItems, globalTree.value, oxidnsGlobalSchema)
  const pluginItems = (plugins.value?.value.items ?? []).map((node, index) => {
    const tag = scalarField(node, 'tag')
    const scope: Scope = { id: node.id, node, schema: pluginSchema(node), label: `插件 ${index + 1}`, title: tag || `插件 ${index + 1}`, nativePath: `plugins[${index + 1}]`, parentId: `category:${getOxidnsPluginCategory(scalarField(node, 'type'))}`, global: false, pluginRoot: true, documentation: pluginDefinition(node)?.documentation || 'https://oxidns.org/plugin-reference/overview/', identity: `plugin:${tag ? JSON.stringify(tag) : index}` }
    scopes.set(scope.id, scope)
    return { id: scope.id, label: scope.title, code: `${scalarField(node, 'type') || '未指定类型'} · ${scope.nativePath}`, help: pluginDefinition(node)?.help || '自定义插件，全部原生参数均保留。', defaultExpanded: false, children: descend(scope) }
  })
  const categories: NavigationItem[] = oxidnsFeatureCategories.map(category => ({ id: `category:${category.id}`, label: category.label, help: category.help, kind: 'section', defaultExpanded: category.id === 'runtime', children: [] }))
  const category = (id: string) => categories.find(item => item.id === `category:${id}`)!
  for (const item of globalItems) {
    const action = additions.get(item.id)
    const scope = scopes.get(item.id)
    const key = action?.kind === 'global' ? action.path[0] : globalTree.value.entries.find(entry => entry.value.id === item.id)?.key || ''
    const group = category(getOxidnsGlobalCategory(key))
    group.children!.push(item)
    if (scope) scope.parentId = group.id
  }
  for (const item of pluginItems) category(getOxidnsPluginCategory(scalarField(scopes.get(item.id)!.node, 'type'))).children!.push(item)
  const configuredTypes = new Set((plugins.value?.value.items ?? []).map(node => scalarField(node, 'type')))
  for (const definition of oxidnsPluginCatalog) {
    if (configuredTypes.has(definition.type)) continue
    const id = `add:plugin:${definition.type}`
    additions.set(id, { kind: 'plugin', type: definition.type })
    category(getOxidnsPluginCategory(definition.type)).children!.push({ id, label: definition.label, code: definition.type, help: definition.help, available: true, disabled: nonListPlugins.value })
  }
  for (const item of categories) {
    const available = item.children!.filter(child => child.available).length
    item.code = `${item.children!.length - available} 已配置 · ${available} 未配置`
  }
  category('advanced').children!.push(
    { id: 'globals', label: '全部全局参数', help: '查看全部顶层字段，或添加自定义扩展字段。' },
    { id: 'plugins', label: '全部插件 / 原始顺序', code: `${pluginItems.length} 个插件`, help: '查看完整插件列表。功能分类仅用于导航，不改变 YAML 中的原始插件顺序。' },
  )
  return { scopes, items: categories, additions }
})
const navigationItems = computed(() => directory.value.items)
const selectedCategory = computed(() => oxidnsFeatureCategories.find(category => selectedId.value === `category:${category.id}`))
const categoryItems = computed(() => navigationItems.value.find(item => item.id === selectedId.value)?.children ?? [])
const categoryPluginChoices = computed(() => pluginChoices.filter(choice => getOxidnsPluginCategory(choice.key) === selectedCategory.value?.id))
const isCategory = (id: string) => oxidnsFeatureCategories.some(category => id === `category:${category.id}`)
const selectedScope = computed(() => directory.value.scopes.get(selectedId.value))
const usesReverseLookupDefaults = computed(() => {
  const scope = selectedScope.value
  const args = scope?.node.entries.find(entry => entry.key === 'args')?.value
  return scope?.pluginRoot && scalarField(scope.node, 'type') === 'reverse_lookup' && (!args || args.type === 'null' || (args.type === 'object' && !args.entries.length))
})
const selectedPluginIndex = computed(() => selectedScope.value?.pluginRoot ? (plugins.value?.value.items ?? []).findIndex(node => node.id === selectedId.value) : -1)
const breadcrumbs = computed(() => {
  const result: Array<{ id: string; title: string }> = []
  let scope = selectedScope.value
  if (!scope) return [{ id: selectedId.value, title: selectedCategory.value?.label || '全部插件 / 原始顺序' }]
  while (scope) {
    result.unshift({ id: scope.id, title: scope.title })
    const category = oxidnsFeatureCategories.find(category => scope!.parentId === `category:${category.id}`)
    if (category) { result.unshift({ id: `category:${category.id}`, title: category.label }); break }
    scope = scope.parentId ? directory.value.scopes.get(scope.parentId) : undefined
  }
  return result
})
function selectScope(id: string) {
  if (props.disabled) return
  const addition = directory.value.additions.get(id)
  if (addition) {
    addition.kind === 'plugin' ? addPlugin(addition.type) : addGlobalFeature(addition.path)
    return
  }
  if (id !== 'plugins' && !isCategory(id) && !directory.value.scopes.has(id)) return
  mode.value = 'full'
  selectedId.value = id
  directoryOpen.value = false
  nextTick(() => { if (formScroll.value) formScroll.value.scrollTop = 0 })
}
watch(() => directory.value, current => {
  if (selectedId.value !== 'plugins' && !isCategory(selectedId.value) && !current.scopes.has(selectedId.value)) selectedId.value = 'category:runtime'
})
function report(message = '') { error.value = message; emit('error', message) }
function publish(text: string) { lastEmitted = text; emit('update:modelValue', text) }
function validate() {
  // Parameter semantics come from the shared server validator. Keep incomplete
  // but serializable values editable, including legitimate null/default forms.
  configNodeToValue(tree.value)
}
function locateIssue(path: Array<string | number>) {
  if (props.disabled || formDraftError.value) return
  let node = tree.value
  let scopeId = 'globals'
  for (const part of path) {
    const child = typeof part === 'number' ? node.items[part] : node.entries.find(entry => entry.key === part)?.value
    if (!child) break
    node = child
    if (directory.value.scopes.has(node.id)) scopeId = node.id
  }
  selectScope(scopeId)
}
function syncGlobals() { globalTree.value = { ...tree.value, entries: tree.value.entries.filter(entry => entry !== plugins.value) } }
function replaceTree(text: string) {
  linkedChange.value = ''
  syncError.value = false
  references.value = []
  const identity = selectedScope.value?.identity
  const overview = selectedId.value === 'plugins' || isCategory(selectedId.value) ? selectedId.value : undefined
  tree.value = parseConfigDocument(text)
  rememberTags()
  syncGlobals()
  selectedId.value = overview || [...directory.value.scopes.values()].find(scope => scope.identity === identity)?.id || 'category:runtime'
  formDraftError.value = false
  invalidScopeId.value = ''
}
function parseDraft() {
  try { replaceTree(draft.value); validate(); report(); return true }
  catch (failure: any) { report(String(failure.message || '请检查 YAML 格式。').replace('基础配置扩展必须是 YAML 对象；可清空文本表示不扩展。', '完整配置必须是 YAML 对象。')); return false }
}
function editYaml(text: string) { draft.value = text; publish(text); parseDraft() }
function firstInvalidNode(node: ConfigValueNode): string {
  const children = node.type === 'object' ? node.entries.map(entry => entry.value) : node.type === 'array' ? node.items : []
  for (const child of children) {
    try { configNodeToValue(child) } catch { return firstInvalidNode(child) }
  }
  return node.id
}
async function commit() {
  const version = ++syncVersion
  linkedChange.value = ''
  const retry = syncError.value || syncing.value
  syncError.value = false
  references.value = []
  let candidate: string
  try {
    candidate = serializeConfigDocument(tree.value)
    formDraftError.value = false
    invalidScopeId.value = ''
  }
  catch (failure: any) {
    setSyncing(false)
    const invalidId = firstInvalidNode(tree.value)
    invalidScopeId.value = directory.value.scopes.has(invalidId) ? invalidId : 'globals'
    formDraftError.value = true
    report(failure.message || '请修正配置内容。')
    return
  }
  if (props.reconcile && (retry || needsOxidnsReconciliation(configNodeToValue(parseConfigDocument(draft.value)), configNodeToValue(tree.value)))) {
    setSyncing(true)
    report()
    try {
      const renames = renamedTags()
      const result = await props.reconcile(draft.value, candidate, Object.keys(renames).length ? renames : undefined)
      if (version !== syncVersion) return
      const value = configNodeToValue(parseConfigDocument(result.native_config))
      reconcileConfigNode(tree.value, value)
      syncGlobals()
      candidate = result.native_config
      linkedChange.value = result.notices?.join(' ') || ''
    } catch (failure: any) {
      if (version !== syncVersion) return
      syncError.value = true
      const data = failure.response?.data
      references.value = Array.isArray(data?.references) ? data.references.filter((item: any) => Array.isArray(item.path)) : []
      report(data?.message || failure.message || '同步配置依赖失败，请重试。')
      return
    } finally { if (version === syncVersion) setSyncing(false) }
  }
  draft.value = candidate
  rememberTags()
  publish(candidate)
  report()
}
function discardNativeChanges() {
  ++syncVersion
  setSyncing(false)
  parseDraft()
}
function commitGlobals() {
  const plugin = plugins.value
  tree.value.entries = [...globalTree.value.entries.filter(entry => entry !== plugin), ...(plugin ? [plugin] : [])]
  commit()
  syncGlobals()
}
function switchMode(next: EditorView) {
  if (props.disabled || syncing.value || next === mode.value) return
  if (next === 'common' && slots.common && (formDraftError.value || syncError.value)) return
  if (next !== 'yaml' && mode.value === 'yaml') {
    // Valid YAML already updated the shared tree; avoid recreating IDs just for a view change.
    try { parseConfigDocument(draft.value) } catch { parseDraft(); return }
  } else if (next === 'yaml' && (formDraftError.value || syncError.value)) return
  mode.value = next
}
function showFull(target?: string) {
  if (props.disabled) return
  const scope = [...directory.value.scopes.values()].find(scope => scope.nativePath === target
    || (scope.pluginRoot && scalarField(scope.node, 'tag') === target))
  selectScope(scope?.id || (target?.startsWith('category:') ? target : 'category:runtime'))
}
defineExpose({ showFull, locateIssue })
watch(mode, value => emit('view', value), { immediate: true })
function addGlobalFeature(path: string[]) {
  if (props.disabled) return
  let current = globalTree.value
  let schema = oxidnsGlobalSchema
  // Resolve the full path before mutating; never coerce an existing scalar or null parent.
  for (const key of path) {
    const field = schema.properties?.[key]
    if (!field || current.type !== 'object') return
    const existing = current.entries.find(entry => entry.key === key)?.value
    current = existing ?? defaultConfigNode(field.type)
    schema = field
  }
  current = globalTree.value
  schema = oxidnsGlobalSchema
  for (const key of path) {
    const field = schema.properties![key]
    current = current.entries.find(entry => entry.key === key)?.value ?? appendConfigEntry(current, key, configNodeToValue(defaultConfigNode(field.type))).value
    schema = field
  }
  selectedId.value = current.id
  directoryOpen.value = false
  commitGlobals()
  nextTick(() => { if (formScroll.value) formScroll.value.scrollTop = 0 })
}
function addPlugin(selectedType: string) {
  if (props.disabled || nonListPlugins.value) return
  if (selectedType !== '__custom__' && !oxidnsPluginCatalog.some(plugin => plugin.type === selectedType)) return
  const type = selectedType === '__custom__' ? '' : selectedType
  const definition = oxidnsPluginCatalog.find(plugin => plugin.type === type)
  const target = plugins.value ?? appendConfigEntry(tree.value, 'plugins', [])
  const used = new Set(target.value.items.map(plugin => scalarField(plugin, 'tag')))
  const base = type || 'custom'
  let tag = base
  for (let i = 2; used.has(tag); i++) tag = `${base}_${i}`
  const plugin = valueToConfigNode({ tag, type, args: configNodeToValue(defaultConfigNode(definition?.args.type ?? 'object')) })
  target.value.items.push(plugin)
  syncGlobals()
  selectedId.value = plugin.id
  directoryOpen.value = false
  commit()
  nextTick(() => { if (formScroll.value) formScroll.value.scrollTop = 0 })
}
function addPluginArgs(pluginId: string) {
  const plugin = plugins.value?.value.items.find(node => node.id === pluginId)
  if (props.disabled || !plugin || plugin.type !== 'object' || plugin.entries.some(entry => entry.key === 'args')) return
  appendConfigEntry(plugin, 'args', configNodeToValue(defaultConfigNode(pluginDefinition(plugin)?.args.type ?? 'object')))
  commit()
}
function movePlugin(index: number, offset: number) {
  if (props.disabled) return
  const items = plugins.value!.value.items
  if (index + offset < 0 || index + offset >= items.length) return
  const [item] = items.splice(index, 1)
  items.splice(index + offset, 0, item)
  commit()
}
function removePlugin(index: number) {
  if (props.disabled) return
  plugins.value!.value.items.splice(index, 1)
  selectedId.value = plugins.value!.value.items[index]?.id || plugins.value!.value.items[index - 1]?.id || 'plugins'
  commit()
}
function removeSelectedScope() {
  const scope = selectedScope.value
  const parent = scope?.ownerId && directory.value.scopes.get(scope.ownerId)
  if (props.disabled || !scope || !parent || scope.pluginRoot) return
  if (parent.node.type === 'object') {
    const index = parent.node.entries.findIndex(entry => entry.value.id === scope.id)
    if (index < 0) return
    parent.node.entries.splice(index, 1)
  } else if (parent.node.type === 'array') {
    const index = parent.node.items.findIndex(item => item.id === scope.id)
    if (index < 0) return
    parent.node.items.splice(index, 1)
  } else return
  selectedId.value = scope.parentId || parent.id
  scope.global ? commitGlobals() : commit()
}
watch(() => props.modelValue, text => {
  if (text === lastEmitted) { lastEmitted = undefined; return }
  ++syncVersion
  setSyncing(false)
  lastEmitted = undefined
  draft.value = text ?? ''
  if (!parseDraft()) {
    try { parseConfigDocument(draft.value) } catch { mode.value = 'yaml' }
  }
}, { immediate: true })
onBeforeUnmount(() => { ++syncVersion })
</script>
