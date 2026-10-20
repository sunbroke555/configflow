<template>
  <Button type="button" variant="outline" size="sm" class="min-h-11 shrink-0 max-sm:size-11 max-sm:gap-0"
    :aria-label="busy ? '正在清理缓存…' : actionLabel" :title="busy ? '正在清理缓存…' : actionLabel" :aria-busy="busy"
    :disabled="disabled || busy || !agentId" @click="flush">
    <Loader2 v-if="busy" class="size-3.5 animate-spin" /><Eraser v-else class="size-3.5" />
    <span class="max-sm:sr-only">{{ busy ? '清理中…' : '清理缓存' }}</span>
  </Button>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { Eraser, Loader2 } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { oxidnsCacheApi } from '@/api/oxidnsCache'
import { confirm, notify } from '@/lib/feedback'

// `target` is the selected cache tag; empty means every cache of this Agent.
const props = defineProps<{ agentId: string; deviceName: string; target?: string; disabled?: boolean }>()
const emit = defineEmits<{ flushed: [agentId: string] }>()
// A request belongs to the Agent it was started for, not the currently viewed one.
const busyAgent = ref('')
const busy = computed(() => Boolean(busyAgent.value) && busyAgent.value === props.agentId)
const knownNames: Record<string, string> = { lazy_cache: '主缓存', local_probe_cache: '本地探测缓存' }
const label = (tag: string) => knownNames[tag] ? `${knownNames[tag]}（${tag}）` : tag
const actionLabel = computed(() => props.target ? `清理缓存实例 ${props.target}` : '清理全部缓存')

function failure(error: any, fallback: string) {
  return error?.response?.data?.message || fallback
}

async function flush() {
  if (props.disabled || busy.value || !props.agentId) return
  const agentId = props.agentId
  const device = props.deviceName || agentId
  const target = props.target || ''
  busyAgent.value = agentId
  let tags: string[]
  try {
    // Resolve targets from the Agent's deployed configuration at click time.
    const { data } = await oxidnsCacheApi.list(agentId)
    tags = target ? data.caches.filter(tag => tag === target) : data.caches
  } catch (error) {
    busyAgent.value = ''
    notify.error('无法读取缓存实例', failure(error, '暂时无法读取设备的缓存配置，请重试。'))
    return
  }
  busyAgent.value = ''
  if (agentId !== props.agentId) return
  if (!tags.length) {
    notify.warning('没有可清理的缓存', target ? `「${device}」当前配置中没有缓存实例 ${target}，请刷新面板后重试。` : `「${device}」当前配置没有缓存插件。`)
    return
  }
  const accepted = await confirm(`将清空「${device}」上${tags.map(label).join('、')}的全部缓存条目。之后的查询需要重新请求上游，DNS 服务持续运行。`, {
    title: '清理 DNS 缓存？', confirmText: '清理缓存', danger: true,
  })
  if (!accepted || agentId !== props.agentId || busyAgent.value) return
  busyAgent.value = agentId
  try {
    await oxidnsCacheApi.flush(agentId, tags)
    notify.success('已清理缓存', `「${device}」：${tags.map(label).join('、')}`)
    emit('flushed', agentId)
  } catch (error: any) {
    const flushed: string[] = Array.isArray(error?.response?.data?.flushed) ? error.response.data.flushed : []
    const reason = failure(error, '清理缓存失败，请重试。')
    notify.error('清理缓存未全部完成', flushed.length ? `${reason} 已清理：${flushed.map(label).join('、')}。` : reason)
    if (flushed.length) emit('flushed', agentId)
  } finally {
    if (busyAgent.value === agentId) busyAgent.value = ''
  }
}
</script>
