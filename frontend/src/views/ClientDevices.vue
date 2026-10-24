<template>
  <div class="min-w-0">
    <PageHeader title="客户端设备" :mobile-help="pageHelp">
      <template #title-extra>
        <span v-if="state && fromAgent" class="min-w-0 max-w-48 truncate rounded-md bg-muted px-2 py-1 text-xs text-muted-foreground" :title="`配置空间：${state.profile_name}`">配置：{{ state.profile_name }}</span>
      </template>
      <template #actions>
        <Button v-if="fromAgent" variant="outline" class="min-h-11" @click="router.push(oxidnsMonitorPath(fromAgent))"><ArrowLeft class="size-4" />返回 OxiDNS 面板</Button>
        <Button variant="outline" class="min-h-11 max-sm:size-11 max-sm:gap-0" aria-label="重新载入" title="重新载入" :disabled="loading || publishing" @click="reload">
          <RefreshCw class="size-4" :class="{ 'animate-spin motion-reduce:animate-none': loading }" /><span class="max-sm:sr-only">重新载入</span>
        </Button>
      </template>
    </PageHeader>
    <p class="mb-5 mt-0 max-w-3xl text-[13px] leading-relaxed text-muted-foreground max-md:hidden">{{ pageHelp }}</p>

    <LoadingRows v-if="loading && !state" :rows="4" />
    <SectionCard v-else-if="!state">
      <EmptyState :icon="MonitorSmartphone" title="暂时无法读取客户端设备" :description="error || '正在读取配置空间。'">
        <Button variant="outline" class="min-h-11" :disabled="loading" @click="load">重试</Button>
      </EmptyState>
    </SectionCard>
    <template v-else>
      <SectionCard title="设备列表" :help="listHelp" class="mb-5">
        <template #actions>
          <span class="flex min-h-11 items-center gap-1.5 text-xs text-muted-foreground" role="status" aria-live="polite">
            <template v-if="savingIp"><Loader2 class="size-3.5 animate-spin motion-reduce:animate-none" />正在保存…</template>
            <template v-else-if="savedOnce"><Check class="size-3.5" />已保存</template>
            <template v-else>修改自动保存</template>
          </span>
        </template>
        <p v-if="!state.editable" role="alert" class="mb-4 mt-0 rounded-lg bg-warning-soft p-3 text-sm text-warning-accent">{{ state.reason }} 名称仍可修改。</p>
        <p v-if="error" role="alert" class="mb-4 mt-0 text-sm text-destructive-accent">{{ error }}</p>
        <div class="mb-4 grid min-w-0 gap-3 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] md:items-end">
          <div class="min-w-0">
            <p class="mb-2 mt-0 text-xs text-muted-foreground">从 OxiDNS 查询记录选择</p>
            <OxiDNSClientSelect :model-value="''" :notes="names" :agent-id="queryAgent" :recorder="recorder" range="all" :allow-all="false"
              :placeholder="queryLoading ? '正在读取查询记录…' : '选择已有客户端…'" @update:model-value="add" />
            <p v-if="queryHint" class="mb-0 mt-1.5 text-xs text-muted-foreground">{{ queryHint }}</p>
          </div>
          <div class="min-w-0">
            <div class="mb-2 flex items-center gap-1">
              <label for="client-device-ip" class="text-xs text-muted-foreground">手动添加 IP / 网段</label>
              <FieldHelp label="手动添加 IP / 网段" :text="rangeHelp" />
            </div>
            <Input id="client-device-ip" v-model="newIP" class="min-h-11 font-mono" placeholder="192.168.50.0/24 或 IPv6" @keydown.enter.prevent="add(newIP)" />
          </div>
          <Button variant="outline" class="min-h-11" :disabled="!newIP.trim()" @click="add(newIP)"><Plus class="size-4" />添加</Button>
        </div>
        <div v-if="rows.length > 8" class="relative mb-3 max-w-sm">
          <Search class="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input v-model="search" class="min-h-11 pl-9" aria-label="搜索设备" placeholder="搜索名称或 IP…" />
        </div>
        <div v-if="visibleRows.length" class="cf-table-wrap">
          <table class="cf-table cf-mobile-records client-device-records w-full min-w-[620px]" role="table">
            <thead role="rowgroup">
              <tr role="row">
                <th role="columnheader" scope="col">IP / 网段</th>
                <th role="columnheader" scope="col">名称</th>
                <th role="columnheader" scope="col"><span class="inline-flex items-center gap-1">DNS 策略<FieldHelp label="DNS 策略" :text="modeHelp" /></span></th>
                <th role="columnheader" scope="col" class="text-right"><span class="sr-only">操作</span></th>
              </tr>
            </thead>
            <tbody role="rowgroup">
              <tr v-for="row in visibleRows" :key="row.ip" role="row" :data-client-ip="row.ip">
                <td role="cell" class="font-mono text-xs" data-label="IP / 网段">
                  {{ row.ip }}
                  <span v-if="!saved(row.ip)" class="chip ml-2 font-sans">未保存</span>
                </td>
                <td role="cell" data-label="名称">
                  <Input v-model="row.name" :aria-label="row.ip + ' 名称'" maxlength="80" placeholder="设备或网段名称" class="min-h-11 min-w-36"
                    :disabled="publishing" @blur="save(row.ip)" @keydown.enter.prevent="save(row.ip)" />
                </td>
                <td role="cell" data-label="DNS 策略">
                  <Select :model-value="row.mode || 'default'" :disabled="publishing || !state.editable" @update:model-value="value => setMode(row, String(value))">
                    <SelectTrigger :aria-label="row.ip + ' DNS 策略'" class="min-h-11 w-36 max-sm:w-full"><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="default">默认</SelectItem>
                      <SelectItem value="smart">智能分流</SelectItem>
                      <SelectItem value="direct">直连</SelectItem>
                    </SelectContent>
                  </Select>
                </td>
                <td role="cell" class="text-right">
                  <div class="flex items-center justify-end gap-2">
                    <Loader2 v-if="savingIp === row.ip" class="size-4 animate-spin text-muted-foreground motion-reduce:animate-none" aria-label="正在保存" />
                    <span v-else-if="failures[row.ip]" class="flex items-center gap-1 text-xs text-destructive-accent" role="alert">
                      {{ failures[row.ip] }}<Button variant="ghost" class="min-h-11 px-2 text-xs" @click="save(row.ip)">重试</Button>
                    </span>
                    <Button variant="ghost" size="icon" class="size-11" :aria-label="'移除 ' + row.ip" :disabled="publishing || (!state.editable && !!saved(row.ip)?.mode)" @click="remove(row)"><Trash2 class="size-4" /></Button>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <p v-else-if="rows.length" class="m-0 py-6 text-center text-sm text-muted-foreground">没有匹配「{{ search }}」的设备。</p>
        <EmptyState v-else :icon="MonitorSmartphone" title="尚未添加客户端设备" description="从 OxiDNS 查询记录选择，或输入 IP / 网段。可为设备命名，或设置 DNS 策略。" />
      </SectionCard>

      <SectionCard title="发布 DNS 策略" :help="publishHelp">
        <p v-if="!state.agents.length" class="m-0 text-sm text-muted-foreground">此配置空间没有 OxiDNS Agent。DNS 策略会保存在草稿中，接入 OxiDNS 并发布后生效。</p>
        <ul v-else class="m-0 list-none divide-y divide-border/70 p-0" aria-label="OxiDNS 发布目标">
          <li v-for="agent in state.agents" :key="agent.id" class="py-2">
            <div class="flex min-h-11 flex-wrap items-center gap-x-3 gap-y-1">
              <Checkbox :model-value="selected.includes(agent.id)" :disabled="publishing" :aria-label="`发布到 ${agent.name}`"
                @update:model-value="value => toggle(agent.id, value === true)" />
              <span class="min-w-0 break-all font-medium">{{ agent.name }}</span>
              <span class="min-w-0 break-all font-mono text-xs text-muted-foreground">{{ agent.host || '—' }}</span>
              <StatusDot :tone="agent.status === 'online' ? 'success' : 'muted'" :label="agent.status === 'online' ? '在线' : '离线'" />
              <Button v-if="deployments[agent.id]?.status === 'ready'" class="ml-auto min-h-11" :disabled="publishing" @click="activate(agent.id)">应用并启动</Button>
            </div>
            <AgentDeploymentStatus v-if="deployments[agent.id]" :task="deployments[agent.id]" compact>
              <template v-if="needsNotice(agent.id)" #notice>
                <AgentOperationSteps :task="deployments[agent.id]" :show-current="false" :show-steps="false" />
                <AgentFailureDetails :show-progress-message="false" :showDNSChecks="false" :task="deployments[agent.id]" />
              </template>
              <template #details><AgentOperationSteps :task="deployments[agent.id]" :show-current="false" :show-message="!needsNotice(agent.id)" embedded /></template>
            </AgentDeploymentStatus>
            <AgentStatusRow v-else-if="rejected[agent.id]" label="配置推送" status="失败" tone="danger">
              <template #notice><AgentFailureDetails :show-progress-message="false" :showDNSChecks="false" :task="rejected[agent.id]" /></template>
            </AgentStatusRow>
          </li>
        </ul>
        <div v-if="state.agents.length" class="mt-4 flex flex-wrap items-center justify-between gap-3 border-t pt-4">
          <p class="m-0 min-w-0 flex-1 text-xs text-muted-foreground">发布的是「{{ state.profile_name }}」的完整已保存配置，包含其他草稿修改。</p>
          <Button class="min-h-11" :disabled="!selected.length || publishing || !!savingIp || selected.some(busy)" @click="publish">
            <Loader2 v-if="publishing" class="size-4 animate-spin" /><Upload v-else class="size-4" />{{ publishing ? '发布中…' : selected.length ? `发布到 ${selected.length} 台 OxiDNS` : '选择要发布的 OxiDNS' }}
          </Button>
        </div>
      </SectionCard>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref } from 'vue'
