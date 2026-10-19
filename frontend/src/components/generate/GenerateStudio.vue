<template>
  <SectionCard :padded="false" class="mb-5 grid min-w-0 max-md:mb-3 md:grid-cols-[152px_minmax(0,1fr)]" aria-label="配置生成工作台">
    <aside class="min-w-0 border-b border-border bg-background/30 md:border-r md:border-b-0">
      <div class="hidden p-2 md:block">
        <h2 class="m-0 px-3 pt-2 pb-3 text-xs font-medium text-muted-foreground">生成软件</h2>
        <nav class="flex flex-col gap-1" aria-label="生成软件">
          <Button
            v-for="target in targets"
            :key="target.key"
            variant="ghost"
            :class="cn('min-h-11 w-full justify-start gap-2 px-3', active === target.key && 'bg-primary-soft text-primary-accent hover:bg-primary-soft hover:text-primary-accent')"
            :aria-pressed="active === target.key"
            :disabled="running"
            @click="active = target.key"
          >
            <component :is="target.icon" class="size-4 shrink-0" aria-hidden="true" />
            {{ target.title }}
            <ChevronRight v-if="active === target.key" class="ml-auto size-3.5 shrink-0" aria-hidden="true" />
          </Button>
        </nav>
      </div>
      <div class="flex items-center gap-2 p-3 md:hidden">
        <label for="generate-software" class="shrink-0 text-xs font-medium text-muted-foreground">生成软件</label>
        <Select v-model="active" :disabled="running">
          <SelectTrigger id="generate-software" class="min-h-11 min-w-0 flex-1 bg-card" aria-label="生成软件">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem v-for="target in targets" :key="target.key" :value="target.key" class="min-h-11">
              <span class="flex items-center gap-2">
                <component :is="target.icon" class="size-4 shrink-0" aria-hidden="true" />
                {{ target.title }}
              </span>
            </SelectItem>
          </SelectContent>
        </Select>
      </div>
    </aside>

    <div class="@container/studio min-w-0">
      <section v-if="current" class="border-b border-border" :aria-label="`${current.title} 配置编辑`">
        <div class="flex flex-wrap items-center justify-between gap-x-5 gap-y-3 px-5 py-4 max-md:gap-2 max-md:p-3">
          <div class="min-w-0">
            <h2 class="m-0 flex items-center gap-2 text-sm font-semibold">
              <component :is="current.icon" class="size-4 text-primary-accent" aria-hidden="true" />
              {{ current.title }} 配置
              <span v-if="isPhone" class="cf-compact-help"><FieldHelp :label="`${current.title} 配置`" :text="current.desc" /></span>
            </h2>
            <p v-if="!isPhone" class="mt-1 mb-0 text-xs text-muted-foreground">{{ current.desc }}</p>
          </div>
          <div class="flex flex-wrap items-center gap-2" role="group" :aria-label="`${current.title} 配置操作`">
            <Button
              v-for="action in settingsActions"
              :key="action.label"
              variant="outline"
              class="min-h-11 border-border-strong bg-primary-soft text-primary-accent hover:bg-accent hover:text-foreground"
              :disabled="action.loading"
              @click="action.run"
            >
              <Loader2 v-if="action.loading" class="size-4 animate-spin" />
              <component :is="action.icon" v-else class="size-4" />
              {{ action.label }}
            </Button>
          </div>
        </div>
      </section>

      <div class="grid grid-cols-[minmax(0,1fr)] @min-[720px]/studio:grid-cols-[260px_minmax(0,1fr)]">
        <section class="order-2 min-w-0 border-t border-border p-5 max-md:p-3 @min-[720px]/studio:order-none @min-[720px]/studio:border-t-0 @min-[720px]/studio:border-r" role="list" aria-label="生成步骤">
          <div
            v-for="(step, i) in steps"
            :key="step.title"
            role="listitem"
            :class="cn('gen-step relative grid grid-cols-[26px_1fr] gap-3 pb-5 max-md:gap-2 max-md:pb-2 last:pb-0', stepState(i))"
          >
            <span
              class="gen-bullet grid size-[26px] place-items-center rounded-full border-[1.5px] border-border-strong bg-card font-mono text-[11px] text-muted-foreground transition-all duration-300"
            >
              <Check v-if="stepState(i) === 'done'" class="size-3.5" :stroke-width="2.5" />
              <X v-else-if="stepState(i) === 'fail'" class="size-3.5" :stroke-width="2.5" />
              <template v-else>{{ i + 1 }}</template>
            </span>
            <div class="min-w-0">
              <b class="mt-[3px] block text-[13px] font-semibold">{{ step.title }}</b>
              <span class="block truncate font-mono text-[12px] text-muted-foreground">{{ step.meta }}</span>
            </div>
          </div>
        </section>

        <section class="min-w-0 overflow-hidden" aria-label="生成结果">
          <div class="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-border px-5 py-3 max-md:gap-2 max-md:px-3 max-md:py-2" role="group" aria-label="生成和导出操作">
            <div class="flex min-w-max flex-1 items-center gap-2.5 max-sm:min-w-0">
              <span class="flex shrink-0 gap-1.5 max-sm:hidden" aria-hidden="true">
                <i class="size-2.5 rounded-full bg-accent" /><i class="size-2.5 rounded-full bg-accent" /><i class="size-2.5 rounded-full bg-accent" />
              </span>
              <span class="truncate font-mono text-[12px] text-muted-foreground" :title="fileName">{{ fileName }}</span>
              <span v-if="lines.length && !running" class="whitespace-nowrap font-mono text-[11px] text-muted-foreground">· {{ lines.length }} 行</span>
            </div>
            <Button v-if="isPhone" size="sm" class="min-h-11 shadow-glow" :disabled="running" @click="generate">
              <Play class="size-3.5" />
              生成
            </Button>
            <div class="flex flex-wrap items-center gap-1.5 max-sm:w-full max-sm:gap-1 max-sm:[&>button]:flex-1 max-sm:[&>button]:px-1 max-sm:[&>button]:text-[13px]">
              <Button variant="ghost" size="sm" class="min-h-11" :disabled="!output || running" @click="copyOutput">
                <Copy v-if="!isPhone" class="size-3.5" />
                复制
              </Button>
              <Button
                v-for="action in exportActions"
                :key="action.label"
                variant="outline"
                size="sm"
                class="min-h-11"
                :aria-busy="action.loading || undefined"
                :disabled="action.loading"
                @click="action.run"
              >
                <Loader2 v-if="action.loading" class="size-3.5 animate-spin" />
                <component :is="action.icon" v-else-if="!isPhone" class="size-3.5" />
                {{ action.label }}
              </Button>
              <Button
                variant="outline"
                size="sm"
                class="min-h-11"
                :disabled="!output || running || pushing || deploying || !pushTargets.length"
                :title="pushTitle"
                @click="pushToAgents"
              >
                <Loader2 v-if="pushing || deploying" class="size-3.5 animate-spin" />
                <Send v-else-if="!isPhone" class="size-3.5" />
                推送
              </Button>
              <Button v-if="!isPhone" size="sm" class="min-h-11 shadow-glow" :disabled="running" @click="generate">
                <Play class="size-3.5" />
                生成
              </Button>
            </div>
          </div>

          <div v-if="pushResults.length" class="min-w-0 space-y-3 border-b border-border px-5 py-3" aria-label="配置发布结果">
            <div v-for="result in pushResults" :key="result.id" class="min-w-0 space-y-1 text-xs">
              <p class="m-0 font-medium">{{ result.name }}<template v-if="!result.task.deployment_id"> · 发布失败</template></p>
              <AgentOperationSteps v-if="result.task.deployment_id" :task="result.task" :busy="deploymentBusy(result.id)" />
              <AgentFailureDetails :show-progress-message="false" :task="result.task" />
              <RouterLink class="inline-flex min-h-11 items-center text-primary-accent underline underline-offset-4" to="/agents">前往 Agent 查看发布记录</RouterLink>
            </div>
          </div>
          <div class="relative">
            <pre
              ref="codeEl"
              class="gen-code m-0 h-[520px] overflow-auto bg-background/70 py-4 font-mono text-[12.5px] leading-[1.7] max-md:py-3"
              :class="lines.length ? 'max-md:h-[60dvh]' : running ? 'max-md:h-40' : 'max-md:hidden'"
              :aria-busy="running"
            ><span
              v-for="(line, i) in lines"
              :key="i"
              class="gen-line block whitespace-pre pr-[18px]"
              :class="i >= freshFrom && 'is-new'"
              v-html="line || ' '"
            /><span v-if="running && phase === 'write'" class="gen-caret" aria-hidden="true" /></pre>

            <div
              v-if="!lines.length && !running"
              class="absolute inset-0 grid place-content-center gap-2.5 bg-background/70 px-4 text-center text-muted-foreground max-md:relative max-md:min-h-40 max-md:py-4"
            >
              <BrandMark class="mx-auto size-11 text-primary" />
              <b class="font-display text-[24px] font-medium text-foreground/80">{{ error ? '生成失败' : '准备就绪' }}</b>
              <span class="text-[12.5px] [overflow-wrap:anywhere]">{{ error || `按「生成」开始编译，或 ${metaKey} + Enter` }}</span>
            </div>
          </div>
        </section>
      </div>

      <footer v-if="current" class="grid min-w-0 items-center gap-3 border-t border-border px-5 py-4 max-md:gap-2 max-md:p-3 @min-[720px]/studio:grid-cols-[220px_minmax(0,1fr)] @min-[720px]/studio:gap-5" aria-label="订阅地址">
        <div class="min-w-0">
          <h3 class="m-0 flex items-center gap-2 text-[13px] font-semibold">
            <component :is="current.icon" class="size-4 shrink-0 text-primary-accent" aria-hidden="true" />
            {{ current.title }} 订阅地址
          </h3>
          <p class="mt-1 mb-0 text-[11.5px] text-muted-foreground">客户端可直接订阅此地址。</p>
        </div>
        <div class="flex min-w-0 items-center gap-2">
          <Input
            :model-value="current.urlDisplay"
            readonly
            class="h-11 min-w-0 bg-background/50 font-mono text-[11.5px]"
            :aria-label="`${current.title} 配置 URL`"
          />
          <Button
            variant="outline"
            size="icon"
            class="size-11 shrink-0"
            :title="`复制 ${current.title} 配置 URL`"
            :aria-label="`复制 ${current.title} 配置 URL`"
            @click="emit('copy', current.url, current.title)"
          >
            <Copy class="size-4" />
          </Button>
        </div>
      </footer>
    </div>
  </SectionCard>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch, type Component } from 'vue'
