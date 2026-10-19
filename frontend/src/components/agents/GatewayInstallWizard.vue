<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { Check, Copy, LoaderCircle, RefreshCw } from '@lucide/vue'
import { gatewayOnboardingApi, type GatewayConfigurationPreview, type GatewayInstallation, type GatewayOnboardingConfig, type GatewayOnboardingSettings } from '@/api/gatewayOnboarding'
import { linuxGatewayApi, type LinuxGatewayCandidate, type LinuxGatewayPreview, type LinuxGatewayStatus } from '@/api/linuxGateway'
import { cleanGatewaySpec, emptyGatewaySpec, gatewayErrorMessage, gatewaySpecKey } from '@/lib/linuxGateway'
import { gatewayEnvironmentErrors, installationHostMatches, installationMatches, installationPhaseLabel, onboardingSettingsKey } from '@/lib/gatewayOnboarding'
import { buildGatewayNetworkProbeCommand, parseGatewayNetworkProbe } from '@/lib/gatewayNetworkDetection'
import { notify } from '@/lib/feedback'
import SettingsWorkspace from '@/components/generate/SettingsWorkspace.vue'
import AgentDeploymentStatus from '@/components/agents/AgentDeploymentStatus.vue'
import FormField from '@/components/common/FormField.vue'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'

const props = defineProps<{ open: boolean; profileId: string; profileName: string }>()
const emit = defineEmits<{ 'update:open': [value: boolean]; 'agents-changed': [] }>()
const steps = ['设备与网络', '分流规则', '安装核心', '发布并检查', '启用与验证']
const step = ref(0)
const contentPane = ref<HTMLElement | null>(null)
const environmentErrorPanel = ref<HTMLElement | null>(null)
const config = ref<GatewayOnboardingConfig | null>(null)
const settings = reactive<GatewayOnboardingSettings>({ rule_set_ids: [], direct_rule_set_ids: [], proxy_rule_set_ids: [], local_dns: 'https://223.5.5.5/dns-query' })
const network = reactive(emptyGatewaySpec())
const name = ref('home-gateway')
const serverUrl = ref('')
const networkProbeResult = ref('')
const networkProbeError = ref('')
const networkProbeApplied = ref(false)
const environmentAttempted = ref(false)
const search = ref('')
const configurationPreview = ref<GatewayConfigurationPreview | null>(null)
const configurationPreviewKey = ref('')
const savedConfiguration = ref<{ key: string; revision: number } | null>(null)
const installation = ref<GatewayInstallation | null>(null)
const networkStatus = ref<LinuxGatewayStatus | null>(null)
const networkRemoved = ref(false)
const networkPreview = ref<LinuxGatewayPreview | null>(null)
const networkPreviewKey = ref('')
const loading = ref(false)
const action = ref('')
const refreshPending = ref(false)
const error = ref('')
const pollError = ref('')
const resumed = ref(false)
const baseline = ref('')
let generation = 0
let disposed = false
let poll: ReturnType<typeof setTimeout> | undefined
const busy = computed(() => loading.value || !!action.value)
const locked = computed(() => !!installation.value)
const availableStep = computed(() => !installation.value ? step.value : installation.value.phase === 'published' ? 4 : installation.value.phase === 'waiting' ? 2 : 3)
const commandExpiry = computed(() => {
  const value = installation.value?.expires_at
  const date = value ? new Date(value) : null
  return date && !Number.isNaN(date.getTime())
    ? `命令有效期至 ${date.toLocaleString()}（本地时间）。`
    : '命令有效期暂时无法确认，请刷新安装进度。'
})
const settingsKey = computed(() => onboardingSettingsKey(settings))
const allDraftKey = computed(() => JSON.stringify([name.value, serverUrl.value, gatewaySpecKey(network), settingsKey.value]))
const dirty = computed(() => !locked.value && !!baseline.value && allDraftKey.value !== baseline.value)
const environmentErrors = computed(() => gatewayEnvironmentErrors(name.value, serverUrl.value, network))
const networkProbeCommand = computed(() => {
  try { return buildGatewayNetworkProbeCommand(serverUrl.value) }
  catch { return '' }
})
const hasNetworkDetails = computed(() => Object.values(network).some(value => value.trim()))
const configReady = computed(() => configurationPreview.value?.ready === true && configurationPreviewKey.value === settingsKey.value)
const configLists = computed(() => [
  { field: 'rule_set_ids' as const, title: '接管的真实 IP', hint: '选需要交给 Mihomo 的 IP 集合，例如 Telegram；只使用 fake-IP 时可留空。', rows: config.value?.ip_candidates || [] },
  { field: 'direct_rule_set_ids' as const, title: '直连域名', hint: '由 OxiDNS 的加密上游解析，返回真实 IP。', rows: config.value?.domain_candidates || [] },
  { field: 'proxy_rule_set_ids' as const, title: '代理域名', hint: '由 Mihomo 返回 fake-IP，出口沿用策略规则。', rows: config.value?.domain_candidates || [] },
].map(list => {
  const rows: LinuxGatewayCandidate[] = [...list.rows, ...settings[list.field].filter(id => !list.rows.some(row => row.id === id)).map(id => ({
    id, name: `已移除的规则集（${id}）`, behavior: '', available: false, enabled: false, reason: '此关联已失效，请取消勾选后重新检查。',
  }))]
  return { ...list, rows, visibleRows: rows.filter(row => `${row.name} ${row.policy || ''}`.toLowerCase().includes(search.value.trim().toLowerCase())) }
}))
const hostAgent = computed(() => installation.value?.agents.mihomo)
const installationBlocked = computed(() => !!(installation.value?.config_changed || installation.value?.target_changed || (installation.value?.expired && installation.value?.phase === 'waiting')))
const canNetwork = computed(() => !installationBlocked.value && installation.value?.phase === 'published' && !!hostAgent.value && installationMatches(installation.value, props.profileId))
const canInspectNetwork = computed(() => installationHostMatches(installation.value, props.profileId))
const hostKey = computed(() => JSON.stringify([installation.value?.id, props.profileId, hostAgent.value?.id, gatewaySpecKey(network)]))
const canApply = computed(() => canNetwork.value && !busy.value && networkPreview.value?.ready === true && networkPreviewKey.value === hostKey.value)
const activeNetwork = computed(() => networkStatus.value?.managed?.active === true)
const token = () => ({ generation, profileId: props.profileId })
const current = (value: ReturnType<typeof token>) => !disposed && props.open && value.generation === generation && value.profileId === props.profileId
const storageKey = () => `configflow.gateway-installation.${props.profileId}`
const settingsBody = (): GatewayOnboardingSettings => ({ ...settings, rule_set_ids: [...settings.rule_set_ids], direct_rule_set_ids: [...settings.direct_rule_set_ids], proxy_rule_set_ids: [...settings.proxy_rule_set_ids], expected_config_revision: config.value?.config_revision })

