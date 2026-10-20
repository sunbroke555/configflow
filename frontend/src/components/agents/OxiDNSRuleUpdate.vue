<template>
  <template v-if="presentation === 'card'">
    <AgentStatusRow v-if="hasStatus" :key="task?.id || 'submitting'" label="规则更新" :status="cardStatus" :tone="cardTone">
      <template v-if="cardTone === 'success'" #default>
        <p class="m-0 num">{{ progress }}</p>
      </template>
      <template v-else #notice>
        <p v-if="task && task.status !== 'idle'" class="m-0 num text-muted-foreground">{{ progress }}</p>
        <p v-if="error" role="alert" class="m-0" :class="cardTone === 'warning' ? 'text-warning-accent' : 'text-destructive-accent'">{{ error }}</p>
        <div v-if="!busy && (needsCheck || error || task?.status === 'failed')" class="flex flex-wrap gap-2">
          <Button type="button" variant="outline" size="sm" class="min-h-11" :disabled="disabled || !agentId" @click="act">{{ needsCheck ? '查询更新结果' : '重新更新' }}</Button>
          <Button v-if="needsCheck" type="button" variant="ghost" size="sm" class="min-h-11" :disabled="disabled || !agentId"
            title="先查询当前任务，未运行时才重新提交更新" @click="retry">重新尝试更新</Button>
        </div>
      </template>
    </AgentStatusRow>
  </template>
  <!-- Toolbar: joins the parent action row. Progress lives on the button and
       success becomes a toast; only results needing attention stay on screen. -->
  <div v-else class="contents">
    <Button type="button" variant="outline" class="min-h-11 shrink-0 max-sm:size-11 max-sm:gap-0"
      :aria-label="toolbarLabel" :title="toolbarLabel" :aria-busy="busy" :disabled="disabled || busy || !agentId" @click="act">
      <Loader2 v-if="busy" class="size-4 animate-spin" /><RefreshCw v-else class="size-4" />
      <span class="num max-sm:sr-only">{{ toolbarLabel }}</span>
    </Button>
    <Button v-if="needsCheck && !busy" type="button" variant="ghost" class="min-h-11 shrink-0"
      :disabled="disabled || !agentId" title="先查询当前任务，未运行时才重新提交更新" @click="retry">重新尝试更新</Button>
    <!-- contain keeps a long reason from widening the action row; phones,
         where the button is an icon, also show running progress here. -->
    <p v-if="attention || running" :role="error || task?.status === 'failed' ? 'alert' : 'status'" aria-live="polite"
      class="order-last m-0 w-full min-w-0 basis-full break-words text-xs leading-5 [contain:inline-size] sm:text-right"
      :class="attention ? needsCheck ? 'text-warning-accent' : 'text-destructive-accent' : 'text-muted-foreground sm:hidden'">
      <span v-if="statusText" class="num">{{ statusText }}</span><span v-if="error">{{ statusText ? ' · ' : '' }}{{ error }}</span>
    </p>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { Loader2, RefreshCw } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import AgentStatusRow from '@/components/agents/AgentStatusRow.vue'
import { oxidnsRuleUpdateApi, type OxiDNSRuleUpdateResponse, type OxiDNSRuleUpdateTask } from '@/api/oxidnsRuleUpdate'
import { notify } from '@/lib/feedback'

