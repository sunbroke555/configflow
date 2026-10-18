<template>
  <section class="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-lg border border-border" aria-label="OxiDNS 常用设置">
    <Tabs v-model="tab" class="flex min-h-0 flex-1 flex-col gap-0">
      <TabsList class="h-auto w-full shrink-0 justify-start gap-0.5 overflow-x-auto rounded-none border-b border-border bg-muted/30 p-2">
        <TabsTrigger v-for="item in tabs" :key="item.value" :value="item.value" class="h-11 min-h-11 flex-none px-3 text-xs md:flex-1" :disabled="disabled">{{ item.label }}</TabsTrigger>
      </TabsList>
      <div class="min-h-0 min-w-0 flex-1 overflow-y-auto p-3 md:p-5">
        <TabsContent force-mount value="core" class="mt-0 space-y-5 data-[state=inactive]:hidden">
          <div class="flex flex-wrap items-center gap-1">
            <h3 class="mr-auto text-sm font-semibold">核心设置</h3>
            <Button variant="outline" type="button" class="min-h-11 text-xs" :disabled="disabled || !canApplyPerformance || Boolean(activeError())" @click="applyPerformance">填入已验证性能参数</Button>
            <FieldHelp label="已验证性能参数" :text="canApplyPerformance ? '将可编辑的性能参数填入当前草稿，包括 2 个工作线程、300 秒 lazy cache、2048 条国内探测缓存及上游并发与备用等待设置。仅修改当前结构支持的项目；上游地址、规则选择及自定义参数保留，最后统一保存。' : '当前性能设置使用自定义结构，请通过完整参数编辑。'" />
          </div>
          <OxidnsFields :settings="draft" :fields="coreFields" :disabled="disabled" :capabilities="capabilities" :navigation-disabled="navigationDisabled" @change="change" @error="setError" @navigate="navigate" />
        </TabsContent>
        <TabsContent force-mount value="rules" class="mt-0 space-y-5 data-[state=inactive]:hidden">
          <div class="flex items-center gap-1"><h3 class="text-sm font-semibold">分流规则</h3><FieldHelp label="分流规则" text="选择参与当前配置分流的规则，按规则管理中的顺序执行。修改会同步当前执行链和规则资源。若当前执行链已有无法对应的手工结构，可通过完整参数编辑。" /></div>
          <div class="grid min-w-0 gap-4 sm:grid-cols-2">
            <FormField v-for="group in ruleGroups" :key="group.key" :label="group.label" :help="group.hint">
              <div v-if="isCustom(group.key)" class="flex min-h-11 items-center gap-1 text-xs text-muted-foreground"><span>暂不可编辑</span><FieldHelp :label="`${group.label}配置状态`" :text="capability(group.key)?.reason || customReason" /><Button type="button" variant="ghost" class="ml-auto min-h-11 text-xs" :disabled="disabled || navigationDisabled" @click="navigate(capability(group.key)?.target || group.key)">完整参数<ChevronRight class="size-3.5" /></Button></div>
              <fieldset v-else class="min-w-0 border-0 p-0 [&_[role=combobox]]:min-h-11" :disabled="disabled"><MultiSelect :model-value="selectedRules(group.key)" :options="ruleOptions(group)" :placeholder="`选择${group.label}`" @update:model-value="change(group.key, $event)" /></fieldset>
            </FormField>
          </div>
          <p v-if="!rules.length && !ruleSets.length" class="text-sm text-muted-foreground">当前空间尚无规则，可先在「规则管理」中添加。</p>
          <OxidnsFields :settings="draft" :fields="hostFields" :disabled="disabled" :capabilities="capabilities" :navigation-disabled="navigationDisabled" @change="change" @error="setError" @navigate="navigate" />
        </TabsContent>
        <TabsContent force-mount value="upstreams" class="mt-0 space-y-4 data-[state=inactive]:hidden"><h3 class="text-sm font-semibold">DNS 上游</h3><OxidnsFields :settings="draft" :fields="upstreamFields" :disabled="disabled" :capabilities="capabilities" :navigation-disabled="navigationDisabled" @change="change" @error="setError" @navigate="navigate" /></TabsContent>
        <TabsContent force-mount value="cache" class="mt-0 space-y-4 data-[state=inactive]:hidden"><h3 class="text-sm font-semibold">缓存</h3><OxidnsFields :settings="draft" :fields="cacheFields" :disabled="disabled" :capabilities="capabilities" :navigation-disabled="navigationDisabled" @change="change" @error="setError" @navigate="navigate" /></TabsContent>
        <TabsContent force-mount value="features" class="mt-0 space-y-3 data-[state=inactive]:hidden">
          <Collapsible v-for="feature in featureGroups" :key="feature.key" :default-open="feature.key === initialFeature" class="group rounded-lg border border-border bg-card">
            <div class="relative flex min-h-12 items-center justify-between gap-2 px-3 py-3 text-sm font-medium">
              <CollapsibleTrigger type="button" :aria-label="`${feature.title}设置`" :disabled="disabled" class="absolute inset-0 cursor-pointer rounded-lg outline-none focus-visible:ring-2 focus-visible:ring-primary-accent disabled:opacity-50" />
              <span class="pointer-events-none flex min-w-0 flex-1 items-center gap-1"><span class="min-w-0">{{ feature.title }}</span><span class="pointer-events-auto relative z-10"><FieldHelp :label="feature.title" :text="feature.hint" /></span></span>
              <span class="chip pointer-events-none shrink-0" :class="!isCustom(`${feature.key}.enabled`) && read(`${feature.key}.enabled`) ? 'chip-ok' : ''">{{ isCustom(`${feature.key}.enabled`) ? '自定义配置' : read(`${feature.key}.enabled`) ? '已启用' : '未启用' }}</span><ChevronDown class="pointer-events-none size-4 shrink-0 text-muted-foreground group-data-[state=open]:rotate-180" />
            </div>
            <CollapsibleContent force-mount class="space-y-4 border-t border-border p-3 data-[state=closed]:hidden">
              <div v-if="isCustom(`${feature.key}.enabled`)" class="flex flex-wrap items-center gap-1 text-xs text-muted-foreground"><span>自定义配置</span><FieldHelp :label="`${feature.title}配置状态`" :text="capability(`${feature.key}.enabled`)?.reason || customReason" /><Button type="button" variant="ghost" class="ml-auto min-h-11 text-xs" :disabled="disabled || navigationDisabled" @click="navigate(capability(`${feature.key}.enabled`)?.target || feature.key)">完整参数<ChevronRight class="size-3.5" /></Button></div>
              <div v-else class="flex min-h-11 items-center gap-3"><Switch :id="`oxi-enable-${feature.key}`" :model-value="Boolean(read(`${feature.key}.enabled`))" :disabled="disabled" @update:model-value="change(`${feature.key}.enabled`, $event)" /><Label :for="`oxi-enable-${feature.key}`">启用{{ feature.title }}</Label></div>
              <OxidnsFields v-show="read(`${feature.key}.enabled`) && !isCustom(`${feature.key}.enabled`)" :settings="draft" :fields="feature.fields" :disabled="disabled" :capabilities="capabilities" :navigation-disabled="navigationDisabled" @change="change" @error="setError" @navigate="navigate" />
            </CollapsibleContent>
          </Collapsible>
        </TabsContent>
        <TabsContent force-mount value="access" class="mt-0 space-y-4 data-[state=inactive]:hidden"><h3 class="text-sm font-semibold">WebUI / 日志</h3><OxidnsFields :settings="draft" :fields="apiFields" :disabled="disabled" :capabilities="capabilities" :navigation-disabled="navigationDisabled" @change="change" @error="setError" @navigate="navigate" /></TabsContent>
        <TabsContent force-mount value="advanced" class="mt-0 space-y-4 data-[state=inactive]:hidden">
          <OxidnsFields :settings="draft" :fields="positionFields" :disabled="disabled" :capabilities="capabilities" :navigation-disabled="navigationDisabled" @change="change" @error="setError" @navigate="navigate" />
          <div v-if="isCustom('custom_matches')" class="flex items-center gap-1 text-xs text-muted-foreground"><span>自定义匹配使用手工结构</span><FieldHelp label="自定义匹配配置状态" :text="capability('custom_matches')?.reason || customReason" /><Button type="button" variant="ghost" class="ml-auto min-h-11 text-xs" :disabled="disabled || navigationDisabled" @click="navigate(capability('custom_matches')?.target || 'custom_matches')">完整参数<ChevronRight class="size-3.5" /></Button></div>
          <OxidnsMatchEditor v-else :model-value="customMatches" :disabled="disabled" :allow-disable="false" @update:model-value="change('custom_matches', $event)" @error="setError('custom_matches', $event ? `自定义匹配：${$event}` : '')" />
          <div class="flex flex-wrap items-center gap-2 border-t border-border pt-3"><span class="text-xs text-muted-foreground">其他插件、网络出站和扩展字段</span><Button type="button" variant="outline" class="ml-auto min-h-11 text-xs" :disabled="disabled || navigationDisabled" @click="emit('full')">完整参数<ChevronRight class="size-3.5" /></Button></div>
        </TabsContent>
      </div>
    </Tabs>
    <div class="flex shrink-0 items-center gap-1 border-t border-border px-3 py-1.5 text-xs text-muted-foreground"><span>编辑当前配置，最后统一保存</span><FieldHelp label="常用设置同步" text="这里显示当前配置的实际值。修改后同步到完整参数和 YAML；原生插件、未展示的参数与附加文件都会保留。使用自定义结构的项目请通过完整参数编辑。" /><Button type="button" variant="ghost" class="ml-auto min-h-11 shrink-0 text-xs" :disabled="disabled || navigationDisabled" @click="emit('full')">全部功能<ChevronRight class="size-3.5" /></Button></div>
  </section>
