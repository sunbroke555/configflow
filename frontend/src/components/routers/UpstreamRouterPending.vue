<script setup lang="ts">
// 上级路由待发布提醒：按实际读取的差异报告与发布预览提示，并直达该路由器的预览发布。
// 只提醒和链接；路由器写入始终在「上游路由」页预览确认后进行。
import { computed, inject, onUnmounted, ref, watch } from 'vue'
import { routerKey, type RouteLocationRaw } from 'vue-router'
import { TriangleAlert } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { upstreamRouterApi, type UpstreamRouterStatus } from '@/api/upstreamRouterPending'
import { summarizeUpstreamRouters, upstreamRouterLink, type UpstreamRouterSummary } from '@/lib/upstreamRouterPending'

const props = defineProps<{
  /** 作为代理网关的 Mihomo Agent；为空时不读取，发出 none */
  agentId: string
  /** 变化时重新读取并跳过服务端短时复用，用于保存草稿、发布或重新检查之后 */
  refreshKey?: unknown
  /** 只显示每台路由器一行，不列出待发布项目 */
  compact?: boolean
}>()
const emit = defineEmits<{ status: [summary: UpstreamRouterSummary] }>()

type LineState = 'pending' | 'blocked' | 'unknown'
interface Line { key: string; state: LineState; name: string; count: number; profile: string; detail: string; action: string; to: RouteLocationRaw }

const navigation = inject(routerKey, null)
const routers = ref<UpstreamRouterStatus[]>([])
const agentProfileId = ref('')
const failure = ref('')
const STATES = new Set(['synced', 'pending', 'blocked', 'unknown'])
let session = 0
let controller: AbortController | null = null
let disposed = false

const valid = (item: any): item is UpstreamRouterStatus => !!item && typeof item === 'object'
  && typeof item.id === 'string' && !!item.id && STATES.has(item.state)
const messageOf = (error: any) => error?.response?.data?.message || error?.message || '读取失败，请稍后重试。'

function itemsText(router: UpstreamRouterStatus) {
  const items = Array.isArray(router.items) ? router.items : []
  const shown = items.slice(0, 3).map(item => `${item.label}：${item.status}`).join('；')
  return items.length > 3 ? `${shown} 等 ${items.length} 项` : shown
}

const lines = computed<Line[]>(() => {
  if (failure.value) {
    return [{ key: '', state: 'unknown', name: '', count: 0, profile: '', detail: failure.value, action: '查看上级路由', to: { path: '/routers' } }]
  }
  return routers.value.filter(router => router.state !== 'synced').map(router => {
    const base = {
      key: `${router.profile_id}:${router.id}`, state: router.state as LineState, name: router.name || router.id,
      count: Number(router.pending) || 0, message: router.message || '',
      profile: router.profile_id && router.profile_id !== agentProfileId.value ? router.profile_name || router.profile_id : '',
    }
    if (router.state === 'pending') {
      return { ...base, detail: props.compact ? '' : itemsText(router), action: '查看并发布', to: upstreamRouterLink(router, true) }
    }
    if (router.state === 'blocked') return { ...base, detail: base.message, action: '查看原因', to: upstreamRouterLink(router, true) }
    return { ...base, detail: base.message || '读取路由器失败。', action: '查看差异报告', to: upstreamRouterLink(router, false) }
  })
})

function hrefOf(to: RouteLocationRaw) {
  if (navigation) return navigation.resolve(to).href
  const target = to as { path: string; query?: Record<string, string> }
  const query = new URLSearchParams(target.query || {}).toString()
  return query ? `${target.path}?${query}` : target.path
}

function go(event: MouseEvent, to: RouteLocationRaw) {
  if (!navigation || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
  event.preventDefault()
  void navigation.push(to)
}

function publish() {
  emit('status', failure.value ? { state: 'unknown', pending: 0 } : summarizeUpstreamRouters(routers.value))
}

async function load(refresh: boolean) {
  controller?.abort()
  controller = null
  const generation = ++session
  const id = props.agentId
  if (!id) {
    routers.value = []
    failure.value = ''
    publish()
    return
  }
  const abort = new AbortController()
  controller = abort
  try {
    const { data } = await upstreamRouterApi.pending(id, { refresh, signal: abort.signal })
    if (disposed || generation !== session) return
    if (data?.success !== true || data.agent_id !== id || !Array.isArray(data.routers)) {
      throw new Error(data?.message || '上级路由状态响应无效，请稍后重试。')
    }
    routers.value = data.routers.filter(valid)
    agentProfileId.value = typeof data.agent_profile_id === 'string' ? data.agent_profile_id : ''
    failure.value = ''
  } catch (error) {
    if (disposed || generation !== session || abort.signal.aborted) return
    // 读取失败不能当成已同步，也不保留旧的状态冒充当前结果。
    routers.value = []
    failure.value = messageOf(error)
  } finally {
    if (!disposed && generation === session) {
      controller = null
      publish()
    }
  }
}

watch(() => [props.agentId, props.refreshKey] as const, ([agentId, refreshKey], previous) => {
  const sameAgent = !!previous && previous[0] === agentId
  if (!sameAgent) {
    routers.value = []
    failure.value = ''
    agentProfileId.value = ''
  }
  void load(sameAgent && previous![1] !== refreshKey)
}, { immediate: true })

onUnmounted(() => {
  disposed = true
  session++
  controller?.abort()
})
</script>

<template>
  <div v-if="lines.length" class="min-w-0 space-y-2 text-xs leading-relaxed" role="status" aria-live="polite" data-upstream-router-pending>
    <div v-for="line in lines" :key="line.key" class="flex min-w-0 flex-wrap items-center justify-between gap-x-3 gap-y-1.5"
      :data-upstream-router="line.key || undefined" :data-state="line.state">
      <div class="flex min-w-0 flex-[1_1_14rem] items-start gap-2 [overflow-wrap:anywhere]">
        <TriangleAlert class="mt-0.5 size-4 shrink-0 text-warning-accent" aria-hidden="true" />
        <div class="min-w-0">
          <p class="m-0 text-foreground">
            <template v-if="line.state === 'pending'">上级路由「{{ line.name }}」有 <span class="num">{{ line.count }}</span> 项待发布</template>
            <template v-else-if="line.state === 'blocked'">上级路由「{{ line.name }}」有改动暂时无法发布</template>
            <template v-else-if="line.name">无法确认上级路由「{{ line.name }}」状态</template>
            <template v-else>无法确认上级路由状态</template>
          </p>
          <p v-if="line.profile || line.detail" class="m-0 text-muted-foreground">
            <template v-if="line.profile">配置空间「{{ line.profile }}」<template v-if="line.detail"> · </template></template>{{ line.detail }}
          </p>
        </div>
      </div>
      <!-- 换行到下一行时与文字对齐（图标 16px + 间距 8px）。 -->
      <Button as-child variant="outline" size="sm" class="ml-6 shrink-0 pointer-coarse:min-h-11">
        <a :href="hrefOf(line.to)" @click="go($event, line.to)">{{ line.action }}</a>
      </Button>
    </div>
  </div>
</template>