import { onBeforeRouteLeave, useRoute, useRouter } from 'vue-router'
import { ArrowLeft, Check, Loader2, MonitorSmartphone, Plus, RefreshCw, Search, Trash2, Upload } from '@lucide/vue'
import { agentApi, clientDeviceApi, oxidnsMonitorApi, type ClientDevice, type ClientDeviceMode, type ClientDevicesState } from '@/api'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import PageHeader from '@/components/common/PageHeader.vue'
import SectionCard from '@/components/common/SectionCard.vue'
import LoadingRows from '@/components/common/LoadingRows.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import FieldHelp from '@/components/common/FieldHelp.vue'
import StatusDot from '@/components/common/StatusDot.vue'
import OxiDNSClientSelect from '@/components/agents/OxiDNSClientSelect.vue'
import AgentDeploymentStatus from '@/components/agents/AgentDeploymentStatus.vue'
import AgentOperationSteps from '@/components/agents/AgentOperationSteps.vue'
import AgentFailureDetails from '@/components/agents/AgentFailureDetails.vue'
import AgentStatusRow from '@/components/agents/AgentStatusRow.vue'
import { createDeploymentId, deploymentNeedsNotice, useAgentDeployments, type AgentDeployment } from '@/composables/useAgentDeployments'
import { oxidnsMonitorPath } from '@/composables/useOxiDNSDevices'
import { confirm, confirmDanger, notify } from '@/lib/feedback'
import { normalizeClientRange } from '@/lib/oxidnsClientRange'
import { usePageReloadGuard } from '@/lib/pageReloadGuard'