</template>
<script setup lang="ts">
import { reactive, ref, watch, computed, onUnmounted } from 'vue'
import { ChevronDown, ChevronRight } from '@lucide/vue'
import { apiFields, applyVerifiedOxidnsTemplate, cacheFields, coreFields as sourceCoreFields, featureGroups, upstreamFields as sourceUpstreamFields, type OxidnsField } from '@/lib/oxidns'
import OxidnsFields, { type OxidnsFieldCapability } from './OxidnsFields.vue'
import OxidnsMatchEditor from './OxidnsMatchEditor.vue'
import FormField from '@/components/common/FormField.vue'
import FieldHelp from '@/components/common/FieldHelp.vue'
import MultiSelect from '@/components/common/MultiSelect.vue'
import { Button } from '@/components/ui/button'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'

const props = withDefaults(defineProps<{ values: Record<string, any>; capabilities: Record<string, OxidnsFieldCapability>; rules: any[]; ruleSets: any[]; disabled?: boolean; navigationDisabled?: boolean; initialFeature?: string }>(), { disabled: false, navigationDisabled: false })
const emit = defineEmits<{ change: [changes: Record<string, unknown>]; error: [message: string]; navigate: [target: string]; full: [] }>()
const clone = <T,>(value: T): T => value === undefined ? value : JSON.parse(JSON.stringify(value))
const draft = reactive<Record<string, any>>(clone(props.values))
const errors = reactive<Record<string, string>>({})
const tab = ref(featureGroups.some(feature => feature.key === props.initialFeature) ? 'features' : 'core')
const customReason = '当前配置使用自定义结构，请通过完整参数编辑，现有内容会保留。'
const coreFields = sourceCoreFields.map(field => ({ ...field, hint: field.hint?.replace('已验证模板为 2', '已验证配置使用 2 个线程') }))
const upstreamDescriptions: Record<string, string> = {
  local_dns: '用于直连分流和响应 IP 探测。逐条添加当前使用的上游地址，展开可设置连接参数，也可切换 YAML 编辑完整上游对象。',
  remote_dns: '用于代理分流；Fake-IP 由外部上游提供。填写当前使用的上游地址；域名上游可配置 bootstrap 或 dial_addr 避免解析环路。',
  fallback_dns: '代理 DNS 迟迟未返回时的兜底上游，填写实际使用的备用地址。已有 SERVFAIL、NXDOMAIN 等 DNS 响应不保证触发备用。',
}
const upstreamFields = sourceUpstreamFields.map(field => upstreamDescriptions[field.path] ? { ...field, hint: upstreamDescriptions[field.path], emptyHint: '尚未配置上游，点击添加上游填写地址。' } : field)
const tabs = [{ value: 'core', label: '核心设置' }, { value: 'rules', label: '分流规则' }, { value: 'upstreams', label: 'DNS 上游' }, { value: 'cache', label: '缓存' }, { value: 'features', label: '特色功能' }, { value: 'access', label: 'WebUI / 日志' }, { value: 'advanced', label: '高级' }]
const hostFields: OxidnsField[] = [{ path: 'custom_hosts', label: '自定义 Hosts', type: 'lines', wide: true, placeholder: 'full:example.com 192.0.2.10 2001:db8::10', hint: '每条填写域名规则和一个或多个 IP；支持 IPv4、IPv6。可切换 YAML 批量编辑。' }]
const positionFields: OxidnsField[] = [{ path: 'custom_match_position', label: '自定义匹配位置', hint: '决定自定义匹配与自动域名分流规则的先后顺序。放在前面可优先覆盖自动规则；放在后面则在自动域名规则未结束请求时执行，仍早于动态域名集和 IP 分流判断。', options: [{ value: 'head', label: '在自动规则之前' }, { value: 'tail', label: '在自动规则之后' }] }]
const ruleGroups = [
  { key: 'direct_rulesets', opposite: 'proxy_rulesets', label: '直连规则集', sets: true, hint: '选择整组域名或 IP 规则，命中后使用直连 DNS。同一规则集不能同时归入代理分组。' },
  { key: 'proxy_rulesets', opposite: 'direct_rulesets', label: '代理规则集', sets: true, hint: '选择整组域名或 IP 规则，命中后进入代理 DNS 与备用 DNS 流程。' },
  { key: 'direct_rules', opposite: 'proxy_rules', label: '直连规则', sets: false, hint: '选择单独添加的域名或 IP 规则，命中后使用直连 DNS。与规则集一起按原有顺序参与分流。' },
  { key: 'proxy_rules', opposite: 'direct_rules', label: '代理规则', sets: false, hint: '选择单独添加的域名或 IP 规则，命中后进入代理 DNS 与备用 DNS 流程。' },
]
const read = (path: string): unknown => path.split('.').reduce<unknown>((value, key) =>
  value !== null && typeof value === 'object' ? (value as Record<string, unknown>)[key] : undefined, draft)
