<template>
  <div class="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-lg border border-border" aria-label="OxiDNS 常用设置">
    <div class="oxi-common-categories grid shrink-0 grid-cols-4 gap-1 border-b border-border bg-muted/30 p-2" role="group" aria-label="常用设置分类">
      <button v-for="section in sections" :key="section.id" type="button" class="min-h-11 rounded-md px-1 text-xs sm:px-3 font-medium outline-none hover:bg-accent focus-visible:ring-2 focus-visible:ring-primary-accent disabled:opacity-50" :class="section.id === sectionId ? 'bg-primary-soft text-primary-accent' : 'text-muted-foreground'" :aria-pressed="section.id === sectionId" :disabled="disabled" @click="emit('update:sectionId', section.id)">{{ section.label }}</button>
    </div>
    <div class="min-h-0 min-w-0 flex-1 space-y-4 overflow-y-auto p-3 md:p-5">
      <div class="flex flex-wrap items-center gap-1.5">
        <h4 class="text-sm font-semibold">{{ currentSection.label }}</h4>
        <FieldHelp :label="`常用${currentSection.label}`" :text="currentSection.help" />
        <OxidnsParameterPicker v-if="pluginChoices.length" :fields="pluginChoices" :label="`常用${currentSection.label}`" button-text="添加实例" :disabled="disabled || nonListPlugins" class="ml-auto" @select="emit('addPlugin', $event)" />
      </div>
      <section v-for="item in items" :key="item.id" class="min-w-0 rounded-md border border-border p-3" :aria-label="`常用 ${item.title}`">
        <OxidnsSchemaValueEditor v-if="item.node" :node="item.node" :schema="item.schema" :label="item.label" :scope-title="item.schema?.label || '参数'" :focused="true" :disabled="disabled" @change="emit('change', item.global)" @navigate="emit('navigate', $event)">
          <template #header>
            <div class="min-w-0 flex-1"><h5 class="break-all text-xs font-semibold">{{ item.title }}</h5><code class="break-all text-[11px] text-muted-foreground">{{ item.code }}</code></div>
            <FieldHelp :label="`常用 ${item.title}`" :text="item.help" />
            <Button type="button" variant="ghost" class="min-h-11 shrink-0 gap-1 text-xs md:min-h-9" :disabled="disabled" :aria-label="`${item.title} 完整参数`" @click="emit('navigate', item.id)">完整参数<ChevronRight class="size-3.5" /></Button>
          </template>
        </OxidnsSchemaValueEditor>
        <div v-else class="flex min-w-0 flex-wrap items-center gap-2"><div class="min-w-0 flex-1"><h5 class="break-all text-xs font-semibold">{{ item.title }}</h5><span class="text-xs text-muted-foreground">尚未配置参数</span></div><FieldHelp :label="`常用 ${item.title}`" :text="item.help" /><Button type="button" variant="outline" class="min-h-11 text-xs md:min-h-9" :disabled="disabled" :aria-label="`${item.title} 添加参数分组`" @click="emit('addArgs', item.id)"><Plus class="size-3.5" />添加参数</Button></div>
      </section>
      <div v-if="missing.length" class="divide-y divide-border rounded-md border border-border">
        <div v-for="item in missing" :key="item.key" class="flex min-w-0 items-center gap-1 px-2">
          <button type="button" class="flex min-h-14 min-w-0 flex-1 items-center gap-3 rounded-md px-2 py-2 text-left hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-accent disabled:opacity-50" :disabled="disabled || (sectionId !== 'runtime' && nonListPlugins)" :aria-label="`常用设置 添加${item.label}`" @click="addMissing(item.key)"><Plus class="size-4 shrink-0 text-primary-accent" /><span class="min-w-0 flex-1"><span class="block text-xs font-medium">{{ item.label }}</span><code class="text-[11px] text-muted-foreground">{{ item.key }}</code></span><span class="shrink-0 text-[11px] text-muted-foreground">未配置</span></button>
          <FieldHelp :label="`常用 ${item.label}`" :text="item.help" />
        </div>
      </div>
      <p v-if="nonListPlugins && sectionId !== 'runtime'" class="text-xs text-warning-accent">plugins 当前不是列表；请在完整参数中修正后添加实例。</p>
      <p class="text-xs text-muted-foreground">其他功能和扩展字段可在完整参数中编辑。三个入口共用当前配置。</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { ChevronRight, Plus } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import FieldHelp from '@/components/common/FieldHelp.vue'
import OxidnsSchemaValueEditor from './OxidnsSchemaValueEditor.vue'
import OxidnsParameterPicker from './OxidnsParameterPicker.vue'
import { oxidnsGlobalSchema, oxidnsPluginCatalog, type OxidnsSchema } from '@/lib/oxidnsSchema'
import type { ConfigValueNode } from '@/lib/oxidnsConfigEditor'