const pageHelp = '为局域网设备命名，并按需设置 OxiDNS 的 DNS 策略。名称在本配置空间共用，Mihomo 连接、数据统计和 OxiDNS 查询记录都按它显示。'
const listHelp = '修改名称立即生效，不需要发布。DNS 策略保存为草稿，发布到 OxiDNS 后生效。名称和 DNS 策略都清空后，设备从列表移除。'
const modeHelp = [
  '默认：按 DNS 规则处理。',
  '智能分流：同样按 DNS 规则处理，可让「直连」网段中的单个设备例外；IPv4 地址还会作为上游路由「策略路由」的来源。',
  '直连：用本地 DNS 上游解析全部域名；是否走代理仍取决于路由器分流。',
  '范围重叠时，单个 IP 优先，其次是更小的网段，与添加顺序无关。',
].join('\n\n')
const rangeHelp = [
  '支持单个 IPv4 / IPv6 和 CIDR 网段，例如 192.168.50.50、192.168.50.0/24，网段会整理为标准写法。',
  '设备需使用 OxiDNS 作为 DNS，且 OxiDNS 能识别真实客户端 IP；填写地址不会修改设备的 DNS。',
].join('\n\n')
const publishHelp = '只发布到勾选的 OxiDNS。未勾选的设备保持现有配置；同一配置空间的 OxiDNS 共用这份草稿。'

