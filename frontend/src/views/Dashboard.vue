<template>
  <div>
    <PageHeader
      title="数据统计"
      description="查看订阅、节点和设备状态，了解最近的更新与配置生成记录。"
    />

    <div class="mb-5 flex flex-wrap items-center gap-x-5 gap-y-2 text-[13px] text-muted-foreground">
      <span class="inline-flex items-center gap-2">
        <Layers class="size-4 shrink-0" aria-hidden="true" />
        共享资源 · {{ sharedKinds }} 类
      </span>
      <span class="inline-flex min-w-0 max-w-full items-center gap-2">
        <IdCard class="size-4 shrink-0" aria-hidden="true" />
        <span class="min-w-0 break-all">当前配置 · <span class="font-medium text-foreground">{{ profileName }}</span></span>
      </span>
    </div>

    <div v-if="loading && !checkedAt" role="status" aria-label="正在加载总览" class="space-y-5">
      <div class="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Skeleton v-for="index in 4" :key="index" class="h-32 rounded-xl" />
      </div>
      <Skeleton class="h-72 rounded-xl" />
      <span class="sr-only">正在加载总览</span>
    </div>
    <template v-else>
      <p v-if="loadFailed" role="alert" class="mb-5 rounded-lg border border-warning-accent/30 bg-warning-soft px-4 py-3 text-sm text-warning-accent">
        部分数据未能加载，显示结果可能不完整。点击「立即检测」重试。
      </p>
    <KpiPanel :items="kpis" />

    <div class="grid grid-cols-[minmax(0,1fr)_320px] items-start gap-5 max-[1100px]:grid-cols-[minmax(0,1fr)]">
      <ActivityList v-model:active="activeTab" :rows="visibleActivity" :tabs="activityTabs" />

      <aside class="grid min-w-0 gap-5 min-[760px]:max-[1100px]:grid-cols-2">
        <HealthPanel
          :rows="healthRows"
          :checked-at="checkedAt"
          :loading="loading"
          @refresh="loadAllData"
        />
        <QuickActions :actions="quickActions" @run="runQuickAction" />
      </aside>
    </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { statsApi, agentApi, profileApi, subscriptionApi } from '@/api'
import api from '@/api'
import { Layers, IdCard } from '@lucide/vue'
import PageHeader from '@/components/common/PageHeader.vue'
import { Skeleton } from '@/components/ui/skeleton'
import KpiPanel, { type KpiItem } from '@/components/dashboard/KpiPanel.vue'
import HealthPanel, { type HealthRow } from '@/components/dashboard/HealthPanel.vue'
import QuickActions, { type QuickAction } from '@/components/dashboard/QuickActions.vue'
import ActivityList, { type ActivityRow } from '@/components/dashboard/ActivityList.vue'
import { useProfileStore } from '@/stores/profile'

const router = useRouter()
const profileStore = useProfileStore()

const loading = ref(false)
const loadFailed = ref(false)
const checkedAt = ref('')

const counts = ref({ subscriptions: 0, nodes: 0, proxyGroups: 0, rules: 0 })
const profileCount = ref(0)
const agents = ref<any[]>([])
const subscriptions = ref<any[]>([])
const activity = ref<ActivityRow[]>([])

const sharedKinds = 4

const profileName = computed(
  () => profileStore.activeProfile.value?.name || profileStore.activeProfileId.value
)

/* ---------- 关键指标 ---------- */
const kpis = computed<KpiItem[]>(() => [
  { label: '配置空间', value: profileCount.value, icon: 'Setting', scope: 'system', route: '/profiles' },
  { label: '引用订阅', value: counts.value.subscriptions, icon: 'Link', scope: 'profile', route: '/proxy-groups' },
  { label: '引用节点', value: counts.value.nodes, icon: 'Connection', scope: 'profile', route: '/proxy-groups' },
  { label: 'Agent', value: agents.value.length, icon: 'Monitor', scope: 'system', route: '/agents' }
])

