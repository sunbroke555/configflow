<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { Check, Copy, LoaderCircle, RefreshCw } from '@lucide/vue'
import { gatewayOnboardingApi, type GatewayConfigurationPreview, type GatewayInstallation, type GatewayOnboardingConfig, type GatewayOnboardingSettings } from '@/api/gatewayOnboarding'
import { linuxGatewayApi, type LinuxGatewayCandidate, type LinuxGatewayPreview, type LinuxGatewayStatus } from '@/api/linuxGateway'
import { cleanGatewaySpec, emptyGatewaySpec, gatewayErrorMessage, gatewaySpecKey } from '@/lib/linuxGateway'
import { gatewayEnvironmentErrors, installationHostMatches, installationMatches, installationPhaseLabel, onboardingSettingsKey } from '@/lib/gatewayOnboarding'
import { notify } from '@/lib/feedback'
import SettingsWorkspace from '@/components/generate/SettingsWorkspace.vue'
import AgentDeploymentStatus from '@/components/agents/AgentDeploymentStatus.vue'
import FormField from '@/components/common/FormField.vue'
import InfoNote from '@/components/common/InfoNote.vue'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'

const props = defineProps<{ open: boolean; profileId: string; profileName: string }>()
const emit = defineEmits<{ 'update:open': [value: boolean]; 'agents-changed': [] }>()
const steps = ['设备与网络', '分流规则', '安装核心', '发布并检查', '启用与验证']
const step = ref(0)
const contentPane = ref<HTMLElement | null>(null)
const config = ref<GatewayOnboardingConfig | null>(null)
const settings = reactive<GatewayOnboardingSettings>({ rule_set_ids: [], direct_rule_set_ids: [], proxy_rule_set_ids: [], local_dns: 'https://223.5.5.5/dns-query' })
const network = reactive(emptyGatewaySpec())
const name = ref('home-gateway')
const serverUrl = ref('')
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
const configReady = computed(() => configurationPreview.value?.ready === true && configurationPreviewKey.value === settingsKey.value)
const configLists = computed(() => [
  { field: 'rule_set_ids' as const, title: '真实 IP 接管范围', hint: '选需要代理的 IP 集合，例如 Telegram。中国 IP、局域网和直连集合通常不用选；只需要 fake-IP 时可留空。', rows: config.value?.ip_candidates || [] },
  { field: 'direct_rule_set_ids' as const, title: '直连域名', hint: '选择国内或明确直连的域名集合，由本地加密 DNS 返回真实 IP。', rows: config.value?.domain_candidates || [] },
  { field: 'proxy_rule_set_ids' as const, title: '代理域名', hint: '选择国外或需代理的域名集合，交给 Mihomo 返回 fake-IP。最终代理节点仍由 Mihomo 的策略规则决定。', rows: config.value?.domain_candidates || [] },
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
function schedulePoll() {
  stopPolling()
  if (!props.open || !installation.value || installation.value.phase === 'published' || installation.value.phase === 'publish_failed') return
  poll = setTimeout(() => { void refreshInstallation() }, 4000)
}
function acceptInstallation(value: GatewayInstallation, restore = false) {
  if (!value?.id || value.profile_id !== props.profileId || !value.network || !value.agents || !value.deployments || (!installationMatches(value, props.profileId) && !value.target_changed) || (installation.value && installation.value.id !== value.id)) throw new Error('安装记录与当前配置空间或目标设备不一致，已停止操作。')
  const phaseChanged = installation.value?.phase !== value.phase
  installation.value = value
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
    notify.success('两项服务配置已保存，复制安装命令到目标 Linux 执行。')
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
async function copyCommand() {
  if (!installation.value?.command || installation.value.expired) return
  try { await navigator.clipboard.writeText(installation.value.command); notify.success('安装命令已复制。') }
  catch { notify.error('浏览器未允许复制，请选中命令手动复制。') }
}
watch(step, () => { void nextTick(() => contentPane.value?.scrollTo?.({ top: 0 })) })
watch(() => networkStatus.value?.managed, managed => { if (managed?.active || managed?.pending) networkRemoved.value = false })
watch(settingsKey, () => { configurationPreview.value = null; savedConfiguration.value = null })
watch(() => [props.open, props.profileId] as const, () => {
  generation++; stopPolling(); installation.value = null; config.value = null; configurationPreview.value = null; savedConfiguration.value = null
  networkStatus.value = null; networkPreview.value = null; networkRemoved.value = false; error.value = ''; pollError.value = ''; resumed.value = false; search.value = ''
  loading.value = false; action.value = ''; refreshPending.value = false; baseline.value = ''; step.value = 0
  Object.assign(network, emptyGatewaySpec()); name.value = 'home-gateway'
  Object.assign(settings, { rule_set_ids: [], direct_rule_set_ids: [], proxy_rule_set_ids: [], local_dns: 'https://223.5.5.5/dns-query', confirm_template_conversion: false })
  try { serverUrl.value = localStorage.getItem('serverDomain') || window.location.origin } catch { serverUrl.value = window.location.origin }
  if (props.open) void load()
}, { immediate: true, flush: 'sync' })
onBeforeUnmount(() => { disposed = true; generation++; stopPolling() })
</script>

<template>
  <SettingsWorkspace :open="open" title="安装 Linux 选择性网关" description="按步骤安装 Mihomo、OxiDNS 与 Agent，再启用经过检查的网关转发。" back-label="返回 Agent 列表" :dirty="dirty" :busy="busy" size="form" @update:open="emit('update:open', $event)">
    <div ref="contentPane" class="min-h-0 min-w-0 flex-1 overflow-y-auto overscroll-contain" data-gateway-install-wizard>
      <div class="mx-auto w-full min-w-0 max-w-4xl space-y-5 p-3 sm:p-6">
        <div class="space-y-2">
          <p class="m-0 break-words text-xs text-muted-foreground">配置空间 <span class="font-semibold text-foreground">{{ profileName }}</span> · Linux 原生安装 · IPv4 单网卡</p>
          <ol class="m-0 flex list-none gap-1 overflow-x-auto p-0" aria-label="安装步骤">
            <li v-for="(label, index) in steps" :key="label" class="min-w-0 sm:flex-1" :class="index === step ? 'flex-[2]' : 'flex-1'">
              <button class="flex min-h-11 w-full items-center justify-center gap-1 rounded-md px-1 text-xs font-medium outline-none focus-visible:ring-2 focus-visible:ring-primary-accent disabled:cursor-not-allowed sm:gap-2" :class="index === step ? 'bg-primary-soft text-primary-accent' : 'text-muted-foreground hover:bg-muted'" :aria-current="index === step ? 'step' : undefined" :disabled="busy || index > availableStep || (locked && index < 2)" @click="step = index">
                <span class="num shrink-0">{{ index + 1 }}</span><span class="hidden sm:inline">{{ label }}</span><span v-if="index === step" class="whitespace-nowrap sm:hidden">{{ label }}</span>
              </button>
            </li>
          </ol>
        </div>
        <p v-if="loading" role="status" class="flex items-center gap-2 text-sm text-muted-foreground"><LoaderCircle class="size-4 animate-spin" />正在读取当前配置与安装进度…</p>
        <div v-if="error" class="rounded-lg border border-destructive/30 bg-destructive-soft p-3 text-sm text-destructive-accent" role="alert">{{ error }}<Button v-if="!config && !installation" variant="ghost" class="min-h-11" :disabled="busy" @click="load">重新读取</Button></div>
        <p v-if="pollError" class="m-0 break-words text-sm text-warning-accent" role="alert">{{ pollError }}</p>
        <div v-if="installationBlocked" role="alert" class="rounded-lg border border-warning/30 bg-warning-soft p-3 text-sm text-warning-accent">
          <p class="m-0">{{ installation?.target_changed ? '本次安装的设备绑定或身份已改变。' : installation?.config_changed ? '配置空间已在本次安装之后修改。' : '安装凭据已过期。' }} 已停止发布与转发操作，请重新准备安装配置。</p>
          <Button variant="outline" class="mt-2 min-h-11" :disabled="busy" @click="startAnotherInstallation">重新准备安装</Button>
        </div>
        <InfoNote v-if="installation"><p>{{ resumed ? '已恢复' : '已建立' }}「{{ installation.name }}」的安装记录，只操作此记录关联的两个 Agent。重新打开此入口会继续当前空间最近一次安装；其他已安装设备请到「生成工作台 → Mihomo → Linux 网关」管理。</p><p>地址或网卡填错时可重新准备；原安装和发布不会被取消，已安装服务会先检查。</p><Button variant="ghost" class="mt-2 min-h-11 px-2" :disabled="busy" @click="startAnotherInstallation">修改安装信息 / 准备另一台设备</Button></InfoNote>

        <section v-if="step === 0" class="space-y-5" aria-label="设备与网络">
          <div><h2 class="m-0 font-display text-2xl">先准备一台固定地址的 Linux</h2><p class="mt-2 mb-0 text-sm leading-relaxed text-muted-foreground">支持 x86_64 / ARM64、systemd / OpenRC（含 Alpine）。电脑与主路由在同一网段；请先在主路由保留此 IP 或在 Linux 配置静态 IP。向导会检查实际地址，不会替你改 IP 或默认路由。</p></div>
          <InfoNote><p>主路由无需静态路由。客户端以后将网关与 DNS 都设为这台 Linux；Linux 自己仍通过主路由上网。请先准备代理订阅、策略组和域名规则。</p></InfoNote>
          <InfoNote><p>新设备会安装缺少的核心与系统服务。已有标准目录的 Mihomo / OxiDNS 可直接接入：先校验并保留现有核心、配置与服务，到第 4 步才发布新配置。</p><p>接入客户端前，请记下原来的网关、DNS、IPv6 和安全 DNS 设置，方便测试后恢复。</p></InfoNote>
          <div class="grid min-w-0 gap-4 sm:grid-cols-2">
            <FormField label="设备名称" html-for="onboarding-name" hint="两个 Agent 会由同一安装记录关联。"><Input id="onboarding-name" v-model="name" class="min-h-11" placeholder="home-gateway" /></FormField>
            <FormField label="ConfigFlow 服务地址" html-for="onboarding-server" hint="需与系统设置中的服务地址一致，并能从 Linux 访问；若是 localhost，请先到系统设置修改。"><Input id="onboarding-server" v-model="serverUrl" class="min-h-11 font-mono text-xs" placeholder="http://192.168.1.10:8000" /></FormField>
            <FormField label="Linux 的固定 IPv4" html-for="onboarding-address" hint="例如 192.168.1.2；不是主路由的地址。"><Input id="onboarding-address" v-model="network.address" class="min-h-11 font-mono" placeholder="192.168.1.2" /></FormField>
            <FormField label="主路由 IPv4" html-for="onboarding-upstream" hint="Linux 当前使用的默认网关，例如 192.168.1.1。"><Input id="onboarding-upstream" v-model="network.upstream_gateway" class="min-h-11 font-mono" placeholder="192.168.1.1" /></FormField>
            <FormField label="LAN 网卡名" html-for="onboarding-interface" hint="在 Linux 执行 ip -br address 查找带上述 IPv4 的网卡。"><Input id="onboarding-interface" v-model="network.interface" class="min-h-11 font-mono" placeholder="eth0 或 enp1s0" /></FormField>
            <FormField label="LAN 网段" html-for="onboarding-cidr" hint="例如地址 192.168.1.2/24 对应网段 192.168.1.0/24。"><Input id="onboarding-cidr" v-model="network.lan_cidr" class="min-h-11 font-mono" placeholder="192.168.1.0/24" /></FormField>
          </div>
          <details class="rounded-lg border border-border p-3 text-sm"><summary class="cursor-pointer font-medium">不知道这些值在哪里看？</summary><p class="mb-2 text-muted-foreground">通过 SSH 登录 Linux，执行下面两条只读命令。第一条找网卡与地址；第二条中 default via 后是主路由，dev 后是网卡。</p><pre class="overflow-x-auto rounded bg-muted p-3 text-xs">ip -br address
ip route show default</pre><p class="mb-2 text-muted-foreground">标准目录指核心在 /usr/local/bin/mihomo 与 /usr/local/bin/oxidns，配置在 /etc/mihomo/config.yaml 与 /etc/oxidns/config.yaml，并有对应的 mihomo / oxidns 系统服务。已有核心需能通过版本与配置检查。</p><p class="mb-0 text-muted-foreground">遇到端口冲突、安装不完整或不兼容的服务布局时，会停止并说明原因；非标准目录请通过普通模式填写现有路径接入 Agent。Linux 需提供 /dev/net/tun 和网络管理权限，不支持普通受限容器。</p></details>
          <details class="rounded-lg border border-border p-3 text-sm"><summary class="cursor-pointer font-medium">安装脚本提示缺少依赖时</summary><p class="my-2 text-xs text-muted-foreground">先在 Linux 安装依赖，再重新执行本向导生成的命令。Debian / Ubuntu：</p><pre class="overflow-x-auto rounded bg-muted p-3 text-xs">sudo apt-get update &amp;&amp; sudo apt-get install -y ca-certificates curl iproute2 nftables tar gzip coreutils</pre><p class="my-2 text-xs text-muted-foreground">Alpine（以 root 执行）：</p><pre class="overflow-x-auto rounded bg-muted p-3 text-xs">apk add ca-certificates curl iproute2 nftables tar gzip coreutils</pre></details>
          <div v-if="environmentErrors.length" class="text-xs leading-relaxed text-muted-foreground"><p class="m-0 font-medium">继续前需要填写：</p><ul class="my-1 list-disc space-y-1 pl-5"><li v-for="item in environmentErrors" :key="item">{{ item }}</li></ul></div>
        </section>

        <section v-else-if="step === 1" class="space-y-5" aria-label="分流规则">
          <div><h2 class="m-0 font-display text-2xl">选规则，同时配置两项服务</h2><p class="mt-2 mb-0 text-sm leading-relaxed text-muted-foreground">直连真实 IP 走 Linux 内核转发；fake-IP 与选中的真实 IP 交给 Mihomo。这里保存的是「{{ profileName }}」内共用的配置，建议使用专用网关空间。</p></div>
          <InfoNote><p>自动包含 fake-IP 网段 <code class="break-all font-mono">{{ config?.fake_ip_range || '由 Mihomo 配置决定' }}</code>，无需手动勾选。IP 集合使用稳定 ID，后续更新、改名或停用会同步。</p></InfoNote>
          <FormField label="搜索规则集" html-for="onboarding-search"><Input id="onboarding-search" v-model="search" class="min-h-11" placeholder="按规则集名称或策略搜索" /></FormField>
          <div v-for="list in configLists" :key="list.field" class="min-w-0 space-y-2">
            <h3 class="m-0 text-sm font-semibold">{{ list.title }} <span class="num font-normal text-muted-foreground">{{ settings[list.field].length }} 项</span></h3><p class="m-0 text-xs leading-relaxed text-muted-foreground">{{ list.hint }}</p>
            <div class="max-h-56 overflow-y-auto rounded-lg border border-border">
              <label v-for="item in list.visibleRows" :key="item.id" class="flex min-h-11 cursor-pointer items-start gap-3 border-b border-border px-3 py-2.5 last:border-b-0 hover:bg-muted" :class="{ 'opacity-70': !item.available && !settings[list.field].includes(item.id) }">
                <Checkbox :model-value="settings[list.field].includes(item.id)" :disabled="busy || (!item.available && !settings[list.field].includes(item.id))" class="mt-0.5 shrink-0" @update:model-value="toggle(list.field, item.id, $event)" />
                <span class="min-w-0 space-y-0.5"><span class="block break-all text-sm font-medium">{{ item.name }}</span><span class="block break-words text-xs text-muted-foreground">{{ item.policy ? `策略：${item.policy} · ` : '' }}{{ item.reason || item.description || item.behavior }}</span></span>
              </label>
              <p v-if="!list.rows.length" class="m-0 p-3 text-xs text-muted-foreground">当前没有已引用的规则集。先到「规则库」添加，再在此空间的「策略规则」引用并启用，然后重新打开向导读取。</p>
              <p v-else-if="!list.visibleRows.length" class="m-0 p-3 text-xs text-muted-foreground" role="status">没有匹配的规则集，请调整或清空搜索词。已勾选的规则会保留。</p>
            </div>
          </div>
          <FormField label="OxiDNS 的直连加密 DNS" html-for="onboarding-dns" hint="默认可直接使用。可按现有 OxiDNS 格式填写多行加密上游；普通 53 端口上游可能被网关接管形成回环。" persistent-hint><Textarea id="onboarding-dns" v-model="settings.local_dns" :disabled="busy" class="min-h-24 font-mono text-xs" :rows="3" /></FormField>
          <InfoNote><p>将自动配置：OxiDNS 在本机 53 端口服务局域网；代理与后备 DNS 指向 Mihomo 的 127.0.0.1:1053；Mihomo 启用 fake-IP 和选择性 TUN。未知域名也交给 Mihomo 处理。</p></InfoNote>
          <label v-if="config?.oxidns_mode === 'native'" class="flex min-h-11 cursor-pointer items-start gap-3 rounded-lg border border-border p-3 text-xs leading-relaxed"><Checkbox v-model="settings.confirm_template_conversion" :disabled="busy" class="mt-0.5 shrink-0" /><span>仅当现有原生 OxiDNS 配置无法安全保留时，允许转换为向导模板。转换会替换原生解析流程；不勾选时会停止并说明原因。</span></label>
          <div v-if="config?.warnings?.length || config?.blockers?.length" class="space-y-2 text-xs text-warning-accent"><p v-for="item in [...(config?.warnings || []), ...(config?.blockers || [])]" :key="item" class="m-0 break-words">{{ item }}</p></div>
          <Button variant="outline" class="min-h-11" :disabled="busy || !config" @click="previewConfiguration"><LoaderCircle v-if="action === 'preview-config'" class="size-4 animate-spin" />检查配置与代理出口</Button>
          <div v-if="configurationPreview" class="space-y-2 rounded-lg border border-border p-3 text-sm" role="status">
            <p class="m-0 font-semibold" :class="configReady ? 'text-success-accent' : 'text-warning-accent'">{{ configReady ? '配置检查通过，可以生成安装命令' : '请先处理以下问题' }}</p>
            <ul v-if="configurationPreview.blockers?.length" class="my-1 list-disc space-y-1 pl-5 text-warning-accent"><li v-for="item in configurationPreview.blockers" :key="item">{{ item }}</li></ul>
            <p v-for="item in configurationPreview.warnings || []" :key="item" class="m-0 text-xs text-warning-accent">{{ item }}</p>
            <ul v-if="configurationPreview.changes?.length" class="my-1 list-disc space-y-1 pl-5 text-xs text-muted-foreground"><li v-for="item in configurationPreview.changes" :key="item">{{ item }}</li></ul>
          </div>
        </section>

        <section v-else-if="step === 2" class="space-y-5" aria-label="安装核心">
          <div><h2 class="m-0 font-display text-2xl">在 Linux 执行这一条命令</h2><p class="mt-2 mb-0 text-sm leading-relaxed text-muted-foreground">脚本会检查环境，为新设备安装缺少的核心与系统服务；已有完整标准安装会保留核心和配置，直接接入两个 Agent。已有完整核心不会重复下载；安装期间保持 Linux 网络可用。</p></div>
          <InfoNote><p>目标设备：<strong>{{ name }}</strong> · <code class="break-all font-mono">{{ network.address }}</code>。此时先不要修改其他设备的网关或 DNS，转发将在最后一步启用。</p></InfoNote>
          <FormField label="一键安装命令" html-for="onboarding-command" hint="在目标 Linux 的终端粘贴整条命令。root 可直接运行；普通用户会调用 sudo，请按提示输入密码。系统没有 sudo 时，先切换到 root 再运行。命令包含安装凭据，请勿公开分享。" persistent-hint><Textarea id="onboarding-command" :model-value="installation?.command || ''" readonly :rows="5" class="font-mono text-xs" /></FormField>
          <Button variant="outline" class="min-h-11" :disabled="!installation?.command || installation.expired" @click="copyCommand"><Copy class="size-4" />复制安装命令</Button>
          <div class="rounded-lg border border-border p-3 text-sm"><p class="m-0 font-semibold">{{ installation ? installationPhaseLabel(installation.phase) : '等待安装记录' }}</p><p class="mt-2 mb-0 break-words text-muted-foreground">{{ installation?.message || '安装完成后，两个 Agent 会自动出现在下一步。' }}</p><p class="mt-2 mb-0 break-words text-xs text-muted-foreground">{{ commandExpiry }} 如果终端显示失败，请按提示处理后，在同一台 Linux 重新运行同一条命令；凭据过期时再重新准备安装。完成登记后回到此处继续发布。</p></div>
          <div class="grid gap-2 sm:grid-cols-2"><div v-for="service in (['mihomo', 'oxidns'] as const)" :key="service" class="rounded-lg border border-border p-3 text-sm"><p class="m-0 font-medium">{{ service === 'mihomo' ? 'Mihomo' : 'OxiDNS' }} Agent</p><p class="mt-1 mb-0 break-words text-xs text-muted-foreground">{{ installation?.agents[service]?.name || '等待此安装命令登记' }} · {{ installation?.agents[service]?.status || '未登记' }}</p></div></div>
          <Button variant="outline" class="min-h-11" :disabled="refreshPending || busy" @click="refreshInstallation"><RefreshCw class="size-4" :class="{ 'animate-spin': refreshPending }" />检查安装进度</Button>
        </section>

        <section v-else-if="step === 3" class="space-y-5" aria-label="发布并检查">
          <div><h2 class="m-0 font-display text-2xl">先让两项服务正常工作</h2><p class="mt-2 mb-0 text-sm leading-relaxed text-muted-foreground">按顺序发布本次保存的配置：先启动并检查 Mihomo，再发布 OxiDNS。任何一步失败都会停下；通过检查后才进入转发设置。</p></div>
          <div v-for="service in (['mihomo', 'oxidns'] as const)" :key="service" class="space-y-3 rounded-lg border border-border p-4">
            <div class="flex min-w-0 items-center justify-between gap-2"><h3 class="m-0 text-sm font-semibold">{{ service === 'mihomo' ? '1 · Mihomo' : '2 · OxiDNS' }}</h3><span class="min-w-0 truncate text-xs text-muted-foreground">{{ installation?.agents[service]?.name }}</span></div>
            <AgentDeploymentStatus v-if="installation?.deployments[service]" :task="installation.deployments[service]!" />
            <p v-else class="m-0 text-sm text-muted-foreground">{{ service === 'oxidns' ? '等待 Mihomo 健康检查通过' : '已登记，等待发布' }}</p>
            <p v-if="installation?.deployments[service]?.error || installation?.deployments[service]?.message" class="m-0 break-words text-xs text-muted-foreground">{{ installation.deployments[service]?.error || installation.deployments[service]?.message }}</p>
          </div>
          <p v-if="installation?.error || installation?.message" class="m-0 break-words text-sm" :class="installation?.phase === 'publish_failed' ? 'text-warning-accent' : 'text-muted-foreground'" role="status">{{ installation.error || installation.message }}</p>
          <div class="flex flex-wrap gap-2"><Button class="min-h-11" :disabled="busy || installationBlocked || !installation || !['enrolled', 'publishing', 'publish_failed'].includes(installation.phase)" @click="publish"><LoaderCircle v-if="action === 'publish'" class="size-4 animate-spin" />{{ installation?.phase === 'publish_failed' ? '处理后继续发布' : installation?.phase === 'publishing' ? '继续确认发布' : '按顺序发布两项配置' }}</Button><Button variant="outline" class="min-h-11" :disabled="refreshPending || busy" @click="refreshInstallation"><RefreshCw class="size-4" />刷新进度</Button></div>
          <InfoNote><p>关闭窗口不会取消已经提交的发布。此入口恢复当前空间最近一次安装；未新建其他安装时，可回来继续查看本次进度。失败时可到 Agent 页面查看具体错误，处理后继续发布，无需重新运行安装命令。</p></InfoNote>
        </section>

        <section v-else class="space-y-5" aria-label="启用与验证">
          <div><h2 class="m-0 font-display text-2xl">{{ networkRemoved ? '网关转发已撤销' : activeNetwork ? '验证一台客户端' : '最后，启用转发并验证一台设备' }}</h2><p class="mt-2 mb-0 text-sm leading-relaxed text-muted-foreground">{{ networkRemoved ? '本向导管理的网络设置已恢复。请将客户端恢复为接入前的设置。' : activeNetwork ? '转发已启用。先完成单台设备测试，再接入其他客户端。' : 'Mihomo 与 OxiDNS 的配置和健康检查已完成。现在检查 Linux 的实际网络，再明确应用转发。' }}</p></div>
          <dl class="grid grid-cols-2 gap-3 rounded-lg border border-border p-3 text-xs"><div><dt class="text-muted-foreground">Linux / 客户端网关</dt><dd class="mx-0 mt-1 break-all font-mono">{{ network.address }}</dd></div><div><dt class="text-muted-foreground">主路由</dt><dd class="mx-0 mt-1 break-all font-mono">{{ network.upstream_gateway }}</dd></div><div><dt class="text-muted-foreground">LAN 网卡</dt><dd class="mx-0 mt-1 break-all font-mono">{{ network.interface }}</dd></div><div><dt class="text-muted-foreground">LAN 网段</dt><dd class="mx-0 mt-1 break-all font-mono">{{ network.lan_cidr }}</dd></div></dl>
          <p v-if="networkStatus?.unsupported_reason || networkStatus?.config_error || networkStatus?.detection_error || networkStatus?.managed?.error" role="alert" class="text-sm text-warning-accent">{{ networkStatus.unsupported_reason || networkStatus.config_error || networkStatus.detection_error || networkStatus.managed?.error }}</p>
          <div class="flex flex-wrap gap-2"><Button variant="outline" class="min-h-11" :disabled="!canNetwork || busy" @click="previewNetwork"><LoaderCircle v-if="action === 'preview-network'" class="size-4 animate-spin" />检查并预览转发</Button><Button class="min-h-11" :disabled="!canApply" @click="applyNetwork"><LoaderCircle v-if="action === 'apply'" class="size-4 animate-spin" />应用网关转发</Button></div>
          <div v-if="networkPreview" class="space-y-2 rounded-lg border border-border p-3 text-sm" role="status"><p class="m-0 font-semibold" :class="networkPreview.ready ? 'text-success-accent' : 'text-warning-accent'">{{ networkPreview.ready ? '检查通过，以下操作等待你应用' : '暂时不能应用，请处理检查结果' }}</p><p v-for="item in [...(networkPreview.blockers || []), ...(networkPreview.warnings || [])]" :key="item" class="m-0 break-words text-xs text-warning-accent">{{ item }}</p><ul class="my-1 list-disc space-y-1 pl-5 text-xs text-muted-foreground"><li v-for="item in networkPreview.commands || []" :key="item" class="break-words">{{ item }}</li></ul></div>
          <div v-if="activeNetwork" class="flex items-start gap-2 rounded-lg bg-success-soft p-3 text-sm text-success-accent" role="status"><Check class="mt-0.5 size-4 shrink-0" /><span>网关转发已启用。请按下面步骤验证客户端。</span></div>
          <div v-if="activeNetwork" class="space-y-3 rounded-lg border border-border p-4"><h3 class="m-0 text-sm font-semibold">客户端接入检查</h3><ol class="m-0 list-decimal space-y-3 pl-5 text-sm leading-relaxed"><li>先选一台手机或电脑，记下原来的网关、DNS、IPv6、浏览器安全 DNS 和系统私有 DNS 设置，再把 <strong>网关</strong> 和 <strong>DNS</strong> 都填为 <code class="font-mono">{{ network.address }}</code>；设备 IP 仍留在原 LAN 网段。</li><li>暂时关闭这台测试设备的 IPv6、浏览器安全 DNS 和系统私有 DNS，避免绕过本次 IPv4 与 OxiDNS 分流。</li><li>访问一个直连站点和一个所选代理域名。代理访问应出现在 Mihomo 连接列表；普通直连流量由 Linux 转发。</li><li>确认都可用后，再逐台修改其他设备。若主路由支持 DHCP 分发网关和 DNS，可将两项都设为 <code class="font-mono">{{ network.address }}</code>。</li></ol></div>
          <div v-else-if="networkRemoved" class="space-y-3 rounded-lg border border-success/30 bg-success-soft p-4 text-sm" role="status"><p class="m-0 font-semibold text-success-accent">网关转发已撤销，请恢复客户端原来的设置</p><p class="m-0">恢复原网关和 DNS（原来自动获取的就改回自动获取），并恢复测试前的 IPv6、浏览器安全 DNS 和系统私有 DNS 设置；若修改过主路由 DHCP，也恢复其原值。</p><p class="m-0">核心服务与已发布的配置仍保留。要停用选择性网关配置，请到「生成工作台 → Mihomo → Linux 网关」关闭模式，再保存并发布到这台 Mihomo；单独撤销转发不会关闭 Mihomo 的 TUN。</p></div>
          <InfoNote v-else><p>请先检查并应用转发，成功后再修改客户端。接入前记下原网关、DNS、IPv6 和安全 DNS 设置；测试失败时可按原值恢复。</p></InfoNote>
          <InfoNote><p>后续到「生成工作台 → Mihomo → Linux 网关」管理接管规则与转发。未选中 IP 的直连访问不自动代理；应用自带 DoH 或绕过此网关的流量也不在接管范围。</p></InfoNote>
          <div class="space-y-2 text-xs text-muted-foreground"><p v-if="activeNetwork || networkStatus?.managed?.pending" class="m-0">需要恢复时，先将客户端的网关与 DNS 恢复为原值，再撤销此向导管理的转发设置。原来通过 DHCP 自动获取的设备可改回自动获取。</p><Button v-if="activeNetwork || networkStatus?.managed?.pending" variant="outline" class="min-h-11" :disabled="busy || !canInspectNetwork" @click="removeNetwork">撤销本次网关转发</Button><Button v-else variant="ghost" class="min-h-11" :disabled="busy || !canInspectNetwork" @click="inspectNetwork">重新读取网络状态</Button></div>
        </section>
      </div>
    </div>
    <template #footer>
      <div class="flex min-w-0 items-center justify-between gap-2">
        <p class="m-0 min-w-0 text-xs text-muted-foreground"><span class="num">{{ step + 1 }} / 5</span><span class="hidden sm:inline"> · {{ steps[step] }}</span></p>
        <div class="flex min-w-0 gap-2"><Button v-if="step === 1 && !locked" variant="outline" class="min-h-11" :disabled="busy" @click="step = 0">上一步</Button><Button v-if="step === 0" class="min-h-11" :disabled="busy || !config || !!environmentErrors.length" @click="step = 1">下一步：选择规则</Button><Button v-else-if="step === 1" class="min-h-11" :disabled="busy || !configReady || !!environmentErrors.length" @click="createInstallation"><LoaderCircle v-if="action === 'create'" class="size-4 animate-spin" />保存并生成安装命令</Button><Button v-else variant="outline" class="min-h-11" :disabled="busy" @click="emit('update:open', false)">{{ activeNetwork || networkRemoved ? '完成，返回 Agent' : '稍后继续' }}</Button></div>
      </div>
    </template>
  </SettingsWorkspace>
</template>
