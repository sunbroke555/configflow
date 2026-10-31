<template>
  <div class="min-w-0">
    <Toolbar v-model:search="search" placeholder="搜索日志">
      <template #filters>
        <Select v-if="sources.length" :model-value="sourceId" @update:model-value="changeSource(String($event))">
          <SelectTrigger class="min-h-11 w-44 max-w-full" aria-label="日志来源"><SelectValue placeholder="日志来源" /></SelectTrigger>
          <SelectContent>
            <SelectItem v-for="source in sources" :key="source.id" :value="source.id">
              {{ source.label }}{{ source.kind === 'unavailable' ? '（不可读）' : '' }}
            </SelectItem>
          </SelectContent>
        </Select>
      </template>
      <template #actions>
        <Button variant="outline" class="min-h-11" :aria-pressed="paused" @click="paused = !paused">
          <Play v-if="paused" class="size-4" /><Pause v-else class="size-4" />{{ paused ? '恢复刷新' : '暂停刷新' }}
        </Button>
        <Button variant="outline" class="min-h-11" :aria-busy="loading" :disabled="loading" @click="refresh">
          <RefreshCw class="size-4" :class="{ 'animate-spin': loading }" />刷新日志
        </Button>
      </template>
    </Toolbar>

    <div class="mb-3 flex min-w-0 flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
      <span role="status">{{ refreshStatus }}</span>
      <span>最近 {{ MAX_LINES }} 行<span v-if="search.trim()"> · 匹配 {{ matchingLines.length }} 行</span></span>
      <span v-if="updatedAt" class="num">更新于 {{ updatedAt }}</span>
      <Button variant="ghost" size="sm" class="ml-auto min-h-11" :class="follow && 'bg-primary-soft text-primary-accent'" :aria-pressed="follow" @click="toggleFollow">
        <ArrowDownToLine class="size-3.5" />跟随日志
      </Button>
    </div>
    <p v-if="location" class="mb-3 break-all font-mono text-xs text-muted-foreground">{{ location }}</p>
    <p v-if="error" role="alert" class="mb-3 break-words text-sm text-destructive-accent">
      {{ error }}<span v-if="updatedAt"> 当前保留上次读取的日志。</span>
    </p>

    <SectionCard v-if="legacy || unavailable || (sourcesReady && !sources.length)">
      <EmptyState :icon="FileText" :title="legacy ? '需要升级 Agent' : '暂无可读取的 Mihomo 日志'" :description="unavailableMessage">
        <Button v-if="legacy" variant="outline" class="min-h-11" as-child><RouterLink to="/agents">前往升级 Agent</RouterLink></Button>
        <Button v-else variant="outline" class="min-h-11" :disabled="loading" @click="refresh">重新读取来源</Button>
      </EmptyState>
    </SectionCard>
    <AgentLogLines v-else ref="logPane" :text="filteredText" :notice="notice" height-class="h-[55dvh] min-h-60 max-h-[640px]" />
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { ArrowDownToLine, FileText, Pause, Play, RefreshCw } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import Toolbar from '@/components/common/Toolbar.vue'
import SectionCard from '@/components/common/SectionCard.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import AgentLogLines from '@/components/agents/AgentLogLines.vue'
import { useTabActive } from '@/components/ui/tabs'
import { agentApi, type AgentLogSource } from '@/api'
import { panelError } from '@/types/mihomo'

const props = withDefaults(defineProps<{ agentId: string; active?: boolean; reloadToken: number }>(), { active: true })
// 所在标签是否为当前标签：由 Tabs 注入，切换标签不经过 props，隐藏的面板不会整组件重新渲染
const active = useTabActive(() => props.active)
const MAX_LINES = 500
const REFRESH_MS = 3000
const sources = ref<AgentLogSource[]>([])
const sourcesReady = ref(false)
const sourceId = ref('')
const legacy = ref(false)
const text = ref('')
const error = ref('')
const loading = ref(false)
const paused = ref(false)
const follow = ref(true)
const search = ref('')
const updatedAt = ref('')
const visible = ref(document.visibilityState !== 'hidden')
const logPane = ref<InstanceType<typeof AgentLogLines> | null>(null)
const currentSource = computed(() => sources.value.find(source => source.id === sourceId.value))
const unavailable = computed(() => currentSource.value?.kind === 'unavailable')
const location = computed(() => currentSource.value?.kind === 'journal'
  ? `journalctl -u ${currentSource.value.unit}` : currentSource.value?.path || '')
const unavailableMessage = computed(() => legacy.value
  ? '旧版 Agent 无法区分核心与 Agent 日志，请升级 Agent 后查看 Mihomo 日志。'
  : currentSource.value?.note || 'Agent 未找到 Mihomo 核心日志，请检查设备上的服务日志配置。')
