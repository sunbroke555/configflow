<template>
  <SectionCard :title="`策略路由 · ${name}`" class="mt-4 min-w-0" :padded="false"
    description="指定哪些设备访问哪些 IP 时，交给代理网关。"
    :help="optionHelp('使用 RouterOS，且希望按设备区分哪些目标连接需转发。', '来源设备、规则集和网关在当前空间共用。保存只更新草稿，预览会列出 OxiDNS 和 ROS 的变更；确认发布后才按顺序应用。')">
    <template #actions>
      <span v-if="dirty" class="text-xs text-warning-accent">未保存</span>
      <Button variant="outline" class="min-h-11" :disabled="locked || editing" @click="reload">重新载入</Button>
      <Button class="min-h-11" :disabled="locked || editing || !state" @click="saveAndPreview"><Loader2 v-if="saving" class="size-4 animate-spin" />保存并预览</Button>
      <Button variant="ghost" class="min-h-11" :disabled="loading || saving || publishing" @click="close"><ChevronUp class="size-4" />收起面板</Button>
    </template>
    <LoadingRows v-if="loading && !state" :rows="4" />
    <p v-if="error" role="alert" class="m-0 break-words px-5 pb-4 text-sm text-destructive-accent">{{ error }}</p>
    <Tabs v-if="state" v-model="tab" class="min-w-0 gap-0">
      <div class="px-5 pb-4 max-md:px-4 max-sm:px-3">
        <TabsList class="h-auto w-full flex-wrap justify-start max-sm:grid max-sm:grid-cols-2 sm:w-auto" aria-label="策略路由设置">
          <TabsTrigger v-for="item in tabs" :key="item.value" :value="item.value" class="min-h-11 px-3">{{ item.label }}</TabsTrigger>
        </TabsList>
      </div>
      <TabsContent value="sources" class="m-0 px-5 pb-4 max-md:px-4 max-sm:px-3">
        <div class="mb-4 flex flex-wrap items-center justify-between gap-2">
          <div class="flex min-w-0 items-center gap-2 text-xs text-muted-foreground">
            <span class="min-w-0 break-words">{{ state.profile_name }} · 共用来源设备</span>
            <FieldHelp label="来源设备" :text="optionHelp('在「客户端设备」中设为智能分流的 IPv4 设备或网段参与 ROS 策略路由。', '更具体的直连例外及 Mihomo 自身会被排除。直连设备仍遵循主表静态路由；IPv6 策略仅作用于 OxiDNS，不会建立 ROS IPv6 策略。修改来源后，需在这里重新保存并预览发布。')" />
          </div>
          <Button variant="outline" class="min-h-11" :disabled="locked || editing" @click="openClientDevices"><MonitorSmartphone class="size-4" />在客户端设备中管理</Button>
        </div>
        <p v-if="!state.sources.editable" role="alert" class="rounded-md bg-warning-soft p-3 text-sm text-warning-accent">{{ state.sources.reason }}</p>
        <div v-if="state.sources.policies.length" class="cf-table-wrap">
          <table class="cf-table cf-mobile-records policy-source-records min-w-[480px] w-full" role="table">
            <thead role="rowgroup"><tr role="row"><th role="columnheader">客户端 IP / 网段</th><th role="columnheader">名称</th><th role="columnheader">DNS 策略</th><th role="columnheader">策略路由</th></tr></thead>
            <tbody role="rowgroup"><tr v-for="row in state.sources.policies" :key="row.ip" role="row">
              <td role="cell" class="font-mono text-xs" data-label="客户端 IP / 网段">{{ row.ip }}</td>
              <td role="cell" class="break-words" data-label="名称">{{ row.name || '—' }}</td>
              <td role="cell" data-label="DNS 策略">{{ row.mode === 'smart' ? '智能分流' : '直连' }}</td>
              <td role="cell" data-label="策略路由"><span :class="['chip', routesSource(row) && 'chip-acc']">{{ routesSource(row) ? '参与' : '不参与' }}</span></td>
            </tr></tbody>
          </table>
        </div>
        <EmptyState v-else :icon="Network" title="尚未设置来源设备" description="在「客户端设备」中把设备的 DNS 策略设为智能分流，它的 IPv4 地址就会作为策略路由的来源。">
          <Button variant="outline" class="min-h-11" :disabled="locked || editing" @click="openClientDevices">前往客户端设备</Button>
        </EmptyState>
      </TabsContent>
      <TabsContent value="settings" class="m-0 px-5 pb-5 max-md:px-4 max-sm:px-3">
        <fieldset class="m-0 grid min-w-0 gap-5 border-0 p-0 [&_[role=combobox]]:min-h-11" :disabled="locked">
          <FormField label="代理网关（mihomo）" html-for="policy-gateway" v-bind="routerCopy.gatewayROS">
            <Select :model-value="settings.proxy_gateway_agent_id || '__none__'" :disabled="locked" @update:model-value="value => settings.proxy_gateway_agent_id = value === '__none__' ? '' : String(value)">
              <SelectTrigger id="policy-gateway" class="min-h-11"><SelectValue placeholder="不关联" /></SelectTrigger>
              <SelectContent><SelectItem value="__none__">不关联</SelectItem><SelectItem v-for="agent in state.gateways" :key="agent.id" :value="agent.id">{{ agent.name }}（{{ agent.host }}）</SelectItem></SelectContent>
            </Select>
          </FormField>
          <div class="space-y-2"><div class="flex flex-wrap items-center justify-between gap-2"><Label for="policy-destinations">使用策略路由的 IP 规则集</Label><Button variant="ghost" class="min-h-11" :disabled="locked" @click="editDestinations()"><Pencil class="size-4" />编辑规则内容</Button></div><MultiSelect :model-value="settings.ros_policy_rule_sets" :options="ruleOptions" trigger-id="policy-destinations" placeholder="不使用策略路由" @update:model-value="value => selectRules('ros_policy_rule_sets', value)" /></div>
          <FormField label="使用静态路由的 IP 规则集" html-for="policy-static-destinations" v-bind="routerCopy.staticRules">
            <MultiSelect :model-value="settings.ip_rule_sets" :options="ruleOptions" trigger-id="policy-static-destinations" placeholder="仅 fake-ip 网段" @update:model-value="value => selectRules('ip_rule_sets', value)" />
          </FormField>
          <p class="m-0 text-xs leading-relaxed text-muted-foreground">两组规则集互斥，移入一组会自动移出另一组。取消后需保存并确认发布，才会清理对应的受管规则。</p>
        </fieldset>
      </TabsContent>
      <TabsContent value="preview" class="m-0 px-5 pb-5 max-md:px-4 max-sm:px-3">
        <div class="space-y-5">
          <fieldset class="m-0 min-w-0 border-0 p-0" :disabled="locked">
            <legend class="mb-3 flex items-center gap-2 text-sm font-semibold">
              OxiDNS 发布目标
              <FieldHelp label="OxiDNS 发布目标" :text="optionHelp('希望把当前空间的设备策略同步到指定 OxiDNS 时选择。', '选中的设备接收完整已保存配置，包含其他草稿改动，并非只更新设备策略。未选设备保持现状；全部取消时只发布 ROS。')" />
            </legend>
            <div class="flex flex-wrap gap-2">
              <label v-for="agent in state.source_agents" :key="agent.id" class="flex min-h-11 max-w-full max-sm:w-full cursor-pointer items-center gap-3 rounded-lg border px-3 py-2 text-sm transition-colors" :class="targets.includes(agent.id) ? 'border-primary-accent/30 bg-primary-soft' : 'border-border hover:bg-muted'">
                <input v-model="targets" type="checkbox" :value="agent.id" class="size-4 shrink-0 accent-primary" />
                <span class="min-w-0"><span class="block break-words font-medium">{{ agent.name }}</span><span class="block font-mono text-xs text-muted-foreground">{{ agent.host }}</span></span>
                <span class="ml-1 flex shrink-0 items-center gap-1.5 text-xs" :class="agent.status === 'online' ? 'text-success-accent' : 'text-muted-foreground'"><span class="size-1.5 rounded-full bg-current" />{{ agent.status === 'online' ? '在线' : '离线' }}</span>
              </label>
            </div>
            <p class="mb-0 mt-2 text-xs text-muted-foreground">{{ targets.length ? '发布完整已保存配置，包含其他草稿改动。' : '本次只发布 ROS，OxiDNS 保持现状。' }}</p>
          </fieldset>
          <p v-if="!preview" class="m-0 border-t border-border pt-4 text-sm text-muted-foreground">点击上方“保存并预览”，核对本次变更后确认发布。</p>
          <template v-else>
            <ul v-if="blockers.length" class="m-0 list-none space-y-2 rounded-lg bg-destructive-soft p-3 text-sm text-destructive-accent" role="alert"><li v-for="item in blockers" :key="item">{{ item }}</li></ul>
            <section class="min-w-0 space-y-3" aria-label="路由变更">
              <div class="flex items-center justify-between gap-3">
                <h3 class="m-0 text-sm font-semibold">路由变更 <span class="ml-2 font-normal text-muted-foreground">{{ name }}</span></h3>
                <span class="shrink-0 text-xs text-muted-foreground">{{ routeChangeCount ? `${routeChangeCount} 项变更` : '已同步' }}</span>
              </div>
              <div v-if="preview.router_plan.warnings.length" class="flex items-start gap-2 rounded-lg bg-warning-soft px-3 py-2.5 text-xs text-warning-accent">
                <Info class="mt-0.5 size-4 shrink-0" />
                <ul class="m-0 list-none space-y-1 p-0"><li v-for="item in preview.router_plan.warnings" :key="item" class="break-words">{{ item }}</li></ul>
              </div>
              <ul class="m-0 list-none divide-y divide-border border-y border-border p-0">
                <li v-for="stage in preview.router_plan.stages" :key="stage.key" class="grid grid-cols-[minmax(0,1fr)_auto] items-start gap-x-4 gap-y-2 py-3 text-sm sm:grid-cols-[11rem_5rem_minmax(0,1fr)]">
                  <div class="flex min-w-0 items-center gap-2"><span class="font-medium">{{ stage.title }}</span><FieldHelp v-if="stage.note" :label="stage.title" :text="stage.note" /></div>
                  <span class="whitespace-nowrap text-xs leading-5" :class="stage.enabled !== false && stage.ops.length ? 'font-medium text-primary-accent' : 'text-muted-foreground'">{{ stage.enabled === false ? '已跳过' : stage.ops.length ? `${stage.ops.length} 项变更` : '无变更' }}</span>
                  <ul v-if="stage.ops.length" class="col-span-2 m-0 min-w-0 list-none space-y-1 p-0 text-sm sm:col-span-1"><li v-for="(op, index) in stage.ops" :key="index" class="min-w-0"><RouterOperationPreview :op="op" /></li></ul>
                  <span v-else class="hidden text-xs text-muted-foreground sm:block">—</span>
                </li>
              </ul>
            </section>
            <div class="flex flex-wrap items-center justify-between gap-3">
              <div class="flex min-w-0 items-center gap-2 text-xs text-muted-foreground"><span>{{ preview.targets.length ? '先发布 OxiDNS，再发布 ROS；失败时暂停。' : '仅发布 ROS，写入后回读校验。' }}</span><FieldHelp label="发布顺序" text="逐台确认 OxiDNS 发布成功后再发布 ROS。失败或结果待确认时停止后续步骤；已成功的设备保留结果，失败设备沿用现有校验与回滚流程。" /></div>
              <Button class="min-h-11 max-sm:w-full" :disabled="!canPublish" @click="publish"><Loader2 v-if="publishing" class="size-4 animate-spin" />确认发布</Button>
            </div>
          </template>
          <section v-if="results.length" class="space-y-3 border-t border-border pt-4" role="status" aria-live="polite" aria-label="发布结果">
            <h3 class="m-0 text-sm font-semibold">发布结果</h3>
            <ul class="m-0 list-none divide-y divide-border p-0">
              <li v-for="row in releaseRows" :key="row.id" class="min-w-0" :data-publish-target="row.id">
                <AgentDeploymentStatus v-if="row.deployment" :task="row.deployment" :label="row.name" compact>
                  <template v-if="deploymentNeedsNotice(row.deployment)" #notice>
                    <AgentOperationSteps :task="row.deployment" :show-current="false" :show-steps="false" />
                    <p v-if="row.status === 'unknown' && row.message && row.message !== row.deployment.message" class="m-0 text-warning-accent">{{ row.message }}</p>
                    <AgentFailureDetails :show-progress-message="false" :showDNSChecks="false" :task="row.deployment" />
                  </template>
                  <template #details>
                    <AgentOperationSteps :task="row.deployment" :show-current="false" :show-message="!deploymentNeedsNotice(row.deployment)" embedded />
                  </template>
                </AgentDeploymentStatus>
                <AgentStatusRow v-else :label="row.name" :status="isROSRow(row) && row.status === 'succeeded' && rosNotices.length ? '发布完成，有提醒' : resultStatus(row.status)" :tone="isROSRow(row) && row.status === 'succeeded' && rosNotices.length ? 'warning' : resultTone(row.status)">
                  <template v-if="(row.status !== 'succeeded' && (row.message || row.task)) || (isROSRow(row) && rosNotices.length)" #notice>
                    <p v-if="row.status !== 'succeeded' && row.message && row.message !== row.task?.error && row.message !== row.task?.message" class="m-0" :class="resultTone(row.status) === 'danger' ? 'text-destructive-accent' : resultTone(row.status) === 'warning' ? 'text-warning-accent' : 'text-foreground'">{{ row.message }}</p>
                    <AgentFailureDetails v-if="row.task" :showDNSChecks="false" :task="row.task" />
                    <ul v-if="isROSRow(row) && rosNotices.length" class="m-0 list-none space-y-1 p-0">
                      <li v-for="(entry, index) in rosNotices" :key="index" :class="entry.status === 'failed' ? 'text-destructive-accent' : 'text-warning-accent'">{{ entry.phase }}：{{ entry.summary }}</li>
                    </ul>
                  </template>
                  <template v-if="(row.status === 'succeeded' && row.message) || hasROSLogs(row)" #default>
                    <p v-if="row.status === 'succeeded' && row.message" class="m-0">{{ row.message }}</p>
                    <template v-if="hasROSLogs(row)">
                      <div v-if="rosResult.log?.length" class="min-w-0">
                        <p class="mb-1 mt-0 font-medium">ROS 发布记录</p>
                        <ul class="m-0 list-none space-y-1 p-0"><li v-for="(entry, index) in rosResult.log" :key="index" class="[overflow-wrap:anywhere]">{{ entry.summary }} · {{ entry.status }}</li></ul>
                      </div>
                      <div v-if="rosResult.rollback?.length" class="min-w-0">
                        <p class="mb-1 mt-0 font-medium">ROS 回滚记录</p>
                        <ul class="m-0 list-none space-y-1 p-0"><li v-for="(entry, index) in rosResult.rollback" :key="index" class="[overflow-wrap:anywhere]">{{ entry.summary }} · {{ entry.status }}</li></ul>
                      </div>
                    </template>
                  </template>
                </AgentStatusRow>
                <AgentDNSChecks :key="row.task?.deployment_id" :checks="row.task?.dns_checks" :checked-at="row.task?.dns_checked_at" compact />
              </li>
            </ul>
            <Button v-if="unresolved" variant="outline" class="min-h-11" :disabled="publishing" @click="refreshResults">核对发布结果</Button>
          </section>
        </div>
      </TabsContent>
      <TabsContent value="objects" class="m-0"><PolicyRoutingObjects :id="id" :profile="profile" :disabled="locked || editing" :sources-editable="state.sources.editable" @add-source="openClientDevices" @edit-destinations="editDestinations()" @edit="editObject" /></TabsContent>
    </Tabs>
    <PolicyDestinationEditor v-if="destinationEditorOpen && state" ref="destinationEditor" :rules="state.rule_sets" :selected-ids="settings.ros_policy_rule_sets" :address="destinationAddress" @close="destinationEditorOpen = false" @saved="contentSaved" />
  </SectionCard>
