<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { customConfigApi, type PublishGuard } from '@/api'
import { notify } from '@/lib/feedback'
import YamlEditor from '@/components/YamlEditor.vue'
import LoadingRows from '@/components/common/LoadingRows.vue'
import { Button } from '@/components/ui/button'
import SettingsWorkspace from './SettingsWorkspace.vue'
import SettingsPublishBar from './SettingsPublishBar.vue'

const props = defineProps<{ open: boolean; profileId: string; contextLabel: string }>()
const emit = defineEmits<{ 'update:open': [value: boolean] }>()
const content = ref('')
const savedContent = ref('')
const loading = ref(false)
const saving = ref(false)
const loadError = ref('')
const dirty = computed(() => content.value !== savedContent.value)
let session = 0
let disposed = false
const current = (request: number) => !disposed && props.open && session === request

async function load() {
  const request = ++session
  loading.value = true
  saving.value = false
  loadError.value = ''
  content.value = savedContent.value = ''
  try {
    const response = await customConfigApi.getMihomo(props.profileId)
    if (current(request)) content.value = savedContent.value = response.data.config || ''
  } catch (failure: any) {
    if (current(request)) loadError.value = failure?.response?.data?.message || failure?.message || '基础配置读取失败，请重试。'
  } finally {
    if (current(request)) loading.value = false
  }
}

async function save(close = true): Promise<PublishGuard | null> {
  if (!props.open || disposed || loading.value || loadError.value || saving.value) return null
  const request = session
  const submitted = content.value
  saving.value = true
  try {
    const response = await customConfigApi.saveMihomo({ config: submitted }, props.profileId)
    if (!current(request)) return null
    savedContent.value = submitted
    notify.success('基础配置草稿已保存')
    if (close) emit('update:open', false)
    return response.data.publish_guard ?? null
  } catch (failure: any) {
    if (current(request)) notify.error('保存基础配置失败', failure?.response?.data?.message || failure?.message)
    return null
  } finally {
    if (current(request)) saving.value = false
  }
}

watch(() => [props.open, props.profileId] as const, () => {
  if (props.open) void load()
  else session++
}, { immediate: true, flush: 'sync' })
onUnmounted(() => { disposed = true })
</script>

<template>
  <SettingsWorkspace :open="open" title="Mihomo 基础配置" :dirty="dirty" :busy="saving"
    :save-disabled="loading || Boolean(loadError)" save-label="保存草稿" back-label="返回 Mihomo 面板" size="wide"
    description="设置端口、DNS、TUN 等基础选项。生成时会合并此配置空间的节点、策略组和规则。请使用 YAML 格式；留空并保存可恢复默认基础配置。"
    @update:open="emit('update:open', $event)" @save="save()">
    <p class="m-0 shrink-0 break-words border-b border-border px-4 py-2 text-xs text-muted-foreground sm:px-5">{{ contextLabel }}</p>
    <LoadingRows v-if="loading" :rows="5" />
    <div v-else-if="loadError" role="alert" class="p-4 text-sm text-destructive-accent">
      <p class="mb-3 break-words">{{ loadError }}</p>
      <Button variant="outline" class="min-h-11" @click="load">重新读取</Button>
    </div>
    <div v-else :inert="saving" class="min-h-0 min-w-0 flex-1 p-4 md:p-5">
      <YamlEditor v-model="content" class="h-full min-h-0" placeholder="输入 Mihomo 基础 YAML 配置，例如 mixed-port、dns、tun。" />
    </div>
    <template #footer="{ disabled }">
      <SettingsPublishBar :open="open" :profile-id="profileId" service-type="mihomo" :disabled="disabled"
        :save="() => save(false)" @save-draft="save()" />
    </template>
  </SettingsWorkspace>
</template>