const matchingLines = computed(() => {
  const keyword = search.value.trim().toLowerCase()
  return text.value.split(/\r?\n/).filter(line => line.trim() && (!keyword || line.toLowerCase().includes(keyword)))
})
const filteredText = computed(() => search.value.trim() ? matchingLines.value.join('\n') : text.value)
const notice = computed(() => {
  if (text.value) return search.value.trim() && !matchingLines.value.length ? '没有匹配的日志' : ''
  if (loading.value) return '正在读取日志…'
  if (error.value) return '日志读取失败，请点击「刷新日志」重试。'
  return updatedAt.value ? 'Mihomo 日志暂无内容' : '等待读取日志'
})
const refreshStatus = computed(() => {
  if (loading.value) return '正在读取日志…'
  if (legacy.value || unavailable.value || (sourcesReady.value && !sources.value.length)) return '日志不可用'
  if (paused.value) return '已暂停刷新'
  if (!visible.value) return '页面隐藏，已暂停刷新'
  if (error.value) return '读取失败，稍后重试'
  return '每 3 秒自动刷新'
})

let timer: ReturnType<typeof setTimeout> | undefined
let controller: AbortController | undefined
let epoch = 0
let disposed = false

function stop() {
  epoch++
  clearTimeout(timer)
  controller?.abort()
  controller = undefined
  loading.value = false
}

function canRead() {
  return !disposed && active.value && visible.value && !!props.agentId
}

function schedule() {
  clearTimeout(timer)
  if (canRead() && !paused.value && !legacy.value && !unavailable.value && !(sourcesReady.value && !sources.value.length)) {
    timer = setTimeout(() => { void load() }, REFRESH_MS)
  }
}

async function load() {
  if (!canRead() || loading.value) return
  clearTimeout(timer)
  const request = ++epoch
  const id = props.agentId
  controller = new AbortController()
  const signal = controller.signal
  loading.value = true
  error.value = ''
  try {
    if (!sourcesReady.value) {
      const { data } = await agentApi.getLogSources(id, { signal })
      if (request !== epoch) return
      if (!data.success) throw new Error(data.message || '读取日志来源失败。')
      legacy.value = Boolean(data.legacy)
      sources.value = (Array.isArray(data.sources) ? data.sources : []).filter((source: AgentLogSource) =>
        (source.id === 'core' || source.id?.startsWith('core-')) && ['file', 'journal', 'unavailable'].includes(source.kind))
      sourcesReady.value = true
      if (!sources.value.some(source => source.id === sourceId.value)) {
        sourceId.value = (sources.value.find(source => source.kind !== 'unavailable') || sources.value[0])?.id || ''
        text.value = ''
        updatedAt.value = ''
      }
    }
    if (legacy.value || !currentSource.value || unavailable.value) return
    const { data } = await agentApi.getLogs(id, MAX_LINES, { source: sourceId.value }, { signal })
    if (request !== epoch) return
    if (!data.success) throw new Error(data.message || '读取 Mihomo 日志失败。')
    text.value = [data.logs, data.error_logs].filter(value => typeof value === 'string' && value)
      .map(value => value.replace(/(?:\r?\n)+$/, '')).join('\n').split(/\r?\n/).slice(-MAX_LINES).join('\n')
    updatedAt.value = new Date().toLocaleTimeString()
    await nextTick()
    if (request === epoch && follow.value) logPane.value?.scrollToBottom()
  } catch (failure) {
    if (request === epoch) error.value = panelError(failure, '暂时无法读取 Mihomo 日志。')
  } finally {
    if (request === epoch) {
      loading.value = false
      controller = undefined
      schedule()
    }
  }
}

function refresh() {
  stop()
  sourcesReady.value = false
  void load()
}

function changeSource(id: string) {
  if (id === sourceId.value) return
  stop()
  sourceId.value = id
  text.value = ''
  updatedAt.value = ''
  error.value = ''
  void load()
}

async function toggleFollow() {
  follow.value = !follow.value
  if (follow.value) {
    await nextTick()
    logPane.value?.scrollToBottom()
  }
}

function visibilityChanged() { visible.value = document.visibilityState !== 'hidden' }
watch([() => props.agentId, active, visible, paused], ([id], previous) => {
  stop()
  if (!previous || id !== previous[0]) {
    sources.value = []
    sourcesReady.value = false
    sourceId.value = ''
    legacy.value = false
    text.value = ''
    error.value = ''
    updatedAt.value = ''
  }
  if (!paused.value) void load()
}, { immediate: true })
watch(() => props.reloadToken, refresh)
onMounted(() => document.addEventListener('visibilitychange', visibilityChanged))
onUnmounted(() => {
  disposed = true
  stop()
  document.removeEventListener('visibilitychange', visibilityChanged)
})
</script>
