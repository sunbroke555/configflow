<template>
  <FormField
    label="Web 监听端口"
    html-for="web-listen-port"
    :help="optionHelp('使用 Docker host 网络，需要避开主机端口冲突时设置。', '修改 ConfigFlow 容器内的 HTTP 监听端口并持久保存。bridge 需要同步修改端口映射，反向代理需要更新后端地址；服务域名与 Agent 的服务地址需另行更新。')"
  >
    <p v-if="loading" class="m-0 text-xs text-muted-foreground" role="status">正在读取监听端口…</p>
    <div v-else-if="loadError" class="flex flex-wrap items-center gap-2">
      <p class="m-0 text-xs text-destructive-accent" role="alert">{{ loadError }}</p>
      <Button variant="outline" size="sm" class="min-h-11" @click="load">重新读取</Button>
    </div>
    <p v-else-if="!status?.supported" class="m-0 text-xs text-muted-foreground">
      {{ status?.message || '当前部署不支持在 Web 中修改监听端口。' }}
    </p>
    <template v-else>
      <div class="flex items-center gap-1.5">
        <Input
          id="web-listen-port"
          v-model="draft"
          type="number"
          inputmode="numeric"
          min="1"
          max="65535"
          step="1"
          class="min-w-0 bg-background/50 font-mono"
          :disabled="!status.editable || busy || !!result"
          :aria-invalid="!!validationError"
          aria-describedby="web-port-status web-port-guidance"
          @keydown.enter.prevent="apply"
        />
        <Button
          v-if="status.editable"
          variant="outline"
          class="min-h-11 shrink-0 border-border/60 bg-background/40"
          :disabled="busy || !changed || !!validationError || !!result"
          @click="apply"
        >
          <Loader2 v-if="busy" class="size-3.5 animate-spin" />
          {{ busy ? '应用中…' : '应用' }}
        </Button>
      </div>
      <p id="web-port-status" class="m-0 text-xs text-muted-foreground" aria-live="polite">
        当前监听：<span class="font-mono num">{{ status.port ?? '未知' }}</span>
        · {{ status.ipv6 ? 'IPv4 / IPv6' : 'IPv4' }}
      </p>
      <p id="web-port-guidance" class="m-0 text-xs leading-relaxed text-muted-foreground">
        {{ status.editable ? 'host 直接使用此端口；bridge 需同步端口映射，反向代理需更新后端地址。' : status.message || '启动环境变量已指定端口，请修改环境变量并重建容器。' }}
      </p>
      <p v-if="validationError || saveError" class="m-0 text-xs text-destructive-accent" role="alert">
        {{ validationError || saveError }}
      </p>
      <div v-if="result" class="space-y-1.5 rounded-lg border border-border bg-background/40 p-3" role="status">
        <p class="m-0 text-[13px] font-medium" :class="result.confirmed ? 'text-success-accent' : 'text-warning-accent'">
          {{ result.confirmed ? `监听端口已改为 ${result.port}` : '应用结果尚未确认' }}
        </p>
        <p class="m-0 text-xs leading-relaxed text-muted-foreground">
          {{ result.confirmed ? '请使用新地址访问，可能需要重新登录；服务域名和 Agent 的服务地址需另行更新。' : '连接中断，端口可能已切换。请检查原地址和下面的新端口地址，再确认当前配置。' }}
        </p>
        <a
          :href="result.url"
          class="inline-flex min-h-11 max-w-full items-center gap-1.5 break-all rounded-sm font-mono text-xs text-primary-accent underline underline-offset-4 outline-none focus-visible:ring-2 focus-visible:ring-primary-accent"
        >
          {{ result.url }}<ExternalLink class="size-3.5 shrink-0" aria-hidden="true" />
        </a>
        <p class="m-0 text-xs leading-relaxed text-muted-foreground">
          这是按当前主机推算的 HTTP 直连地址，尚未验证外部可达性。经 HTTPS、反向代理或 bridge 访问时，请先核对主机地址与映射。
        </p>
      </div>
    </template>
  </FormField>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { isAxiosError } from 'axios'
import { ExternalLink, Loader2 } from '@lucide/vue'
import { webPortApi, type WebPortStatus } from '@/api'
import FormField from '@/components/common/FormField.vue'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { confirm, notify } from '@/lib/feedback'
import { optionHelp } from '@/lib/optionCopy'

const loading = ref(true)
const loadError = ref('')
const status = ref<WebPortStatus | null>(null)
const draft = ref<string | number>('')
const busy = ref(false)
const saveError = ref('')
const result = ref<{ port: number; url: string; confirmed: boolean } | null>(null)
let active = true
const port = computed(() => Number(draft.value))
const validationError = computed(() => status.value?.editable &&
  (!/^\d+$/.test(String(draft.value)) || !Number.isInteger(port.value) || port.value < 1 || port.value > 65535)
  ? '请输入 1–65535 之间的整数端口。' : '')
const changed = computed(() => port.value !== status.value?.port)

function referenceUrl(nextPort: number): string {
  const url = new URL(window.location.href)
  url.protocol = 'http:'
  url.port = String(nextPort)
  url.pathname = '/system-settings'
  url.search = ''
  url.hash = ''
  return url.href
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const { data } = await webPortApi.get()
    if (!active) return
    status.value = data
    draft.value = data.port ?? ''
  } catch (error) {
    if (!active) return
    loadError.value = isAxiosError(error) ? error.response?.data?.message || '读取监听端口失败，请重新读取。' : '读取监听端口失败，请重新读取。'
  } finally {
    if (active) loading.value = false
  }
}

async function apply() {
  if (busy.value || result.value || !status.value?.editable || !changed.value || validationError.value) return
  const nextPort = port.value
  busy.value = true
  saveError.value = ''
  try {
    const accepted = await confirm(
      `将 Web 监听端口从 ${status.value.port} 改为 ${nextPort}。应用后当前连接可能断开，需通过新端口访问。bridge 需同步端口映射，反向代理需更新后端地址；服务域名和 Agent 的服务地址需另行更新。`,
      { title: '修改 Web 监听端口', confirmText: '应用新端口' },
    )
    if (!accepted || !active) return
    const { data } = await webPortApi.update({ port: nextPort })
    if (!active) return
    if (!data.success) {
      saveError.value = data.message || '应用监听端口失败，请检查服务状态。'
      return
    }
    status.value = data
    draft.value = data.port ?? nextPort
    result.value = { port: data.port ?? nextPort, url: referenceUrl(data.port ?? nextPort), confirmed: true }
    notify.success('Web 监听端口已更新')
  } catch (error) {
    if (!active) return
    if (isAxiosError(error) && error.response) {
      saveError.value = error.response.data?.message || '应用监听端口失败，请检查服务状态。'
    } else {
      result.value = { port: nextPort, url: referenceUrl(nextPort), confirmed: false }
    }
  } finally {
    if (active) busy.value = false
  }
}

onMounted(load)
onBeforeUnmount(() => { active = false })
</script>
