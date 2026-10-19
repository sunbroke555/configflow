<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { Check, ChevronRight, Copy, LoaderCircle, Network, RefreshCw, RotateCcw } from '@lucide/vue'
import { agentApi, profileApi, type PublishGuard } from '@/api'
import { linuxGatewayApi, type LinuxGatewayCandidate, type LinuxGatewayPreview, type LinuxGatewaySettings, type LinuxGatewayStatus } from '@/api/linuxGateway'
import { cleanGatewaySpec, emptyGatewaySpec, gatewayErrorMessage, gatewaySettingsKey, gatewaySpecErrors, gatewaySpecKey } from '@/lib/linuxGateway'
import { confirm, notify } from '@/lib/feedback'
import SettingsWorkspace from './SettingsWorkspace.vue'
import SettingsPublishBar from './SettingsPublishBar.vue'
import FormField from '@/components/common/FormField.vue'
import InfoNote from '@/components/common/InfoNote.vue'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'

type Agent = { id: string; name: string; profile_id?: string; service_type: string; status?: string; enabled?: boolean; deployment_method?: string }
type Profile = { id: string; name: string; is_default?: boolean }
const props = defineProps<{ open: boolean; profileId: string }>()
const emit = defineEmits<{ 'update:open': [value: boolean] }>()
const settings = reactive<LinuxGatewaySettings>({ enabled: false, rule_set_ids: [] })
const savedSettings = ref<LinuxGatewaySettings>({ enabled: false, rule_set_ids: [] })
const candidates = ref<LinuxGatewayCandidate[]>([])
const fakeIPRange = ref('')
const warnings = ref<string[]>([])
const refreshInterval = ref(300)
const search = ref('')
const agents = ref<Agent[]>([])
const profiles = ref<Profile[]>([])
const selectedAgentId = ref('')
const network = reactive(emptyGatewaySpec())
const networkBaseline = ref(gatewaySpecKey(network))
const status = ref<LinuxGatewayStatus | null>(null)
const preview = ref<LinuxGatewayPreview | null>(null)
const previewKey = ref('')
const loading = ref(false)
const saving = ref(false)
const networkAction = ref<'' | 'status' | 'preview' | 'apply' | 'remove'>('')
const loadError = ref('')
const targetError = ref('')
const networkError = ref('')
const networkResult = ref('')
let session = 0
let hostSession = 0
let disposed = false
const profileName = computed(() => profiles.value.find(item => item.id === props.profileId)?.name || props.profileId)
const defaultProfileId = computed(() => profiles.value.find(item => item.is_default)?.id || 'default')
const targets = computed(() => agents.value.filter(item => item.service_type === 'mihomo'
  && (item.profile_id || defaultProfileId.value) === props.profileId))
const selectedAgent = computed(() => targets.value.find(item => item.id === selectedAgentId.value))
const online = computed(() => selectedAgent.value?.status === 'online' && selectedAgent.value?.enabled !== false)
const shell = computed(() => selectedAgent.value?.deployment_method !== 'docker')
const managedNetwork = computed(() => !!(status.value?.managed?.active || status.value?.managed?.pending))
const configDirty = computed(() => gatewaySettingsKey(settings) !== gatewaySettingsKey(savedSettings.value))
const networkDirty = computed(() => !!selectedAgentId.value && gatewaySpecKey(network) !== networkBaseline.value)
const busy = computed(() => loading.value || saving.value || !!networkAction.value)
const configured = computed(() => savedSettings.value.enabled && settings.enabled && !configDirty.value)
const errors = computed(() => gatewaySpecErrors(network))
const currentPreviewKey = computed(() => JSON.stringify([props.profileId, selectedAgentId.value, gatewaySpecKey(network), gatewaySettingsKey(settings)]))
const canPreview = computed(() => configured.value && online.value && shell.value && !!status.value && status.value.supported !== false && !busy.value && errors.value.length === 0)
const canApply = computed(() => canPreview.value && status.value?.enabled_in_config === true && preview.value?.ready === true && previewKey.value === currentPreviewKey.value)
const rows = computed(() => {
  const all = [...candidates.value]
  for (const id of settings.rule_set_ids) {
    if (!all.some(item => item.id === id)) all.push({ id, name: `已移除的规则集（${id}）`, behavior: '', available: false, enabled: false, description: '接管范围已排除此项，可取消勾选清理关联。' })
  }
  return all.filter(item => `${item.name} ${item.policy || ''}`.toLowerCase().includes(search.value.trim().toLowerCase()))
})
const selectedAvailable = computed(() => candidates.value.filter(item => item.available && settings.rule_set_ids.includes(item.id)).length)
const capture = () => ({ session, hostSession, profileId: props.profileId, agentId: selectedAgentId.value })
const current = (token: ReturnType<typeof capture>, host = false) => !disposed && props.open && token.session === session
  && token.profileId === props.profileId && (!host || (token.hostSession === hostSession && token.agentId === selectedAgentId.value))

