<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { Loader2 } from '@lucide/vue'
import { profileApi, type ProfileAgentBinding, type ProfileRebindResult } from '@/api'
import { useProfileStore, type Profile } from '@/stores/profile'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Label } from '@/components/ui/label'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'

const props = defineProps<{ profile: Profile; deleteRequested?: boolean }>()
const emit = defineEmits<{ close: []; changed: []; 'continue-delete': [] }>()
const { profiles } = useProfileStore()
const choices = computed(() => profiles.value.filter(profile => profile.id !== props.profile.id))
const agents = ref<ProfileAgentBinding[]>([])
const loading = ref(true)
const saving = ref(false)
const error = ref('')
const loaded = ref(false)
const target = ref('')
const result = ref<ProfileRebindResult | null>(null)
let disposed = false
onUnmounted(() => { disposed = true })

const serviceName = (service: string) => ({ mihomo: 'Mihomo', oxidns: 'OxiDNS', mosdns: 'MosDNS' }[service] || service)
const address = (agent: ProfileAgentBinding) => {
  if (!agent.host) return agent.id
  const host = agent.host.includes(':') ? `[${agent.host}]` : agent.host
  return agent.port ? `${host}:${agent.port}` : host
}
const close = () => { if (!saving.value) emit('close') }
const load = async () => {
  loading.value = true
  loaded.value = false
  error.value = ''
  try {
    const { data } = await profileApi.agentBindings(props.profile.id)
    if (disposed) return
    agents.value = data.agents
    loaded.value = true
  } catch (failure: any) {
    if (!disposed) error.value = failure.response?.data?.message || '读取 Agent 绑定失败，请重试'
  } finally {
    if (!disposed) loading.value = false
  }
}
const rebind = async () => {
  if (!loaded.value || loading.value || saving.value || !agents.value.length || !choices.value.some(profile => profile.id === target.value)) return
  saving.value = true
  error.value = ''
  try {
    const { data } = await profileApi.rebindAgents(props.profile.id, target.value, agents.value.map(agent => agent.id))
    if (disposed) return
    result.value = data
    emit('changed')
  } catch (failure: any) {
    if (disposed) return
    error.value = failure.response?.data?.message || '未能确认改绑结果，请刷新列表后核对'
    // Even an ambiguous network failure must be reviewed again, never blindly retried.
    loaded.value = false
  } finally {
    if (!disposed) saving.value = false
  }
}
onMounted(load)
</script>

<template>
  <Dialog :open="true" @update:open="value => { if (!value) close() }">
    <DialogContent class="max-w-[520px]" :show-close-button="!saving" @escape-key-down="event => { if (saving) event.preventDefault() }" @interact-outside="event => { if (saving) event.preventDefault() }">
      <DialogHeader>
        <DialogTitle>{{ result ? 'Agent 绑定已调整' : deleteRequested ? '删除前调整 Agent 绑定' : 'Agent 绑定' }}</DialogTitle>
        <DialogDescription class="break-words">
          <template v-if="result">以下 {{ result.agents.length }} 个 Agent 已解除与「{{ result.from_profile.name }}」的绑定，并改绑到「{{ result.to_profile.name }}」。</template>
          <template v-else-if="deleteRequested">「{{ profile.name }}」仍被 Agent 使用。请先改绑到其他配置空间，再继续删除。</template>
          <template v-else>查看使用「{{ profile.name }}」的设备，或一次调整它们的配置空间。</template>
        </DialogDescription>
      </DialogHeader>

      <p v-if="loading" class="flex items-center gap-2 text-sm text-muted-foreground" role="status"><Loader2 class="size-4 animate-spin" />正在读取 Agent 绑定…</p>
      <template v-else>
        <ul v-if="(result?.agents || agents).length" class="m-0 max-h-56 list-none divide-y divide-border overflow-y-auto rounded-lg border border-border px-3">
          <li v-for="agent in result?.agents || agents" :key="agent.id" class="flex min-w-0 items-start justify-between gap-2 py-3">
            <div class="min-w-0">
              <p class="m-0 break-words text-sm font-medium">{{ agent.name }}</p>
              <p class="mt-1 mb-0 break-all font-mono text-xs text-muted-foreground">{{ address(agent) }}</p>
            </div>
            <Badge variant="outline" class="shrink-0">{{ serviceName(agent.service_type) }}</Badge>
          </li>
        </ul>
        <p v-else-if="loaded" class="text-sm text-muted-foreground">当前没有 Agent 绑定。</p>

        <template v-if="!result && agents.length">
          <div class="flex min-w-0 flex-col gap-1.5">
            <Label for="agent-binding-target">改绑到</Label>
            <Select v-model="target" :disabled="saving || !loaded">
              <SelectTrigger id="agent-binding-target" class="w-full min-w-0"><SelectValue placeholder="选择其他配置空间" /></SelectTrigger>
              <SelectContent>
                <SelectItem v-for="option in choices" :key="option.id" :value="option.id">{{ option.name }}（{{ option.id }}）</SelectItem>
              </SelectContent>
            </Select>
            <p v-if="!choices.length" class="text-xs text-warning-accent">Agent 必须归属一个配置空间，请先创建其他配置空间。</p>
          </div>
          <div class="space-y-2 text-xs leading-relaxed text-muted-foreground">
            <p>仅调整上列 Agent 的配置空间归属；Agent 接入信息、已部署文件和发布记录保留。后续拉取或发布配置将使用新空间。</p>
            <p>原空间的策略组、规则、DNS 等设置不会复制到新空间。本次操作不推送配置或重启服务。</p>
          </div>
        </template>
        <p v-if="result" class="text-xs leading-relaxed text-muted-foreground">已调整：上列 Agent 的配置空间归属。保留：Agent 接入信息、已部署文件、发布记录和原空间设置。本次未推送配置或重启服务；后续拉取或发布将使用「{{ result.to_profile.name }}」。</p>
      </template>

      <div v-if="error" role="alert" class="space-y-2 rounded-lg bg-destructive-soft p-3 text-sm text-destructive-accent">
        <p class="m-0 break-words">{{ error }}</p>
        <Button variant="outline" size="sm" :disabled="loading || saving" @click="load">刷新绑定列表</Button>
      </div>

      <DialogFooter>
        <Button variant="outline" :disabled="saving" @click="close">{{ result ? '完成' : '关闭' }}</Button>
        <Button v-if="!result && agents.length" :disabled="!target || !loaded || loading || saving" @click="rebind">
          <Loader2 v-if="saving" class="size-4 animate-spin" />
          {{ saving ? '正在调整绑定…' : '解除绑定并改绑' }}
        </Button>
        <Button v-else-if="deleteRequested && (result || loaded && !agents.length)" variant="destructive" @click="emit('continue-delete')">继续删除配置空间</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