function stopPolling() { if (poll) clearTimeout(poll); poll = undefined }
function resetNetworkProbe() {
  networkProbeResult.value = ''; networkProbeError.value = ''; networkProbeApplied.value = false; environmentAttempted.value = false
}
function applyNetworkProbe() {
  if (!props.open || locked.value || busy.value || step.value !== 0) return
  networkProbeError.value = ''; networkProbeApplied.value = false
  if (!networkProbeResult.value.trim()) return
  try {
    const detected = parseGatewayNetworkProbe(networkProbeResult.value)
    Object.assign(network, detected)
    networkProbeApplied.value = true
  } catch (failure) { networkProbeError.value = gatewayErrorMessage(failure) }
}
function updateNetworkProbeResult(value: string | number) {
  if (!props.open || locked.value || busy.value || step.value !== 0) return
  networkProbeResult.value = String(value)
  applyNetworkProbe()
}
function continueToRules() {
  if (busy.value || !config.value || locked.value) return
  environmentAttempted.value = true
  if (!environmentErrors.value.length) step.value = 1
  else {
    const captured = token()
    void nextTick(() => { if (current(captured) && step.value === 0) environmentErrorPanel.value?.scrollIntoView?.({ block: 'nearest' }) })
  }
}
function selectStep(value: string | number) {
  const index = Number(value)
  if (busy.value || !Number.isInteger(index) || index < 0 || index > availableStep.value || (locked.value && index < 2)) return
  step.value = index
}
function schedulePoll() {
  stopPolling()
  if (!props.open || !installation.value || installation.value.phase === 'published' || installation.value.phase === 'publish_failed') return
  poll = setTimeout(() => { void refreshInstallation() }, 4000)
}
function acceptInstallation(value: GatewayInstallation, restore = false) {
  if (!value?.id || value.profile_id !== props.profileId || !value.network || !value.agents || !value.deployments || (!installationMatches(value, props.profileId) && !value.target_changed) || (installation.value && installation.value.id !== value.id)) throw new Error('安装记录与当前配置空间或目标设备不一致，已停止操作。')
  const phaseChanged = installation.value?.phase !== value.phase
  installation.value = value
  resetNetworkProbe()
  Object.assign(network, value.network)
  name.value = value.name
  if (value.server_url) serverUrl.value = value.server_url
  try { localStorage.setItem(storageKey(), value.id) } catch { /* The server record remains resumable. */ }
  if (restore) resumed.value = true
  if (restore || phaseChanged) {
    step.value = availableStep.value
    if (value.phase === 'published') void nextTick(inspectNetwork)
  }
  emit('agents-changed')
  schedulePoll()
}
async function load() {
  const captured = token()
  loading.value = true
  error.value = ''
  const initialServerUrl = serverUrl.value
  const [configResponse, sessionsResponse] = await Promise.allSettled([
    gatewayOnboardingApi.get(captured.profileId), gatewayOnboardingApi.sessions(captured.profileId),
  ])
  if (!current(captured)) return
  if (configResponse.status === 'fulfilled') {
    config.value = configResponse.value.data
    if (config.value.server_url && serverUrl.value === initialServerUrl) serverUrl.value = config.value.server_url
    Object.assign(settings, config.value.settings)
    baseline.value = allDraftKey.value
  } else error.value = gatewayErrorMessage(configResponse.reason)
  if (sessionsResponse.status === 'fulfilled') {
    try {
      const result = sessionsResponse.value.data
      if (result.success === false || !Object.prototype.hasOwnProperty.call(result, 'session')) throw new Error('安装记录响应无效。')
      if (result.session) acceptInstallation(result.session, true)
    } catch (failure) { error.value = gatewayErrorMessage(failure) }
  } else pollError.value = `暂时无法读取先前的安装记录：${gatewayErrorMessage(sessionsResponse.reason)}`
  loading.value = false
}
function toggle(field: 'rule_set_ids' | 'direct_rule_set_ids' | 'proxy_rule_set_ids', id: string, checked: boolean | 'indeterminate') {
  if (locked.value) return
  settings[field] = checked === true ? [...new Set([...settings[field], id])] : settings[field].filter(item => item !== id)
  // A domain can have only one DNS destination; move it instead of leaving overlap.
  if (checked === true && field !== 'rule_set_ids') {
    const other = field === 'direct_rule_set_ids' ? 'proxy_rule_set_ids' : 'direct_rule_set_ids'
    settings[other] = settings[other].filter(item => item !== id)
  }
}
async function previewConfiguration() {
  if (busy.value || locked.value || !config.value) return
  const captured = token()
  const key = settingsKey.value
  action.value = 'preview-config'; error.value = ''; configurationPreview.value = null
  try {
    const { data } = await gatewayOnboardingApi.preview(captured.profileId, settingsBody())
    if (!current(captured) || key !== settingsKey.value) return
    configurationPreview.value = data
    configurationPreviewKey.value = key
    if (data.success === false && !data.blockers?.length) throw new Error(data.message || '配置检查失败。')
  } catch (failure) { if (current(captured)) error.value = gatewayErrorMessage(failure) }
  finally { if (current(captured)) action.value = '' }
}
async function createInstallation() {
  if (busy.value || locked.value || !configReady.value || environmentErrors.value.length || !config.value) return
  const captured = token()
  action.value = 'create'; error.value = ''
  try {
    const key = settingsKey.value
    if (savedConfiguration.value?.key !== key) {
      const saved = (await gatewayOnboardingApi.save(captured.profileId, settingsBody())).data
      if (!current(captured)) return
      if (!saved.success || !Number.isSafeInteger(saved.config_revision)) throw new Error(saved.message || saved.blockers?.join('；') || '配置没有保存成功。')
      config.value.config_revision = saved.config_revision!
      savedConfiguration.value = { key, revision: saved.config_revision! }
    }
    if (key !== settingsKey.value) throw new Error('配置已保存，但表单已改变，请重新检查后继续。')
    // Retrying a lost create response must reuse the saved revision and server idempotency.
    const { data } = await gatewayOnboardingApi.create({ profile_id: captured.profileId, name: name.value.trim(), server_url: serverUrl.value.trim().replace(/\/$/, ''), network: cleanGatewaySpec(network), config_revision: savedConfiguration.value!.revision })
    if (!current(captured)) return
    acceptInstallation(data)
    baseline.value = allDraftKey.value
    notify.success('配置已保存，请在目标 Linux 执行安装命令。')
  } catch (failure) { if (current(captured)) error.value = gatewayErrorMessage(failure) }
  finally { if (current(captured)) action.value = '' }
}
async function refreshInstallation() {
  if (!installation.value || refreshPending.value) return
  const captured = token(), id = installation.value.id
  refreshPending.value = true
  try {
    const { data } = await gatewayOnboardingApi.session(id, captured.profileId)
    if (!current(captured) || installation.value?.id !== id) return
    acceptInstallation(data)
    pollError.value = ''
  } catch (failure) { if (current(captured)) pollError.value = `状态暂时无法确认，未继续操作：${gatewayErrorMessage(failure)}` }
  finally { if (current(captured)) { refreshPending.value = false; schedulePoll() } }
}
async function publish() {
  if (busy.value || installationBlocked.value || !installation.value || !['enrolled', 'publishing', 'publish_failed'].includes(installation.value.phase)) return
  const captured = token(), id = installation.value.id
  action.value = 'publish'; error.value = ''
  try {
    const { data } = await gatewayOnboardingApi.publish(id, captured.profileId, installation.value.phase === 'publish_failed')
    if (current(captured) && installation.value?.id === id) acceptInstallation(data)
  } catch (failure) {
    if (current(captured)) { error.value = `发布状态需要确认：${gatewayErrorMessage(failure)}。请刷新进度，不要重复安装。`; void refreshInstallation() }
  } finally { if (current(captured)) action.value = '' }
}
async function inspectNetwork() {
  if (!canInspectNetwork.value || !hostAgent.value || action.value) return
  const captured = token(), agentId = hostAgent.value.id
  action.value = 'inspect'; error.value = ''
  try {
    const { data } = await linuxGatewayApi.status(agentId, captured.profileId)
    if (!current(captured) || hostAgent.value?.id !== agentId) return
    networkStatus.value = data
    if (!data.success) throw new Error(data.message || '网络检测失败。')
  } catch (failure) { if (current(captured)) error.value = gatewayErrorMessage(failure, true) }
  finally { if (current(captured)) action.value = '' }
}
async function previewNetwork() {
  if (!canNetwork.value || busy.value || !hostAgent.value) return
  const captured = token(), key = hostKey.value, agentId = hostAgent.value.id
  action.value = 'preview-network'; error.value = ''; networkPreview.value = null
  try {
    const { data } = await linuxGatewayApi.preview(agentId, captured.profileId, cleanGatewaySpec(network))
    if (!current(captured) || key !== hostKey.value) return
    networkPreview.value = data; networkPreviewKey.value = key
    if (data.success === false && !data.blockers?.length) throw new Error(data.message || '转发检查失败。')
  } catch (failure) { if (current(captured)) error.value = gatewayErrorMessage(failure, true) }
  finally { if (current(captured)) action.value = '' }
}
async function applyNetwork() {
  if (!canApply.value || !hostAgent.value || !installation.value) return
  const captured = token(), key = hostKey.value, agentId = hostAgent.value.id, id = installation.value.id
  action.value = 'apply'; error.value = ''; networkPreview.value = null
  try {
    // Recheck the exact registered pair after preview; a stale UI may never pick another host.
    const fresh = (await gatewayOnboardingApi.session(id, captured.profileId)).data
    if (!current(captured) || key !== hostKey.value) return
    if (!installationMatches(fresh, captured.profileId) || fresh.id !== id || fresh.phase !== 'published' || fresh.agents.mihomo?.id !== agentId || gatewaySpecKey(fresh.network) !== gatewaySpecKey(network) || fresh.config_changed || fresh.target_changed) throw new Error('服务发布状态或设备绑定已改变，请刷新安装进度。')
    const { data } = await linuxGatewayApi.apply(agentId, captured.profileId, cleanGatewaySpec(network))
    if (!current(captured) || key !== hostKey.value) return
    networkStatus.value = data
    if (!data.success) throw new Error(data.message || '转发应用失败，请检查设备状态。')
    networkRemoved.value = false
    notify.success('网关转发已启用。请先用一台客户端完成连接验证。')
  } catch (failure) { if (current(captured)) error.value = gatewayErrorMessage(failure, true) }
  finally { if (current(captured)) action.value = '' }
}
async function removeNetwork() {
  if (!canInspectNetwork.value || !hostAgent.value || busy.value || !installation.value) return
  const captured = token(), agentId = hostAgent.value.id, id = installation.value.id
  action.value = 'remove'; error.value = ''; networkPreview.value = null
  try {
    const fresh = (await gatewayOnboardingApi.session(id, captured.profileId)).data
    if (!current(captured) || hostAgent.value?.id !== agentId) return
    if (!installationHostMatches(fresh, captured.profileId) || fresh.id !== id || fresh.agents.mihomo?.id !== agentId) throw new Error('原目标设备的身份或绑定已改变，未撤销任何设备的网络设置。')
    const { data } = await linuxGatewayApi.remove(agentId, captured.profileId)
    if (!current(captured) || hostAgent.value?.id !== agentId) return
    networkStatus.value = data
    if (!data.success) throw new Error(data.message || '恢复未完成，请检查设备状态。')
    networkRemoved.value = true
    notify.success('本向导管理的转发已撤销，请恢复客户端原来的网关与 DNS 设置。')
  } catch (failure) { if (current(captured)) error.value = gatewayErrorMessage(failure, true) }
  finally { if (current(captured)) action.value = '' }
}
async function startAnotherInstallation() {
  if (busy.value) return
  stopPolling(); installation.value = null; networkStatus.value = null; networkPreview.value = null; networkRemoved.value = false
  configurationPreview.value = null; savedConfiguration.value = null; resumed.value = false; step.value = 0; error.value = ''; pollError.value = ''
  resetNetworkProbe()
  try { localStorage.removeItem(storageKey()) } catch { /* The original installation remains on the server. */ }
  const captured = token()
  loading.value = true
  try {
    const { data } = await gatewayOnboardingApi.get(captured.profileId)
    if (!current(captured)) return
    config.value = data; Object.assign(settings, data.settings); baseline.value = allDraftKey.value
  } catch (failure) { if (current(captured)) { config.value = null; error.value = gatewayErrorMessage(failure) } }
  finally { if (current(captured)) loading.value = false }
}
async function copyToClipboard(text: string) {
  if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(text)
  const field = document.createElement('textarea')
  field.value = text; field.setAttribute('readonly', ''); field.style.position = 'fixed'; field.style.opacity = '0'
  document.body.appendChild(field)
  try {
    field.select()
    if (!document.execCommand('copy')) throw new Error('无法复制')
  } finally { field.remove() }
}
async function copyNetworkCommand() {
  if (!props.open || locked.value || busy.value || step.value !== 0) return
  const captured = token()
  networkProbeError.value = ''
  let command: string
  try { command = buildGatewayNetworkProbeCommand(serverUrl.value) }
  catch (failure) { networkProbeError.value = gatewayErrorMessage(failure); return }
  try { await copyToClipboard(command); if (current(captured)) notify.success('检测命令已复制，请在目标 Linux 执行。') }
  catch { if (current(captured)) notify.error('复制失败，请选中检测命令手动复制。') }
}
async function copyCommand() {
  if (!installation.value?.command || installation.value.expired) return
  const captured = token()
  try { await copyToClipboard(installation.value.command); if (current(captured)) notify.success('安装命令已复制。') }
  catch { if (current(captured)) notify.error('复制失败，请选中安装命令手动复制。') }
}
watch(step, () => { void nextTick(() => {
  contentPane.value?.scrollTo?.({ top: 0 })
  contentPane.value?.querySelector<HTMLElement>('[data-gateway-steps] [aria-current="step"]')?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' })
}) })
watch(() => networkStatus.value?.managed, managed => { if (managed?.active || managed?.pending) networkRemoved.value = false })
watch(settingsKey, () => { configurationPreview.value = null; savedConfiguration.value = null })
watch(() => [props.open, props.profileId] as const, () => {
  generation++; stopPolling(); installation.value = null; config.value = null; configurationPreview.value = null; savedConfiguration.value = null
  networkStatus.value = null; networkPreview.value = null; networkRemoved.value = false; error.value = ''; pollError.value = ''; resumed.value = false; search.value = ''
  loading.value = false; action.value = ''; refreshPending.value = false; baseline.value = ''; step.value = 0
  resetNetworkProbe()
  Object.assign(network, emptyGatewaySpec()); name.value = 'home-gateway'
  Object.assign(settings, { rule_set_ids: [], direct_rule_set_ids: [], proxy_rule_set_ids: [], local_dns: 'https://223.5.5.5/dns-query', confirm_template_conversion: false })
  try { serverUrl.value = localStorage.getItem('serverDomain') || window.location.origin } catch { serverUrl.value = window.location.origin }
  if (props.open) void load()
}, { immediate: true, flush: 'sync' })
onBeforeUnmount(() => { disposed = true; generation++; stopPolling() })
</script>