async function load() {
  const token = capture()
  loading.value = true
  loadError.value = ''
  targetError.value = ''
  const [configResult, agentsResult, profilesResult] = await Promise.allSettled([
    linuxGatewayApi.get(token.profileId), agentApi.getAll(), profileApi.list(),
  ])
  if (!current(token)) return
  if (configResult.status === 'fulfilled') {
    const data = configResult.value.data
    Object.assign(settings, { enabled: data.settings.enabled, rule_set_ids: [...data.settings.rule_set_ids] })
    savedSettings.value = { enabled: settings.enabled, rule_set_ids: [...settings.rule_set_ids] }
    candidates.value = data.candidates || []
    fakeIPRange.value = data.fake_ip_range
    warnings.value = data.warnings || []
    refreshInterval.value = data.refresh_interval || 300
  } else loadError.value = gatewayErrorMessage(configResult.reason)
  if (profilesResult.status === 'fulfilled') profiles.value = profilesResult.value.data
  if (agentsResult.status === 'fulfilled' && profilesResult.status === 'fulfilled') {
    agents.value = agentsResult.value.data
    if (targets.value.length === 1) selectedAgentId.value = targets.value[0].id
  } else targetError.value = '暂时无法读取设备或配置空间列表，仍可保存网关配置。请重试读取。'
  loading.value = false
}

async function save(): Promise<PublishGuard | null> {
  if (loading.value || saving.value || loadError.value) throw new Error('配置尚未就绪，请重试读取后再保存。')
  const token = capture()
  const draft = { enabled: settings.enabled, rule_set_ids: [...settings.rule_set_ids] }
  saving.value = true
  try {
    const { data } = await linuxGatewayApi.save(token.profileId, draft)
    if (!current(token)) throw new Error('设置页面已关闭或配置空间已改变。')
    if (data.success === false) throw new Error((data as any).message || '保存失败')
    savedSettings.value = draft
    if (data.candidates) candidates.value = data.candidates
    if (data.warnings) warnings.value = data.warnings
    if (data.fake_ip_range) fakeIPRange.value = data.fake_ip_range
    preview.value = null
    networkResult.value = ''
    return data.publish_guard || null
  } catch (error) {
    if (current(token)) notify.error(`保存网关配置失败：${gatewayErrorMessage(error)}`)
    throw error
  } finally { if (current(token)) saving.value = false }
}

async function reloadTargets() {
  const token = capture()
  loading.value = true
  targetError.value = ''
  try {
    const [agentResponse, profileResponse] = await Promise.all([agentApi.getAll(), profileApi.list()])
    if (!current(token)) return
    profiles.value = profileResponse.data
    agents.value = agentResponse.data
    if (selectedAgentId.value && !targets.value.some(item => item.id === selectedAgentId.value)) selectedAgentId.value = ''
  } catch (error) {
    if (current(token)) targetError.value = `暂时无法读取目标设备：${gatewayErrorMessage(error)}`
  } finally { if (current(token)) loading.value = false }
}

async function saveDraft() {
  try { await save(); if (props.open && !disposed) notify.success('网关草稿已保存。请发布 Mihomo，再继续配置设备网络。') }
  catch { /* The save callback also supplies feedback for SettingsPublishBar. */ }
}

function toggleRule(id: string, checked: boolean | 'indeterminate') {
  settings.rule_set_ids = checked === true ? [...new Set([...settings.rule_set_ids, id])] : settings.rule_set_ids.filter(item => item !== id)
}