import { useMediaQuery } from '@vueuse/core'
import { RouterLink, useRouter } from 'vue-router'
import { Check, ChevronRight, Copy, Loader2, Play, Send, X } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import BrandMark from '@/components/common/BrandMark.vue'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import SectionCard from '@/components/common/SectionCard.vue'
import FieldHelp from '@/components/common/FieldHelp.vue'
import { agentApi, generateApi, ruleSetApi, statsApi } from '@/api'
import { createDeploymentId, useAgentDeployments, type AgentDeployment } from '@/composables/useAgentDeployments'
import AgentFailureDetails from '@/components/agents/AgentFailureDetails.vue'
import AgentOperationSteps from '@/components/agents/AgentOperationSteps.vue'
import { submitAgentRequests } from '@/lib/agentRequests'
import { deploymentErrorSummary } from '@/lib/agentFailure'
import { consumeAction } from '@/lib/actions'
import { notify } from '@/lib/feedback'
import { copyText } from '@/lib/clipboard'
import { cn } from '@/lib/utils'
import { useLive } from '@/stores/live'

const isPhone = useMediaQuery('(max-width: 639px)')

type TargetKey = 'mihomo' | 'surge' | 'loon' | 'mosdns' | 'oxidns'

export interface StudioTarget {
  key: TargetKey
  title: string
  desc: string
  icon: Component
  url: string
  urlDisplay: string
  actions: Array<{ label: string; icon: Component; run: () => void; loading?: boolean; group?: 'settings' | 'export' }>
}