</template>
<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ChevronUp, Info, Loader2, MonitorSmartphone, Network, Pencil } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import SectionCard from '@/components/common/SectionCard.vue'
import LoadingRows from '@/components/common/LoadingRows.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import FieldHelp from '@/components/common/FieldHelp.vue'
import { optionHelp } from '@/lib/optionCopy'
import { routerCopy } from '@/lib/routerCopy'
import FormField from '@/components/common/FormField.vue'
import MultiSelect from '@/components/common/MultiSelect.vue'
import AgentFailureDetails from '@/components/agents/AgentFailureDetails.vue'
import AgentOperationSteps from '@/components/agents/AgentOperationSteps.vue'
import AgentDeploymentStatus from '@/components/agents/AgentDeploymentStatus.vue'
import AgentStatusRow from '@/components/agents/AgentStatusRow.vue'
import AgentDNSChecks from '@/components/agents/AgentDNSChecks.vue'
import PolicyRoutingObjects from './PolicyRoutingObjects.vue'
import PolicyDestinationEditor from './PolicyDestinationEditor.vue'
import RouterOperationPreview from './RouterOperationPreview.vue'
import { usePolicyRoutingWorkspace, type PolicyReleaseRow } from '@/composables/usePolicyRoutingWorkspace'
import { deploymentLabels, deploymentNeedsNotice } from '@/composables/useAgentDeployments'
import { getActiveProfileId } from '@/profileContext'
import type { PolicyRoutingObject } from '@/api'
import type { OxiDNSDevicePolicy } from '@/types/oxidnsMonitor'
const props = defineProps<{ id: string; name: string; profile: string }>()
const emit = defineEmits<{ close: []; saved: []; busy: [value: boolean] }>()
const { state, settings, targets, tab, loading, saving, publishing, locked, dirty, error, preview,
  results, rosResult, unresolved, blockers, canPublish, reload, canLeave: canLeaveWorkspace, selectRules, saveAndPreview, publish, refreshResults } = usePolicyRoutingWorkspace(props, () => emit('saved'))