const route = useRoute()
const router = useRouter()
/** 从 OxiDNS 面板进入时固定使用该设备绑定的配置空间，不切换全局空间 */
const fromAgent = typeof route.query.agent === 'string' ? route.query.agent : ''
const state = ref<ClientDevicesState | null>(null)
const rows = ref<ClientDevice[]>([])
const loading = ref(false)
const error = ref('')
const newIP = ref('')
const search = ref('')
const savingIp = ref('')
const savedOnce = ref(false)
const failures = reactive<Record<string, string>>({})
const selected = ref<string[]>([])
const publishing = ref(false)
const rejected = reactive<Record<string, Partial<AgentDeployment>>>({})
const queryAgent = ref(''), recorder = ref(''), queryLoading = ref(false), queryHint = ref('')
let disposed = false
let queue: Promise<unknown> = Promise.resolve()

const { deployments, busy, track, beginSubmission, endSubmission, activate } = useAgentDeployments(() => undefined)
usePageReloadGuard(() => publishing.value || !!savingIp.value || unsaved().length > 0)

const names = computed(() => Object.fromEntries((state.value?.devices || []).filter(row => row.name).map(row => [row.ip, row.name])))
const saved = (ip: string) => state.value?.devices.find(row => row.ip === ip)
const unsaved = () => rows.value.filter(row => {
  const previous = saved(row.ip)
  return previous ? previous.name !== row.name.trim() || previous.mode !== row.mode : !!(row.name.trim() || row.mode)
})
const visibleRows = computed(() => {
  const term = search.value.trim().toLowerCase()
  return term ? rows.value.filter(row => row.ip.includes(term) || row.name.toLowerCase().includes(term)) : rows.value
})
const needsNotice = (id: string) => !!deployments[id] && deploymentNeedsNotice(deployments[id])
const messageOf = (failure: any) => failure?.response?.data?.message || failure?.message || '操作失败，请重试。'

function adopt(data: ClientDevicesState) {
  const latest = new Map(data.devices.map(row => [row.ip, row]))
  const before = new Map((state.value?.devices || []).map(row => [row.ip, row]))
  state.value = data
  // Rows keep their place. Edits still being typed stay, and rows only added
  // here stay until they get a name or a policy; rows removed elsewhere leave.
  const local = rows.value.flatMap(row => {
    const next = latest.get(row.ip), previous = before.get(row.ip)
    const edited = !previous || row.name.trim() !== previous.name || row.mode !== previous.mode
    if (!next) return previous ? [] : [row]
    return [edited && previous ? row : { ...next }]
  })
  const listed = new Set(local.map(row => row.ip))
  rows.value = [...local, ...data.devices.filter(row => !listed.has(row.ip)).map(row => ({ ...row }))]
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const { data } = await clientDeviceApi.get({ agentId: fromAgent || undefined })
    if (disposed) return
    rows.value = []
    state.value = null
    adopt(data)
    const online = data.agents.filter(agent => agent.status === 'online').map(agent => agent.id)
    selected.value = fromAgent ? data.agents.filter(agent => agent.id === fromAgent).map(agent => agent.id) : online
    for (const agent of data.agents) if (agent.deployment) track(agent.id, agent.deployment, false)
    void loadRecorder(data)
  } catch (failure) {
    if (!disposed) error.value = messageOf(failure)
  } finally {
    loading.value = false
  }
}

async function loadRecorder(data: ClientDevicesState) {
  // Query records come from an online OxiDNS, preferably the one this page was opened from.
  const online = data.agents.filter(item => item.status === 'online')
  const agent = online.find(item => item.id === fromAgent) || online[0]
  queryAgent.value = agent?.id || ''
  recorder.value = ''
  queryHint.value = agent ? '' : '没有在线的 OxiDNS，可手动添加 IP 或网段。'
  if (!agent) return
  queryLoading.value = true
  try {
    const { data: overview } = await oxidnsMonitorApi.overview(agent.id)
    if (disposed || queryAgent.value !== agent.id) return
    recorder.value = overview.recorders?.[0] || ''
    if (!recorder.value) queryHint.value = `「${agent.name}」尚无可读取的查询记录，可手动添加 IP 或网段。`
  } catch {
    if (!disposed) queryHint.value = `暂时无法读取「${agent.name}」的查询记录，可手动添加 IP 或网段。`
  } finally {
    queryLoading.value = false
  }
}

async function reload() {
  await queue
  if (!unsaved().length || await confirm('重新载入会丢弃尚未保存的修改。', { title: '重新载入客户端设备', confirmText: '重新载入' })) await load()
}