const props = defineProps<{ targets: StudioTarget[] }>()
const emit = defineEmits<{ (e: 'copy', url: string, title: string): void }>()

const router = useRouter()
const live = useLive()
const metaKey = /Mac|iPhone|iPad/.test(navigator.platform) ? '⌘' : 'Ctrl'

const active = ref<TargetKey>('mihomo')
const current = computed(() => props.targets.find(t => t.key === active.value))
const settingsActions = computed(() => current.value?.actions.filter(action => action.group === 'settings') ?? [])
const exportActions = computed(() => current.value?.actions.filter(action => action.group !== 'settings') ?? [])

const FILES: Record<TargetKey, string> = { mihomo: 'config.yaml', surge: 'surge.conf', loon: 'loon.conf', mosdns: 'mosdns.yaml', oxidns: 'oxidns.yaml' }
const fileName = computed(() => FILES[active.value])

/* ---------- 步骤（计数取真实数据） ---------- */
const counts = ref({ subscriptions: 0, nodes: 0, proxyGroups: 0, rules: 0, ruleSets: 0 })

const steps = computed(() => {
  const c = counts.value
  if (active.value === 'mosdns' || active.value === 'oxidns') {
    return [
      { title: '收集资源', meta: `${c.ruleSets} 规则集 · ${c.rules} 条规则` },
      { title: '读取分流规则', meta: '直连 / 代理规则集' },
      { title: '组装 DNS 上游', meta: '国内 / 国外 / Fallback' },
      { title: '编译分流序列', meta: '缓存 · Hosts · 默认转发' },
      { title: '写出配置', meta: fileName.value }
    ]
  }
  return [
    { title: '收集资源', meta: `${c.subscriptions} 订阅 · ${c.nodes} 节点` },
    { title: '解析节点', meta: 'Sub-Store 转换' },
    { title: '组装策略组', meta: `${c.proxyGroups} 组 · 正则筛选` },
    { title: '编译规则', meta: `${c.rules} 条 · ${c.ruleSets} 规则集` },
    { title: '写出配置', meta: fileName.value }
  ]
})