const selectedRules = (path: string): string[] => {
  const value = read(path)
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []
}
const customMatches = computed(() => {
  const value = read('custom_matches')
  return Array.isArray(value) ? value.filter((item): item is Record<string, unknown> =>
    item !== null && typeof item === 'object' && !Array.isArray(item)) : []
})
function capability(path: string): OxidnsFieldCapability | undefined {
  const parts = path.split('.')
  while (parts.length) { const found = props.capabilities[parts.join('.')]; if (found) return found; parts.pop() }
}
const isCustom = (path: string) => capability(path)?.editable === false
const ruleOptions = (group: typeof ruleGroups[number]) => {
  const opposite = selectedRules(group.opposite)
  const selected = selectedRules(group.key)
  return (group.sets ? props.ruleSets : props.rules)
    .filter(item => !opposite.includes(item.id) || selected.includes(item.id))
    .map(item => ({ value: item.id, label: item.name || `${item.type || item.rule_type || '规则'} · ${item.value || item.rule_value || item.id}` }))
}
function write(path: string, value: unknown) {
  const parts = path.split('.'); const key = parts.pop()!
  const target = parts.reduce((object, part) => (object[part] ??= {}), draft)
  if (value === undefined) delete target[key]
  else target[key] = clone(value)
}
function change(path: string, value: unknown) {
  if (props.disabled || isCustom(path)) return
  write(path, value)
  reportErrors()
  emit('change', { [path]: value === undefined ? null : clone(value) })
}
function activeError() {
  return Object.entries(errors).find(([path, message]) => message && !isCustom(path) && !featureGroups.some(feature => path.startsWith(`${feature.key}.`) && !path.endsWith('.enabled') && !read(`${feature.key}.enabled`)))?.[1] || ''
}
function reportErrors() { emit('error', activeError()) }
function setError(path: string, message: string) { errors[path] = message; reportErrors() }
const navigate = (target: string) => emit('navigate', target)
const performance = applyVerifiedOxidnsTemplate({})
const availablePerformance = computed(() => Object.fromEntries(Object.entries(performance).filter(([path]) => !isCustom(path))))
const canApplyPerformance = computed(() => Object.keys(availablePerformance.value).length > 0)
function applyPerformance() {
  if (props.disabled || !canApplyPerformance.value) return
  for (const [path, value] of Object.entries(availablePerformance.value)) write(path, value)
  emit('change', clone(availablePerformance.value))
}
// Parent replaces this projection once its patch queue is drained. Reconcile the
// current draft even when the server returns the same values as before (for
// example, restoring TLS after a coalesced off/on). Keep incomplete input intact.
watch(() => props.values, value => {
  const sync = (before: any, after: any, path = '') => {
    if (JSON.stringify(before) === JSON.stringify(after)) return
    if (Object.entries(errors).some(([key, message]) => message && (path === key || path.startsWith(`${key}.`)))) return
    if (before && after && typeof before === 'object' && typeof after === 'object' && !Array.isArray(before) && !Array.isArray(after)) {
      for (const key of new Set([...Object.keys(before), ...Object.keys(after)])) sync(before[key], after[key], path ? `${path}.${key}` : key)
    } else if (path) write(path, after)
  }
  sync(draft, value)
}, { deep: true })
onUnmounted(() => emit('error', ''))
</script>