async function loadStatus(fill = false) {
  if (!selectedAgentId.value || !online.value || !shell.value) return
  const token = capture()
  networkAction.value = 'status'
  preview.value = null
  networkError.value = ''
  try {
    const { data } = await linuxGatewayApi.status(token.agentId, token.profileId)
    if (!current(token, true)) return
    if (data.success === false) throw new Error(data.message || '设备检测失败')
    status.value = data
    networkError.value = [data.unsupported_reason, data.detection_error, data.config_error, data.managed?.error].filter(Boolean).join('；')
    if (fill) {
      Object.assign(network, emptyGatewaySpec(), data.managed?.spec || data.detection?.suggested || {})
      networkBaseline.value = gatewaySpecKey(network)
    }
  } catch (error) {
    if (current(token, true)) { status.value = null; networkError.value = gatewayErrorMessage(error, true) }
  } finally { if (current(token, true)) networkAction.value = '' }
}

async function previewNetwork() {
  if (!canPreview.value) return
  const token = capture()
  const key = currentPreviewKey.value
  preview.value = null
  networkAction.value = 'preview'
  networkError.value = ''
  networkResult.value = ''
  try {
    const { data } = await linuxGatewayApi.preview(token.agentId, token.profileId, cleanGatewaySpec(network))
    if (!current(token, true) || key !== currentPreviewKey.value) return
    if (data.success === false) throw new Error(data.message || '网络预览失败')
    preview.value = data
    previewKey.value = key
    if (data.managed) status.value = { ...status.value!, managed: data.managed }
  } catch (error) {
    if (current(token, true)) networkError.value = gatewayErrorMessage(error, true)
  } finally { if (current(token, true)) networkAction.value = '' }
}

async function applyNetwork() {
  if (!canApply.value) return
  const token = capture()
  const key = currentPreviewKey.value
  networkAction.value = 'apply'
  networkError.value = ''
  preview.value = null
  try {
    // Only this explicit action can mutate the host; saves and mode switches cannot.
    const { data } = await linuxGatewayApi.apply(token.agentId, token.profileId, cleanGatewaySpec(network))
    if (!current(token, true) || key !== currentPreviewKey.value) return
    if (!data.success) throw new Error(data.message || '应用网关网络失败')
    status.value = { ...status.value!, ...data }
    networkBaseline.value = gatewaySpecKey(network)
    preview.value = null
    networkResult.value = data.message || '网关网络已应用。现在可以按第 5 步测试一台客户端。'
    notify.success('Linux 网关网络已应用')
  } catch (error) {
    if (current(token, true)) {
      const failure = error as any
      if (failure?.response?.data?.managed) status.value = { ...status.value!, managed: failure.response.data.managed }
      networkError.value = `${gatewayErrorMessage(error, true)} 请刷新检测，确认设备实际状态后再操作。`
    }
  } finally { if (current(token, true)) networkAction.value = '' }
}

async function removeNetwork() {
  if (busy.value || !selectedAgentId.value || !online.value) return
  const token = capture()
  const accepted = await confirm('请先恢复客户端原来的网关和 DNS；原先自动获取的设备恢复自动获取。此操作会移除 ConfigFlow 管理的转发规则，并恢复它接管的系统参数。', {
    title: `恢复「${selectedAgent.value?.name}」的网关网络？`, confirmText: '恢复网络', cancelText: '取消',
  })
  if (!accepted || !current(token, true) || busy.value) return
  networkAction.value = 'remove'
  networkError.value = ''
  try {
    const { data } = await linuxGatewayApi.remove(token.agentId, token.profileId)
    if (!current(token, true)) return
    if (!data.success) throw new Error(data.message || '恢复网关网络失败')
    status.value = { ...status.value!, ...data }
    preview.value = null
    networkBaseline.value = gatewaySpecKey(network)
    networkResult.value = data.message || '网关网络已恢复。接下来关闭选择性网关模式，再保存并发布 Mihomo。'
    notify.success('Linux 网关网络已恢复')
  } catch (error) {
    if (current(token, true)) networkError.value = `${gatewayErrorMessage(error, true)} 请刷新检测确认设备状态。`
  } finally { if (current(token, true)) networkAction.value = '' }
}

async function copyCommands() {
  try { await navigator.clipboard.writeText((preview.value?.commands || []).join('\n')); notify.success('已复制操作说明') }
  catch { notify.error('复制失败，请手动选择操作说明复制') }
}