/** -1 未开始；0..4 进行中；5 全部完成 */
const stepIndex = ref(-1)
const failedAt = ref(-1)
const stepState = (i: number) => {
  if (failedAt.value === i) return 'fail'
  if (i < stepIndex.value) return 'done'
  if (i === stepIndex.value) return 'run'
  return ''
}

/* ---------- 生成 ---------- */
const running = ref(false)
const phase = ref<'steps' | 'write' | ''>('')
const output = ref('')
const lines = ref<string[]>([])
const freshFrom = ref(0)
const error = ref('')
const codeEl = ref<HTMLElement | null>(null)

const wait = (ms: number) => new Promise(resolve => window.setTimeout(resolve, ms))
const frame = () => new Promise(resolve => requestAnimationFrame(() => resolve(null)))

const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')

/** 轻量高亮：YAML 键 / 字符串 / 数字与布尔 / 注释；Surge / Loon 的 [段] 与 key = value */
const highlight = (raw: string): string => {
  if (/^\s*#/.test(raw) || /^\s*\/\//.test(raw)) return `<span class="y-c">${esc(raw)}</span>`
  if (/^\[.*\]\s*$/.test(raw)) return `<span class="y-k">${esc(raw)}</span>`
  let s = esc(raw)
  s = s.replace(/(&quot;|")([^"]*)\1|'([^']*)'/g, m => `<span class="y-s">${m}</span>`)
  s = s.replace(/^(\s*-?\s*)([\w.-]+)(:)(?=\s|$)/, '$1<span class="y-k">$2</span><span class="y-d">$3</span>')
  s = s.replace(/^([\w .-]+?)(\s=\s)/, '<span class="y-k">$1</span><span class="y-d">$2</span>')
  s = s.replace(/(:\s|=\s|,\s?)(-?\d+(?:\.\d+)?|true|false|null)(?=\s*(?:,|$|\}))/g, '$1<span class="y-n">$2</span>')
  s = s.replace(/\b(DIRECT|REJECT|MATCH|FINAL)\b/g, '<span class="y-n">$1</span>')
  return s
}