const router = useRouter()
const destinationEditorOpen = ref(false), destinationAddress = ref('')
const destinationEditor = ref<InstanceType<typeof PolicyDestinationEditor>>()
const editing = computed(() => destinationEditorOpen.value)
const tabs = [{ value: 'sources', label: '来源设备' }, { value: 'settings', label: '规则集与网关' }, { value: 'preview', label: '发布预览' }, { value: 'objects', label: '已下发对象' }]
const ruleOptions = computed(() => state.value?.rule_sets.map(rule => ({ value: rule.id, label: `${rule.name}（${rule.policy}）` })) || [])
const routeChangeCount = computed(() => preview.value?.router_plan.stages.reduce((count, stage) => count + (stage.enabled === false ? 0 : stage.ops.length), 0) || 0)
watch([locked, editing], ([busy, editing]) => emit('busy', busy || editing), { immediate: true })
watch(() => [props.id, props.profile], () => { destinationEditorOpen.value = false })
// Only smart IPv4 rows feed ROS; the server still subtracts direct exceptions and the gateway.
const routesSource = (row: OxiDNSDevicePolicy) => row.mode === 'smart' && !row.ip.includes(':')
/** 来源在「客户端设备」编辑；空间不是当前全局空间时带上它，不切换全局空间 */
function openClientDevices() {
  void router.push({ path: '/client-devices', query: props.profile === getActiveProfileId() ? {} : { profile: props.profile } })
}
function editDestinations(address = '') {
  if (locked.value) return
  destinationAddress.value = address; destinationEditorOpen.value = true
}
function contentSaved() {
  preview.value = null; destinationEditorOpen.value = false; emit('saved')
}
async function editObject(item: PolicyRoutingObject) {
  if (locked.value) return
  if (item.editor === 'destination') { editDestinations(item.configured_address); return }
  if (item.editor === 'source') { openClientDevices(); return }
  tab.value = 'settings'; await nextTick(); document.getElementById('policy-gateway')?.focus()
}
async function canLeave() {
  if (destinationEditor.value && !await destinationEditor.value.canLeave()) return false
  return canLeaveWorkspace()
}
async function close() { if (await canLeave()) emit('close') }
const releaseRows = computed(() => results.value.map(row => {
  const task = row.task
  // Network failures can leave a last-known task while the workspace waits for
  // confirmation. Preserve that task and its steps without claiming its result.
  const deployment = task?.deployment_id && task.status ? {
    ...task, deployment_id: task.deployment_id, status: task.status,
    query_error: task.query_error || (row.status === 'unknown' && task.status !== 'unknown'),
  } : undefined
  return { ...row, deployment }
}))
const isROSRow = (row: PolicyReleaseRow) => !row.deployment_id && row.id === props.id
const hasROSLogs = (row: PolicyReleaseRow) => isROSRow(row) && !!(rosResult.value?.log?.length || rosResult.value?.rollback?.length)
const rosNotices = computed(() => [
  { phase: '发布', entries: rosResult.value?.log || [] },
  { phase: '回滚', entries: rosResult.value?.rollback || [] },
].flatMap(group => group.entries
  .filter((entry: { status: string }) => entry.status === 'failed' || entry.status === 'warning')
  .map((entry: { summary: string; status: string }) => ({ ...entry, phase: group.phase }))))
const resultStatus = (status: string) => ({ succeeded: '成功', failed: '失败', unknown: '待确认', waiting: '等待中', skipped: '已跳过' } as Record<string, string>)[status] || deploymentLabels[status] || status
const resultTone = (status: string) => status === 'succeeded' ? 'success'
  : ['failed', 'rolled_back', 'rollback_failed'].includes(status) ? 'danger'
  : ['unknown', 'ready', 'recovery_pending', 'rolling_back'].includes(status) ? 'warning'
  : ['waiting', 'skipped'].includes(status) ? 'neutral' : 'progress'
onBeforeUnmount(() => { emit('busy', false) })
defineExpose({ canLeave })
</script>