watch(() => [props.open, props.profileId], () => {
  session++
  hostSession++
  loading.value = false
  saving.value = false
  networkAction.value = ''
  selectedAgentId.value = ''
  status.value = null
  preview.value = null
  networkError.value = ''
  networkResult.value = ''
  search.value = ''
  Object.assign(network, emptyGatewaySpec())
  networkBaseline.value = gatewaySpecKey(network)
  if (props.open) void load()
}, { immediate: true, flush: 'sync' })

watch(selectedAgentId, () => {
  hostSession++
  status.value = null
  preview.value = null
  networkAction.value = ''
  networkError.value = ''
  networkResult.value = ''
  Object.assign(network, emptyGatewaySpec())
  networkBaseline.value = gatewaySpecKey(network)
  if (props.open && selectedAgentId.value) void loadStatus(true)
}, { flush: 'sync' })
watch(currentPreviewKey, () => { preview.value = null; previewKey.value = '' }, { flush: 'sync' })
onBeforeUnmount(() => { disposed = true; session++; hostSession++ })
</script>

<template>
  <SettingsWorkspace :open="open" title="Linux 选择性网关" description="适用于同一局域网内、以 Shell 安装的 Linux + Mihomo + OxiDNS。按步骤完成规则、DNS、系统转发和客户端设置，无需主路由静态路由。" :dirty="configDirty || networkDirty" :busy="busy" :save-disabled="!!loadError || loading" size="form" @update:open="emit('update:open', $event)" @save="saveDraft">
    <div class="min-h-0 min-w-0 flex-1 overflow-y-auto overscroll-contain px-4 py-4 md:px-5 [overflow-wrap:anywhere]">
      <div v-if="loading" role="status" class="flex items-center gap-2 py-8 text-sm text-muted-foreground"><LoaderCircle class="size-4 animate-spin" />正在读取网关配置…</div>
      <div v-else-if="loadError" role="alert" class="space-y-3 text-sm"><p class="text-destructive-accent">{{ loadError }}</p><Button variant="outline" class="min-h-11" @click="load">重试读取</Button></div>
      <div v-else class="space-y-6">
        <p class="m-0 text-xs text-muted-foreground">配置空间「{{ profileName }}」。网关模式与规则选择由此空间的 Mihomo 设备共用；网络地址仅应用到你选择的 Linux 设备。</p>
        <InfoNote><p>直连流量由 Linux 内核转发；fake-IP 和勾选规则集中的目标 IPv4 进入 Mihomo，再按现有规则决定出口。所有上网流量仍经过这台 Linux。</p><p>主路由无需静态路由。先手动设置一台客户端，成功后再考虑通过 DHCP 下发网关和 DNS。</p></InfoNote>

        <section aria-labelledby="gateway-step-rules" class="space-y-3">
          <h3 id="gateway-step-rules" class="flex items-center gap-2 text-sm font-semibold"><span class="gateway-step">1</span>开启模式，选择接管规则</h3>
          <label for="linux-gateway-enabled" class="flex min-h-11 cursor-pointer items-center justify-between gap-4 rounded-lg border border-border bg-card px-3 py-3">
            <span><span class="block text-sm font-medium">启用 Linux 选择性网关</span><span class="mt-1 block text-xs text-muted-foreground">默认关闭。保存后仅生成此模式的 Mihomo 配置，应用设备网络需要单独操作。</span></span>
            <Switch id="linux-gateway-enabled" v-model="settings.enabled" :disabled="busy" />
          </label>
          <p v-if="!settings.enabled && savedSettings.enabled" class="text-xs leading-relaxed text-warning-accent">准备停用：先按第 5 步恢复客户端和设备网络，再保存并发布已关闭模式的 Mihomo 配置。</p>
          <div :class="{ 'opacity-70': !settings.enabled }" class="space-y-3">
            <div class="flex min-h-11 items-center gap-2 rounded-lg border border-border bg-muted px-3 text-xs"><Check class="size-4 shrink-0 text-success-accent" /><span>自动包含 fake-IP 网段 <code class="font-mono">{{ fakeIPRange || '由 Mihomo DNS 配置确定' }}</code>，不需要勾选。</span></div>
            <p class="m-0 text-xs leading-relaxed text-muted-foreground">只选择“直接使用真实 IP，也必须进入代理”的服务，例如 Telegram 的目标 IP 集合。不要把中国 IP、全网 0.0.0.0/0 或普通直连规则一并选入。这里决定接管范围，不会改变规则的出口策略。</p>
            <Input v-model="search" aria-label="搜索可接管的 IP 规则集" placeholder="搜索规则集名称或策略" class="min-h-11" :disabled="busy" />
            <div class="max-h-72 overflow-y-auto rounded-lg border border-border">
              <label v-for="item in rows" :key="item.id" :for="`gateway-rule-${item.id}`" class="flex min-h-11 cursor-pointer items-start gap-3 border-b border-border px-3 py-3 last:border-b-0 hover:bg-accent/40">
                <Checkbox :id="`gateway-rule-${item.id}`" class="mt-1" :model-value="settings.rule_set_ids.includes(item.id)" :disabled="busy || !settings.enabled || (!item.available && !settings.rule_set_ids.includes(item.id))" @update:model-value="toggleRule(item.id, $event)" />
                <span class="min-w-0 flex-1 text-xs leading-relaxed"><span class="block text-sm font-medium">{{ item.name }}</span><span class="text-muted-foreground">{{ item.behavior === 'classical' ? '仅提取目的 IPv4，忽略域名、来源 IP 等其他规则' : '目的 IP 规则集' }}<template v-if="item.policy"> · 出口策略：{{ item.policy }}</template></span><span v-if="!item.available" class="block text-warning-accent">{{ item.reason || item.description || '已停用或不可用于接管；当前不会进入接管范围。' }}</span><span v-else-if="item.policy?.toUpperCase() === 'DIRECT'" class="block text-warning-accent">当前策略为 DIRECT：接管后仍会由 Mihomo 直连，请确认是否需要。</span></span>
              </label>
              <p v-if="!rows.length" class="m-0 px-3 py-5 text-xs text-muted-foreground">{{ search ? '没有匹配的规则集。' : '当前空间没有可用 IP 规则集。仍可只接管 fake-IP；需要时先在「规则库」添加 IP 集合，再到当前空间的「策略规则」关联并启用。' }}</p>
            </div>
            <p class="m-0 text-xs text-muted-foreground">已选 <span class="font-mono">{{ selectedAvailable }}</span> 个可用规则集。按规则集 ID 关联，改名不丢失选择；内容与停用状态随接管规则刷新同步（约 {{ Math.ceil(refreshInterval / 60) }} 分钟）。新增勾选需要重新保存并发布 Mihomo。</p>
          </div>
          <ul v-if="warnings.length" class="m-0 space-y-1 rounded-lg bg-warning-soft px-4 py-3 text-xs leading-relaxed text-warning-accent"><li v-for="warning in warnings" :key="warning">{{ warning }}</li></ul>
        </section>

        <section aria-labelledby="gateway-step-publish" class="space-y-3 border-t border-border pt-5">
          <h3 id="gateway-step-publish" class="flex items-center gap-2 text-sm font-semibold"><span class="gateway-step">2</span>保存并发布 Mihomo</h3>
          <p class="m-0 text-xs leading-relaxed text-muted-foreground">在底部选择这台 Linux 的 Mihomo 设备，点击「保存并发布」。等待“发布成功”；如果显示“待激活”，还需要点击「应用并启动」。此步骤自动生成 TUN 接管配置，保留原有节点与策略，无需手改基础 YAML。</p>
          <p class="m-0 text-xs leading-relaxed text-muted-foreground">首次安装或接入已有核心：到「Agent → 生成安装脚本」，选择「Linux 选择性网关」，按向导完成核心安装、双 Agent 登记与配置发布；已有标准目录核心会先校验再接入。请使用 Mihomo 稳定版 v1.19.0 或更新版本、Agent 1.8.25-go 或更新版本，并提供 root 权限、TUN 和 nftables；容器不在此向导的支持范围。</p>
          <p class="m-0 text-xs leading-relaxed text-muted-foreground">普通模式默认接入已有核心，也可勾选安装核心；网关安装向导会继续完成分流配置与转发设置。系统设置中的服务地址需要能被 Linux 访问；启用登录时还需配置订阅令牌，供真实 IP 规则自动刷新。</p>
        </section>

        <section aria-labelledby="gateway-step-dns" class="space-y-3 border-t border-border pt-5">
          <h3 id="gateway-step-dns" class="flex items-center gap-2 text-sm font-semibold"><span class="gateway-step">3</span>配置并发布 OxiDNS</h3>
          <p class="m-0 text-xs leading-relaxed text-muted-foreground">返回生成工作台，打开「OxiDNS → 设置」核对下表；需要调整时，保存并发布到同一台 Linux 的 OxiDNS 设备。网关安装向导已自动准备两项服务的配置；本管理页不自动改写 OxiDNS 设置。</p>
          <dl class="gateway-dns rounded-lg border border-border text-xs">
            <dt>客户端 DNS 入口</dt><dd><code>0.0.0.0:53</code>，同时开启 UDP 和 TCP</dd>
            <dt>本地 / 直连上游</dt><dd>使用 DoH / DoT，例如 <code>https://223.5.5.5/dns-query</code>。避免普通 UDP / TCP 53 被 Mihomo 的 DNS 劫持接管；需要时填写拨号 IP / 引导 IP。</dd>
            <dt>远程 / 代理上游</dt><dd><code>127.0.0.1:1053</code></dd>
            <dt>代理备用上游</dt><dd>同样填写 <code>127.0.0.1:1053</code>，避免失败后返回真实 IP 绕过接管</dd>
            <dt>域名规则选择</dt><dd>国内 / 内网域名使用本地上游；需要代理的域名使用远程上游，获得 fake-IP。与第 1 步选择的真实 IP 集合分别配置。</dd>
          </dl>
          <InfoNote><p>Mihomo 的 DNS 上游不能再指回 OxiDNS，否则会循环。代理域名也不能被 Mihomo 的 fake-IP 过滤规则改为真实 IP。</p><p>第 1 步只增加接管范围；Mihomo 仍按规则顺序选择出口。请确认代理规则对应的策略组已选择可用代理节点。</p></InfoNote>
        </section>

        <section aria-labelledby="gateway-step-network" class="space-y-3 border-t border-border pt-5">
          <h3 id="gateway-step-network" class="flex items-center gap-2 text-sm font-semibold"><span class="gateway-step">4</span>检测并应用 Linux 网络</h3>
          <p class="m-0 text-xs leading-relaxed text-muted-foreground">选择已完成第 2 步发布的 Mihomo 设备。自动读取实际地址供你核对，随后预览转发、NAT 和持久化配置；只有点击「应用网关网络」才会执行。</p>
          <div v-if="targetError" role="alert" class="text-xs text-destructive-accent">{{ targetError }} <Button variant="ghost" class="min-h-11" :disabled="busy" @click="reloadTargets">重试读取</Button></div>
          <FormField label="承担网关的 Linux 设备" html-for="gateway-agent">
            <Select v-model="selectedAgentId" :disabled="busy"><SelectTrigger id="gateway-agent" class="min-h-11 w-full"><SelectValue placeholder="选择当前空间的 Mihomo 设备" /></SelectTrigger><SelectContent><SelectItem v-for="agent in targets" :key="agent.id" :value="agent.id">{{ agent.name }}{{ agent.enabled === false ? '（已禁用）' : agent.status !== 'online' ? '（离线）' : '' }}</SelectItem></SelectContent></Select>
          </FormField>
          <p v-if="!targets.length && !targetError" class="text-xs text-muted-foreground">此空间尚未绑定 Mihomo 设备。请先在 Agent 页面完成安装和绑定；规则设置仍可保存草稿。</p>
          <template v-if="selectedAgentId">
            <p v-if="!online" class="text-xs text-warning-accent">设备离线或已禁用，无法检测和修改网络。保存草稿仍可使用。</p>
            <p v-else-if="!shell" class="text-xs text-warning-accent">Docker 设备无法通过此向导修改宿主机网络。请选择 Linux Shell 安装的设备。</p>
            <div class="flex min-w-0 flex-wrap items-center justify-between gap-2">
              <span role="status" class="text-xs" :class="managedNetwork ? (status?.managed.pending || status?.managed.healthy === false ? 'text-warning-accent' : 'text-success-accent') : 'text-muted-foreground'">{{ networkAction === 'status' ? '正在检测设备网络…' : status?.managed?.pending ? '应用未完成，需要恢复或重新检查' : status?.managed?.active ? (status.managed.healthy === false ? '已管理，检测到网络配置需要修复' : '已应用 ConfigFlow 网关网络') : status ? '尚未应用网关网络' : '等待设备检测' }}</span>
              <Button variant="outline" class="min-h-11" :disabled="busy || !online || !shell" @click="loadStatus()"><RefreshCw class="size-3.5" />刷新检测</Button>
            </div>
            <div class="grid min-w-0 grid-cols-1 gap-3 sm:grid-cols-2">
              <FormField label="LAN 网卡" html-for="gateway-interface" hint="Linux 与客户端、主路由相连的实际网卡，例如 eth0 或 enp1s0。"><Input id="gateway-interface" v-model="network.interface" :disabled="busy || !status" class="min-h-11 font-mono" placeholder="eth0" autocomplete="off" /></FormField>
              <FormField label="Linux 局域网 IPv4" html-for="gateway-address" hint="客户端以后会使用此地址作为默认网关和 DNS。请先为 Linux 设置固定地址。"><Input id="gateway-address" v-model="network.address" :disabled="busy || !status" class="min-h-11 font-mono" placeholder="192.168.1.2" inputmode="decimal" /></FormField>
              <FormField label="LAN 网段（CIDR）" html-for="gateway-cidr" hint="例如设备 192.168.1.x、子网掩码 255.255.255.0，对应 192.168.1.0/24。"><Input id="gateway-cidr" v-model="network.lan_cidr" :disabled="busy || !status" class="min-h-11 font-mono" placeholder="192.168.1.0/24" /></FormField>
              <FormField label="上游主路由 IPv4" html-for="gateway-upstream" hint="Linux 自己的默认网关仍指向主路由。本向导不会修改 Linux 的固定 IP 或默认路由。"><Input id="gateway-upstream" v-model="network.upstream_gateway" :disabled="busy || !status" class="min-h-11 font-mono" placeholder="192.168.1.1" inputmode="decimal" /></FormField>
            </div>
            <ul v-if="status && errors.length" class="m-0 space-y-1 text-xs text-warning-accent"><li v-for="error in errors" :key="error">{{ error }}</li></ul>
            <p v-if="!configured" class="m-0 text-xs text-warning-accent">先开启模式并保存规则设置，再发布 Mihomo。存在未保存规则修改时不能预览或应用设备网络。</p>
            <p v-else-if="status && !status.enabled_in_config" class="m-0 text-xs text-warning-accent">设备尚未检测到已部署的选择性网关配置。请先发布并激活 Mihomo，再刷新检测；预览会列出需要解决的问题。</p>
            <div class="flex flex-wrap gap-2"><Button variant="outline" class="min-h-11" :disabled="!canPreview" @click="previewNetwork"><LoaderCircle v-if="networkAction === 'preview'" class="size-4 animate-spin" /><Network v-else class="size-4" />预览并检查</Button><Button class="min-h-11" :disabled="!canApply" @click="applyNetwork"><LoaderCircle v-if="networkAction === 'apply'" class="size-4 animate-spin" />应用网关网络<ChevronRight v-if="networkAction !== 'apply'" class="size-3.5" /></Button></div>
          </template>
          <div v-if="networkError" role="alert" class="rounded-lg bg-destructive-soft px-3 py-3 text-xs leading-relaxed text-destructive-accent">{{ networkError }}</div>
          <div v-if="networkResult" role="status" class="rounded-lg bg-success-soft px-3 py-3 text-xs leading-relaxed text-success-accent">{{ networkResult }}</div>
          <div v-if="preview" class="space-y-3 rounded-lg border border-border bg-card p-3">
            <p class="m-0 text-sm font-medium">{{ preview.ready ? '检查通过，可以应用' : '先解决以下问题，再重新检查' }}</p>
            <ul v-if="preview.blockers?.length" class="m-0 space-y-1 text-xs text-destructive-accent"><li v-for="blocker in preview.blockers" :key="blocker">{{ blocker }}</li></ul>
            <ul v-if="preview.warnings?.length" class="m-0 space-y-1 text-xs text-warning-accent"><li v-for="warning in preview.warnings" :key="warning">{{ warning }}</li></ul>
            <details v-if="preview.commands?.length"><summary class="min-h-11 cursor-pointer py-3 text-xs font-medium">查看网络操作预览</summary><p class="m-0 text-xs text-muted-foreground">以下为 Agent 将执行的操作说明，不是可粘贴到终端的 Shell 脚本。</p><div class="flex justify-end"><Button variant="ghost" class="min-h-11" @click="copyCommands"><Copy class="size-3.5" />复制操作说明</Button></div><pre class="max-h-64 overflow-auto whitespace-pre-wrap rounded-md bg-muted p-3 font-mono text-[11px] leading-relaxed">{{ preview.commands.join('\n') }}</pre></details>
          </div>
        </section>

        <section aria-labelledby="gateway-step-clients" class="space-y-3 border-t border-border pt-5">
          <h3 id="gateway-step-clients" class="flex items-center gap-2 text-sm font-semibold"><span class="gateway-step">5</span>连接一台客户端，验证后再推广</h3>
          <ol class="m-0 list-decimal space-y-2 pl-5 text-xs leading-relaxed text-muted-foreground"><li>在一台客户端的网络设置中，把默认网关和 DNS 都改为 <code class="text-foreground">{{ network.address || 'Linux 的局域网 IP' }}</code>；Linux 本身继续使用主路由 <code>{{ network.upstream_gateway || '的 IP' }}</code> 作为默认网关。</li><li>关闭客户端自身代理，刷新 DNS 缓存或重新连接网络。先测试一个直连网站，再测试一个被 OxiDNS 代理域名规则命中的网站。</li><li>直连域名应返回真实 IP，流量不出现在 Mihomo 连接中；代理域名应返回 fake-IP，连接出现在 Mihomo。再测试勾选的真实 IP 服务，确认连接命中预期策略。</li><li>单设备成功后，可以逐台设置；若主路由 DHCP 支持自定义网关和 DNS，可下发 Linux 地址。主路由不支持时继续手动设置客户端。</li></ol>
          <InfoNote><p>此向导先接管 IPv4。测试客户端若仍通过主路由获得 IPv6 默认路由，IPv6 可能绕过；先关闭该客户端 IPv6，或另行部署 IPv6 网关。</p><p>应用自带 DoH / DoT、缓存的真实 IP、未选入规则集的真实 IP 不会自动代理。测试时关闭浏览器“安全 DNS”，并检查应用是否使用系统 DNS。</p></InfoNote>
          <details class="rounded-lg border border-border px-3"><summary class="min-h-11 cursor-pointer py-3 text-xs font-medium">停用和恢复步骤</summary><div class="space-y-3 pb-3 text-xs leading-relaxed text-muted-foreground"><p class="m-0">先恢复客户端原来的网关和 DNS（原先自动获取的设备恢复自动获取），再恢复为测试暂时关闭的 IPv6、浏览器安全 DNS 或系统私有 DNS；然后在下面恢复选中 Linux 的网关网络；最后关闭第 1 步开关，保存并发布 Mihomo。恢复操作只处理 ConfigFlow 管理的网络配置，不卸载核心。</p><Button variant="outline" class="min-h-11" :disabled="busy || !online || !shell || !managedNetwork" @click="removeNetwork"><LoaderCircle v-if="networkAction === 'remove'" class="size-4 animate-spin" /><RotateCcw v-else class="size-3.5" />恢复所选设备网络</Button><p v-if="!managedNetwork" class="m-0">当前设备没有已应用或未完成的 ConfigFlow 网关网络，或尚未完成状态检测。</p></div></details>
        </section>
      </div>
    </div>
    <template #footer="{ disabled }"><SettingsPublishBar :open="open" :profile-id="profileId" service-type="mihomo" :disabled="disabled || !!loadError || loading" :save="save" @save-draft="saveDraft" /></template>
  </SettingsWorkspace>
</template>

<style scoped>
.gateway-step { display: inline-flex; align-items: center; justify-content: center; flex-shrink: 0; width: 24px; height: 24px; border-radius: var(--radius); background: var(--primary-soft); color: var(--primary-accent); font-family: var(--font-mono); font-size: 12px; }
.gateway-dns { display: grid; grid-template-columns: minmax(120px, 1fr) minmax(0, 3fr); overflow: hidden; }
.gateway-dns dt, .gateway-dns dd { margin: 0; padding: 12px; border-bottom: 1px solid var(--border); line-height: 1.6; }
.gateway-dns dt { background: var(--muted); font-weight: 500; }
.gateway-dns dd { color: var(--muted-foreground); }
.gateway-dns dt:nth-last-child(2), .gateway-dns dd:last-child { border-bottom: 0; }
@media (max-width: 639px) { .gateway-dns { grid-template-columns: minmax(0, 1fr); } .gateway-dns dt { border-bottom: 0; padding-bottom: 4px; } .gateway-dns dd { padding-top: 8px; } }
</style>
