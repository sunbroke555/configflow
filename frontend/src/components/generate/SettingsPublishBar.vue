<script setup lang="ts">
import { computed, onUnmounted, reactive, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { LoaderCircle, RefreshCw, Save, Upload } from '@lucide/vue'
import { agentApi, profileApi, type PublishGuard } from '@/api'
import { Button } from '@/components/ui/button'
import AgentDeploymentStatus from '@/components/agents/AgentDeploymentStatus.vue'
import AgentFailureDetails from '@/components/agents/AgentFailureDetails.vue'
import AgentOperationSteps from '@/components/agents/AgentOperationSteps.vue'
import AgentDNSChecks from '@/components/agents/AgentDNSChecks.vue'
import AgentStatusRow from '@/components/agents/AgentStatusRow.vue'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { createDeploymentId, deploymentNeedsNotice, deploymentStatusLabel, useAgentDeployments, type AgentDeployment } from '@/composables/useAgentDeployments'
import { stripTrailingPunctuation } from '@/lib/text'

type ServiceType = 'mihomo' | 'mosdns' | 'oxidns' | 'surge'
type Agent = { id: string; name: string; profile_id?: string; service_type: string; status?: string; enabled?: boolean; latest_deployment?: AgentDeployment; dns_follow?: { source_agent_id: string } | null; oxidns_ha?: import('@/types/agentDNSHA').AgentDNSHAConfig | null }
type Profile = { id: string; name: string; is_default?: boolean }
type Attempt = { session: number; agentId: string; agentName: string; profileId: string; serviceType: ServiceType; deploymentId?: string; saved: boolean; inherited?: boolean }

const props = defineProps<{
  open: boolean
  profileId: string
  serviceType: ServiceType
  preferredAgentId?: string
  disabled?: boolean
  save: () => Promise<PublishGuard | null>
}>()
const emit = defineEmits<{ 'save-draft': [] }>()
const agents = ref<Agent[]>([])
const profiles = ref<Profile[]>([])
const selectedId = ref('')
const targetUnavailable = ref(false)
const loading = ref(false)
const loadError = ref('')
const error = ref('')
const submissionFailure = ref<Partial<AgentDeployment> | null>(null)
const phase = ref<'idle' | 'saving' | 'checking' | 'submitting'>('idle')
const attempt = ref<Attempt | null>(null)
const synchronousResult = ref<'idle' | 'done' | 'unknown'>('idle')
const pendingPosts = reactive(new Set<string>())
const submittedScopes = new Map<string, { profileId: string; serviceType: ServiceType }>()
let session = 0
let targetsInitialized = false
let disposed = false
const terminal = new Set(['succeeded', 'failed', 'rolled_back', 'rollback_failed'])
const { deployments, busy, track, beginSubmission, endSubmission, activate } = useAgentDeployments(() => {}, (agentId, task) => {
  const current = attempt.value
  return !!current && stillCurrent(current) && current.agentId === agentId && current.deploymentId === task.deployment_id
})
const serviceLabel = computed(() => ({ mihomo: 'Mihomo', mosdns: 'MosDNS', oxidns: 'OxiDNS', surge: 'Surge' })[props.serviceType])
const profileName = computed(() => profiles.value.find(profile => profile.id === props.profileId)?.name || props.profileId)
const defaultProfile = (items: Profile[]) => items.find(profile => profile.is_default === true)?.id
  || items.find(profile => profile.is_default === undefined && profile.id === 'default')?.id
const matches = (agent: Agent, items: Profile[], profileId = props.profileId, serviceType = props.serviceType) =>
  agent.service_type === serviceType && (agent.profile_id || defaultProfile(items)) === profileId
const online = (agent?: Agent) => !!agent && agent.status === 'online' && agent.enabled !== false
const candidates = computed(() => agents.value.filter(agent => matches(agent, profiles.value)))
const selected = computed(() => candidates.value.find(agent => agent.id === selectedId.value))
const haPeer = computed(() => selected.value?.oxidns_ha?.enabled === true
  ? agents.value.find(agent => agent.id === selected.value?.oxidns_ha?.peer_id) : undefined)
const visibleAttempt = computed(() => attempt.value?.session === session && props.open ? attempt.value : null)
const task = computed(() => {
  const current = visibleAttempt.value
  const found = current && deployments[current.agentId]
  return found && found.deployment_id === current?.deploymentId ? found : undefined
})
const unresolved = computed(() => !!task.value && !terminal.has(task.value.status))
const hasDNSFollowers = computed(() => visibleAttempt.value?.serviceType === 'mihomo'
  && agents.value.some(agent => agent.service_type === 'oxidns' && agent.dns_follow?.source_agent_id === visibleAttempt.value?.agentId))
const blockedRecovery = computed(() => task.value?.status === 'rollback_failed')
const working = computed(() => phase.value !== 'idle' || unresolved.value || synchronousResult.value === 'unknown')
const canPublish = computed(() => props.open && !props.disabled && !loading.value && !loadError.value
  && online(selected.value) && !working.value && !blockedRecovery.value && !pendingPosts.has(selectedId.value)
  && !busy(selectedId.value) && deployments[selectedId.value]?.status !== 'ready'
  && deployments[selectedId.value]?.status !== 'rollback_failed')
const progressLabel = computed(() => {
  if (phase.value === 'saving') return '正在保存草稿…'
  if (phase.value === 'checking') return visibleAttempt.value?.inherited ? '正在确认目标设备…' : '草稿已保存，正在确认目标设备…'
  if (error.value) return error.value
  if (synchronousResult.value === 'done') return '草稿已保存，配置已推送。'
  if (synchronousResult.value === 'unknown') return '草稿已保存，发布结果待确认。请到 Agent 页面核实设备状态后再操作。'
  if (phase.value === 'submitting' && !task.value) return '草稿已保存，正在推送配置…'
  if (!task.value) return ''
  const prefix = visibleAttempt.value?.inherited ? '设备上次发布：' : '草稿已保存，'
  if (task.value.query_error || task.value.pending || task.value.not_recorded) return `${prefix}发布结果待确认。`
  if (task.value.status === 'succeeded') return `${prefix}${visibleAttempt.value?.inherited ? '成功' : '发布成功，服务检查已通过。'}`
  return `${prefix}${deploymentStatusLabel(task.value)}`
})
const progressTask = computed<AgentDeployment | undefined>(() => {
  if (phase.value === 'saving' || phase.value === 'checking' || error.value || synchronousResult.value !== 'idle') return undefined
  return task.value
})
const progressNeedsNotice = computed(() => !!progressTask.value && (deploymentNeedsNotice(progressTask.value) || !!submissionFailure.value))
const stageStatus = computed(() => phase.value === 'saving' ? '保存草稿' : phase.value === 'checking' ? '确认设备'
  : error.value ? '失败' : synchronousResult.value === 'done' ? '已推送'
    : synchronousResult.value === 'unknown' ? '待确认' : '推送中')
const stageTone = computed(() => error.value ? 'danger' : synchronousResult.value === 'done' ? 'success'
  : synchronousResult.value === 'unknown' ? 'warning' : 'progress')
const scopeMessage = computed(() => visibleAttempt.value && !visibleAttempt.value.inherited && (task.value || synchronousResult.value !== 'idle')
  ? `本次发布到「${visibleAttempt.value.agentName}」，仅包含本次保存的配置；后续修改需重新保存并发布。` : '')
const messageOf = (failure: any) => failure?.response?.data?.message || failure?.message || '请稍后重试。'
const stillCurrent = (current: Attempt) => !disposed && props.open && session === current.session
  && props.profileId === current.profileId && props.serviceType === current.serviceType

async function readTargets() {
  // Always re-read bindings before publishing; content-sync metadata is unused.
  const [agentResponse, profileResponse] = await Promise.all([agentApi.summaries(), profileApi.list()])
  if (!Array.isArray(agentResponse.data) || !Array.isArray(profileResponse.data)) throw new Error('设备列表响应无效。')
  return { agents: agentResponse.data as Agent[], profiles: profileResponse.data as Profile[] }
}

function updateTargets(data: { agents: Agent[]; profiles: Profile[] }) {
  agents.value = data.agents
  profiles.value = data.profiles
  if (!targetsInitialized) {
    selectedId.value = props.preferredAgentId
      ? candidates.value.find(agent => agent.id === props.preferredAgentId)?.id || ''
      : candidates.value.length === 1 ? candidates.value[0].id : ''
    targetUnavailable.value = !!props.preferredAgentId && !selectedId.value
    targetsInitialized = true
  } else if (selectedId.value && !candidates.value.some(agent => agent.id === selectedId.value)) {
    // Losing a target must require another explicit choice, even when only one device remains.
    selectedId.value = ''
    targetUnavailable.value = true
    attempt.value = null
    synchronousResult.value = 'idle'
  }
}

function restorePending() {
  const agent = selected.value
  if (!agent || attempt.value) return
  const recorded = deployments[agent.id] || agent.latest_deployment
  const scope = recorded && submittedScopes.get(recorded.deployment_id)
  if (scope && (scope.profileId !== props.profileId || scope.serviceType !== props.serviceType)) return
  if (!recorded || (recorded.profile_id && recorded.profile_id !== props.profileId)
    || (recorded.service_type && recorded.service_type !== props.serviceType)
    || (terminal.has(recorded.status) && recorded.status !== 'rollback_failed')) return
  attempt.value = { session, agentId: agent.id, agentName: agent.name, profileId: props.profileId,
    serviceType: props.serviceType, deploymentId: recorded.deployment_id, saved: false, inherited: true }
  if (!deployments[agent.id]) track(agent.id, recorded, false)
}

async function loadTargets() {
  const generation = session
  loading.value = true
  loadError.value = ''
  try {
    const data = await readTargets()
    if (disposed || !props.open || generation !== session) return
    updateTargets(data)
    restorePending()
  } catch (failure) {
    if (!disposed && generation === session) loadError.value = `暂时无法读取目标设备：${stripTrailingPunctuation(messageOf(failure))}。仍可保存草稿。`
  } finally { if (!disposed && generation === session) loading.value = false }
}

async function verifyTarget(current: Attempt) {
  const data = await readTargets()
  if (!stillCurrent(current)) return false
  updateTargets(data)
  const agent = data.agents.find(item => item.id === current.agentId)
  if (!data.profiles.some(profile => profile.id === current.profileId) || !agent
    || !matches(agent, data.profiles, current.profileId, current.serviceType) || !online(agent)) {
    error.value = `${current.inherited ? '未激活' : '草稿已保存，未发布'}：目标设备的绑定、类型或在线状态已改变，请重新选择目标。`
    return false
  }
  const snapshotChanged = current.deploymentId && [deployments[agent.id], agent.latest_deployment].some(record =>
    record && record.deployment_id === current.deploymentId
    && ((record.profile_id && record.profile_id !== current.profileId)
      || (record.service_type && record.service_type !== current.serviceType)))
  if (snapshotChanged) {
    error.value = '未激活：待应用配置的配置空间或软件类型与当前设备不一致，请到 Agent 页面检查该发布。'
    return false
  }
  const existing = deployments[agent.id] || agent.latest_deployment
  if (existing && existing.deployment_id !== current.deploymentId
    && (!terminal.has(existing.status) || existing.status === 'rollback_failed')) {
    error.value = '草稿已保存，未发布：设备有尚未结束的发布，请先到 Agent 页面处理。'
    return false
  }
  return true
}

function selectTarget(value: unknown) {
  if (working.value) return
  selectedId.value = String(value || '')
  targetUnavailable.value = false
  attempt.value = null
  synchronousResult.value = 'idle'
  error.value = ''
  submissionFailure.value = null
  restorePending()
}

async function saveAndPublish() {
  if (!canPublish.value || !selected.value) return
  const current: Attempt = { session, agentId: selected.value.id, agentName: selected.value.name,
    profileId: props.profileId, serviceType: props.serviceType, saved: false }
  attempt.value = current
  error.value = ''
  submissionFailure.value = null
  synchronousResult.value = 'idle'
  phase.value = 'saving'
  try {
    const savedGuard = await props.save()
    if (savedGuard === null) return
    current.saved = true
    if (!stillCurrent(current)) return
    if (!savedGuard || savedGuard.expected_profile_id !== current.profileId
      || savedGuard.expected_service_type !== current.serviceType
      || !Number.isInteger(savedGuard.expected_config_revision) || savedGuard.expected_config_revision < 0) {
      error.value = '草稿已保存，未发布：无法确认本次保存的配置版本，请重新保存后重试。'
      return
    }
    const publishGuard = { ...savedGuard }
    phase.value = 'checking'
    if (!await verifyTarget(current) || !stillCurrent(current)) return
    phase.value = 'submitting'
    current.deploymentId = createDeploymentId()
    submittedScopes.set(current.deploymentId, { profileId: current.profileId, serviceType: current.serviceType })
    // Reassign after updating the captured object so the progress computed sees its identity.
    attempt.value = { ...current }
    if (current.serviceType !== 'surge' && !beginSubmission(current.agentId, current.deploymentId)) return
    pendingPosts.add(current.agentId)
    try {
      const { data } = await agentApi.pushConfig(current.agentId, current.deploymentId, publishGuard)
      if (disposed) return
      if (current.serviceType === 'surge') {
        if (!stillCurrent(current)) return
        if (data?.success !== true || data?.restart?.success === false) {
          error.value = data?.restart?.success === false ? '草稿已保存，配置已写入，但重启失败。' : '草稿已保存，发布失败。'
          submissionFailure.value = { ...data, error: data?.restart?.message || data?.error || data?.message || '目标设备未确认应用成功。', error_details: data?.restart?.error_details || data?.error_details }
        } else synchronousResult.value = 'done'
      } else if (data?.deployment_id === current.deploymentId && typeof data?.status === 'string') {
        endSubmission(current.agentId, current.deploymentId, data)
      } else {
        // A transactional endpoint must confirm its identity; HTTP 200 alone is not success.
        endSubmission(current.agentId, current.deploymentId, 'unknown')
      }
    } catch (failure: any) {
      if (disposed) return
      if (stillCurrent(current) && !failure.response?.data?.deployment_id && failure.response?.data?.error_details) {
        submissionFailure.value = { ...failure.response.data, message: messageOf(failure) }
      }
      if (current.serviceType === 'surge') {
        if (!stillCurrent(current)) return
        if (!failure.response || failure.response.status >= 500) synchronousResult.value = 'unknown'
        else { error.value = '草稿已保存，发布失败。'; submissionFailure.value = { ...failure.response.data, error: messageOf(failure) } }
      } else if (failure.response?.data?.deployment_id) {
        const result = failure.response.data as AgentDeployment
        if (result.deployment_id === current.deploymentId) endSubmission(current.agentId, current.deploymentId, result)
        else {
          endSubmission(current.agentId, current.deploymentId, 'rejected')
          if (stillCurrent(current)) {
            error.value = '草稿已保存，本次未发布：设备有另一项发布尚未结束，请到 Agent 页面查看。'
            attempt.value = null
          }
          track(current.agentId, result, false)
        }
      } else if (!failure.response || failure.response.status >= 500) endSubmission(current.agentId, current.deploymentId, 'unknown')
      else if (!endSubmission(current.agentId, current.deploymentId, 'rejected') && stillCurrent(current)) {
        error.value = '草稿已保存，发布失败。'
        submissionFailure.value = { ...failure.response.data, error: messageOf(failure) }
      }
    } finally { pendingPosts.delete(current.agentId) }
  } catch (failure) {
    if (stillCurrent(current)) error.value = current.saved
      ? `草稿已保存，未发布：${messageOf(failure)}` : `草稿保存失败，未发布：${messageOf(failure)}`
  } finally { if (stillCurrent(current)) phase.value = 'idle' }
}

async function activateSaved() {
  const current = visibleAttempt.value
  if (!current || task.value?.status !== 'ready' || phase.value !== 'idle' || props.disabled) return
  phase.value = 'checking'
  error.value = ''
  submissionFailure.value = null
  try {
    if (await verifyTarget(current) && stillCurrent(current)) await activate(current.agentId)
  } catch (failure) {
    if (stillCurrent(current)) error.value = `未激活：${messageOf(failure)}`
  } finally { if (stillCurrent(current)) phase.value = 'idle' }
}

watch(() => [props.open, props.profileId, props.serviceType, props.preferredAgentId] as const, () => {
  session++
  agents.value = []
  profiles.value = []
  selectedId.value = ''
  targetUnavailable.value = false
  targetsInitialized = false
  loading.value = false
  loadError.value = ''
  phase.value = 'idle'
  attempt.value = null
  error.value = ''
  submissionFailure.value = null
  synchronousResult.value = 'idle'
  if (props.open) void loadTargets()
}, { immediate: true, flush: 'sync' })
onUnmounted(() => { disposed = true; session++ })
</script>

<template>
  <div v-if="open" class="min-w-0 space-y-3" data-settings-publish-bar>
    <div class="flex min-w-0 flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
      <div class="min-w-0 flex-1 space-y-1.5 text-xs text-muted-foreground">
        <p class="m-0 break-words">配置空间：<span class="font-medium text-foreground">{{ profileName }}</span></p>
        <p v-if="loading" class="m-0" role="status">正在读取目标设备…</p>
        <div v-else-if="candidates.length > 1 || (candidates.length && !selected)" class="flex min-w-0 items-center gap-2">
          <span class="shrink-0">发布到</span>
          <Select :model-value="selectedId" :disabled="working || disabled" @update:model-value="selectTarget">
            <SelectTrigger class="min-h-11 w-full min-w-0 max-w-sm [&_[data-slot=select-value]]:min-w-0" aria-label="发布目标设备">
              <SelectValue placeholder="选择一台设备" />
            </SelectTrigger>
            <SelectContent class="max-w-[calc(100vw-2rem)]">
              <SelectItem v-for="agent in candidates" :key="agent.id" :value="agent.id" :disabled="!online(agent)" class="min-h-11 break-all whitespace-normal">
                {{ agent.name }}{{ agent.enabled === false ? '（已禁用）' : agent.status !== 'online' ? '（离线）' : '' }}
              </SelectItem>
            </SelectContent>
          </Select>
        </div>
        <p v-else-if="selected" class="m-0 break-words">发布到：<span class="font-medium text-foreground">{{ selected.name }}</span>{{ selected.enabled === false ? '（已禁用）' : selected.status !== 'online' ? '（离线）' : '' }}</p>
        <p v-else-if="!loadError && !targetUnavailable" class="m-0 break-words">此配置空间尚未绑定 {{ serviceLabel }} 设备，可先保存草稿。</p>
        <p v-if="selected?.oxidns_ha?.enabled" class="m-0 break-words text-foreground" data-ha-publish-target>同时更新高可用对端 {{ haPeer?.name || selected.oxidns_ha.peer_id }}，先备机、检查成功后再主机。</p>
        <p v-if="!loading && !loadError && targetUnavailable" class="m-0 break-words text-warning-accent" role="status">原目标设备不可用于此配置空间的 {{ serviceLabel }} 配置，请重新选择设备；仍可保存草稿。</p>
      </div>
      <div class="grid min-w-0 shrink-0 grid-cols-2 gap-2 sm:flex">
        <Button type="button" variant="outline" class="min-h-11 min-w-0 px-3" :disabled="disabled || phase !== 'idle'" @click="emit('save-draft')">
          <Save class="size-4 shrink-0" aria-hidden="true" />保存草稿
        </Button>
        <Button type="button" class="min-h-11 min-w-0 px-3" :disabled="!canPublish" @click="saveAndPublish">
          <LoaderCircle v-if="phase !== 'idle' || unresolved" class="size-4 shrink-0 animate-spin" aria-hidden="true" />
          <Upload v-else class="size-4 shrink-0" aria-hidden="true" />
          {{ phase === 'saving' ? '保存中…' : phase !== 'idle' || unresolved ? '发布中…' : '保存并发布' }}
        </Button>
      </div>
    </div>
    <div v-if="loadError" class="flex min-w-0 flex-wrap items-center gap-2 text-xs text-warning-accent" role="alert">
      <span class="min-w-0 flex-1 break-words">{{ loadError }}</span>
      <Button type="button" variant="ghost" class="min-h-11 shrink-0" :disabled="loading || working" @click="loadTargets"><RefreshCw class="size-3.5" aria-hidden="true" />重试读取</Button>
    </div>
    <div v-if="progressLabel" aria-label="配置发布进度" class="settings-publish-progress min-w-0 divide-y divide-border/70 text-xs leading-relaxed text-foreground">
      <AgentDeploymentStatus v-if="progressTask" :task="progressTask" label="配置发布" compact>
        <template v-if="progressNeedsNotice" #notice>
          <p class="m-0 break-words">{{ progressLabel }}</p>
          <AgentOperationSteps :task="progressTask" :show-current="false" :show-steps="false" />
          <p v-if="scopeMessage" class="m-0 break-words text-muted-foreground">{{ scopeMessage }}</p>
          <AgentFailureDetails :show-progress-message="false" :showDNSChecks="false" :task="submissionFailure || progressTask" />
        </template>
        <template #details>
          <template v-if="!progressNeedsNotice">
            <p class="m-0 break-words">{{ progressLabel }}</p>
            <p v-if="scopeMessage" class="m-0 break-words">{{ scopeMessage }}</p>
          </template>
          <AgentOperationSteps :task="progressTask" :show-current="false" :show-message="!progressNeedsNotice" embedded />
        </template>
      </AgentDeploymentStatus>
      <AgentStatusRow v-else label="配置发布" :status="stageStatus" :tone="stageTone">
        <template v-if="stageTone !== 'success'" #notice>
          <p class="m-0 break-words">{{ progressLabel }}</p>
          <p v-if="scopeMessage" class="m-0 break-words text-muted-foreground">{{ scopeMessage }}</p>
          <AgentFailureDetails v-if="submissionFailure" :show-progress-message="false" :showDNSChecks="false" :task="submissionFailure" />
        </template>
        <template v-if="stageTone === 'success'" #default>
          <p class="m-0 break-words">{{ progressLabel }}</p>
          <p v-if="scopeMessage" class="m-0 break-words">{{ scopeMessage }}</p>
        </template>
      </AgentStatusRow>
      <AgentDNSChecks :checks="(submissionFailure || task)?.dns_checks" :checked-at="(submissionFailure || task)?.dns_checked_at" compact />
      <p v-if="task?.status === 'succeeded' && hasDNSFollowers" class="m-0 break-words text-muted-foreground" data-dns-follow-hint>
        后台会继续处理已绑定的 OxiDNS；<RouterLink class="inline-flex min-h-11 items-center text-primary-accent underline underline-offset-4" to="/agents">到 Agent 页面查看同步结果或重试</RouterLink>。
      </p>
      <p v-if="unresolved && task?.status !== 'ready'" class="m-0">可以离开此页面；已提交的发布会继续执行，结果可在 Agent 页面查看。</p>
      <div v-if="task?.status === 'ready'" class="flex min-w-0 flex-wrap items-center gap-2">
        <span class="min-w-0 flex-1">待应用的是本次已校验的配置，不包含之后保存的草稿修改。</span>
        <Button type="button" variant="outline" class="min-h-11" :disabled="disabled || phase !== 'idle'" @click="activateSaved">应用并启动</Button>
      </div>
      <p v-if="blockedRecovery" class="m-0 text-destructive-accent">请先到 Agent 页面处理回滚结果，确认服务恢复后再发布。</p>
    </div>
  </div>
</template>

<style scoped>
@media (max-height: 760px) {
  .settings-publish-progress {
    max-height: min(100px, 20dvh);
    overflow-y: auto;
    overflow-wrap: anywhere;
    overscroll-behavior: contain;
  }
}
</style>
