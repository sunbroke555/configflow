<template>
  <LoadingRows v-if="loading && !state" :rows="4" />
  <SectionCard v-else-if="!state">
    <EmptyState :icon="Monitor" title="暂时无法读取设备策略" :description="error || '正在读取配置空间。'">
      <Button variant="outline" class="min-h-11" :disabled="loading" @click="load">重试</Button>
    </EmptyState>
  </SectionCard>
  <SectionCard
    v-else
    class="[&>header]:items-start [&>header_h2]:whitespace-nowrap max-sm:[&>header>div:first-child]:basis-full"
    title="设备策略"
    :help="devicePolicyHelp"
  >
    <template #title-extra>
      <span class="min-w-0 max-w-48 truncate rounded-md bg-muted px-2 py-1 text-xs font-normal text-muted-foreground" :title="`配置空间：${state.profile_name}`">配置：{{ state.profile_name }}</span>
    </template>
    <template #actions>
      <span v-if="dirty" class="text-xs text-warning-accent">未保存</span>
      <Button variant="outline" class="min-h-11" :disabled="saving || publishing || loading" @click="reload">重新载入</Button>
      <Button class="min-h-11" :disabled="!dirty || !state.editable || saving || publishing || loading" @click="save">
        <Loader2 v-if="saving" class="size-4 animate-spin" />保存草稿
      </Button>
    </template>
    <p v-if="state.agents.length > 1" class="mb-4 mt-0 break-words text-xs leading-relaxed text-muted-foreground">共用草稿：{{ state.agents.map(agent => agent.name).join('、') }}</p>
    <p v-if="!state.editable" role="alert" class="mb-4 rounded-lg bg-warning-soft p-3 text-sm text-warning-accent">{{ state.reason }}</p>
    <p v-if="error" role="alert" class="mb-4 text-sm text-destructive-accent">{{ error }}</p>
    <fieldset :disabled="!state.editable || saving || publishing || loading" class="m-0 min-w-0 border-0 p-0">
      <div class="mb-4 grid min-w-0 gap-3 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] md:items-start md:gap-y-2">
        <div class="min-w-0 md:contents">
          <p class="mb-2 mt-0 flex min-h-6 items-center text-xs text-muted-foreground md:col-start-1 md:row-start-1 md:mb-0 md:self-stretch">从查询记录选择</p>
          <OxiDNSClientSelect class="md:col-start-1 md:row-start-2" :model-value="''" :notes="state.device_notes || {}" :agent-id="agentId" :recorder="recorder" range="all" :allow-all="false" placeholder="选择已有客户端…" @update:model-value="add" />
          <p v-if="!recorder" class="mb-0 mt-1.5 text-xs text-muted-foreground md:col-start-1 md:row-start-3 md:mt-0">暂无可读取的查询记录，可手动添加 IP 或网段。</p>
        </div>
        <div class="min-w-0 max-md:grid max-md:grid-cols-[minmax(0,1fr)_auto] max-md:items-center max-md:gap-x-2 md:contents">
          <div class="mb-2 flex min-h-6 items-center gap-1 max-md:col-span-2 md:col-start-2 md:row-start-1 md:mb-0 md:self-stretch">
            <label for="policy-client-ip" class="text-xs text-muted-foreground">手动添加 IP / 网段</label>
            <FieldHelp label="手动添加 IP / 网段" :text="clientRangeHelp" />
          </div>
          <Input id="policy-client-ip" v-model="newIP" class="min-h-11 font-mono md:col-start-2 md:row-start-2" placeholder="192.168.50.0/24 或 IPv6" @keydown.enter.prevent="add(newIP)" />
          <Button variant="outline" class="min-h-11 md:col-start-3 md:row-start-2" :disabled="!newIP.trim() || rows.length >= 512" @click="add(newIP)"><Plus class="size-4" /><span>添加<span class="max-sm:sr-only">策略</span></span></Button>
        </div>
      </div>
      <div v-if="rows.length" class="cf-table-wrap">
        <table class="cf-table cf-mobile-records policy-source-records w-full min-w-[540px]" role="table">
          <thead role="rowgroup"><tr role="row"><th role="columnheader" scope="col">客户端 IP / 网段</th><th role="columnheader" scope="col">备注</th><th role="columnheader" scope="col">策略</th><th role="columnheader" scope="col"><span class="sr-only">操作</span></th></tr></thead>
          <tbody role="rowgroup">
            <tr role="row" v-for="(row, index) in rows" :key="row.ip">
              <td role="cell" class="cf-record-wide font-mono text-xs" data-label="客户端 IP / 网段">{{ row.ip }}</td>
              <td role="cell" data-label="备注"><Input v-model="row.name" :aria-label="row.ip + ' 备注'" maxlength="80" placeholder="设备或网段名称（可选）" class="min-h-11 min-w-32" /></td>
              <td role="cell" data-label="策略">
                <Select v-model="row.mode"><SelectTrigger :aria-label="row.ip + ' 策略'" class="min-h-11 w-32"><SelectValue /></SelectTrigger>
                  <SelectContent><SelectItem value="smart">智能分流</SelectItem><SelectItem value="direct">直连</SelectItem></SelectContent>
                </Select>
              </td>
              <td role="cell" class="cf-record-wide text-right"><Button variant="ghost" size="icon" class="size-11" :aria-label="'移除 ' + row.ip" @click="rows.splice(index, 1)"><Trash2 class="size-4" /></Button></td>
            </tr>
          </tbody>
        </table>
      </div>
      <EmptyState v-else :icon="Monitor" title="尚未添加设备策略" description="从已有客户端中选择，或输入 IP / CIDR 网段。添加后可选择智能分流或直连。" />
    </fieldset>
    <div class="mt-5 flex flex-wrap items-center justify-between gap-3 border-t pt-4">
      <div class="min-w-0 text-xs text-muted-foreground" role="status" aria-live="polite">
        <AgentOperationSteps v-if="task" :task="task" :busy="busy(agentId)" />
        <p v-else class="m-0">{{ submissionFailure ? '发布失败' : '草稿尚未在此处发布；运行状态以设备实际配置为准。' }}</p>
        <AgentFailureDetails :show-progress-message="false" v-if="submissionFailure || task" :task="submissionFailure || task!" class="mt-1" />
        <p v-if="task?.status === 'ready'" class="mb-0 mt-1">待激活的是已上传的配置快照，不包含之后保存的草稿修改。</p>
      </div>
      <Button v-if="task?.status === 'ready'" class="min-h-11" :disabled="publishing" @click="activate(agentId)">应用并启动</Button>
      <Button v-else class="min-h-11" :disabled="dirty || !state.editable || saving || loading || publishing || busy(agentId)" @click="publish">
        <Loader2 v-if="publishing || busy(agentId)" class="size-4 animate-spin" /><Upload v-else class="size-4" />{{ publishing || busy(agentId) ? '发布中…' : '发布到当前设备' }}
      </Button>
    </div>
  </SectionCard>
