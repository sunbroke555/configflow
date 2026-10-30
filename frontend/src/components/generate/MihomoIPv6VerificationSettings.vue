<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { ChevronDown, LoaderCircle } from '@lucide/vue'
import { agentApi } from '@/api'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import type { IPv6VerificationSettingsResult } from '@/types/mihomoIPv6'

const props = defineProps<{ open: boolean; agentId: string; profileId: string; disabled?: boolean }>()
const emit = defineEmits<{ saved: [] }>()
const expanded = ref(false)
const pending = ref(false)
const saving = ref(false)
const error = ref('')
const saved = ref(false)
const selectedId = ref('')
const testURL = ref('')
const candidates = ref<IPv6VerificationSettingsResult['candidates']>([])
const loaded = ref(false)
let generation = 0
const missingSelection = computed(() => !!selectedId.value && !candidates.value.some(item => item.id === selectedId.value))
const unavailable = (id: string) => {
  const candidate = candidates.value.find(item => item.id === id)
  return !!id && (!candidate || candidate.enabled === false || candidate.status !== 'online' || candidate.supported === false)
}

function accept(data: IPv6VerificationSettingsResult) {
  if (!data?.success || data.agent_id !== props.agentId || data.profile_id !== props.profileId
    || !data.settings || !Array.isArray(data.candidates)) throw new Error('验收设置与当前设备不一致，请重新读取。')
  candidates.value = data.candidates
  selectedId.value = data.settings.client_agent_id || ''
  testURL.value = data.settings.test_url || ''
  loaded.value = true
}

async function load() {
  if (!props.open || !expanded.value || pending.value || saving.value) return
  const session = generation
  pending.value = true
  error.value = ''
  try {
    const { data } = await agentApi.getIPv6VerificationSettings(props.agentId, props.profileId)
    if (session === generation && props.open) accept(data)
  } catch (failure: any) {
    if (session === generation) error.value = failure?.response?.data?.message || failure?.message || '验收设置读取失败。'
  } finally { if (session === generation) pending.value = false }
}

async function save() {
  if (!loaded.value || props.disabled || pending.value || saving.value || unavailable(selectedId.value)) return
  const value = testURL.value.trim()
  if (value) {
    try {
      const url = new URL(value)
      if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) throw new Error()
    } catch { error.value = '检测网址需以 http:// 或 https:// 开头，且不能包含账号密码。'; return }
  }
  const session = generation
  saving.value = true
  saved.value = false
  error.value = ''
  try {
    const { data } = await agentApi.setIPv6VerificationSettings(props.agentId, props.profileId, { client_agent_id: selectedId.value, test_url: value })
    if (session !== generation || !props.open) return
    accept(data)
    saved.value = true
    emit('saved')
  } catch (failure: any) {
    if (session === generation) error.value = failure?.response?.data?.message || failure?.message || '验收设置保存失败。'
  } finally { if (session === generation) saving.value = false }
}

watch(expanded, value => { if (value && !loaded.value) void load() })
watch(() => [props.open, props.agentId, props.profileId] as const, () => {
  generation++
  expanded.value = false
  loaded.value = false
  pending.value = false
  saving.value = false
  saved.value = false
  error.value = ''
  selectedId.value = ''
  testURL.value = ''
  candidates.value = []
})
onUnmounted(() => { generation++ })
</script>

<template>
  <div class="min-w-0 border-t border-border pt-2" data-ipv6-verification-settings>
    <Button type="button" variant="ghost" class="min-h-11 px-2" :aria-expanded="expanded" @click="expanded = !expanded">验收设置<ChevronDown class="size-3.5" :class="expanded && 'rotate-180'" aria-hidden="true" /></Button>
    <div v-if="expanded" class="space-y-3 py-2">
      <p class="m-0 text-xs leading-relaxed text-muted-foreground">默认检查 Mihomo 主机。选择独立检测客户端后，发布成功还会从该设备检查整条上网路径。它需要位于 ROS 后面，并使用平时的 DNS 和网关。</p>
      <p v-if="pending" class="m-0 flex items-center gap-2 text-xs" role="status"><LoaderCircle class="size-4 animate-spin" aria-hidden="true" />正在读取验收设置…</p>
      <template v-if="loaded">
        <div class="space-y-1.5">
          <Label>独立检测客户端</Label>
          <Select :model-value="selectedId || '__none__'" :disabled="disabled || saving" @update:model-value="value => { selectedId = value === '__none__' ? '' : String(value); saved = false }">
            <SelectTrigger class="min-h-11 w-full min-w-0" aria-label="独立检测客户端"><SelectValue placeholder="仅检查 Mihomo 主机" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="__none__" class="min-h-11">不配置，仅检查 Mihomo 主机</SelectItem>
              <SelectItem v-if="missingSelection" :value="selectedId" disabled class="min-h-11">原检测客户端不可用，请重新选择</SelectItem>
              <SelectItem v-for="candidate in candidates" :key="candidate.id" :value="candidate.id" :disabled="unavailable(candidate.id)" class="min-h-11 break-words whitespace-normal">{{ candidate.name }}{{ candidate.supported === false ? '（需确认 Linux 环境并更新 Agent）' : unavailable(candidate.id) ? '（不可用）' : '' }}</SelectItem>
            </SelectContent>
          </Select>
          <p v-if="selectedId && unavailable(selectedId)" class="m-0 text-xs text-warning-accent">原检测客户端已离线或不符合条件，请明确选择另一台设备，或改为仅检查主机。</p>
          <p v-else-if="!candidates.length" class="m-0 text-xs text-muted-foreground">当前没有可用的独立 Linux Agent；不会自动把 Mihomo 主机当作客户端。</p>
        </div>
        <div class="space-y-1.5">
          <Label :for="`ipv6-verification-url-${agentId}`">检测网址</Label>
          <Input :id="`ipv6-verification-url-${agentId}`" v-model="testURL" class="min-h-11" :disabled="disabled || saving" placeholder="留空使用运行配置的健康检查网址" @input="saved = false" />
        </div>
        <Button type="button" variant="outline" class="min-h-11" :disabled="disabled || saving || pending || unavailable(selectedId)" @click="save">{{ saving ? '保存中…' : '保存验收设置' }}</Button>
        <p v-if="saved" class="m-0 text-xs text-success-accent" role="status">验收设置已保存，下次验证或发布后使用。</p>
      </template>
      <p v-if="error" class="m-0 break-words text-xs text-destructive-accent" role="alert">{{ error }}</p>
      <Button v-if="error && !loaded" type="button" variant="outline" class="min-h-11" :disabled="pending" @click="load">重新读取</Button>
    </div>
  </div>
</template>