const props = defineProps<{ agentId: string; disabled?: boolean; presentation?: 'toolbar' | 'card' }>()
const emit = defineEmits<{ updated: [] }>()
const task = ref<OxiDNSRuleUpdateTask | null>(null)
const error = ref('')
const busy = ref(false)
const submitting = ref(false)
const needsCheck = ref(false)
const actionLabel = computed(() => busy.value ? task.value?.status === 'running' || submitting.value ? '更新规则中…' : '查询更新状态…' : needsCheck.value ? '查询更新结果' : '更新规则')
const hasStatus = computed(() => Boolean(submitting.value || needsCheck.value || error.value || (task.value && task.value.status !== 'idle')))
const cardStatus = computed(() => {
  if (submitting.value) return '正在提交'
  if (busy.value) return task.value?.status === 'running' ? task.value.phase === 'reloading' ? '正在热重载' : '正在下载' : '正在查询'
  if (needsCheck.value) return '待确认'
  if (error.value || task.value?.status === 'failed') return '失败'
  return '已完成'
})
const cardTone = computed(() => busy.value ? 'progress' : needsCheck.value ? 'warning' : error.value || task.value?.status === 'failed' ? 'danger' : 'success')
let generation = 0
let controller: AbortController | undefined
let timer: ReturnType<typeof setTimeout> | undefined
let polls = 0
let failures = 0
let watching = false
let awaitingNewTask = false
let previousTaskId = ''
let observedTaskId = ''
let watchedTaskId = ''
let baselineKnown = false
const count = (value: number) => Number.isFinite(value) && value >= 0 ? value : 0
const progress = computed(() => task.value ? `下载 ${count(task.value.downloaded)}/${count(task.value.download_total)} · 热重载 ${count(task.value.reloaded)}/${count(task.value.reload_total)}` : '')
const toolbarLabel = computed(() => {
  if (running.value && task.value) {
    const value = task.value
    return value.phase === 'reloading' ? `更新中 · 热重载 ${count(value.reloaded)}/${count(value.reload_total)}`
      : `更新中 · 下载 ${count(value.downloaded)}/${count(value.download_total)}`
  }
  return actionLabel.value
})
const running = computed(() => busy.value && task.value?.status === 'running' && !submitting.value)
// Successful and idle states need no persistent text in the toolbar.
const attention = computed(() => Boolean(error.value || task.value?.status === 'failed' || (needsCheck.value && !busy.value)))
const statusText = computed(() => {
  if (!task.value || task.value.status === 'idle') return ''
  const labels = { running: task.value.phase === 'reloading' ? '正在热重载规则' : '正在下载规则', succeeded: '规则更新完成', failed: task.value.code === 'RULE_UPDATE_UNKNOWN' ? '规则更新结果待确认' : '规则更新失败' }
  return `${needsCheck.value && !busy.value ? '规则更新结果待确认' : labels[task.value.status]} · ${progress.value}`
})
const messages: Record<string, string> = {
  UPGRADE_REQUIRED: '请将此 Agent 升级到 1.8.30-go 或更新版本后使用更新规则。',
  API_NOT_CONFIGURED: '请在 OxiDNS 设置中启用管理 API 并发布一次。',
  RULE_UPDATE_NOT_CONFIGURED: '请先配置可下载的规则并重新发布 OxiDNS 配置，然后重试。',
  CORE_UPDATE_UNSUPPORTED: '当前 OxiDNS 核心不支持规则更新接口，请升级核心后重试。',
  SERVICE_BUSY: 'Agent 正在执行其他操作，请等待完成后重试。',
  RULE_UPDATE_BUSY: '此设备正在更新规则，请等待当前更新完成。',
  CORE_BUSY: 'OxiDNS 正在执行下载或热重载，请稍后重试。',
  SNAPSHOT_CHANGED: '运行中的配置与部署文件不一致，请确认配置已应用后重试。',
  DOWNLOAD_FAILED: '规则未全部下载成功，请检查 OxiDNS 日志后重试。',
  RELOAD_FAILED: '部分规则热重载失败，请检查 OxiDNS 日志后重试。',
  RULE_UPDATE_UNKNOWN: '规则更新结果尚未确认，请查询结果或核对 OxiDNS 日志。',
}
function message(data: any, fallback: string) {
  return typeof data?.message === 'string' && data.message ? data.message : messages[data?.code] || fallback
}
function clearTimer() { if (timer) clearTimeout(timer); timer = undefined }
function unknown(reason: string) {
  clearTimer()
  busy.value = false
  needsCheck.value = true
  error.value = reason
}
function queue(current: number) {
  clearTimer()
  if (current !== generation) return
  if (polls >= 300 || failures >= 5) {
    busy.value = false
    needsCheck.value = true
    error.value = '暂未确认最终结果，请稍后查询更新结果。'
    return
  }
  busy.value = true
  timer = setTimeout(() => { timer = undefined; void check(current) }, 2000)
}
function apply(data: OxiDNSRuleUpdateResponse, current: number, initial = false) {
  if (current !== generation) return
  if (!data.task || !['idle', 'running', 'succeeded', 'failed'].includes(data.task.status)) {
    throw new Error(message(data, '规则更新状态无效，请稍后查询结果。'))
  }
  const next = data.task
  if (awaitingNewTask && (next.status === 'idle' || next.id === previousTaskId || (!baselineKnown && next.status !== 'running'))) {
    unknown('更新请求尚未确认，暂未找到本次任务。请查询更新结果或核对 OxiDNS 日志。')
    return
  }
  if (!initial && watching && watchedTaskId && (next.status === 'idle' || next.id !== watchedTaskId)) {
    unknown('更新任务已丢失或被替换，结果未知。请查询更新结果或核对 OxiDNS 日志。')
    return
  }
  awaitingNewTask = false
  baselineKnown = true
  observedTaskId = next.id
  task.value = next
  error.value = ''
  if (next.status === 'failed' && next.code === 'RULE_UPDATE_UNKNOWN') {
    unknown(message(next, message(data, messages.RULE_UPDATE_UNKNOWN)))
    if (watching && !initial) notify.warning('OxiDNS 规则更新结果待确认', error.value)
    watching = false
    return
  }
  if (next.status === 'running') {
    watching = true
    watchedTaskId = next.id
    needsCheck.value = true
    queue(current)
  } else {
    busy.value = false
    needsCheck.value = false
    if (next.status === 'failed') error.value = message(next, message(data, '部分规则可能已更新，请检查规则下载和热重载任务后重试。'))
    if (watching && !initial) {
      if (next.status === 'succeeded') { notify.success('OxiDNS 规则更新完成', progress.value); emit('updated') }
      if (next.status === 'failed') notify.error('OxiDNS 规则更新失败', error.value)
    }
    watching = false
  }
}
async function check(current: number, initial = false) {
  const id = props.agentId
  controller = new AbortController()
  if (!initial) polls++
  try {
    const { data } = await oxidnsRuleUpdateApi.status(id, controller.signal)
    if (current !== generation) return
    apply(data, current, initial)
    failures = 0
  } catch (failure: any) {
    if (current !== generation) return
    if (!initial) error.value = message(failure?.response?.data, failure?.message || '无法读取规则更新状态，请重试。')
    if (watching || awaitingNewTask) { failures++; queue(current) }
    else busy.value = false
  }
}
async function act() {
  if (props.disabled || busy.value || !props.agentId) return
  const current = generation
  polls = 0
  failures = 0
  busy.value = true
  error.value = ''
  if (needsCheck.value) { await check(current); return }
  previousTaskId = observedTaskId
  watchedTaskId = ''
  task.value = null
  watching = true
  submitting.value = true
  controller = new AbortController()
  try {
    const { data } = await oxidnsRuleUpdateApi.start(props.agentId, controller.signal)
    if (current !== generation) return
    apply(data, current)
  } catch (failure: any) {
    if (current !== generation) return
    // A lost POST response is ambiguous: query before allowing another write.
    const code = failure?.response?.data?.code
    if (!failure?.response || ['AGENT_UNREACHABLE', 'INVALID_RESPONSE', 'RULE_UPDATE_UNKNOWN'].includes(code) || (failure.response.status >= 500 && !code)) {
      awaitingNewTask = true
      needsCheck.value = true
      error.value = '更新请求结果未收到，正在查询任务状态。'
      await check(current)
    } else {
      watching = false
      busy.value = false
      error.value = message(failure.response.data, '规则更新请求失败，请检查 Agent 和规则维护配置后重试。')
      notify.error('无法更新 OxiDNS 规则', error.value)
    }
  } finally {
    if (current === generation) submitting.value = false
  }
}
async function retry() {
  if (props.disabled || busy.value || !props.agentId) return
  const current = generation
  busy.value = true
  controller = new AbortController()
  try {
    const { data } = await oxidnsRuleUpdateApi.status(props.agentId, controller.signal)
    if (current !== generation) return
    if (!data.task || !['idle', 'running', 'succeeded', 'failed'].includes(data.task.status)) throw new Error('无法确认当前任务状态，请稍后重试。')
    // A deliberate retry must first establish a fresh baseline. Never submit
    // another write while the Agent reports a running task.
    awaitingNewTask = false
    watchedTaskId = ''
    watching = false
    polls = 0
    failures = 0
    apply(data, current, true)
    if (data.task.status !== 'running') await act()
  } catch (failure: any) {
    if (current !== generation) return
    unknown(message(failure?.response?.data, '无法确认当前任务状态，尚未重新提交更新，请稍后重试。'))
  }
}
watch(() => props.agentId, id => {
  generation++
  clearTimer()
  controller?.abort()
  task.value = null
  error.value = ''
  needsCheck.value = false
  watching = false
  awaitingNewTask = false
  watchedTaskId = ''
  baselineKnown = false
  observedTaskId = ''
  polls = 0
  failures = 0
  busy.value = Boolean(id)
  submitting.value = false
  if (id) void check(generation, true)
}, { immediate: true })
onBeforeUnmount(() => { generation++; clearTimer(); controller?.abort() })
// The card places the action beside rule sync controls and progress in its status area.
defineExpose({ act, busy, actionLabel, hasStatus })
</script>
