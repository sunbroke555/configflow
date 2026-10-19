<template>
  <Button variant="outline" class="border-border/60 bg-background/40 max-sm:size-11 max-sm:px-0" aria-label="测速设置" title="测速设置" :disabled="disabled" @click="openSettings">
    <Settings2 class="size-4" /><span class="max-sm:hidden">测速设置</span>
  </Button>
  <Dialog :open="open" @update:open="value => { if (!saving) open = value }">
    <DialogContent class="max-w-md" :show-close-button="!saving" @escape-key-down="event => { if (saving) event.preventDefault() }" @interact-outside="event => { if (saving) event.preventDefault() }">
      <DialogHeader>
        <DialogTitle>测速设置</DialogTitle>
        <DialogDescription>仅影响节点库测速，不修改系统或设备的 DNS。</DialogDescription>
      </DialogHeader>
      <p v-if="error" role="alert" class="m-0 break-words text-sm text-destructive-accent">{{ error }}</p>
      <p v-if="loading" role="status" class="m-0 text-sm text-muted-foreground">正在读取测速设置…</p>
      <form v-else-if="loaded" class="space-y-4" @submit.prevent="save">
        <FormField label="测速 DNS" html-for="node-latency-dns" hint="填写 IPv4 或 IPv4:端口，多个地址用逗号或换行分隔；留空使用系统 DNS。" persistent-hint>
          <Textarea id="node-latency-dns" v-model="dns" class="min-h-24 font-mono" :disabled="saving || disabled" :rows="3" placeholder="留空使用系统 DNS" spellcheck="false" />
        </FormField>
        <p class="m-0 text-xs leading-relaxed text-muted-foreground">若节点域名被解析为 Fake-IP，请指定能返回真实 IP 的 DNS。</p>
        <DialogFooter>
          <Button type="button" variant="outline" class="min-h-11" :disabled="saving" @click="open = false">取消</Button>
          <Button type="submit" class="min-h-11" :disabled="saving || disabled"><Loader2 v-if="saving" class="size-4 animate-spin" />{{ saving ? '正在保存…' : '保存' }}</Button>
        </DialogFooter>
      </form>
      <Button v-else variant="outline" class="min-h-11" @click="load">重新读取</Button>
    </DialogContent>
  </Dialog>
</template>

<script setup lang="ts">
import { onUnmounted, ref } from 'vue'
import { Loader2, Settings2 } from '@lucide/vue'
import { nodeApi } from '@/api'
import FormField from '@/components/common/FormField.vue'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Textarea } from '@/components/ui/textarea'
import { notify } from '@/lib/feedback'

const props = defineProps<{ disabled?: boolean }>()
const emit = defineEmits<{ saved: [] }>()
const open = ref(false)
const loading = ref(false)
const loaded = ref(false)
const saving = ref(false)
const dns = ref('')
const error = ref('')
let disposed = false
let loadVersion = 0

async function openSettings() {
  if (props.disabled) return
  open.value = true
  await load()
}

async function load() {
  const version = ++loadVersion
  loading.value = true
  loaded.value = false
  error.value = ''
  try {
    const { data } = await nodeApi.latencySettings()
    if (disposed || version !== loadVersion) return
    if (!data?.success || !Array.isArray(data.dns_servers)) throw new Error('invalid settings')
    dns.value = data.dns_servers.join('\n')
    loaded.value = true
  } catch (failure: any) {
    if (!disposed && version === loadVersion) error.value = failure.response?.data?.message || '读取测速设置失败，请重试。'
  } finally {
    if (!disposed && version === loadVersion) loading.value = false
  }
}

async function save() {
  if (!loaded.value || saving.value || props.disabled) return
  saving.value = true
  error.value = ''
  try {
    const servers = dns.value.split(/[,\n]/).map(value => value.trim()).filter(Boolean)
    const { data } = await nodeApi.saveLatencySettings(servers)
    if (disposed) return
    if (!data?.success) throw new Error('save failed')
    notify.success('测速设置已保存')
    emit('saved')
    open.value = false
  } catch (failure: any) {
    if (!disposed) error.value = failure.response?.data?.message || '保存测速设置失败，请重试。'
  } finally {
    if (!disposed) saving.value = false
  }
}

onUnmounted(() => { disposed = true })
</script>
