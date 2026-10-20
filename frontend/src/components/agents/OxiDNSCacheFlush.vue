<template>
  <Button type="button" variant="outline" class="min-h-11 shrink-0" :class="!busy && 'max-sm:size-11 max-sm:gap-0'"
    :aria-label="label" :title="label" :aria-busy="busy" :disabled="disabled || busy || !agentId" @click="flush">
    <Loader2 v-if="busy" class="size-4 animate-spin" /><Eraser v-else class="size-4" />
    <span :class="!busy && 'max-sm:sr-only'">{{ label }}</span>
  </Button>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { Eraser, Loader2 } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { oxidnsCacheApi } from '@/api/oxidnsCache'
import { confirm, notify } from '@/lib/feedback'

// Clears every cache plugin of the selected device's deployed configuration.
const props = defineProps<{ agentId: string; deviceName: string; disabled?: boolean }>()
const emit = defineEmits<{ flushed: [agentId: string] }>()
// A request belongs to the Agent it was started for, not the currently viewed one.
const busyAgent = ref('')
const busy = computed(() => Boolean(busyAgent.value) && busyAgent.value === props.agentId)
const label = computed(() => busy.value ? '清理缓存中…' : '清理缓存')
const knownNames: Record<string, string> = { lazy_cache: '主缓存', local_probe_cache: '本地探测缓存' }
const cacheName = (tag: string) => knownNames[tag] || tag

function failure(error: any, fallback: string) {
  return error?.response?.data?.message || fallback
}

async function flush() {
  if (props.disabled || busy.value || !props.agentId) return
  const agentId = props.agentId
  const device = props.deviceName || agentId
  busyAgent.value = agentId
  let tags: string[]
  try {
    // Resolve caches from the Agent's deployed configuration at click time.
    tags = (await oxidnsCacheApi.list(agentId)).data.caches
  } catch (error) {
    notify.error('无法读取缓存配置', failure(error, '暂时无法读取设备的缓存配置，请重试。'))
    return
  } finally {
    if (busyAgent.value === agentId) busyAgent.value = ''
  }
  if (agentId !== props.agentId) return
  if (!tags.length) {
    notify.warning('没有可清理的缓存', `「${device}」当前配置没有缓存插件。`)
    return
  }
  const accepted = await confirm(`将清空「${device}」的全部 DNS 缓存（${tags.map(cacheName).join('、')}）。之后的查询需要重新请求上游，DNS 服务持续运行。`, {
    title: '清理 DNS 缓存？', confirmText: '清理缓存', danger: true,
  })
  if (!accepted || agentId !== props.agentId || busy.value) return
  busyAgent.value = agentId
  try {
    await oxidnsCacheApi.flush(agentId, tags)
    notify.success('已清理缓存', `「${device}」的 DNS 缓存已清空。`)
    emit('flushed', agentId)
  } catch (error: any) {
    const flushed: string[] = Array.isArray(error?.response?.data?.flushed) ? error.response.data.flushed : []
    const reason = failure(error, '清理缓存失败，请重试。')
    notify.error('清理缓存未全部完成', flushed.length ? `${reason} 已清理：${flushed.map(cacheName).join('、')}。` : reason)
    if (flushed.length) emit('flushed', agentId)
  } finally {
    if (busyAgent.value === agentId) busyAgent.value = ''
  }
}
</script>