<template>
  <SettingsWorkspace :open="open" title="安装 Linux 网关" description="安装 Mihomo、OxiDNS 与 Agent，检查后启用转发。" back-label="返回 Agent 列表" :dirty="dirty" :busy="busy" size="form" @update:open="emit('update:open', $event)">
    <div ref="contentPane" class="min-h-0 min-w-0 flex-1 overflow-y-auto overscroll-contain" data-gateway-install-wizard>
      <Tabs :model-value="String(step)" activation-mode="manual" class="mx-auto w-full min-w-0 max-w-3xl gap-4 p-3 sm:p-5" @update:model-value="selectStep">
        <div class="min-w-0 space-y-2">
          <div class="flex min-w-0 flex-wrap items-center justify-between gap-x-3 gap-y-1 text-xs text-muted-foreground">
            <p class="m-0 min-w-0 break-words">配置空间 · <span class="font-medium text-foreground">{{ profileName }}</span></p>
            <span>IPv4 · 单网卡</span>
          </div>
          <TabsList variant="strip" class="h-auto w-full justify-start gap-1 overflow-x-auto rounded-none border-b border-border bg-card p-1" aria-label="安装步骤" data-gateway-steps>
            <TabsTrigger v-for="(label, index) in steps" :key="label" :value="String(index)" class="h-11 min-h-11 flex-none px-3 text-xs" :aria-label="`${index + 1} ${label}`" :aria-current="index === step ? 'step' : undefined" :disabled="busy || index > availableStep || (locked && index < 2)">
              <span class="num">{{ index + 1 }}</span>{{ label }}
            </TabsTrigger>
          </TabsList>
        </div>
        <p v-if="loading" role="status" class="m-0 flex items-center gap-2 text-sm text-muted-foreground"><LoaderCircle class="size-4 animate-spin" />正在读取配置与安装进度…</p>
        <div v-if="error" class="rounded-lg border border-destructive/30 bg-destructive-soft p-3 text-sm text-destructive-accent" role="alert">
          {{ error }}<Button v-if="!config && !installation" variant="ghost" class="min-h-11" :disabled="busy" @click="load">重新读取</Button>
        </div>
        <p v-if="pollError" class="m-0 break-words text-sm text-warning-accent" role="alert">{{ pollError }}</p>
        <div v-if="installationBlocked" role="alert" class="rounded-lg border border-warning/30 bg-warning-soft p-3 text-sm text-warning-accent">
          <p class="m-0">{{ installation?.target_changed ? '设备绑定或身份已改变。' : installation?.config_changed ? '配置空间在安装后有修改。' : '安装凭据已过期。' }} 已停止发布与转发操作，请重新准备安装。</p>
        </div>
        <div v-if="installation" class="border-b border-border pb-3">
          <div class="flex min-w-0 flex-wrap items-center justify-between gap-x-3 gap-y-1">
            <p class="m-0 min-w-0 break-words text-sm">{{ resumed ? '已恢复' : '安装目标' }} · <strong class="font-medium">{{ installation.name }}</strong> <span class="font-mono text-xs text-muted-foreground">{{ network.address }}</span></p>
            <Button variant="ghost" class="min-h-11 px-2 text-xs" :disabled="busy" @click="startAnotherInstallation">重新准备安装</Button>
          </div>
          <p class="m-0 text-xs text-muted-foreground">重新准备不会撤销已安装的服务或已提交的发布。</p>
        </div>

        <TabsContent :value="String(step)" as-child>
        <section v-if="step === 0" :key="`${profileId}:${open}`" class="space-y-4" aria-label="设备与网络">
          <div>
            <h2 class="m-0 text-lg font-semibold">设备与网络</h2>
            <p class="mt-1 mb-0 text-sm text-muted-foreground">Linux 需使用固定 IP，并与主路由在同一网段。</p>
          </div>
          <div class="grid min-w-0 gap-3 sm:grid-cols-2">
            <FormField label="设备名称" html-for="onboarding-name"><Input id="onboarding-name" v-model="name" class="min-h-11" placeholder="home-gateway" :disabled="busy" /></FormField>
            <FormField label="ConfigFlow 服务地址" html-for="onboarding-server"><Input id="onboarding-server" v-model="serverUrl" class="min-h-11 font-mono text-xs" placeholder="http://192.168.1.10:8000" :disabled="locked" /></FormField>
          </div>
          <div class="space-y-2 border-t border-border pt-4">
            <div class="flex flex-wrap items-baseline justify-between gap-2">
              <h3 class="m-0 text-sm font-semibold">检测网络</h3>
              <span v-if="!hasNetworkDetails" class="text-xs text-muted-foreground" role="status">尚未读取网络信息</span>
            </div>
            <p class="m-0 text-xs leading-relaxed text-muted-foreground">在目标 Linux 执行命令，再粘贴输出，自动读取网卡、地址和网关。检测不会修改网络。</p>
            <div class="grid min-w-0 gap-3 sm:grid-cols-2">
              <div class="min-w-0 space-y-1.5">
                <div class="flex min-h-11 items-center justify-between gap-2">
                  <label for="onboarding-network-command" class="text-xs font-medium">1. 在 Linux 执行</label>
                  <Button class="min-h-11 shrink-0 px-3 text-xs" :disabled="busy || locked" @click="copyNetworkCommand"><Copy class="size-3.5" />复制检测命令</Button>
                </div>
                <Textarea id="onboarding-network-command" :model-value="networkProbeCommand" readonly :rows="3" class="h-28 min-h-28 max-h-28 resize-none font-mono text-xs" placeholder="先填写 Linux 能访问的服务地址" />
              </div>
              <div class="min-w-0 space-y-1.5">
                <div class="flex min-h-11 items-center justify-between gap-2">
                  <label for="onboarding-network-result" class="text-xs font-medium">2. 粘贴检测结果</label>
                  <Button v-if="networkProbeResult.trim() && !networkProbeApplied" variant="ghost" class="min-h-11 shrink-0 px-2 text-xs" :disabled="busy || locked" @click="applyNetworkProbe">填入网络信息</Button>
                  <span v-else-if="networkProbeApplied" class="flex items-center gap-1 text-xs text-success-accent" role="status"><Check class="size-3.5" />已填入</span>
                </div>
                <Textarea id="onboarding-network-result" :model-value="networkProbeResult" :disabled="busy || locked" :rows="3" class="h-28 min-h-28 max-h-28 resize-none font-mono text-xs" placeholder="粘贴终端输出的整段 JSON，自动填入网络信息" spellcheck="false" @update:model-value="updateNetworkProbeResult" />
              </div>
            </div>
            <p v-if="networkProbeError" class="m-0 break-words text-xs leading-relaxed text-destructive-accent" role="alert">{{ networkProbeError }} 已有网络信息未更改。</p>
          </div>
          <dl v-if="hasNetworkDetails" class="m-0 grid grid-cols-2 gap-x-4 gap-y-3 border-y border-border py-3 text-xs sm:grid-cols-4" aria-label="读取的网络信息">
            <div class="min-w-0"><dt class="text-muted-foreground">Linux IPv4</dt><dd class="mx-0 mt-1 break-all font-mono">{{ network.address || '未填写' }}</dd></div>
            <div class="min-w-0"><dt class="text-muted-foreground">主路由</dt><dd class="mx-0 mt-1 break-all font-mono">{{ network.upstream_gateway || '未填写' }}</dd></div>
            <div class="min-w-0"><dt class="text-muted-foreground">LAN 网卡</dt><dd class="mx-0 mt-1 break-all font-mono">{{ network.interface || '未填写' }}</dd></div>
            <div class="min-w-0"><dt class="text-muted-foreground">LAN 网段</dt><dd class="mx-0 mt-1 break-all font-mono">{{ network.lan_cidr || '未填写' }}</dd></div>
          </dl>
          <details class="border-b border-border text-sm">
            <summary class="min-h-11 cursor-pointer content-center py-2 font-medium outline-none focus-visible:ring-2 focus-visible:ring-primary-accent">手动填写或修改</summary>
            <div class="grid min-w-0 gap-3 pt-1 pb-4 sm:grid-cols-2">
              <FormField label="Linux 的固定 IPv4" html-for="onboarding-address"><Input id="onboarding-address" v-model="network.address" :disabled="busy" class="min-h-11 font-mono" placeholder="192.168.1.2" /></FormField>
              <FormField label="主路由 IPv4" html-for="onboarding-upstream"><Input id="onboarding-upstream" v-model="network.upstream_gateway" :disabled="busy" class="min-h-11 font-mono" placeholder="192.168.1.1" /></FormField>
              <FormField label="LAN 网卡名" html-for="onboarding-interface"><Input id="onboarding-interface" v-model="network.interface" :disabled="busy" class="min-h-11 font-mono" placeholder="例如 ens18、br0" /></FormField>
              <FormField label="LAN 网段" html-for="onboarding-cidr"><Input id="onboarding-cidr" v-model="network.lan_cidr" :disabled="busy" class="min-h-11 font-mono" placeholder="192.168.1.0/24" /></FormField>
            </div>
          </details>
          <details class="border-b border-border text-sm">
            <summary class="min-h-11 cursor-pointer content-center py-2 font-medium outline-none focus-visible:ring-2 focus-visible:ring-primary-accent">安装要求</summary>
            <div class="space-y-3 pt-1 pb-4 text-xs leading-relaxed text-muted-foreground">
              <p class="m-0">支持 x86_64 / ARM64，systemd / OpenRC（含 Alpine）。需要 root 或 sudo、<code>/dev/net/tun</code> 和网络管理权限，不支持普通受限容器。</p>
              <p class="m-0">在主路由保留 Linux 的 IP，或为 Linux 设置静态 IP。向导不会修改设备 IP 或默认路由，主路由无需添加静态路由。</p>
              <div>
                <p class="mt-0 mb-1 font-medium text-foreground">缺少依赖时</p>
                <p class="my-1">Debian / Ubuntu：</p>
                <pre class="m-0 overflow-x-auto rounded-md bg-muted p-2 font-mono text-xs">sudo apt-get update &amp;&amp; sudo apt-get install -y ca-certificates curl iproute2 nftables tar gzip coreutils</pre>
                <p class="mt-2 mb-1">Alpine（root）：</p>
                <pre class="m-0 overflow-x-auto rounded-md bg-muted p-2 font-mono text-xs">apk add ca-certificates curl iproute2 nftables tar gzip coreutils</pre>
              </div>
              <div>
                <p class="mt-0 mb-1 font-medium text-foreground">已有 Mihomo / OxiDNS</p>
                <p class="m-0">标准安装可直接接入：核心位于 <code class="break-all">/usr/local/bin/</code>，配置位于 <code class="break-all">/etc/mihomo/config.yaml</code> 和 <code class="break-all">/etc/oxidns/config.yaml</code>，并有同名系统服务。安装时先校验并保留现有核心与配置，第 4 步再发布新配置。</p>
                <p class="mt-2 mb-0">端口冲突或安装不完整时会停止。非标准目录请用普通模式填写现有路径，接入 Agent。</p>
              </div>
            </div>
          </details>
          <div v-if="environmentAttempted && environmentErrors.length" ref="environmentErrorPanel" class="text-xs leading-relaxed text-destructive-accent" role="alert">
            <p class="m-0 font-medium">请补全或修正以下信息</p><ul class="mt-1 mb-0 list-disc space-y-1 pl-5"><li v-for="item in environmentErrors" :key="item">{{ item }}</li></ul>
          </div>
        </section>

        <section v-else-if="step === 1" class="space-y-4" aria-label="分流规则">
          <div><h2 class="m-0 text-lg font-semibold">分流规则</h2><p class="mt-1 mb-0 text-sm text-muted-foreground">选择接管 IP、直连和代理域名。将更新「{{ profileName }}」共用的 Mihomo 与 OxiDNS 配置。</p></div>
          <p class="m-0 text-xs text-muted-foreground">自动包含 fake-IP 网段 <code class="break-all font-mono">{{ config?.fake_ip_range || '由 Mihomo 配置决定' }}</code>。未选中的 IP 走 Linux 直连转发。</p>
          <FormField label="搜索规则集" html-for="onboarding-search"><Input id="onboarding-search" v-model="search" class="min-h-11" placeholder="按名称或策略搜索" /></FormField>
          <div v-for="list in configLists" :key="list.field" class="min-w-0 space-y-2">
            <h3 class="m-0 text-sm font-semibold">{{ list.title }} <span class="num ml-1 font-normal text-muted-foreground">{{ settings[list.field].length }} 项</span></h3>
            <p class="m-0 text-xs text-muted-foreground">{{ list.hint }}</p>
            <div class="max-h-48 overflow-y-auto rounded-md border border-border">
              <label v-for="item in list.visibleRows" :key="item.id" class="flex min-h-11 cursor-pointer items-start gap-3 border-b border-border px-3 py-2.5 last:border-b-0 hover:bg-muted" :class="{ 'opacity-70': !item.available && !settings[list.field].includes(item.id) }">
                <Checkbox :model-value="settings[list.field].includes(item.id)" :disabled="busy || (!item.available && !settings[list.field].includes(item.id))" class="mt-0.5 shrink-0" @update:model-value="toggle(list.field, item.id, $event)" />
                <span class="min-w-0 space-y-0.5"><span class="block break-all text-sm font-medium">{{ item.name }}</span><span class="block break-words text-xs text-muted-foreground">{{ item.policy ? `策略：${item.policy} · ` : '' }}{{ item.reason || item.description || item.behavior }}</span></span>
              </label>
              <p v-if="!list.rows.length" class="m-0 p-3 text-xs text-muted-foreground">先到「规则库」添加，再在此空间的「策略规则」引用并启用，然后重新打开向导。</p>
              <p v-else-if="!list.visibleRows.length" class="m-0 p-3 text-xs text-muted-foreground" role="status">没有匹配的规则集。已选规则会保留。</p>
            </div>
          </div>
          <FormField label="直连加密 DNS" html-for="onboarding-dns" hint="可填写多行。请使用加密 DNS，普通 53 端口上游可能形成回环。" persistent-hint><Textarea id="onboarding-dns" v-model="settings.local_dns" :disabled="busy" class="min-h-20 font-mono text-xs" :rows="2" /></FormField>
          <details class="border-b border-border text-xs">
            <summary class="min-h-11 cursor-pointer content-center py-2 font-medium">将修改哪些设置</summary>
            <p class="mt-1 mb-3 leading-relaxed text-muted-foreground">OxiDNS 在 53 端口服务局域网，代理与后备 DNS 指向 Mihomo 的 127.0.0.1:1053。Mihomo 启用 fake-IP 和选择性 TUN；未知域名交给 Mihomo 处理。</p>
          </details>
          <label v-if="config?.oxidns_mode === 'native'" class="flex min-h-11 cursor-pointer items-start gap-3 text-xs leading-relaxed"><Checkbox v-model="settings.confirm_template_conversion" :disabled="busy" class="mt-0.5 shrink-0" /><span>无法保留原生 OxiDNS 配置时，允许转换为向导模板。转换会替换原生解析流程；未勾选时会停止并说明原因。</span></label>
          <div v-if="config?.warnings?.length || config?.blockers?.length" class="space-y-2 text-xs text-warning-accent"><p v-for="item in [...(config?.warnings || []), ...(config?.blockers || [])]" :key="item" class="m-0 break-words">{{ item }}</p></div>
          <Button variant="outline" class="min-h-11" :disabled="busy || !config" @click="previewConfiguration"><LoaderCircle v-if="action === 'preview-config'" class="size-4 animate-spin" />检查配置与代理出口</Button>
          <div v-if="configurationPreview" class="space-y-2 border-t border-border pt-3 text-sm" role="status">
            <p class="m-0 font-semibold" :class="configReady ? 'text-success-accent' : 'text-warning-accent'">{{ configReady ? '配置检查通过，可以生成安装命令' : '请先处理以下问题' }}</p>
            <ul v-if="configurationPreview.blockers?.length" class="my-1 list-disc space-y-1 pl-5 text-warning-accent"><li v-for="item in configurationPreview.blockers" :key="item">{{ item }}</li></ul>
            <p v-for="item in configurationPreview.warnings || []" :key="item" class="m-0 text-xs text-warning-accent">{{ item }}</p>
            <details v-if="configurationPreview.changes?.length" class="text-xs text-muted-foreground"><summary class="min-h-11 cursor-pointer content-center py-2">查看配置变更</summary><ul class="mt-0 mb-2 list-disc space-y-1 pl-5"><li v-for="item in configurationPreview.changes" :key="item">{{ item }}</li></ul></details>
          </div>
        </section>

        <section v-else-if="step === 2" class="space-y-4" aria-label="安装核心">
          <div><h2 class="m-0 text-lg font-semibold">安装核心与 Agent</h2><p class="mt-1 mb-0 text-sm text-muted-foreground">在目标 Linux 执行命令。已有标准安装会先校验并保留，缺少的服务会自动安装。</p></div>
          <p class="m-0 text-xs text-warning-accent">安装命令含临时凭据，请勿公开分享。完成检查前，请勿修改客户端网关或 DNS。</p>
          <FormField label="安装命令" html-for="onboarding-command"><Textarea id="onboarding-command" :model-value="installation?.command || ''" readonly :rows="4" class="min-h-28 max-h-48 font-mono text-xs" /></FormField>
          <div class="flex flex-wrap items-center gap-x-3 gap-y-1">
            <Button class="min-h-11" :disabled="!installation?.command || installation.expired" @click="copyCommand"><Copy class="size-4" />复制安装命令</Button>
            <span class="text-xs text-muted-foreground">使用 root，或按提示输入 sudo 密码。</span>
          </div>
          <p class="m-0 break-words text-xs text-muted-foreground">{{ commandExpiry }}</p>
          <div class="space-y-3 border-y border-border py-3 text-sm">
            <p class="m-0 font-semibold">{{ installation ? installationPhaseLabel(installation.phase) : '等待安装记录' }}</p>
            <p v-if="installation?.message" class="m-0 break-words text-xs text-muted-foreground">{{ installation.message }}</p>
            <div v-for="service in (['mihomo', 'oxidns'] as const)" :key="service" class="flex min-w-0 flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
              <p class="m-0 font-medium">{{ service === 'mihomo' ? 'Mihomo' : 'OxiDNS' }} Agent</p>
              <p class="m-0 min-w-0 break-words text-xs text-muted-foreground">{{ installation?.agents[service]?.name || '等待登记' }} · {{ installation?.agents[service]?.status || '未登记' }}</p>
            </div>
          </div>
          <div class="flex flex-wrap items-center gap-x-3 gap-y-1">
            <Button variant="outline" class="min-h-11" :disabled="refreshPending || busy" @click="refreshInstallation"><RefreshCw class="size-4" :class="{ 'animate-spin': refreshPending }" />检查安装进度</Button>
            <span class="text-xs text-muted-foreground">登记成功后，进入发布步骤。</span>
          </div>
          <details class="text-xs text-muted-foreground"><summary class="min-h-11 cursor-pointer content-center py-2 font-medium">安装失败时</summary><p class="mt-1 mb-0 leading-relaxed">按终端提示处理后，在同一台 Linux 重跑原命令。凭据过期再重新准备安装；系统没有 sudo 时，先切换 root。已完成登记则直接继续发布，无需重装。</p></details>
        </section>

        <section v-else-if="step === 3" class="space-y-4" aria-label="发布并检查">
          <div><h2 class="m-0 text-lg font-semibold">发布配置</h2><p class="mt-1 mb-0 text-sm text-muted-foreground">先发布并检查 Mihomo，再发布 OxiDNS；失败时会停止。</p></div>
          <div class="divide-y divide-border border-y border-border">
            <div v-for="service in (['mihomo', 'oxidns'] as const)" :key="service" class="space-y-2 py-3">
              <div class="flex min-w-0 flex-wrap items-baseline justify-between gap-2"><h3 class="m-0 text-sm font-semibold">{{ service === 'mihomo' ? 'Mihomo' : 'OxiDNS' }}</h3><span class="min-w-0 break-words text-xs text-muted-foreground">{{ installation?.agents[service]?.name }}</span></div>
              <AgentDeploymentStatus v-if="installation?.deployments[service]" :task="installation.deployments[service]!" />
              <p v-else class="m-0 text-xs text-muted-foreground">{{ service === 'oxidns' ? '等待 Mihomo 健康检查通过' : '已登记，等待发布' }}</p>
            </div>
          </div>
          <p v-if="installation?.error || installation?.message" class="m-0 break-words text-sm" :class="installation?.phase === 'publish_failed' ? 'text-warning-accent' : 'text-muted-foreground'" role="status">{{ installation.error || installation.message }}</p>
          <div class="flex flex-wrap gap-2"><Button class="min-h-11" :disabled="busy || installationBlocked || !installation || !['enrolled', 'publishing', 'publish_failed'].includes(installation.phase)" @click="publish"><LoaderCircle v-if="action === 'publish'" class="size-4 animate-spin" />{{ installation?.phase === 'publish_failed' ? '处理后继续发布' : installation?.phase === 'publishing' ? '继续确认发布' : '按顺序发布两项配置' }}</Button><Button variant="outline" class="min-h-11" :disabled="refreshPending || busy" @click="refreshInstallation"><RefreshCw class="size-4" />刷新进度</Button></div>
          <p class="m-0 text-xs leading-relaxed text-muted-foreground">关闭窗口后发布仍会继续，重新打开可查看进度。失败时在 Agent 查看原因，处理后继续发布，无需重装。</p>
        </section>

        <section v-else class="space-y-4" aria-label="启用与验证">
          <div><h2 class="m-0 text-lg font-semibold">{{ networkRemoved ? '网关转发已撤销' : activeNetwork ? '验证客户端' : '启用网关转发' }}</h2><p class="mt-1 mb-0 text-sm text-muted-foreground">{{ networkRemoved ? '请恢复客户端接入前的设置。' : activeNetwork ? '先测试一台设备，再接入其他客户端。' : '服务检查已通过。先检查实际网络，再应用转发。' }}</p></div>
          <dl class="m-0 grid grid-cols-2 gap-3 border-y border-border py-3 text-xs sm:grid-cols-4"><div class="min-w-0"><dt class="text-muted-foreground">Linux IPv4</dt><dd class="mx-0 mt-1 break-all font-mono">{{ network.address }}</dd></div><div class="min-w-0"><dt class="text-muted-foreground">主路由</dt><dd class="mx-0 mt-1 break-all font-mono">{{ network.upstream_gateway }}</dd></div><div class="min-w-0"><dt class="text-muted-foreground">LAN 网卡</dt><dd class="mx-0 mt-1 break-all font-mono">{{ network.interface }}</dd></div><div class="min-w-0"><dt class="text-muted-foreground">LAN 网段</dt><dd class="mx-0 mt-1 break-all font-mono">{{ network.lan_cidr }}</dd></div></dl>
          <p v-if="networkStatus?.unsupported_reason || networkStatus?.config_error || networkStatus?.detection_error || networkStatus?.managed?.error" role="alert" class="m-0 text-sm text-warning-accent">{{ networkStatus.unsupported_reason || networkStatus.config_error || networkStatus.detection_error || networkStatus.managed?.error }}</p>
          <div class="flex flex-wrap gap-2"><Button variant="outline" class="min-h-11" :disabled="!canNetwork || busy" @click="previewNetwork"><LoaderCircle v-if="action === 'preview-network'" class="size-4 animate-spin" />检查并预览转发</Button><Button class="min-h-11" :disabled="!canApply" @click="applyNetwork"><LoaderCircle v-if="action === 'apply'" class="size-4 animate-spin" />应用网关转发</Button></div>
          <div v-if="networkPreview" class="space-y-2 border-t border-border pt-3 text-sm" role="status">
            <p class="m-0 font-semibold" :class="networkPreview.ready ? 'text-success-accent' : 'text-warning-accent'">{{ networkPreview.ready ? '检查通过，等待应用' : '请处理以下检查结果' }}</p>
            <p v-for="item in [...(networkPreview.blockers || []), ...(networkPreview.warnings || [])]" :key="item" class="m-0 break-words text-xs text-warning-accent">{{ item }}</p>
            <ul v-if="networkPreview.commands?.length" class="my-1 list-disc space-y-1 pl-5 text-xs text-muted-foreground"><li v-for="item in networkPreview.commands" :key="item" class="break-words">{{ item }}</li></ul>
          </div>
          <p v-if="activeNetwork" class="m-0 flex items-center gap-2 text-sm font-medium text-success-accent" role="status"><Check class="size-4 shrink-0" />网关转发已启用</p>
          <div v-if="activeNetwork" class="space-y-3">
            <h3 class="m-0 text-sm font-semibold">客户端接入检查</h3>
            <ol class="m-0 list-decimal space-y-2 pl-5 text-sm leading-relaxed">
              <li>记下测试设备原来的网关、DNS、IPv6 和安全 DNS 设置。将 <strong>网关</strong> 和 <strong>DNS</strong> 都填为 <code class="font-mono">{{ network.address }}</code>，设备 IP 保持在原网段。</li>
              <li>暂时关闭测试设备的 IPv6、浏览器安全 DNS 和系统私有 DNS，避免绕过分流。</li>
              <li>分别访问直连站点和代理域名，在 Mihomo 连接列表确认代理访问。</li>
              <li>测试通过后再接入其他设备。支持 DHCP 分发网关与 DNS 的主路由，可将两项都设为 <code class="font-mono">{{ network.address }}</code>。</li>
            </ol>
          </div>
          <div v-else-if="networkRemoved" class="space-y-2 text-sm" role="status">
            <p class="m-0 font-medium text-success-accent">请恢复客户端原来的设置</p>
            <p class="m-0 leading-relaxed">恢复原网关和 DNS（原来自动获取的改回自动获取），以及测试前的 IPv6、浏览器安全 DNS 和系统私有 DNS。修改过主路由 DHCP 时，也恢复原值。</p>
            <p class="m-0 text-xs leading-relaxed text-muted-foreground">核心和已发布配置仍保留。撤销转发不会关闭 Mihomo TUN；要停用，请到「生成工作台 → Mihomo → Linux 网关」关闭模式，再保存并发布。</p>
          </div>
          <p v-else class="m-0 text-xs leading-relaxed text-muted-foreground">应用成功后再修改客户端。接入前记下原网关、DNS、IPv6 和安全 DNS 设置，以便恢复。</p>
          <div class="space-y-2 border-t border-border pt-3 text-xs text-muted-foreground">
            <p v-if="activeNetwork || networkStatus?.managed?.pending" class="m-0">撤销前，请先恢复客户端原来的网关与 DNS。</p>
            <Button v-if="activeNetwork || networkStatus?.managed?.pending" variant="outline" class="min-h-11" :disabled="busy || !canInspectNetwork" @click="removeNetwork">撤销本次网关转发</Button>
            <Button v-else variant="ghost" class="min-h-11 px-2" :disabled="busy || !canInspectNetwork" @click="inspectNetwork">重新读取网络状态</Button>
            <p class="m-0 leading-relaxed">后续管理：生成工作台 → Mihomo → Linux 网关。未选 IP、应用自带 DoH 或绕过网关的流量不自动代理。</p>
          </div>
        </section>
        </TabsContent>
      </Tabs>
    </div>
    <template #footer>
      <div class="flex min-w-0 flex-wrap items-center justify-between gap-2">
        <p class="m-0 shrink-0 text-xs text-muted-foreground"><span class="num">{{ step + 1 }} / 5</span><span class="hidden sm:inline"> · {{ steps[step] }}</span></p>
        <div class="ml-auto flex min-w-0 flex-wrap justify-end gap-2">
          <Button v-if="step === 1 && !locked" variant="outline" class="min-h-11" :disabled="busy" @click="step = 0">上一步</Button>
          <Button v-if="step === 0" class="min-h-11" :disabled="busy || !config" @click="continueToRules">下一步：选择规则</Button>
          <Button v-else-if="step === 1" class="min-h-11" :disabled="busy || !configReady || !!environmentErrors.length" @click="createInstallation"><LoaderCircle v-if="action === 'create'" class="size-4 animate-spin" />保存并生成安装命令</Button>
          <Button v-else variant="outline" class="min-h-11" :disabled="busy" @click="emit('update:open', false)">{{ activeNetwork || networkRemoved ? '完成，返回 Agent' : '稍后继续' }}</Button>
        </div>
      </div>
    </template>
  </SettingsWorkspace>
</template>