/* ---------- 配置健康：全部由真实数据推导，不展示无法计算的项 ---------- */
const healthRows = computed<HealthRow[]>(() => {
  const rows: HealthRow[] = []

  const total = subscriptions.value.length
  const enabled = subscriptions.value.filter(s => s.enabled).length
  rows.push({
    label: '共享订阅来源',
    value: total ? `${enabled}/${total} 启用` : '未配置',
    level: total === 0 ? 'warn' : enabled === total ? 'ok' : 'warn'
  })

  rows.push({
    label: '引用节点',
    value: `${counts.value.nodes} 个节点`,
    level: counts.value.nodes > 0 ? 'ok' : 'warn'
  })

  rows.push({
    label: '策略组',
    value: `${counts.value.proxyGroups} 组`,
    level: counts.value.proxyGroups > 0 ? 'ok' : 'warn'
  })

  rows.push({
    label: '策略规则',
    value: `${counts.value.rules} 条`,
    level: counts.value.rules > 0 ? 'ok' : 'warn'
  })

  const online = agents.value.filter(a => a.status === 'online').length
  rows.push({
    label: 'Agent 状态',
    value: agents.value.length ? `${online}/${agents.value.length} 在线` : '未注册',
    level: agents.value.length === 0 ? 'warn' : online === agents.value.length ? 'ok' : 'err'
  })

  return rows
})

/* ---------- 快速操作：一个视图只有一个主操作 ---------- */
const quickActions = computed<QuickAction[]>(() => [
  { label: '生成配置', icon: 'Download', primary: true, route: '/generate' },
  { label: '更新订阅', icon: 'Refresh', route: '/subscriptions' },
  { label: '推送到 Agent', icon: 'Promotion', route: '/agents', disabled: agents.value.length === 0 },
  { label: '查看运行日志', icon: 'Tickets', route: '/logs' }
])

const runQuickAction = (action: QuickAction) => {
  if (action.route) router.push(action.route)
}

/* ---------- 运行状态：从真实日志解析，不编造记录 ---------- */
const activityTabs = ['全部', '订阅', '配置生成', 'Agent']
const activeTab = ref('全部')

const visibleActivity = computed(() => {
  if (activeTab.value === '全部') return activity.value
  return activity.value.filter(row => row.task.includes(activeTab.value))
})

const LOG_LINE = /^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})[,.]?\d*\s+-\s+([\w.]+)\s+-\s+(\w+)\s+-\s+(.*)$/

const classifyTask = (logger: string, message: string): string => {
  const text = `${logger} ${message}`.toLowerCase()
  if (text.includes('subscription') || text.includes('订阅')) return '订阅'
  if (text.includes('generate') || text.includes('生成')) return '配置生成'
  if (text.includes('agent')) return 'Agent'
  return '系统'
}

const parseLogs = (lines: string[]): ActivityRow[] =>
  lines
    .map(line => {
      const m = LOG_LINE.exec(line)
      if (!m) return null
      const [, time, logger, level, message] = m
      const lv = level === 'ERROR' ? 'err' : level === 'WARNING' ? 'warn' : 'ok'
      return {
        task: classifyTask(logger, message),
        detail: message,
        status: lv === 'ok' ? '成功' : lv === 'warn' ? '警告' : '失败',
        level: lv as ActivityRow['level'],
        time: time.slice(5)
      }
    })
    .filter((row): row is ActivityRow => row !== null)
    .reverse()

/* ---------- 数据加载 ---------- */
const loadAllData = async () => {
  loading.value = true
  try {
    const results = await Promise.allSettled([
      statsApi.getOverview(),
      agentApi.getAll(),
      profileApi.list(),
      subscriptionApi.getAll(),
      api.get('/logs/tail', { params: { lines: 40 } })
    ])

    const [stats, agentRes, profileRes, subRes, logRes] = results
    loadFailed.value = results.some(result => result.status === 'rejected')
      || (stats.status === 'fulfilled' && !stats.value.data?.success)

    if (stats.status === 'fulfilled' && stats.value.data?.success) {
      const d = stats.value.data.data
      counts.value = {
        subscriptions: d.subscriptions?.total ?? 0,
        nodes: d.nodes?.total ?? 0,
        proxyGroups: d.proxyGroups?.total ?? 0,
        rules: d.rules?.total ?? 0
      }
    }
    if (agentRes.status === 'fulfilled') agents.value = agentRes.value.data || []
    if (profileRes.status === 'fulfilled') {
      const data = profileRes.value.data
      profileCount.value = (Array.isArray(data) ? data : data?.profiles || []).length
    }
    if (subRes.status === 'fulfilled') subscriptions.value = subRes.value.data || []
    if (logRes.status === 'fulfilled') {
      activity.value = parseLogs(logRes.value.data?.logs || []).slice(0, 12)
    }

    checkedAt.value = new Date().toLocaleTimeString('zh-CN', { hour12: false })
  } finally {
    loading.value = false
  }
}

let timer: number

onMounted(async () => {
  await loadAllData()
  timer = setInterval(loadAllData, 30000)
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
})
</script>
