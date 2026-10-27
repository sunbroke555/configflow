<script setup lang="ts">
import { computed, onUnmounted, reactive, ref, watch } from 'vue'
import { Loader2, RefreshCw } from '@lucide/vue'
import { agentApi } from '@/api'
import { confirm } from '@/lib/feedback'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import FormField from '@/components/common/FormField.vue'
import AgentDNSHA from './AgentDNSHA.vue'
import DockerHAHostForm from './DockerHAHostForm.vue'
import DockerHAGuideCommand from './DockerHAGuideCommand.vue'
import DockerHAComposeDeployment from './DockerHAComposeDeployment.vue'
import DockerHAPreflightFeedback from './DockerHAPreflightFeedback.vue'
import type { Agent } from '@/types'
import type { DockerHAEnvironment, DockerHAGuideResult, DockerHAHostForm as HostForm, DockerHANetwork } from '@/types/dockerHAGuide'

const props = defineProps<{ profileId: string; profileName: string; serverUrl: string }>()
const emit = defineEmits<{ changed: [] }>()
// This installation keeps its chosen profile even if the global space changes elsewhere.
const profile = { id: props.profileId, name: props.profileName }
const server = ref(props.serverUrl)
const image = ref('thsrite/config-flow-agent:latest')
const network = ref<DockerHANetwork>('host')
const step = ref(0)
const commandHost = ref('0')
const installMethods = ref<('compose_direct' | 'script')[]>(['compose_direct', 'compose_direct'])
const steps = ['准备环境', '主机 1', '主机 2', '执行安装', '验证与配对']
const hosts = reactive<HostForm[]>([0, 1].map(index => ({
  mode: 'new', agent_name: index ? 'dns-b' : 'dns-a', agent_ip: '', oxidns_port: 8082,
  data_dir: '', container_name: '', existing_agent_name: '', agent_mount: { type: 'bind', source: '' },
  core_mount: { type: 'bind', source: '' }, ha_data_dir: '', backup_dir: '',
  ha_host_id_path: '/etc/machine-id', create_host_identity: false, ha_parent: '', ha_subnet: '', ha_gateway: '',
})))
const results = ref<(DockerHAGuideResult | null)[]>([null, null])
const inspecting = ref([false, false])
const inspection = ref(['', ''])
const inspectionError = ref(['', ''])
const generating = ref([false, false])
const errors = ref(['', ''])
const requestIds = [0, 0]
const inspectIds = [0, 0]
let disposed = false
const failureText = (error: any) => error?.response?.data?.message || error?.response?.data?.error || error?.message || '请求失败，请重试。'
const currentHost = computed(() => Number(commandHost.value))
const currentResult = computed(() => results.value[currentHost.value])
const useCompose = computed(() => hosts[currentHost.value]!.mode === 'new' && installMethods.value[currentHost.value] === 'compose_direct')
const currentLogs = computed(() => currentResult.value?.command_blocks?.find(block => block.id === 'logs'))
hosts.forEach((host, index) => {
  watch([() => JSON.stringify(host), () => installMethods.value[index], network, image, server], () => {
    requestIds[index]++
    generating.value[index] = false
    results.value[index] = null
    errors.value[index] = ''
  }, { flush: 'sync' })
  watch(() => [host.container_name, host.mode], () => {
    inspectIds[index]++
    inspecting.value[index] = false
    inspection.value[index] = ''
    inspectionError.value[index] = ''
  }, { flush: 'sync' })
})
const preflight = ref('')
const preflightBusy = ref(false)
const preflightError = ref('')
async function inspectHost(index?: number) {
  if (index === undefined) { preflightBusy.value = true; preflightError.value = '' }
  else { inspecting.value[index] = true; inspectionError.value[index] = '' }
  const token = index === undefined ? 0 : ++inspectIds[index]
  try {
    const { data } = await agentApi.generateDockerHAGuide({ action: 'inspect', ...(index !== undefined && hosts[index]!.mode === 'migrate' ? { container_name: hosts[index]!.container_name } : {}) }, profile.id)
    if (disposed || (index !== undefined && token !== inspectIds[index])) return
    if (!data.success) throw new Error(data.message || '无法生成检查命令。')
    if (index === undefined) preflight.value = data.inspection_command
    else inspection.value[index] = data.inspection_command
  } catch (error) {
    if (disposed || (index !== undefined && token !== inspectIds[index])) return
    if (index === undefined) preflightError.value = failureText(error)
    else inspectionError.value[index] = failureText(error)
  } finally {
    if (index === undefined) preflightBusy.value = false
    else if (token === inspectIds[index]) inspecting.value[index] = false
  }
}
async function generate(index: number) {
  const token = ++requestIds[index]
  generating.value[index] = true
  errors.value[index] = ''
  results.value[index] = null
  try {
    const { data } = await agentApi.generateDockerHAGuide({
      ...JSON.parse(JSON.stringify(hosts[index])), action: 'generate', server_url: server.value,
      network_mode: network.value, image: image.value,
      install_method: hosts[index]!.mode === 'new' ? installMethods.value[index] : 'script',
      enable_dns_ha: true, enable_oxidns: true, enable_mihomo: false,
    }, profile.id)
    if (disposed || token !== requestIds[index]) return
    if (!data.success) throw new Error(data.message || '无法生成安装命令。')
    if (hosts[index]!.mode === 'new' && installMethods.value[index] === 'compose_direct' && (!data.compose_deployment || !data.compose?.trim())) throw new Error('Compose 未生成，请重试或切换为 Shell 脚本。')
    results.value[index] = data
  } catch (error) {
    if (!disposed && token === requestIds[index]) errors.value[index] = failureText(error)
  } finally {
    if (token === requestIds[index]) generating.value[index] = false
  }
}

