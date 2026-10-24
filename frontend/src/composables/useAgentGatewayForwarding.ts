import { computed, ref, watch, type Ref } from 'vue'
import { agentApi } from '@/api'
import { confirm, notify } from '@/lib/feedback'
import { persistConfirmText, type GatewayForwardingEntry } from '@/lib/gatewayForwarding'
import { routerSplitErrorMessage } from '@/lib/routerSplit'

interface AgentLike { id: string; status?: string; version?: string }

/**
 * Agent 页：被路由器用作代理网关的设备的 IPv4 转发状态与一键开启。
 * 列表独立读取，不阻塞设备列表；设备上下线或换版本时重新读取，过期响应按请求令牌丢弃。
 * 读取失败时保留上一次的结果，不弹提示。
 */
export function useAgentGatewayForwarding(agents: Ref<AgentLike[]>) {
  const entries = ref<Record<string, GatewayForwardingEntry>>({})
  const busy = ref('')
  let request = 0
  let loading = false

  const load = async () => {
    const current = ++request
    loading = true
    try {
      const { data } = await agentApi.gatewayForwarding()
      if (current !== request) return
      const next: Record<string, GatewayForwardingEntry> = {}
      for (const entry of Array.isArray(data?.gateways) ? data.gateways : []) {
        if (entry && typeof entry.agent_id === 'string') next[entry.agent_id] = entry
      }
      entries.value = next
    } catch {
      // 保留上一次读到的状态；这一行只是提醒，不打断页面。
    } finally {
      if (current === request) loading = false
    }
  }

  // 轮询只更新心跳时不重新读取；上下线、更新版本或增删设备时才读取。
  const signature = computed(() => agents.value.map(agent => `${agent.id}:${agent.status || ''}:${agent.version || ''}`).join('|'))
  watch(signature, value => { if (value) void load() }, { immediate: true })

  const persist = async (agentId: string) => {
    const entry = entries.value[agentId]
    if (!entry) return
    if (busy.value) {
      if (busy.value !== agentId) notify.info('另一台设备正在开启 IPv4 转发，请稍候。')
      return
    }
    const { title, description } = persistConfirmText(entry)
    if (!await confirm(description, { title, confirmText: '开启并持久化' })) return
    busy.value = agentId
    try {
      const { data } = await agentApi.persistGatewayForwarding(agentId)
      notify.success('IPv4 转发已开启并持久化。')
      // 仍在进行的读取可能早于这次开启，不能覆盖新状态；作废它并在之后重新读取。
      const readInFlight = loading
      request += 1
      loading = false
      if (data?.gateway && data.gateway.agent_id === agentId) {
        // 开启结果不含防火墙检查，保留上一次的提醒直到下次读取。
        const forward_filter = data.gateway.forward_filter ?? entries.value[agentId]?.forward_filter ?? null
        entries.value = { ...entries.value, [agentId]: { ...data.gateway, forward_filter } }
        if (readInFlight) void load()
      } else {
        void load()
      }
    } catch (error: any) {
      const body = error?.response?.data
      notify.error(routerSplitErrorMessage(body?.code, typeof body?.message === 'string' ? body.message : ''))
      void load()
    } finally {
      if (busy.value === agentId) busy.value = ''
    }
  }

  return { entries, busy, load, persist }
}
