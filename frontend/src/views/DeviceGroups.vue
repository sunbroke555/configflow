<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, RouterLink } from 'vue-router'
import { Loader2, Plus, RefreshCw, Search, Trash2, Users } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import PageHeader from '@/components/common/PageHeader.vue'
import SectionCard from '@/components/common/SectionCard.vue'
import LoadingRows from '@/components/common/LoadingRows.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import FieldHelp from '@/components/common/FieldHelp.vue'
import DeviceGroupEditor from '@/components/device-groups/DeviceGroupEditor.vue'
import DeviceGroupsPreview from '@/components/device-groups/DeviceGroupsPreview.vue'
import DeviceGroupLookup from '@/components/device-groups/DeviceGroupLookup.vue'
import DeviceGroupsPublication from '@/components/device-groups/DeviceGroupsPublication.vue'
import { deviceGroupsApi } from '@/api/deviceGroups'
import { useProfileStore } from '@/stores/profile'
import { confirm, confirmDanger, notify } from '@/lib/feedback'
import { usePageReloadGuard } from '@/lib/pageReloadGuard'
import { createDeploymentId } from '@/composables/useAgentDeployments'
import { deviceGroupCopy } from '@/lib/deviceGroupCopy'
import { deviceGroupModeLabel, type DeviceGroup, type DeviceGroupActivity, type DeviceGroupDiagnostic, type DeviceGroupPublication, type DeviceGroupsDraft, type DeviceGroupsPreview as Preview, type DeviceGroupsSnapshot } from '@/types/deviceGroups'

const { activeProfileId } = useProfileStore()
const snapshot = ref<DeviceGroupsSnapshot | null>(null)
const draft = ref<DeviceGroupsDraft | null>(null)
const loading = ref(false), error = ref(''), operation = ref('')
const activity = ref<DeviceGroupActivity | null>(null), activityError = ref('')
const diagnostics = ref<DeviceGroupDiagnostic[]>([]), diagnosticError = ref(''), checking = ref(false)
const editing = ref<DeviceGroup | null>(null), editor = ref<InstanceType<typeof DeviceGroupEditor>>()
const showLookup = ref(false), review = ref<Preview | null>(null), reviewError = ref('')
const reviewAction = ref<'save' | 'publish' | 'migrate'>('save')
const publication = ref<DeviceGroupPublication | null>(null), publicationError = ref(''), publicationBusy = ref(false), pendingBatch = ref('')
const publicationNotRecorded = ref(false)
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value))
const draftOf = (state: DeviceGroupsSnapshot): DeviceGroupsDraft => {
  const model = clone(state.device_groups || { version: 1 as const, groups: [] })
  if (!model.groups.some(group => group.id === 'default')) model.groups.push({ id: 'default', name: '默认组', members: [] })
  return { device_groups: model, policy_routing_sources: clone(state.policy_routing_sources || { include: [], exclude_groups: [] }), device_notes: { ...state.device_notes } }
}
const keyOf = (value: DeviceGroupsDraft | null) => JSON.stringify(value)
const dirty = computed(() => !!snapshot.value && keyOf(draft.value) !== keyOf(draftOf(snapshot.value)))
const groups = computed(() => [...(draft.value?.device_groups.groups || [])].sort((a, b) => Number(a.id === 'default') - Number(b.id === 'default')))
const editorSnapshot = computed(() => snapshot.value && draft.value ? { ...snapshot.value, ...draft.value } : null)
const batchRunning = computed(() => !!pendingBatch.value || !!publication.value && (['preparing', 'running'].includes(publication.value.state) || publication.value.state === 'paused' && !publication.value.actions.requires_new_preview))
const busy = computed(() => loading.value || !!operation.value)
let epoch = 0, reviewKey = '', disposed = false
let controller: AbortController | undefined
let pendingRequest: { profile: string; batchId: string; previewToken: string; targets: string[] } | null = null
let poll: ReturnType<typeof setTimeout> | undefined
const messageOf = (failure: any) => failure?.response?.data?.message || failure?.message || '操作失败，请稍后重试。'
const current = (profile: string, version: number) => !disposed && profile === activeProfileId.value && version === epoch
usePageReloadGuard(() => dirty.value || !!operation.value || !!pendingBatch.value)