const agents = ref<Agent[]>([])
const selections = ref(['', ''])
const environments = ref<Record<string, DockerHAEnvironment>>({})
const environmentErrors = ref<Record<string, string>>({})
const refreshing = ref(false)
const refreshError = ref('')
const checkedAt = ref('')
let refreshId = 0
const selected = computed(() => selections.value.map(id => agents.value.find(agent => agent.id === id)))
const candidates = computed(() => agents.value.filter(agent => agent.service_type === 'oxidns'))
const blockers = computed(() => {
  const reasons: string[] = []
  for (let index = 0; index < 2; index++) {
    const agent = selected.value[index]
    const label = `主机 ${index + 1}`
    if (!agent) { reasons.push(`${label}：请选择已安装的 OxiDNS Agent（Docker 或 Shell）。`); continue }
    if (agent.status !== 'online') reasons.push(`${label}：Agent 离线，检查容器日志、服务器地址和端口连通性。`)
    if (agent.profile_id !== profile.id) reasons.push(`${label}：尚未绑定配置空间「${profile.name}」。`)
    const environment = environments.value[agent.id]
    if (environmentErrors.value[agent.id]) reasons.push(`${label}：${environmentErrors.value[agent.id]}`)
    else if (!environment) reasons.push(`${label}：尚未实时检查 HA 环境，请刷新。`)
    else {
      const detail = environment.oxidns_ha_environment
      const container = agent.deployment_method === 'docker' || detail?.kind === 'container'
      if (!environment.oxidns_ha_protocols?.includes(1) || !environment.oxidns_ha_handoff_protocols?.includes(1) || (container && !environment.oxidns_ha_container_protocols?.includes(1))) reasons.push(`${label}：Agent 缺少所需的 HA 或计划迁移能力，请先升级。`)
      if (environment.service_type !== 'oxidns' || (container ? detail?.kind !== 'container' : detail?.kind !== 'native')) reasons.push(`${label}：实时返回的服务或部署类型不符。`)
      if (detail?.ready !== true) reasons.push(`${label}：${detail?.blockers?.join('；') || 'HA 环境未就绪。'}`)
      if (!detail?.host_id) reasons.push(`${label}：无法确认宿主身份。`)
    }
  }
  if (selections.value[0] && selections.value[0] === selections.value[1]) reasons.push('需要选择两台不同的 Agent。')
  selected.value.forEach((agent, index) => {
    if (agent?.oxidns_ha?.enabled && agent.oxidns_ha.peer_id !== selections.value[1 - index]) reasons.push(`主机 ${index + 1} 已与其他设备组成 HA，请选择它实际的对端。`)
  })
  const identities = selected.value.map(agent => agent && environments.value[agent.id]?.oxidns_ha_environment?.host_id)
  if (identities[0] && identities[0] === identities[1]) reasons.push('两台 Agent 的宿主身份相同；同一宿主机上的两个容器不能组成这组 HA。')
  const details = selected.value.map(agent => agent && environments.value[agent.id]?.oxidns_ha_environment)
  const bootIds = details.map(detail => detail?.host_boot_id)
  const validBootIds = bootIds.every(id => typeof id === 'string' && /^[0-9a-f]{64}$/.test(id))
  if (details.some(detail => detail?.host_id_source === 'docker-volume') && !validBootIds) reasons.push('使用 Docker 身份卷时，两端需提供宿主校验证据；请升级 Agent 后重新检查。')
  if (details.some(detail => detail?.host_id_source === 'unknown')) reasons.push('无法识别宿主身份来源，请升级 Agent 后重新检查。')
  if (validBootIds && bootIds[0] === bootIds[1]) reasons.push('两台 Agent 来自同一宿主，不能组成主机高可用。')
  if (refreshError.value) reasons.push(refreshError.value)
  return reasons
})
const canPair = computed(() => !refreshing.value && blockers.value.length === 0)
const pairAgent = computed(() => selected.value[0])
const pairAgents = computed(() => selected.value.filter((agent): agent is Agent => !!agent))
const binding = ref('')
async function bind(agent: Agent) {
  if (!await confirm(`将「${agent.name}」绑定到配置空间「${profile.name}」？后续开启 HA 会将该空间的 OxiDNS 配置和规则同步到两台设备。`, { title: '绑定安装配置空间', confirmText: '绑定' })) return
  binding.value = agent.id
  refreshError.value = ''
  try { await agentApi.bindProfile(agent.id, profile.id); await refresh() }
  catch (error) { refreshError.value = failureText(error) }
  finally { binding.value = '' }
}
async function refresh() {
  const token = ++refreshId
  refreshing.value = true
  refreshError.value = ''
  checkedAt.value = ''
  environments.value = {}
  environmentErrors.value = {}
  try {
    const { data } = await agentApi.getAll()
    if (disposed || token !== refreshId) return
    agents.value = data
    selections.value = selections.value.map((id, index) => {
      if (candidates.value.some(agent => agent.id === id)) return id
      const matches = candidates.value.filter(agent => agent.host === hosts[index]!.agent_ip && Number(agent.port) === Number(hosts[index]!.oxidns_port))
      return matches.length === 1 ? matches[0]!.id : ''
    })
    const ids = [...new Set(selections.value.filter(Boolean))]
    await Promise.all(ids.map(async id => {
      try {
        const { data: environment } = await agentApi.getDockerHAEnvironment(id)
        if (disposed || token !== refreshId) return
        if (!environment.success) throw new Error(environment.message || '无法实时读取 HA 环境。')
        environments.value[id] = environment
      } catch (error) {
        if (!disposed && token === refreshId) environmentErrors.value[id] = failureText(error)
      }
    }))
    if (!disposed && token === refreshId) { checkedAt.value = new Date().toLocaleTimeString(); emit('changed') }
  } catch (error) {
    if (!disposed && token === refreshId) refreshError.value = failureText(error)
  } finally {
    if (token === refreshId) refreshing.value = false
  }
}
function selectionChanged(index: number, id: unknown) {
  selections.value[index] = String(id || '')
  refreshId++
  refreshing.value = false
  checkedAt.value = ''
  environments.value = {}
  environmentErrors.value = {}
}
const verification = ref('')
const verificationError = ref('')
const verificationBusy = ref(false)
const vip = computed(() => pairAgent.value?.oxidns_ha?.enabled ? pairAgent.value.oxidns_ha.vip : '')
let verifyId = 0
watch(vip, () => { verifyId++; verification.value = ''; verificationError.value = ''; verificationBusy.value = false })
async function generateVerification() {
  const token = ++verifyId
  verificationBusy.value = true
  verificationError.value = ''
  try {
    const { data } = await agentApi.generateDockerHAGuide({ action: 'verify', vip: vip.value }, profile.id)
    if (disposed || token !== verifyId) return
    if (!data.success || !data.verification_command) throw new Error(data.message || '无法生成 DNS 验证命令。')
    verification.value = data.verification_command
  } catch (error) { if (!disposed && token === verifyId) verificationError.value = failureText(error) }
  finally { if (token === verifyId) verificationBusy.value = false }
}
onUnmounted(() => { disposed = true; refreshId++; verifyId++; requestIds.forEach((_, index) => requestIds[index]++); inspectIds.forEach((_, index) => inspectIds[index]++) })
</script>

