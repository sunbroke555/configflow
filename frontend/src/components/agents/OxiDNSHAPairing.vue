<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { RefreshCw } from '@lucide/vue'
import { agentApi } from '@/api'
import { confirm } from '@/lib/feedback'
import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import FormField from '@/components/common/FormField.vue'
import AgentDNSHA from './AgentDNSHA.vue'
import DockerHAGuideCommand from './DockerHAGuideCommand.vue'
import type { Agent } from '@/types'
import type { DockerHAEnvironment, DockerHANetwork } from '@/types/dockerHAGuide'

const props = withDefaults(defineProps<{
  profileId: string
  profileName: string
  hosts?: { agent_ip: string; oxidns_port: number }[]
  initialAgentId?: string
  network?: DockerHANetwork
  active?: boolean
}>(), { active: true })
const emit = defineEmits<{ changed: [] }>()
// Keep the chosen configuration space fixed throughout this pairing flow.
const profile = { id: props.profileId, name: props.profileName }
let disposed = false
const failureText = (error: any) => error?.response?.data?.message || error?.response?.data?.error || error?.message || '请求失败，请重试。'

const agents = ref<Agent[]>([])
const selections = ref([props.initialAgentId || '', ''])
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
    if (agent.status !== 'online') reasons.push(`${label}：Agent 离线，检查服务日志、服务器地址和端口连通性。`)
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
  if (identities[0] && identities[0] === identities[1]) reasons.push('两台 Agent 的宿主身份相同；同一宿主机上的两个服务不能组成主机高可用。')
  const details = selected.value.map(agent => agent && environments.value[agent.id]?.oxidns_ha_environment)
  const bootIds = details.map(detail => detail?.host_boot_id)
  const validBootIds = bootIds.every(id => typeof id === 'string' && /^[0-9a-f]{64}$/.test(id))
  if (details.some(detail => ['docker-volume', 'installation'].includes(detail?.host_id_source || '')) && !validBootIds) reasons.push('使用自动身份时，两端需提供宿主校验证据；请升级 Agent 后重新检查。')
  if (details.some(detail => detail?.host_id_source === 'unknown')) reasons.push('无法识别宿主身份来源，请升级 Agent 后重新检查。')
  if (validBootIds && bootIds[0] === bootIds[1]) reasons.push('两台 Agent 来自同一宿主，不能组成主机高可用。')
  if (refreshError.value) reasons.push(refreshError.value)
  return reasons
})
const canPair = computed(() => props.active && !refreshing.value && blockers.value.length === 0)
const pairAgent = computed(() => selected.value[0])
const pairAgents = computed(() => selected.value.filter((agent): agent is Agent => !!agent))
const binding = ref('')
async function bind(agent: Agent) {
  if (!props.active || disposed || !selections.value.includes(agent.id)) return
  if (!await confirm(`将「${agent.name}」绑定到配置空间「${profile.name}」？后续开启 HA 会将该空间的 OxiDNS 配置和规则同步到两台设备。`, { title: '绑定安装配置空间', confirmText: '绑定' })) return
  if (!props.active || disposed || !selections.value.includes(agent.id)) return
  binding.value = agent.id
  refreshError.value = ''
  try { await agentApi.bindProfile(agent.id, profile.id); await refresh() }
  catch (error) { if (!disposed && props.active) refreshError.value = failureText(error) }
  finally { binding.value = '' }
}
async function refresh() {
  if (!props.active) return
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
      const host = props.hosts?.[index]
      const matches = host?.agent_ip ? candidates.value.filter(agent => agent.host === host.agent_ip && Number(agent.port) === Number(host.oxidns_port)) : []
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
  if (!props.active) return
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

const includesNative = computed(() => selected.value.some(agent => agent && agent.deployment_method !== 'docker') || (!selected.value.some(Boolean) && !props.network))
const includesDocker = computed(() => selected.value.some(agent => agent?.deployment_method === 'docker') || (!selected.value.some(Boolean) && !!props.network))
watch(() => props.active, active => {
  if (active) return
  refreshId++
  verifyId++
  refreshing.value = false
  verificationBusy.value = false
  checkedAt.value = ''
  environments.value = {}
  environmentErrors.value = {}
  verification.value = ''
  verificationError.value = ''
}, { flush: 'sync' })
onUnmounted(() => { disposed = true; refreshId++; verifyId++ })
</script>

<template>
  <section class="flex min-w-0 flex-col gap-4" aria-label="OxiDNS 高可用验证与配对">
      <div class="flex flex-wrap items-center justify-between gap-2"><h4 class="m-0 text-sm font-semibold">检查两台 Agent 的能力与宿主身份</h4><Button variant="outline" class="min-h-11" :disabled="!active || refreshing" @click="refresh"><RefreshCw class="size-4" :class="refreshing && 'animate-spin'" />刷新并检查两台 Agent</Button></div>
      <p class="m-0 text-[13px] leading-relaxed text-muted-foreground">支持两台 Shell、两台 Docker，或 Shell 与 Docker 混合。先刷新设备列表，再选择两台重新检查；在线心跳不能代替安装检查。</p>
      <p v-if="includesNative" class="m-0 text-[13px] leading-relaxed text-muted-foreground">Shell 端需预装 keepalived 和 ip；正式开启时还会检查权限、服务管理和 DNS 配置。</p>
      <details class="text-[13px] leading-relaxed">
        <summary class="min-h-11 cursor-pointer py-3">没有出现 Agent，或检查失败？</summary>
        <ul class="mb-0 list-disc space-y-2 pl-5">
          <li>确认 OxiDNS 与 Agent 服务已启动，ConfigFlow 地址从这台主机可达，服务器能访问 Agent 地址和端口；按返回原因修正后重新刷新。</li>
          <li v-if="includesNative">Shell 端查看服务日志，检查 keepalived、ip 和服务管理器；已有 OxiDNS 无需重装。两端应监听局域网可访问的 DNS 地址，跨主机上游使用局域网地址，不能使用 127.0.0.1。</li>
          <li v-if="includesDocker">Docker 端查看容器日志；Host 检查 53 端口冲突、companion、共享目录和只读宿主挂载。macvlan 检查容器 DNS 监听、固定地址、网卡和宿主隔离限制，不要求宿主机 53 端口空闲。</li>
          <li>新装凭据过期时，保留全部设置重新生成；不要删除已有身份和安装记录。</li>
        </ul>
      </details>
      <div class="grid min-w-0 grid-cols-1 gap-4 sm:grid-cols-2">
        <div v-for="index in [0, 1]" :key="index" class="min-w-0 space-y-2">
          <FormField :label="`主机 ${index + 1} 的 OxiDNS Agent`" :html-for="`docker-ha-verify-${index + 1}`">
            <Select :model-value="selections[index]" @update:model-value="selectionChanged(index, $event)"><SelectTrigger :id="`docker-ha-verify-${index + 1}`" class="min-h-11 w-full min-w-0"><SelectValue placeholder="先刷新，再选择设备" /></SelectTrigger><SelectContent><SelectItem v-for="agent in candidates" :key="agent.id" :value="agent.id">{{ agent.name }} · {{ agent.host }} · {{ agent.deployment_method === 'docker' ? 'Docker' : 'Shell' }}</SelectItem></SelectContent></Select>
          </FormField>
          <Button v-if="selected[index] && selected[index]!.profile_id !== profile.id" variant="outline" class="min-h-11 h-auto w-full whitespace-normal" :disabled="!!binding" @click="bind(selected[index]!)">绑定到「{{ profile.name }}」</Button>
          <p v-if="selected[index] && environments[selected[index]!.id]" class="m-0 text-xs text-muted-foreground">实时环境：{{ environments[selected[index]!.id]!.oxidns_ha_environment?.network || (environments[selected[index]!.id]!.oxidns_ha_environment?.kind === 'native' ? 'Shell 原生' : '未知网络') }} · {{ environments[selected[index]!.id]!.oxidns_ha_environment?.ready ? (environments[selected[index]!.id]!.oxidns_ha_environment?.kind === 'native' ? '身份已读取，依赖将在开启时检查' : '环境就绪') : '环境未就绪' }}</p>
        </div>
      </div>
      <div v-if="blockers.length" role="status" class="rounded-md border border-warning/30 bg-warning-soft p-3 text-[13px]"><p class="mb-2 mt-0 font-semibold">配对前需要处理</p><ul class="m-0 list-disc space-y-1 pl-5"><li v-for="reason in blockers" :key="reason">{{ reason }}</li></ul></div>
      <p v-if="checkedAt" class="m-0 text-xs text-muted-foreground">实时检查时间：{{ checkedAt }}。开启 HA 时服务端仍会重新校验两台环境。</p>
      <template v-if="active && pairAgent">
        <p class="m-0 text-[13px] leading-relaxed">以下将主机 1 配置为优先主机，主机 2 为备用，实际角色以 VIP 持有情况为准。填写预留 VIP 后，首次开启会先把同一份配置与规则同步到两台；等待同步和 DNS 检查完成。</p>
        <AgentDNSHA :agent="pairAgent" :agents="pairAgents" :blocked="!canPair" :show-install-guide="false" @changed="refresh" />
      </template>
      <div class="space-y-3 border-t border-border pt-4">
        <h4 class="m-0 text-sm font-semibold">从第三台局域网设备验证 VIP</h4>
        <p class="m-0 text-[13px] leading-relaxed">HA 状态正常后，在另一台 LAN 设备查询 VIP；确认 DNS 有正常结果，再把路由器 DHCP / 客户端 DNS 改成 VIP。页面不会把复制命令当作 DNS 验证成功。</p>
        <Button variant="outline" class="min-h-11" :disabled="!active || !vip || verificationBusy" @click="generateVerification">生成 VIP DNS 验证命令</Button>
        <p v-if="!vip" class="m-0 text-xs text-muted-foreground">配对成功并刷新后，使用实际保存的 VIP 生成命令。</p>
        <p v-if="verificationError" role="alert" class="m-0 text-sm text-destructive-accent">{{ verificationError }}</p>
        <DockerHAGuideCommand v-if="verification" title="从第三台 LAN 设备查询" :command="verification" />
        <p class="m-0 text-[13px] leading-relaxed text-muted-foreground">以后发布配置和规则会按 HA 顺序同步，并在确认 VIP 迁移后更新原持有者；<template v-if="includesDocker">Docker 镜像升级仍需要手动重建容器。</template>网络或主机故障仍可能造成短暂中断。</p>
      </div>
    </section>
</template>