</template>

<script setup lang="ts">
import { usePageReloadGuard } from '@/lib/pageReloadGuard'
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { Loader2, Monitor, Plus, Trash2, Upload } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import SectionCard from '@/components/common/SectionCard.vue'
import LoadingRows from '@/components/common/LoadingRows.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import FieldHelp from '@/components/common/FieldHelp.vue'
import OxiDNSClientSelect from './OxiDNSClientSelect.vue'
import AgentFailureDetails from './AgentFailureDetails.vue'
import AgentOperationSteps from './AgentOperationSteps.vue'
import { agentApi, oxidnsDevicePolicyApi } from '@/api'
import { confirm, notify } from '@/lib/feedback'
import { normalizeClientRange } from '@/lib/oxidnsClientRange'
import { createDeploymentId, useAgentDeployments, type AgentDeployment } from '@/composables/useAgentDeployments'
import type { OxiDNSDevicePolicies, OxiDNSDevicePolicy } from '@/types/oxidnsMonitor'

const props = defineProps<{ agentId: string; recorder: string }>()
const devicePolicyHelp = [
  '选择设备或填写 IP / 网段，再选择策略。其他设备继续使用原有规则。',
  '智能分流：按当前 DNS 规则处理。\n直连：使用本地 DNS 查询域名；是否走代理，仍取决于路由器的分流设置。',
  '备注与同一配置空间的 Mihomo 来源设备共用，保存后即可显示；修改备注不会改变设备策略。',
  '先点「保存草稿」，再点「发布到当前设备」，设置才会生效。',
  '设备需使用这台 OxiDNS 作为 DNS，查询记录中应能看到设备自己的 IP。',
].join('\n\n')
const clientRangeHelp = [
  '为指定设备或网段设置 DNS 解析策略，未出现在查询记录中的设备也可手动添加。',
  '支持单个 IPv4 / IPv6 和 CIDR 网段，例如 192.168.50.50、192.168.50.0/24。',
  '网段会自动整理为标准写法。范围重叠时，单个 IP 优先，其次是更小的网段，与添加顺序无关。',
  '添加后选择智能分流（沿用现有规则）或直连（使用本地 DNS 上游），保存草稿并发布到当前设备后生效。',
  '设备需使用 OxiDNS，且 OxiDNS 能识别真实客户端 IP；填写地址不会自动修改设备 DNS。',
].join('\n\n')
const emit = defineEmits<{ published: [] }>()
const state = ref<OxiDNSDevicePolicies | null>(null)
const rows = ref<OxiDNSDevicePolicy[]>([])
const newIP = ref('')
const loading = ref(false)
const saving = ref(false)
const publishing = ref(false)
const error = ref('')
const submissionFailure = ref<Partial<AgentDeployment> | null>(null)
const dirty = computed(() => !!state.value && JSON.stringify(rows.value) !== JSON.stringify(state.value.policies))
usePageReloadGuard(() => publishing.value || !!newIP.value.trim())
const { deployments, busy, track, beginSubmission, endSubmission, activate } = useAgentDeployments(() => emit('published'))
const task = computed(() => deployments[props.agentId])
let epoch = 0
let disposed = false
let controller: AbortController | undefined
const messageOf = (failure: any) => failure?.response?.data?.message || failure?.message || '操作失败，请重试。'