function add(value: string) {
  let ip = value.trim()
  if (!ip) return
  try {
    ip = normalizeClientRange(ip)
  } catch {
    notify.error('请输入有效的客户端 IPv4、IPv6 或 CIDR 网段，例如 192.168.50.0/24。')
    return
  }
  newIP.value = ''
  search.value = ''
  if (!rows.value.some(row => row.ip === ip)) rows.value.unshift({ ip, name: '', mode: '' })
  else notify.info('此设备已在列表中。')
  void nextTick(() => [...document.querySelectorAll<HTMLElement>('[data-client-ip]')]
    .find(row => row.dataset.clientIp === ip)?.querySelector('input')?.focus())
}

function setMode(row: ClientDevice, value: string) {
  row.mode = (value === 'default' ? '' : value) as ClientDeviceMode
  void save(row.ip)
}

async function remove(row: ClientDevice) {
  const previous = saved(row.ip)
  if (previous && !await confirmDanger(`将清除「${previous.name || row.ip}」的名称${previous.mode ? '和 DNS 策略' : ''}。${previous.mode ? 'DNS 策略的修改需要发布后生效。' : ''}`,
    { title: '移除客户端设备', confirmText: '移除' })) return
  if (!previous) {
    rows.value = rows.value.filter(item => item.ip !== row.ip)
    return
  }
  row.name = ''
  row.mode = ''
  await save(row.ip)
}

/** 逐行保存，排队执行，避免同一行的两次修改乱序 */
function save(ip: string): Promise<unknown> {
  queue = queue.then(() => saveNow(ip))
  return queue
}

async function saveNow(ip: string) {
  const row = rows.value.find(item => item.ip === ip)
  if (!row || !state.value || disposed) return
  const previous = saved(ip) || { name: '', mode: '' as ClientDeviceMode }
  const device = { ip, name: row.name.trim(), mode: row.mode }
  if (device.name === previous.name && device.mode === previous.mode) return
  savingIp.value = ip
  delete failures[ip]
  try {
    const { data } = await clientDeviceApi.save(state.value.profile_id, device, { name: previous.name, mode: previous.mode })
    if (disposed) return
    adopt(data)
    savedOnce.value = true
  } catch (failure: any) {
    if (disposed) return
    if (failure?.response?.status === 409) {
      notify.error('设备已在其他页面修改', messageOf(failure))
      await load()
    } else {
      failures[ip] = messageOf(failure)
    }
  } finally {
    if (savingIp.value === ip) savingIp.value = ''
  }
}

function toggle(id: string, value: boolean) {
  selected.value = value ? [...new Set([...selected.value, id])] : selected.value.filter(item => item !== id)
}

async function publish() {
  await queue
  if (!state.value || publishing.value || Object.keys(failures).length) {
    if (Object.keys(failures).length) notify.error('有设备未保存', '先处理保存失败的设备，再发布。')
    return
  }
  const targets = state.value.agents.filter(agent => selected.value.includes(agent.id))
  const expected = { expected_profile_id: state.value.profile_id, expected_oxidns_revision: state.value.oxidns_revision }
  if (!targets.length || !await confirm(`将发布「${state.value.profile_name}」的完整已保存配置到 ${targets.map(agent => `「${agent.name}」`).join('、')}，并按现有发布流程校验、备份和应用配置。`,
    { title: '发布 DNS 策略', confirmText: '发布' })) return
  publishing.value = true
  try {
    for (const agent of targets) {
      if (disposed) return
      delete rejected[agent.id]
      const deploymentId = createDeploymentId()
      if (!beginSubmission(agent.id, deploymentId)) continue
      try {
        const { data } = await agentApi.pushConfig(agent.id, deploymentId, expected)
        endSubmission(agent.id, deploymentId, data)
      } catch (failure: any) {
        if (failure.response?.data?.deployment_id) endSubmission(agent.id, deploymentId, failure.response.data)
        else if (!failure.response || failure.response.status >= 500) endSubmission(agent.id, deploymentId, 'unknown')
        else if (!endSubmission(agent.id, deploymentId, 'rejected')) rejected[agent.id] = { ...failure.response.data, error: messageOf(failure) }
        // A rejected guard means the draft changed; later devices would be rejected too.
        if (failure.response?.status === 409) {
          notify.error('发布已停止', messageOf(failure))
          return
        }
      }
    }
  } finally {
    publishing.value = false
  }
}

onBeforeRouteLeave(async () => {
  await queue
  return !unsaved().length || await confirm('有设备尚未保存，离开会丢失这些修改。', { title: '离开客户端设备', confirmText: '放弃修改' })
})
onMounted(load)
onUnmounted(() => { disposed = true })
</script>
