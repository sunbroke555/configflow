import { computed, ref } from 'vue'
import { loadAgentSummaries } from '@/lib/agentSummaries'
import { useProfileStore } from '@/stores/profile'

/**
 * 实时运行数据：Agent 列表与网卡吞吐。
 *
 * 顶栏吞吐、侧栏曲线、总览流向图共用这一份轮询，避免各自请求。
 * 吞吐来自 Agent 心跳上报的 system_metrics.network.speed_*（字节/秒），
 * 只统计绑定到当前配置空间且在线的 Agent；没有任何上报时为 null，界面据此隐藏，不编造数字。
 */
const POLL_MS = 5000
const HISTORY = 40

const agents = ref<any[]>([])
const loaded = ref(false)
/** 下行吞吐的会话内采样，供侧栏曲线使用 */
const downHistory = ref<number[]>([])

let timer: number | undefined
let consumers = 0
let inflight = false

const speedOf = (agent: any) => {
  const net = agent?.system_metrics?.network ?? agent?.network
  return { up: Number(net?.speed_sent) || 0, down: Number(net?.speed_recv) || 0, reported: !!net }
}

const profileStore = useProfileStore()

const boundAgents = computed(() =>
  agents.value.filter(a => (a.profile_id || profileStore.defaultProfileId.value) === profileStore.activeProfileId.value)
)

const isOnline = (a: any) => a.status === 'online' && a.enabled !== false

const throughput = computed(() => {
  const live = boundAgents.value.filter(a => isOnline(a) && speedOf(a).reported)
  if (!live.length) return null
  return live.reduce(
    (sum, a) => {
      const sp = speedOf(a)
      return { up: sum.up + sp.up, down: sum.down + sp.down, agents: sum.agents + 1 }
    },
    { up: 0, down: 0, agents: 0 }
  )
})

const onlineCount = computed(() => boundAgents.value.filter(isOnline).length)

const poll = async () => {
  if (document.hidden || inflight) return
  inflight = true
  try {
    const data = await loadAgentSummaries()
    agents.value = Array.isArray(data) ? data : []
    loaded.value = true
    const t = throughput.value
    if (t) {
      downHistory.value = [...downHistory.value, t.down].slice(-HISTORY)
    }
  } catch {
    // 轮询失败保留上次结果，等待下一轮
  } finally {
    inflight = false
  }
}

/** 页面挂载时调用 start、卸载时调用 stop；按引用计数共享同一个定时器 */
const start = () => {
  consumers++
  if (consumers === 1) {
    poll()
    timer = window.setInterval(poll, POLL_MS)
  }
}

const stop = () => {
  consumers = Math.max(0, consumers - 1)
  if (consumers === 0 && timer) {
    clearInterval(timer)
    timer = undefined
  }
}

/** 字节/秒 → [数值, 单位] */
export const formatRate = (bytes: number): [string, string] => {
  const units = ['B/s', 'KB/s', 'MB/s', 'GB/s']
  let v = bytes
  let i = 0
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i++
  }
  return [v >= 100 || i === 0 ? v.toFixed(0) : v.toFixed(1), units[i]]
}

export const useLive = () => ({
  agents,
  loaded,
  boundAgents,
  throughput,
  onlineCount,
  downHistory,
  speedOf,
  isOnline,
  refresh: poll,
  start,
  stop
})