async function canLeave() {
  if (saving.value || publishing.value) {
    notify.info('正在提交，请稍候再切换。')
    return false
  }
  return !dirty.value || await confirm('设备策略尚未保存，离开会丢失本次修改。', { title: '离开设备策略', confirmText: '放弃修改' })
}
defineExpose({ canLeave })

async function load() {
  controller?.abort()
  const current = controller = new AbortController()
  const generation = ++epoch
  loading.value = true
  error.value = ''
  submissionFailure.value = null
  try {
    const { data } = await oxidnsDevicePolicyApi.get(props.agentId, { signal: current.signal })
    if (disposed || generation !== epoch) return
    if (!data?.success) throw new Error(data?.message || '设备策略响应无效。')
    state.value = data
    rows.value = data.policies.map((row: OxiDNSDevicePolicy) => ({ ...row }))
    if (data.deployment) track(props.agentId, data.deployment, false)
  } catch (failure) {
    if (!disposed && generation === epoch) error.value = messageOf(failure)
  } finally {
    if (generation === epoch) loading.value = false
  }
}
async function reload() { if (await canLeave()) await load() }
function add(value: string) {
  if (!state.value?.editable || saving.value || publishing.value) return
  let ip = value.trim()
  if (!ip || rows.value.length >= 512) return
  try {
    ip = normalizeClientRange(ip)
  } catch {
    notify.error('请输入有效的客户端 IPv4、IPv6 或 CIDR 网段，例如 192.168.50.0/24。')
    return
  }
  if (rows.value.some(row => row.ip === ip)) { notify.info('此客户端 IP 或网段已在列表中。'); return }
  rows.value.push({ ip, name: state.value.device_notes?.[ip] || '', mode: 'smart' })
  newIP.value = ''
}
async function save() {
  if (!state.value || !dirty.value || saving.value) return
  const id = props.agentId
  saving.value = true
  error.value = ''
  submissionFailure.value = null
  try {
    const { data } = await oxidnsDevicePolicyApi.save(id, { profile_id: state.value.profile_id, revision: state.value.revision, policies: rows.value })
    if (disposed || id !== props.agentId) return
    state.value = data
    rows.value = data.policies.map((row: OxiDNSDevicePolicy) => ({ ...row }))
    notify.success('设备策略草稿已保存，发布后生效。')
  } catch (failure) { if (!disposed && id === props.agentId) error.value = messageOf(failure) }
  finally { saving.value = false }
}
async function publish() {
  if (!state.value || dirty.value || publishing.value || busy(props.agentId)) return
  const id = props.agentId
  const expected = { expected_profile_id: state.value.profile_id, expected_oxidns_revision: state.value.revision }
  if (!await confirm(`将发布「${state.value.profile_name}」的完整已保存配置到当前设备，并按现有发布流程校验、备份和应用配置。`, { title: '发布配置', confirmText: '发布' })) return
  if (disposed || id !== props.agentId || dirty.value || publishing.value || busy(id)) return
  const deploymentId = createDeploymentId()
  if (!beginSubmission(id, deploymentId)) return
  publishing.value = true
  error.value = ''
  submissionFailure.value = null
  try {
    const { data } = await agentApi.pushConfig(id, deploymentId, expected)
    if (!disposed) endSubmission(id, deploymentId, data)
  } catch (failure: any) {
    if (disposed) return
    if (id === props.agentId && !failure.response?.data?.deployment_id && failure.response?.data?.error_details) {
      submissionFailure.value = { ...failure.response.data, message: messageOf(failure) }
    }
    if (failure.response?.data?.deployment_id) endSubmission(id, deploymentId, failure.response.data)
    else if (!failure.response || failure.response.status >= 500) endSubmission(id, deploymentId, 'unknown')
    else if (!endSubmission(id, deploymentId, 'rejected')) {
      error.value = '发布失败，请查看下方原因。'
      submissionFailure.value = { ...failure.response.data, error: messageOf(failure) }
    }
  } finally { publishing.value = false }
}
function beforeUnload(event: BeforeUnloadEvent) {
  if (dirty.value || saving.value) { event.preventDefault(); event.returnValue = '' }
}
watch(() => props.agentId, () => { state.value = null; rows.value = []; newIP.value = ''; void load() }, { immediate: true })
onMounted(() => window.addEventListener('beforeunload', beforeUnload))
onUnmounted(() => { disposed = true; epoch++; controller?.abort(); window.removeEventListener('beforeunload', beforeUnload) })
</script>