export type OxidnsCommonSection = 'runtime' | 'upstreams' | 'cache' | 'listeners'
const props = withDefaults(defineProps<{ globalNode: ConfigValueNode; pluginNodes: ConfigValueNode[]; sectionId: OxidnsCommonSection; disabled?: boolean; nonListPlugins?: boolean }>(), { disabled: false, nonListPlugins: false })
const emit = defineEmits<{ 'update:sectionId': [value: OxidnsCommonSection]; change: [global: boolean]; navigate: [id: string]; addGlobal: [path: string[]]; addPlugin: [type: string]; addArgs: [pluginId: string] }>()
const sections: Array<{ id: OxidnsCommonSection; label: string; help: string; types: string[] }> = [
  { id: 'runtime', label: '运行设置', help: '调整当前配置的工作线程、日志和管理 API。未配置的分组需要主动添加；添加不会填入整组默认值。', types: [] },
  { id: 'upstreams', label: 'DNS 上游', help: '这里列出当前配置中全部 forward 实例。修改上游地址、并发数和连接参数会同步到完整参数及 YAML。新增实例还需在执行链中引用才参与查询。', types: ['forward'] },
  { id: 'cache', label: '缓存', help: '编辑当前缓存和反向查询缓存实例。新增插件还需在执行链中引用；省略的可选参数由 OxiDNS 使用自身默认值。', types: ['cache', 'reverse_lookup'] },
  { id: 'listeners', label: '监听服务', help: '编辑 UDP、TCP、HTTP 和 QUIC 监听实例。填写监听地址、处理入口，以及对应协议所需的证书等必填参数后才能保存。', types: ['udp_server', 'tcp_server', 'http_server', 'quic_server'] },
]
const currentSection = computed(() => sections.find(section => section.id === props.sectionId)!)
const scalarField = (node: ConfigValueNode, key: string) => node.entries.find(entry => entry.key === key)?.value.scalar ?? ''
interface CommonItem { id: string; title: string; code: string; help: string; label: string; node?: ConfigValueNode; schema?: OxidnsSchema; global: boolean }
const items = computed<CommonItem[]>(() => {
  if (props.sectionId === 'runtime') return ['runtime', 'log', 'api'].flatMap(key => {
    const node = props.globalNode.entries.find(entry => entry.key === key)?.value
    const schema = oxidnsGlobalSchema.properties?.[key]
    if (!node) return []
    // The common management field is the HTTP group; skip its structural wrapper.
    // Other api keys remain in the shared tree and are reachable through the complete form.
    const http = key === 'api' && node.type === 'object' ? node.entries.find(entry => entry.key === 'http')?.value : undefined
    return [{ id: node.id, title: schema?.label || key, code: http ? 'api.http' : key, help: schema?.help || '配置此运行分组。', label: http ? '全局参数.api.http' : `全局参数.${key}`, node: http || node, schema: http ? schema?.properties?.http : schema, global: true }]
  })
  return props.pluginNodes.flatMap((plugin, index) => {
    const type = scalarField(plugin, 'type')
    if (!currentSection.value.types.includes(type)) return []
    const definition = oxidnsPluginCatalog.find(item => item.type === type)
    return [{ id: plugin.id, title: scalarField(plugin, 'tag') || `插件 ${index + 1}`, code: type, help: definition?.help || '当前配置中的插件实例。', label: `插件 ${index + 1}.args`, node: plugin.entries.find(entry => entry.key === 'args')?.value, schema: definition?.args, global: false }]
  })
})
const pluginChoices = computed(() => oxidnsPluginCatalog.filter(plugin => currentSection.value.types.includes(plugin.type)).map(plugin => ({ key: plugin.type, schema: { type: 'string' as const, label: plugin.label, help: plugin.help } })))
const missing = computed(() => props.sectionId === 'runtime'
  ? ['runtime', 'log', 'api'].filter(key => oxidnsGlobalSchema.properties?.[key] && !props.globalNode.entries.some(entry => entry.key === key)).map(key => ({ key, label: oxidnsGlobalSchema.properties![key].label || key, help: oxidnsGlobalSchema.properties![key].help }))
  : pluginChoices.value.filter(choice => !props.pluginNodes.some(plugin => scalarField(plugin, 'type') === choice.key)).map(choice => ({ key: choice.key, label: choice.schema.label, help: choice.schema.help })))
function addMissing(key: string) { props.sectionId === 'runtime' ? emit('addGlobal', [key]) : emit('addPlugin', key) }
</script>

<style scoped>
@media (width < 640px) {
  .oxi-common-categories {
    position: relative;
    isolation: isolate;
    height: var(--cf-touch);
    border: 0;
    padding-block: 0;
    background: transparent;
  }

  .oxi-common-categories::before {
    content: '';
    position: absolute;
    z-index: -1;
    inset-inline: 0;
    top: calc((var(--cf-touch) - var(--cf-choice-track)) / 2);
    height: var(--cf-choice-track);
    border-bottom: 1px solid var(--border);
    background: color-mix(in oklab, var(--muted) 30%, transparent);
    pointer-events: none;
  }

  .oxi-common-categories button {
    position: relative;
    isolation: isolate;
    height: var(--cf-touch);
    background: transparent;
  }

  .oxi-common-categories button::before {
    content: '';
    position: absolute;
    z-index: -1;
    inset-inline: 0;
    top: calc((var(--cf-touch) - var(--cf-choice-selected)) / 2);
    height: var(--cf-choice-selected);
    border-radius: inherit;
    pointer-events: none;
  }

  .oxi-common-categories button[aria-pressed='true']::before {
    background: var(--primary-soft);
  }

  .oxi-common-categories button[aria-pressed='false']:hover::before {
    background: var(--accent);
  }

  .oxi-common-categories button:focus-visible {
    outline: 2px solid var(--primary-accent);
    outline-offset: -2px;
    box-shadow: none;
  }
}
</style>