const loadCounts = async () => {
  const [stats, ruleSets] = await Promise.allSettled([statsApi.getOverview(), ruleSetApi.getAll()])
  if (stats.status === 'fulfilled' && stats.value.data?.success) {
    const d = stats.value.data.data
    counts.value = {
      ...counts.value,
      subscriptions: d.subscriptions?.total ?? 0,
      nodes: d.nodes?.total ?? 0,
      proxyGroups: d.proxyGroups?.total ?? 0,
      rules: d.rules?.total ?? 0
    }
  }
  if (ruleSets.status === 'fulfilled') counts.value = { ...counts.value, ruleSets: (ruleSets.value.data || []).length }
}

const generate = async () => {
  if (running.value) return
  running.value = true
  phase.value = 'steps'
  error.value = ''
  failedAt.value = -1
  lines.value = []
  output.value = ''
  freshFrom.value = 0
  const target = active.value
  const api = {
    mihomo: generateApi.previewMihomo,
    surge: generateApi.previewSurge,
    loon: generateApi.previewLoon,
    mosdns: generateApi.previewMosdns,
    oxidns: generateApi.previewOxidns
  }[target]

  const request = api()
    .then(res => ({ ok: true as const, content: String(res.data?.content ?? res.data ?? '') }))
    .catch((err: any) => ({ ok: false as const, message: err.response?.data?.message || '生成失败' }))
  loadCounts()

  try {
    for (let i = 0; i < 4; i++) {
      stepIndex.value = i
      await wait(320 + Math.random() * 180)
    }
    const result = await request
    if (!result.ok) {
      failedAt.value = 3
      error.value = result.message
      notify.error(`${FILES[target]} 生成失败`, result.message)
      return
    }

    stepIndex.value = 4
    phase.value = 'write'
    output.value = result.content
    const all = result.content.replace(/\n$/, '').split('\n')
    // 约 150 帧写完：短配置逐行出现，长配置按块追加，避免卡顿
    const perFrame = Math.max(1, Math.ceil(all.length / 150))
    for (let i = 0; i < all.length; i += perFrame) {
      freshFrom.value = lines.value.length
      lines.value.push(...all.slice(i, i + perFrame).map(highlight))
      await frame()
      if (codeEl.value) codeEl.value.scrollTop = codeEl.value.scrollHeight
    }
    stepIndex.value = 5
    freshFrom.value = lines.value.length
    await nextTick()
    if (codeEl.value) codeEl.value.scrollTop = 0
    notify.success(`${FILES[target]} 已生成 · ${all.length} 行`)
  } finally {
    running.value = false
    phase.value = ''
  }
}

const copyOutput = async () => {
  try {
    await copyText(output.value)
    notify.success('已复制到剪贴板')
  } catch {
    notify.error('复制失败，请在面板中手动选择')
  }
}