async function canLeave() {
  if (operation.value) { notify.info('正在处理草稿，请稍候。'); return false }
  if (pendingBatch.value) { notify.warning('发布登记结果待确认，请先确认结果或按原批次重试。'); return false }
  if (editing.value && !await editor.value?.canLeave()) return false
  return !dirty.value || await confirm('设备组修改尚未保存，离开会丢失页面草稿。', { title: '离开设备组', confirmText: '放弃修改', cancelText: '继续编辑' })
}
onBeforeRouteLeave(canLeave)
function beforeUnload(event: BeforeUnloadEvent) { if (dirty.value || operation.value || pendingBatch.value) { event.preventDefault(); event.returnValue = '' } }
window.addEventListener('beforeunload', beforeUnload)

async function load() {
  controller?.abort(); clearTimeout(poll)
  const signal = (controller = new AbortController()).signal
  const version = ++epoch, profile = activeProfileId.value
  loading.value = true; error.value = ''; activityError.value = ''; diagnosticError.value = ''
  review.value = null; editing.value = null; showLookup.value = false
  activity.value = null; diagnostics.value = []; publication.value = null; pendingBatch.value = ''; pendingRequest = null; publicationNotRecorded.value = false; publicationError.value = ''
  try {
    const { data } = await deviceGroupsApi.get(profile, signal)
    if (!current(profile, version)) return
    snapshot.value = data; draft.value = draftOf(data)
    if (data.active_publication) adoptPublication(data.active_publication)
    void loadActivity(profile, version, signal)
  } catch (failure) { if (current(profile, version) && !signal.aborted) error.value = messageOf(failure) }
  finally { if (current(profile, version)) loading.value = false }
}
async function loadActivity(profile = activeProfileId.value, version = epoch, signal = controller?.signal) {
  activityError.value = ''
  try { const { data } = await deviceGroupsApi.activity(profile, signal); if (current(profile, version)) activity.value = data }
  catch (failure) { if (current(profile, version) && !signal?.aborted) activityError.value = messageOf(failure) }
}
async function diagnose() {
  const profile = activeProfileId.value, version = epoch
  checking.value = true; diagnosticError.value = ''
  try { const { data } = await deviceGroupsApi.diagnostics(profile, controller?.signal); if (current(profile, version)) diagnostics.value = data.diagnostics }
  catch (failure) { if (current(profile, version)) diagnosticError.value = messageOf(failure) }
  finally { if (current(profile, version)) checking.value = false }
}
async function reload() { if (await canLeave()) await load() }
function newGroup() {
  editing.value = { id: `dg_${createDeploymentId().replace(/-/g, '')}`, name: '', members: [], mode: 'follow' }
}
function applyGroup(group: DeviceGroup, notes: Record<string, string>) {
  if (!draft.value) return
  const index = draft.value.device_groups.groups.findIndex(item => item.id === group.id)
  if (index < 0) draft.value.device_groups.groups.splice(Math.max(0, draft.value.device_groups.groups.findIndex(item => item.id === 'default')), 0, group)
  else draft.value.device_groups.groups.splice(index, 1, group)
  draft.value.device_notes = notes; editing.value = null; review.value = null
}
async function removeGroup(group: DeviceGroup) {
  if (!draft.value || group.id === 'default') return
  const excluded = draft.value.policy_routing_sources.exclude_groups.includes(group.id)
  if (!await confirmDanger(`组内成员会回到默认组，按全局规则处理。${excluded ? '该组还在策略路由的排除列表中，移除后成员可能重新进入策略路由。' : ''}删除先应用到页面草稿，预览并保存后才写入。`, { title: `删除设备组「${group.name}」？` })) return
  draft.value.device_groups.groups = draft.value.device_groups.groups.filter(item => item.id !== group.id)
  draft.value.policy_routing_sources.exclude_groups = draft.value.policy_routing_sources.exclude_groups.filter(id => id !== group.id)
  review.value = null
}
async function preview(action: 'save' | 'publish' | 'migrate') {
  if (!snapshot.value || !draft.value || busy.value) return
  const profile = activeProfileId.value, version = epoch
  operation.value = 'preview'; error.value = ''; reviewError.value = ''; reviewAction.value = action
  const sent = clone(draft.value)
  try {
    const { data } = action === 'migrate'
      ? await deviceGroupsApi.migrationPreview(profile, snapshot.value.revision, controller?.signal)
      : await deviceGroupsApi.preview(profile, sent, snapshot.value.revision, controller?.signal)
    if (!current(profile, version)) return
    reviewKey = keyOf(sent); review.value = data
  } catch (failure) { if (current(profile, version)) error.value = messageOf(failure) }
  finally { if (current(profile, version)) operation.value = '' }
}
async function confirmReview() {
  if (!review.value || !snapshot.value || !draft.value || busy.value) return
  const selectedReview = clone(review.value), action = reviewAction.value
  if (action !== 'migrate' && reviewKey !== keyOf(draft.value)) { review.value = null; error.value = '页面草稿已变化，请重新预览。'; return }
  const profile = activeProfileId.value, version = epoch
  operation.value = 'saving'; reviewError.value = ''; let saved = false
  try {
    if (action === 'migrate' || dirty.value) {
      const { data } = action === 'migrate'
        ? await deviceGroupsApi.migrate(profile, snapshot.value.revision, selectedReview.preview_token)
        : await deviceGroupsApi.save(profile, clone(draft.value), snapshot.value.revision, selectedReview.preview_token)
      if (!current(profile, version)) return
      snapshot.value = data; draft.value = draftOf(data); saved = true
      notify.success(action === 'migrate' ? '已升级为设备组，草稿已保存' : '设备组草稿已保存', '已在线的设备最长需要等到 DNS TTL 过期，或重新连接网络；保存后仍需发布。')
    }
    review.value = null
    if (action === 'publish') {
      const fresh = await deviceGroupsApi.preview(profile, clone(draft.value), snapshot.value.revision, controller?.signal)
      if (!current(profile, version)) return
      const originalTargets = selectedReview.affected_agents.map(agent => [agent.id, agent.target_fingerprint])
      if (fresh.data.config_revision !== snapshot.value.config_revision || JSON.stringify(fresh.data.affected_agents.map(agent => [agent.id, agent.target_fingerprint])) !== JSON.stringify(originalTargets)) throw new Error('配置或发布目标已变化，草稿已保留，请重新预览后发布。')
      if (fresh.data.blockers.length) throw new Error(fresh.data.blockers.join('；'))
      operation.value = 'publishing'
      await startPublication(profile, fresh.data)
    } else if (action === 'migrate' && snapshot.value.capabilities.keep_blocking_editable) {
      notify.info('让直连设备仍执行拦截规则？', '打开后还需预览、保存并发布。', { label: '打开「仍执行 DNS 拦截」', onClick: () => {
        if (!current(profile, version)) return
        const group = draft.value?.device_groups.groups.find(item => item.mode === 'direct')
        if (group) group.keep_blocking = true
      } })
    }
    void loadActivity(profile, version)
  } catch (failure) {
    if (current(profile, version)) { if (saved || !review.value) error.value = messageOf(failure); else reviewError.value = messageOf(failure) }
  } finally { if (current(profile, version)) operation.value = '' }
}
function adoptPublication(value: DeviceGroupPublication) {
  publication.value = value; pendingBatch.value = ''; pendingRequest = null; publicationNotRecorded.value = false; publicationError.value = ''
  clearTimeout(poll)
  if (['preparing', 'running', 'paused'].includes(value.state)) poll = setTimeout(() => { void refreshPublication() }, value.state === 'paused' ? 5000 : 2000)
}
async function startPublication(profile: string, result: Preview) {
  if (!result.affected_agents.length) throw new Error('此配置空间没有受影响的 Agent，草稿已保留。')
  const batchId = createDeploymentId(), version = epoch
  pendingBatch.value = batchId
  pendingRequest = { profile, batchId, previewToken: result.preview_token, targets: result.affected_agents.map(agent => agent.id) }
  try { const { data } = await deviceGroupsApi.publish(profile, batchId, result.preview_token, result.affected_agents.map(agent => agent.id)); if (current(profile, version)) adoptPublication(data.publication) }
  catch (failure: any) {
    if (!current(profile, version)) return
    if (failure.response && [400, 401, 403, 404, 409, 422].includes(failure.response.status)) { pendingBatch.value = ''; pendingRequest = null; throw failure }
    // A lost create response is recovered by the identity minted before POST.
    await refreshPublication()
    if (pendingBatch.value) publicationError.value = `发布登记结果待确认：${messageOf(failure)}请按原批次重新确认。`
  }
}
async function refreshPublication() {
  const id = pendingBatch.value || publication.value?.batch_id
  if (!id || disposed) return
  const profile = activeProfileId.value, version = epoch
  try { const { data } = await deviceGroupsApi.publication(profile, id, controller?.signal); if (current(profile, version)) adoptPublication(data.publication) }
  catch (failure: any) {
    if (!current(profile, version) || controller?.signal.aborted) return
    publicationNotRecorded.value = failure.response?.status === 404
    publicationError.value = messageOf(failure)
    clearTimeout(poll); poll = setTimeout(() => { void refreshPublication() }, 5000)
  }
}
async function retryRegistration() {
  const request = pendingRequest
  if (!request || busy.value) return
  const version = epoch
  operation.value = 'publishing'; publicationError.value = ''
  try {
    const { data } = await deviceGroupsApi.publish(request.profile, request.batchId, request.previewToken, request.targets)
    if (current(request.profile, version)) adoptPublication(data.publication)
  } catch (failure: any) {
    if (!current(request.profile, version)) return
    if (failure.response && [400, 401, 403, 409, 422].includes(failure.response.status)) { pendingBatch.value = ''; pendingRequest = null; error.value = messageOf(failure) }
    else publicationError.value = messageOf(failure)
  } finally { if (current(request.profile, version)) operation.value = '' }
}
async function publicationAction(action: 'reconcile' | 'retry' | 'activate', agentId?: string) {
  if (!publication.value || publicationBusy.value) return
  const profile = activeProfileId.value, version = epoch, id = publication.value.batch_id
  publicationBusy.value = true; publicationError.value = ''
  try { const { data } = await deviceGroupsApi.retryPublication(profile, id, action, agentId); if (current(profile, version)) adoptPublication(data.publication) }
  catch (failure) { if (current(profile, version)) publicationError.value = messageOf(failure) }
  finally { if (current(profile, version)) publicationBusy.value = false }
}
const ruleCount = (group: DeviceGroup) => group.id !== 'default' && group.mode !== 'follow' && group.keep_blocking
  ? dirty.value ? '预览后统计' : `${snapshot.value?.effective_groups.find(item => item.id === group.id)?.blocking_count ?? '—'} 条拦截` : '—'