<template>
  <section class="flex min-w-0 flex-col gap-5" aria-label="OxiDNS Docker HA 安装引导">
    <div>
      <h3 class="m-0 font-display text-xl">两台主机，共用一个 DNS 地址</h3>
      <p class="mb-0 mt-2 text-[13px] leading-relaxed text-muted-foreground">已有 Shell 节点无需重装，只部署 Docker 端，再到“验证与配对”中选择两台。本次使用配置空间「{{ profile.name }}」。</p>
    </div>
    <nav class="grid grid-cols-3 gap-1.5 sm:grid-cols-5" aria-label="安装步骤">
      <Button v-for="(label, index) in steps" :key="label" variant="outline" class="min-h-11 h-auto whitespace-normal px-2 py-2 text-xs" :class="step === index ? 'border-primary-accent bg-primary-soft text-primary-accent' : ''" :aria-current="step === index ? 'step' : undefined" @click="step = index">{{ index + 1 }}. {{ label }}</Button>
    </nav>

    <div v-show="step === 0" class="flex flex-col gap-4">
      <div class="rounded-md border border-border bg-muted p-3 text-[13px] leading-relaxed">
        <p class="mb-2 mt-0 font-semibold">开始前准备</p>
        <ul class="m-0 list-disc space-y-1 pl-5">
          <li>两台不同的 Linux 宿主机，位于同一局域网，使用固定 IPv4。两个容器放在同一主机不算双机 HA。</li>
          <li>普通 Linux 先安装 Docker Engine；新安装使用单份 Compose，需要 Docker Compose 2.20 或更新版本；也可选择不依赖 Compose 的 Shell 脚本。NAS 在套件中心安装 Docker / Container Manager。Compose 可直接导入项目启动；手动检查或使用 Shell 脚本时再通过 SSH 操作。</li>
          <li>需要 rootful Docker；Docker Desktop、rootless 和普通 bridge 网络不满足此引导。</li>
          <li data-docker-ha-port-note><template v-if="network === 'host'">Host 使用宿主机网络，默认配置需要 TCP / UDP 53；已有监听需先核对并处理，不要直接停止正在服务的 DNS。</template><template v-else>macvlan 使用容器独立地址，宿主机 TCP / UDP 53 的占用不直接阻断；仍需确认容器地址与 VIP 未被其他设备使用。</template></li>
          <li>准备同一份 OxiDNS 配置：监听所有地址的 UDP 53，上游 DNS 从两台设备都可达。开启 HA 时会同步这个配置空间的配置和规则。</li>
          <li>为 VIP 预留一个同网段、未使用且在 DHCP 地址池之外的 IPv4；不要填写主机、容器或网关现有地址。</li>
        </ul>
      </div>
      <FormField label="Docker HA 网络模式">
        <RadioGroup v-model="network" class="flex flex-wrap gap-3" aria-label="Docker HA 网络模式">
          <label class="flex min-h-11 items-center gap-2"><RadioGroupItem value="host" />Host + 独立 HA 容器（推荐）</label>
          <label class="flex min-h-11 items-center gap-2"><RadioGroupItem value="macvlan" />macvlan 独立地址</label>
        </RadioGroup>
      </FormField>
      <p v-if="network === 'host'" class="m-0 text-[13px] leading-relaxed">使用宿主机地址；OxiDNS 和 HA companion 分开运行，NET_ADMIN / NET_RAW 只授予 HA 容器。核心容器停止后 HA 仍可探测并让出 VIP；强杀 HA 容器或整个 Docker 可能留下宿主 VIP，需要恢复 HA 或人工清理。</p>
      <p v-else class="m-0 rounded-md bg-warning-soft p-3 text-[13px] leading-relaxed">需要宿主机和网络支持 macvlan，为每台容器分配独立 LAN 地址。macvlan 默认不能访问本宿主机；ConfigFlow 应部署在可达的其他 LAN 主机或同 macvlan 网络。NAS 不支持时使用 Host。</p>
      <FormField label="ConfigFlow 服务器地址" html-for="docker-ha-server" persistent-hint hint="两台容器都必须能访问，不能填写 localhost 或 127.0.0.1。"><Input id="docker-ha-server" v-model="server" class="font-mono" placeholder="http://192.168.1.10:8000" /></FormField>
      <FormField label="Agent Docker 镜像" html-for="docker-ha-image" hint="需包含单文件 Compose 初始化与 Docker HA 支持的新镜像；旧镜像需先升级。"><Input id="docker-ha-image" v-model="image" class="font-mono" /></FormField>
      <Button variant="outline" class="min-h-11 self-start" :disabled="preflightBusy" @click="inspectHost()"><Loader2 v-if="preflightBusy" class="size-4 animate-spin" />生成环境检查命令</Button>
      <p v-if="preflightError" role="alert" class="m-0 text-sm text-destructive-accent">{{ preflightError }}</p>
      <DockerHAGuideCommand v-if="preflight" title="两台宿主机分别检查" :command="preflight" collapsed note="复制完整命令到两台宿主机分别执行；将完整输出粘贴到下方检测，逐项查看需要处理的问题。" />
      <DockerHAPreflightFeedback :profile-id="profile.id" :network="network" />
    </div>

    <div v-if="step === 1 || step === 2" class="flex min-w-0 flex-col gap-4">
      <h4 class="m-0 text-sm font-semibold">填写主机 {{ step }} · {{ network === 'host' ? '宿主机地址' : 'macvlan 容器地址' }}</h4>
      <DockerHAHostForm :host="hosts[step - 1]!" :index="step - 1" :network="network" :direct-compose="installMethods[step - 1] === 'compose_direct' && hosts[step - 1]!.mode === 'new'" />
      <details v-if="hosts[step - 1]!.mode === 'migrate'" class="text-[13px] leading-relaxed">
        <summary class="min-h-11 cursor-pointer py-3">旧容器已有 HA、额外挂载或自定义启动命令？</summary>
        <p class="mt-0">自动迁移仅支持官方单 OxiDNS 容器、默认入口和启动命令，以及原 Agent / OxiDNS 两个数据挂载。已有 Docker HA 容器停用后仍保留 HA 环境变量和额外挂载，不能使用自动迁移脚本。请按下面步骤保留原 Compose 手动重建，不要把新安装的 Compose 直接覆盖上去。</p>
        <ol class="mb-0 list-decimal space-y-2 pl-5">
          <li>保存原 Compose 项目、环境变量、全部挂载和自定义启动参数；核对 Agent 完整名称和身份目录。备份原 Agent / OxiDNS 数据，保留旧镜像和容器作为回滚来源。</li>
          <li>以原 Compose 为基础，对照生成的 HA 定义补充专用 HA 持久目录、宿主身份挂载和网络设置。Host 还需独立 companion、共享 HA 目录与宿主网络证明挂载，网络权限只给 companion；macvlan 则给核心容器所需权限。保留原名称、数据、环境和其他业务挂载。</li>
          <li>已有 HA 先将路由器 DHCP / 客户端 DNS 临时改为仍在服务的健康 OxiDNS 固定地址，发布路由器配置并确认客户端解析正常。记录当前 VIP 持有者，然后在 Agent 卡片的「DNS 高可用 → 设置」中停用原组并等待清理完成；停用会让 VIP 下线。</li>
          <li>一次只重建一台，先处理原备用；确认原 Agent 身份仍在、DNS 健康和新环境检查通过，再把临时 DNS 切到这台已恢复的固定地址，确认解析后处理另一台。两台恢复后重新配对，并从第三台 LAN 设备验证 VIP，最后才把客户端 DNS 切回 VIP。</li>
          <li>失败时停止新容器，使用原 Compose / 旧容器和备份恢复，检查原 DNS；不要删除原数据卷。无法确认自定义参数兼容时停止迁移，继续使用旧服务。</li>
        </ol>
      </details>
      <Button variant="outline" class="min-h-11 self-start" :disabled="inspecting[step - 1] || (hosts[step - 1]!.mode === 'migrate' && !hosts[step - 1]!.container_name.trim())" @click="inspectHost(step - 1)"><Loader2 v-if="inspecting[step - 1]" class="size-4 animate-spin" />生成这台主机的只读检查命令</Button>
      <p v-if="inspectionError[step - 1]" role="alert" class="m-0 text-sm text-destructive-accent">{{ inspectionError[step - 1] }}</p>
      <DockerHAGuideCommand v-if="inspection[step - 1]" :title="`主机 ${step} 安装前检查`" :command="inspection[step - 1]!" note="在这台宿主机运行；迁移时从输出核对完整 Agent 名称、原 Agent 身份挂载和 OxiDNS 数据挂载。" />
    </div>

    <div v-else-if="step === 3" class="flex min-w-0 flex-col gap-4">
      <RadioGroup v-model="commandHost" class="flex gap-4" aria-label="执行命令的主机">
        <label v-for="index in [0, 1]" :key="index" class="flex min-h-11 items-center gap-2"><RadioGroupItem :value="String(index)" />主机 {{ index + 1 }} · {{ hosts[index]!.agent_ip || '未填写地址' }}</label>
      </RadioGroup>
      <p v-if="hosts[currentHost]!.mode === 'migrate'" class="m-0 text-[13px] leading-relaxed">迁移会核对原挂载并备份，请在对应宿主机执行脚本。已启用 HA 的旧容器会被迁移脚本拒绝；请返回主机步骤，展开手动重建说明。</p>
      <template v-else>
        <RadioGroup v-model="installMethods[currentHost]" class="flex flex-wrap gap-4" aria-label="新安装部署方式">
          <label class="flex min-h-11 cursor-pointer items-center gap-2 text-[13px]"><RadioGroupItem value="compose_direct" />Compose 直接部署</label>
          <label class="flex min-h-11 cursor-pointer items-center gap-2 text-[13px]"><RadioGroupItem value="script" />Shell 脚本</label>
        </RadioGroup>
        <p class="m-0 text-[12px] leading-relaxed text-muted-foreground">为当前 Docker 主机生成配置，已有 Shell 节点无需再次安装。</p>
      </template>
      <Button class="min-h-11 self-start" :disabled="generating[currentHost]" @click="generate(currentHost)"><Loader2 v-if="generating[currentHost]" class="size-4 animate-spin" />生成主机 {{ currentHost + 1 }} 的{{ useCompose ? 'Compose' : '安装命令' }}</Button>
      <p v-if="errors[currentHost]" role="alert" class="m-0 text-[13px] text-destructive-accent">{{ errors[currentHost] }}</p>
      <template v-if="currentResult">
        <p class="m-0 text-[13px] text-muted-foreground">{{ useCompose ? 'Compose 已生成，尚未安装。' : '命令已生成，尚未验证安装。复制命令不会自动执行，也不代表设备已就绪。' }}</p>
        <DockerHAComposeDeployment v-if="useCompose && currentResult.compose_deployment" :deployment="currentResult.compose_deployment" :compose="currentResult.compose" />
        <template v-else>
          <ul v-if="currentResult.warnings?.length" class="m-0 list-disc space-y-1 pl-5 text-[13px]"><li v-for="warning in currentResult.warnings" :key="warning">{{ warning }}</li></ul>
          <DockerHAGuideCommand title="1. 检查宿主机" :command="currentResult.inspection_command" collapsed />
          <DockerHAGuideCommand v-if="currentResult.identity_command" title="2. 创建这台宿主机的身份文件" :command="currentResult.identity_command" note="仅在该宿主机执行一次；不要把文件复制给另一台主机。" collapsed />
          <DockerHAGuideCommand :title="currentResult.identity_command ? '3. 安装或迁移容器' : '2. 安装或迁移容器'" :command="currentResult.install_command" :note="hosts[currentHost]!.mode === 'migrate' ? '此脚本会核对原挂载、停止并备份旧容器、保留旧容器再重建；不要跳过脚本直接运行 Compose。迁移期间这台 DNS 会暂停。' : '复制到对应宿主机执行。脚本创建持久目录并启动容器，结束后前往下一步刷新 Agent。'" collapsed />
        </template>
        <details v-if="hosts[currentHost]!.mode === 'new'" class="min-w-0 text-[13px] leading-relaxed">
          <summary class="min-h-11 cursor-pointer py-3">安装失败或凭据过期，如何继续？</summary>
          <p class="mt-0">保留全部设置和持久数据，修正报错后重试。首次安装凭据过期时，用相同设置重新生成{{ useCompose ? 'Compose，替换原文件后再次启动项目' : '安装命令' }}。已成功登记的 Compose 容器重启时复用本机身份，不需要重新注册。</p>
          <p class="mb-0">不要删除 Agent 身份和安装记录。改变名称、地址或目录前，先核对已有安装并选择迁移。</p>
        </details>
        <DockerHAGuideCommand v-if="currentLogs" :title="currentLogs.title" :command="currentLogs.command" note="在同一台宿主机执行，查看实际容器的启动错误；修正原因后再刷新 Agent。" />
        <details v-if="currentResult.compose" class="min-w-0"><summary class="min-h-11 cursor-pointer py-3 text-[13px]">查看 Compose 定义</summary><DockerHAGuideCommand :title="useCompose ? 'compose.yaml' : 'Compose 定义（供核对）'" :command="currentResult.compose" :note="useCompose ? '此文件包含启动所需配置，可直接部署。' : '迁移必须使用上方安装脚本，以保留检查、备份和回滚路径。'" /></details>
        <DockerHAGuideCommand v-if="currentResult.rollback_command" title="迁移失败时回滚" :command="currentResult.rollback_command" note="只在对应主机运行。保留旧容器、备份和原数据，不删除卷；恢复后检查原 Agent 与 DNS 服务。" />
      </template>
    </div>

    <div v-else-if="step === 4" class="flex min-w-0 flex-col gap-4">
      <div class="flex flex-wrap items-center justify-between gap-2"><h4 class="m-0 text-sm font-semibold">读取两台 Agent 的真实环境</h4><Button variant="outline" class="min-h-11" :disabled="refreshing" @click="refresh"><RefreshCw class="size-4" :class="refreshing && 'animate-spin'" />刷新并检查两台 Agent</Button></div>
      <p class="m-0 text-[13px] leading-relaxed text-muted-foreground">可选择已存在的 Shell 节点与新 Docker 节点。先刷新设备列表，再读取两端 HA 能力和环境。在线心跳不能代替安装检查；更换选择后需要重新刷新。</p>
      <details class="text-[13px] leading-relaxed"><summary class="min-h-11 cursor-pointer py-3">没有出现 Agent，或环境检查失败？</summary><ul class="mb-0 list-disc space-y-2 pl-5"><li>先查看对应宿主机的 Docker / Container Manager 容器日志，确认镜像拉取成功、OxiDNS 与 Agent 已启动。{{ network === 'host' ? 'Host 检查宿主机 53 端口冲突。' : 'macvlan 检查容器自身的 DNS 监听和地址冲突，不要求宿主机 53 端口空闲。' }}</li><li>确认填写的 ConfigFlow 地址从容器可达、服务器能访问所填 Agent 地址和端口，防火墙允许流量。新装凭据有效期为 24 小时，过期时保留全部设置不变再重新生成；不要删除已有身份和安装记录。</li><li>Host 检查 companion 是否运行、共享 HA 目录和只读身份挂载是否一致；macvlan 检查网卡、子网、固定地址与宿主隔离限制。按下面实时返回的具体原因修正后重新刷新。</li></ul></details>
      <div class="grid min-w-0 grid-cols-1 gap-4 sm:grid-cols-2">
        <div v-for="index in [0, 1]" :key="index" class="min-w-0 space-y-2">
          <FormField :label="`主机 ${index + 1} 的 OxiDNS Agent`" :html-for="`docker-ha-verify-${index + 1}`">
            <Select :model-value="selections[index]" @update:model-value="selectionChanged(index, $event)"><SelectTrigger :id="`docker-ha-verify-${index + 1}`" class="min-h-11 w-full min-w-0"><SelectValue placeholder="先刷新，再选择设备" /></SelectTrigger><SelectContent><SelectItem v-for="agent in candidates" :key="agent.id" :value="agent.id">{{ agent.name }} · {{ agent.host }} · {{ agent.deployment_method === 'docker' ? 'Docker' : 'Shell' }}</SelectItem></SelectContent></Select>
          </FormField>
          <Button v-if="selected[index] && selected[index]!.profile_id !== profile.id" variant="outline" class="min-h-11 h-auto w-full whitespace-normal" :disabled="!!binding" @click="bind(selected[index]!)">绑定到「{{ profile.name }}」</Button>
          <p v-if="selected[index] && environments[selected[index]!.id]" class="m-0 text-xs text-muted-foreground">实时环境：{{ environments[selected[index]!.id]!.oxidns_ha_environment?.network || (environments[selected[index]!.id]!.oxidns_ha_environment?.kind === 'native' ? 'Shell 原生' : '未知网络') }} · {{ environments[selected[index]!.id]!.oxidns_ha_environment?.ready ? '环境就绪' : '环境未就绪' }}</p>
        </div>
      </div>
      <div v-if="blockers.length" role="status" class="rounded-md border border-warning/30 bg-warning-soft p-3 text-[13px]"><p class="mb-2 mt-0 font-semibold">配对前需要处理</p><ul class="m-0 list-disc space-y-1 pl-5"><li v-for="reason in blockers" :key="reason">{{ reason }}</li></ul></div>
      <p v-if="checkedAt" class="m-0 text-xs text-muted-foreground">实时检查时间：{{ checkedAt }}。开启 HA 时服务端仍会重新校验两台环境。</p>
      <template v-if="pairAgent">
        <p class="m-0 text-[13px] leading-relaxed">以下将主机 1 配置为优先主机，主机 2 为备用，实际角色以 VIP 持有情况为准。填写预留 VIP 后，首次开启会先把同一份配置与规则同步到两台；等待同步和 DNS 检查完成。</p>
        <AgentDNSHA :agent="pairAgent" :agents="pairAgents" :blocked="!canPair" :show-install-guide="false" @changed="refresh" />
      </template>
      <div class="space-y-3 border-t border-border pt-4">
        <h4 class="m-0 text-sm font-semibold">从第三台局域网设备验证 VIP</h4>
        <p class="m-0 text-[13px] leading-relaxed">HA 状态正常后，在另一台 LAN 设备查询 VIP；确认 DNS 有正常结果，再把路由器 DHCP / 客户端 DNS 改成 VIP。页面不会把复制命令当作 DNS 验证成功。</p>
        <Button variant="outline" class="min-h-11" :disabled="!vip || verificationBusy" @click="generateVerification">生成 VIP DNS 验证命令</Button>
        <p v-if="!vip" class="m-0 text-xs text-muted-foreground">配对成功并刷新后，使用实际保存的 VIP 生成命令。</p>
        <p v-if="verificationError" role="alert" class="m-0 text-sm text-destructive-accent">{{ verificationError }}</p>
        <DockerHAGuideCommand v-if="verification" title="从第三台 LAN 设备查询" :command="verification" />
        <p class="m-0 text-[13px] leading-relaxed text-muted-foreground">以后发布配置和规则会按 HA 顺序同步，并在确认 VIP 迁移后更新原持有者；Docker 镜像升级仍需要手动重建容器。此引导不承诺网络故障或容器强杀时零中断。</p>
      </div>
    </div>
    <div class="flex justify-between gap-2 border-t border-border pt-4">
      <Button variant="outline" class="min-h-11" :disabled="step === 0" @click="step--">上一步</Button>
      <Button v-if="step < steps.length - 1" class="min-h-11" @click="step++">下一步：{{ steps[step + 1] }}</Button>
    </div>
  </section>
</template>