/* ---------- 推送：当前配置空间里同类型且在线的 Agent ---------- */
const pushing = ref(false)
const submissionFailures = ref<Record<string, Partial<AgentDeployment>>>({})
const pushedAgents = ref<Record<string, { name: string; serviceType: string }>>({})
const pushTargets = computed(() =>
  active.value === 'loon'
    ? []
    : live.boundAgents.value.filter(a => a.service_type === active.value && live.isOnline(a))
)
const pushTitle = computed(() => {
  if (active.value === 'loon') return 'Loon 没有对应的 Agent'
  if (!pushTargets.value.length) return `没有在线的 ${current.value?.title} Agent`
  return `推送到 ${pushTargets.value.map(a => a.name).join('、')}`
})

// push-config 只是提交事务发布（202），必须轮询到终态；否则服务端任务停在中间状态，
// 下一次推送会因「未完成的发布」被 409 拒绝。
const { deployments, busy: deploymentBusy, track: trackDeployment, beginSubmission, endSubmission } = useAgentDeployments(() => live.refresh())
const deploying = computed(() => pushTargets.value.some(a => deploymentBusy(a.id)))
const pushResults = computed(() => Object.entries(pushedAgents.value).flatMap(([id, agent]) => {
  const task = submissionFailures.value[id] || deployments[id]
  return agent.serviceType === active.value && live.boundAgents.value.some(bound => bound.id === id) && task
    ? [{ id, name: agent.name, task }] : []
}))
// 「已校验，待激活」需要在 Agent 页手动激活，这里没有激活入口，只给出指引。
watch(() => Object.entries(deployments).filter(([, task]) => task.status === 'ready').map(([id]) => id).join(','), ids => {
  if (!ids) return
  notify.info('配置已校验，等待激活', '请到 Agent 页面激活本次发布')
})

type PushOutcome = 'submitted' | 'done' | 'failed' | 'busy'

const pushOne = async (agent: { id: string; name: string }): Promise<PushOutcome> => {
  delete submissionFailures.value[agent.id]
  pushedAgents.value[agent.id] = { name: agent.name, serviceType: active.value }
  const deploymentId = createDeploymentId()
  const transactional = active.value !== 'surge'
  if (transactional && !beginSubmission(agent.id, deploymentId)) return 'busy'
  try {
    const { data } = await agentApi.pushConfig(agent.id, deploymentId)
    if (data.deployment_id === deploymentId && typeof data.status === 'string') {
      endSubmission(agent.id, deploymentId, data)
      return 'submitted'
    }
    if (transactional) {
      endSubmission(agent.id, deploymentId, 'unknown')
      return 'submitted'
    }
    if (data?.success === false || data?.restart?.success === false) {
      submissionFailures.value[agent.id] = { ...data, error: data.restart?.success === false ? `配置已写入，但重启失败：${data.restart.message || '服务未确认启动。'}` : data.error || data.message, error_details: data.restart?.error_details || data.error_details }
      notify.error(`「${agent.name}」配置发布失败`, deploymentErrorSummary(submissionFailures.value[agent.id]))
      return 'failed'
    }
    return 'done'
  } catch (error: any) {
    // 409 携带的未完成任务同样跟踪到终态，旧任务收尾后即可再次推送。
    if (error.response?.data?.deployment_id) {
      if (error.response.data.deployment_id === deploymentId) {
        endSubmission(agent.id, deploymentId, error.response.data)
        return 'submitted'
      }
      endSubmission(agent.id, deploymentId, 'rejected')
      trackDeployment(agent.id, error.response.data)
      if (error.response.status === 409) notify.warning(`「${agent.name}」上一次发布尚未结束，正在等待其完成后再推送`)
      return 'submitted'
    }
    if (transactional && (!error.response || error.response.status >= 500)) {
      endSubmission(agent.id, deploymentId, 'unknown')
      return 'submitted'
    }
    if (transactional && endSubmission(agent.id, deploymentId, 'rejected')) return 'submitted'
    submissionFailures.value[agent.id] = { ...error.response?.data, error: error.response?.data?.error || error.response?.data?.message || '发布请求失败，请重试。' }
    notify.error(`「${agent.name}」配置发布失败`, deploymentErrorSummary(submissionFailures.value[agent.id]))
    return 'failed'
  }
}