watch(activeProfileId, () => { snapshot.value = null; draft.value = null; operation.value = ''; checking.value = false; publicationBusy.value = false; void load() }, { immediate: true })
onUnmounted(() => { disposed = true; epoch++; controller?.abort(); clearTimeout(poll); window.removeEventListener('beforeunload', beforeUnload) })
</script>

<template>
  <div class="min-w-0">
    <PageHeader title="设备组"><template #actions><Button variant="outline" class="min-h-11" :disabled="!snapshot || busy" @click="showLookup = true"><Search class="size-4" />查询 IP 归属</Button><Button class="min-h-11" :disabled="!snapshot || busy || snapshot.migration.available" @click="newGroup"><Plus class="size-4" />新建设备组</Button></template></PageHeader>
    <LoadingRows v-if="loading && !snapshot" :rows="5" />
    <SectionCard v-else-if="!snapshot"><EmptyState :icon="Users" title="暂时无法读取设备组" :description="error || '正在读取配置空间。'"><Button variant="outline" class="min-h-11" @click="load">重试</Button></EmptyState></SectionCard>
    <template v-else-if="draft">
      <div class="mb-4 flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground"><span>配置：{{ snapshot.profile_name }}</span><span class="min-w-0 break-words">涉及 Agent：{{ snapshot.affected_agents.map(agent => agent.name).join('、') || '尚未绑定' }}</span><span v-if="dirty" class="text-warning-accent">未保存</span><Button variant="ghost" class="ml-auto min-h-11 px-2 text-xs" :disabled="busy" @click="reload"><RefreshCw class="size-3.5" />重新载入</Button></div>
      <SectionCard v-if="snapshot.migration.available" title="升级为设备组" class="mb-4" persistent-description description="现有设备策略继续按原方式生成。升级前先预览新组、路由来源、两份配置差异与需发布的 Agent。"><p class="m-0 text-sm">{{ snapshot.migration.reason || '升级需要显式确认，不会在读取配置时自动改写。' }}</p><template #actions><Button class="min-h-11" :disabled="busy" @click="preview('migrate')">升级为设备组</Button></template></SectionCard>
      <p v-if="error" role="alert" class="mb-4 break-words rounded-lg bg-destructive-soft p-3 text-sm text-destructive-accent">{{ error }}</p>
      <ul v-if="snapshot.warnings.length" class="mb-4 mt-0 list-none space-y-2 rounded-lg bg-warning-soft p-3 text-sm"><li v-for="(warning, index) in snapshot.warnings" :key="`${warning.code}:${index}`" class="break-words">{{ warning.message }}</li></ul>
      <p v-for="sample in activity?.infrastructure_samples || []" :key="sample.ip" class="mb-3 break-words rounded-lg bg-warning-soft p-3 text-sm" role="status">样本中 {{ (sample.share * 100).toFixed(1) }}% 的查询来自 {{ sample.ip }}（{{ sample.kind === 'router' ? '路由器' : '基础设施' }}），{{ snapshot.migration.available ? '升级后这些查询按默认组处理。' : '这些查询按默认组处理。' }}</p>
      <SectionCard title="设备组列表" :padded="false" :help="deviceGroupCopy.mode.help">
        <div v-if="groups.length" class="cf-table-wrap"><table class="cf-table cf-mobile-records w-full" role="table"><thead><tr><th>设备组</th><th>成员</th><th>模式</th><th>适用规则</th><th class="text-right">操作</th></tr></thead><tbody><tr v-for="group in groups" :key="group.id" :data-device-group="group.id"><td class="cf-record-wide max-w-64 break-words" data-label="设备组"><Users class="mr-1.5 inline size-4" />{{ group.name }}<span v-if="group.id === 'default'" class="chip ml-2">兜底</span><p v-if="group.note" class="mb-0 mt-1 text-xs text-muted-foreground">{{ group.note }}</p></td><td data-label="成员"><span>{{ group.id === 'default' ? `未归组 · ${group.members.length} 项例外` : `${group.members.length} 项成员` }}</span><details v-if="group.members.length" class="mt-1 text-xs"><summary class="min-h-11 cursor-pointer py-2.5 text-muted-foreground">查看成员</summary><ul class="m-0 max-h-40 list-none space-y-1 overflow-auto p-0 font-mono"><li v-for="ip in group.members" :key="ip" class="break-all">{{ ip }}</li></ul></details></td><td data-label="模式"><span>{{ group.id === 'default' ? '全局规则' : deviceGroupModeLabel(group.mode) }}</span><span v-if="group.keep_blocking" class="mt-1 block text-xs text-muted-foreground">仍执行 DNS 拦截</span></td><td data-label="适用规则" class="text-xs">{{ ruleCount(group) }}</td><td class="text-right"><div class="flex justify-end gap-1"><Button variant="ghost" class="min-h-11 px-2" :disabled="busy || snapshot.migration.available" :aria-label="`编辑设备组 ${group.name}`" @click="editing = clone(group)">编辑</Button><Button v-if="group.id !== 'default'" variant="ghost" size="icon" class="size-11" :disabled="busy" :aria-label="`删除设备组 ${group.name}`" @click="removeGroup(group)"><Trash2 class="size-4" /></Button></div></td></tr></tbody></table></div>
        <EmptyState v-else :icon="Users" title="尚未启用设备组" description="创建设备组后，未归组设备继续按全局规则处理。" />
        <div class="flex flex-wrap items-center justify-between gap-3 border-t border-border p-3 sm:p-5"><p class="m-0 min-w-0 text-xs text-muted-foreground">更具体的 IP / 网段优先；展示顺序不改变归属。</p><div class="flex flex-wrap gap-2"><Button variant="outline" class="min-h-11" :disabled="busy || !dirty || snapshot.migration.available" @click="preview('save')"><Loader2 v-if="operation === 'preview'" class="size-4 animate-spin" />预览并保存草稿</Button><Button class="min-h-11" :disabled="busy || batchRunning || snapshot.migration.available || !snapshot.affected_agents.length" @click="preview('publish')">{{ dirty ? '保存并发布' : '发布全部受影响 Agent' }}</Button></div></div>
        <p v-if="!snapshot.affected_agents.length" class="m-0 px-3 pb-3 text-xs text-muted-foreground sm:px-5 sm:pb-5">当前没有受影响的 Agent，可先保存草稿；绑定此配置空间的 Mihomo 或 OxiDNS 后再发布。</p>
      </SectionCard>
      <SectionCard title="客户端 IP 与查询样本" class="mt-4" description="查询样本只反映引擎观察到的 IP，不保证所有 DNS 与连接都经过设备组。" persistent-description>
        <template #actions><Button variant="outline" class="min-h-11" :disabled="checking || busy" @click="diagnose"><Loader2 v-if="checking" class="size-4 animate-spin" />{{ checking ? '正在检查…' : '检查客户端 IP' }}</Button></template>
        <p class="m-0 text-xs text-muted-foreground">{{ activity ? `成功样本内 ${activity.sample_size.toLocaleString()} 次查询${activity.partial ? '，部分 Agent 的记录不可用' : ''}。` : activityError ? '查询记录暂不可用，可手动添加成员。' : '正在读取查询样本…' }}{{ activity?.sample_limited ? '每台 Agent 最多取 10,000 条；多台 Agent 可能重复观察同一查询。' : '' }}</p>
        <p v-if="activityError" class="mb-0 mt-2 text-xs text-warning-accent">{{ activityError }}<Button variant="ghost" class="min-h-11 px-2 text-xs" @click="loadActivity()">重试读取</Button></p>
        <p v-if="activity && !activity.agents.some(agent => agent.state === 'available')" class="mb-0 mt-2 text-xs text-muted-foreground">无查询记录，不据此判断设备组是否生效。</p>
        <p v-for="agent in activity?.agents.filter(item => item.state !== 'available') || []" :key="agent.id" class="mb-0 mt-2 break-words text-xs text-muted-foreground">{{ agent.name }}：{{ agent.message || ({ offline: 'Agent 离线', upgrade_required: '请升级 Agent 后读取查询记录', recorder_disabled: '查询记录未开启', unavailable: '暂时无法读取' } as Record<string, string>)[agent.state] }}</p>
        <p v-if="!diagnostics.length && groups.some(group => group.mode === 'direct' || group.mode === 'proxy')" class="mb-0 mt-3 text-xs text-warning-accent">尚未确认连接路径保留了客户端源 IP。全部直连与全部代理的连接行为可能无法按组生效。</p>
        <div v-for="(diagnostic, index) in diagnostics" :key="`${diagnostic.router_id || diagnostic.agent_id}:${index}`" class="mt-3 border-t border-border pt-3"><p class="m-0 break-words text-sm" :class="diagnostic.preserves_source_ip === true ? 'text-muted-foreground' : 'text-warning-accent'">{{ diagnostic.message }}</p><p v-for="warning in diagnostic.warnings" :key="warning.code" class="mb-0 mt-1 break-words text-xs text-warning-accent">{{ warning.message }}</p><p v-if="diagnostic.checked_at" class="mb-0 mt-1 text-xs text-muted-foreground">检查时间：{{ new Date(diagnostic.checked_at).toLocaleString() }}</p></div>
        <p v-if="diagnosticError" role="alert" class="mb-0 mt-3 text-sm text-warning-accent">{{ diagnosticError }}</p>
        <p class="mb-0 mt-3 text-xs text-muted-foreground">IPv6 路径需单独核查，隐私地址可能轮换。<FieldHelp label="设备组限制" text="设备组只看引擎看到的源 IP。DNS guard、路由器或其他 DNS 转发器可能隐藏来源；RouterOS 的源 NAT 也可能让连接层归组不同。选择性网关仅接管 fake-IP 和指定目标网段，直接访问 IP 字面量可能不进入 Mihomo。hosts、AdGuard、动态分类及 fake-IP 池仍按全局工作；绑定自定义规则表的 Mihomo 监听器可能绕过主表。" /></p>
        <RouterLink to="/routers" class="mt-2 inline-flex min-h-11 items-center text-xs text-primary-accent underline underline-offset-4">前往上游路由编辑策略路由来源</RouterLink>
        <DeviceGroupsPublication v-if="publication" :publication="publication" :busy="publicationBusy" :error="publicationError" @action="publicationAction" />
        <div v-else-if="pendingBatch" class="mt-4 border-t border-border pt-4"><p class="m-0 text-sm text-warning-accent">{{ publicationError || '正在确认发布批次是否已登记…' }}</p><div class="mt-2 flex flex-wrap gap-2"><Button variant="outline" class="min-h-11" :disabled="busy" @click="refreshPublication">确认发布结果</Button><Button v-if="publicationNotRecorded" variant="outline" class="min-h-11" :disabled="busy" @click="retryRegistration">重试发布登记</Button></div></div>
      </SectionCard>
      <DeviceGroupEditor v-if="editing && editorSnapshot" ref="editor" :key="editing.id" :group="editing" :snapshot="editorSnapshot" :notes="draft.device_notes" :activity="activity" @close="editing = null" @apply="applyGroup" />
      <DeviceGroupsPreview v-if="review" :preview="review" :migration="reviewAction === 'migrate'" :confirm-label="reviewAction === 'migrate' ? '升级并保存草稿' : reviewAction === 'publish' ? '确认保存并发布' : '确认保存草稿'" :busy="!!operation" :error="reviewError" @close="review = null" @confirm="confirmReview" />
      <DeviceGroupLookup v-if="showLookup" :profile-id="snapshot.profile_id" @close="showLookup = false" />
    </template>
  </div>
</template>