const pushToAgents = async () => {
  const targets = pushTargets.value.filter(a => !deploymentBusy(a.id))
  if (!targets.length || pushing.value) return
  pushing.value = true
  try {
    const outcomes = await submitAgentRequests(targets, pushOne)
    const done = outcomes.filter(o => o === 'done').length
    if (done) notify.success(`已推送到 ${done} 台 Agent`)
    // submitted 的结果由 useAgentDeployments 轮询到终态后提示。
  } finally {
    pushing.value = false
    live.refresh()
  }
}

/* ---------- 快捷键与跨页动作 ---------- */
const onKeydown = (event: KeyboardEvent) => {
  if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
    event.preventDefault()
    generate()
  }
}

const runPending = () => {
  if (consumeAction(router, 'generate-mihomo')) {
    active.value = 'mihomo'
    generate()
  }
}

watch(active, () => {
  if (running.value) return
  lines.value = []
  output.value = ''
  stepIndex.value = -1
  failedAt.value = -1
  error.value = ''
})

watch(() => router?.currentRoute.value.query.run, run => run === 'generate-mihomo' && runPending())

onMounted(() => {
  live.start()
  loadCounts()
  document.addEventListener('keydown', onKeydown)
  runPending()
})

onUnmounted(() => {
  live.stop()
  document.removeEventListener('keydown', onKeydown)
})
</script>

<style scoped>
.gen-step::before {
  content: '';
  position: absolute;
  top: 26px;
  bottom: 2px;
  left: 12px;
  width: 2px;
  background: var(--border-strong);
}

.gen-step::after {
  content: '';
  position: absolute;
  top: 26px;
  left: 12px;
  width: 2px;
  height: 0;
  background: var(--primary);
  transition: height 0.5s var(--ease-flow);
}

.gen-step:last-child::before,
.gen-step:last-child::after {
  display: none;
}

.gen-step.done::after {
  height: calc(100% - 28px);
}

.gen-step.run .gen-bullet {
  border-color: var(--primary);
  color: var(--primary-accent);
  box-shadow: 0 0 0 5px var(--primary-soft);
}

.gen-step.done .gen-bullet {
  border-color: var(--primary);
  background: var(--primary);
  color: var(--primary-foreground);
}

.gen-step.fail .gen-bullet {
  border-color: var(--destructive);
  background: var(--destructive);
  color: var(--destructive-foreground);
}

.gen-code {
  counter-reset: ln;
}

.gen-line::before {
  counter-increment: ln;
  content: counter(ln);
  display: inline-block;
  width: 46px;
  padding-right: 14px;
  text-align: right;
  color: var(--muted-foreground);
  opacity: 0.6;
}

.gen-line.is-new {
  animation: line-in 0.35s var(--ease-flow);
}

@keyframes line-in {
  from {
    background: var(--primary-soft);
  }
}

.gen-caret {
  display: inline-block;
  width: 8px;
  height: 15px;
  margin-left: 60px;
  vertical-align: -3px;
  background: var(--primary-accent);
  animation: caret-blink 1s steps(1) infinite;
}

@keyframes caret-blink {
  50% {
    opacity: 0;
  }
}

.gen-code :deep(.y-k) {
  color: var(--primary-accent);
}

.gen-code :deep(.y-s) {
  color: var(--success-accent);
}

.gen-code :deep(.y-n) {
  color: var(--info-accent);
}

.gen-code :deep(.y-c) {
  color: var(--muted-foreground);
  font-style: italic;
}

.gen-code :deep(.y-d) {
  color: var(--muted-foreground);
}
</style>
