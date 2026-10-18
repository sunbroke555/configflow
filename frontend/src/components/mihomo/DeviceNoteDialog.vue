<template>
  <Dialog :open="true" @update:open="value => { if (!value && !saving) emit('close') }">
    <DialogContent class="max-w-md" @escape-key-down="event => { if (saving) event.preventDefault() }" @interact-outside="event => { if (saving) event.preventDefault() }">
      <DialogHeader>
        <DialogTitle>设备备注</DialogTitle>
        <DialogDescription>同一配置空间的 Mihomo 来源和 DNS 设备策略共用此备注。</DialogDescription>
      </DialogHeader>
      <p class="m-0 break-all font-mono text-sm">{{ ip }}</p>
      <p v-if="catalog" class="m-0 text-xs text-muted-foreground">配置空间：{{ catalog.profile_name }}</p>
      <p v-if="error" role="alert" class="m-0 text-sm text-destructive-accent">{{ error }}</p>
      <p v-if="loading" role="status" class="text-sm text-muted-foreground">正在读取备注…</p>
      <form v-else-if="catalog" class="space-y-4" @submit.prevent="save">
        <FormField label="备注" html-for="device-note-name" hint="例如：客厅电视、工作电脑。留空可清除备注。">
          <Input id="device-note-name" v-model="name" maxlength="80" class="min-h-11" :disabled="saving" placeholder="填写设备备注" />
        </FormField>
        <DialogFooter>
          <Button type="button" variant="outline" class="min-h-11" :disabled="saving" @click="emit('close')">取消</Button>
          <Button type="submit" class="min-h-11" :disabled="saving"><Loader2 v-if="saving" class="size-4 animate-spin" />保存备注</Button>
        </DialogFooter>
      </form>
      <Button v-else variant="outline" class="min-h-11" @click="load">重新读取</Button>
    </DialogContent>
  </Dialog>
</template>
<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { Loader2 } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import FormField from '@/components/common/FormField.vue'
import { clientToolsApi } from '@/api'
import { type DeviceNotes } from '@/lib/device-notes'
import { panelError } from '@/types/mihomo'
import { notify } from '@/lib/feedback'
const props = defineProps<{ agentId: string; ip: string }>()
const emit = defineEmits<{ close: []; saved: [catalog: DeviceNotes] }>()
const catalog = ref<DeviceNotes | null>(null)
const name = ref('')
const error = ref('')
const loading = ref(true)
const saving = ref(false)
let disposed = false
async function load() {
  loading.value = true; error.value = ''
  try {
    const { data } = await clientToolsApi.notes(props.agentId)
    if (disposed) return
    if (!data?.success) throw new Error('读取备注失败。')
    catalog.value = data; name.value = data.notes[props.ip] || ''
  } catch (failure) { if (!disposed) error.value = panelError(failure) }
  finally { if (!disposed) loading.value = false }
}
async function save() {
  if (!catalog.value || saving.value) return
  saving.value = true; error.value = ''
  try {
    const { data } = await clientToolsApi.saveNote(props.agentId, { profile_id: catalog.value.profile_id, ip: props.ip, name: name.value, previous_name: catalog.value.notes[props.ip] || '' })
    if (disposed) return
    if (!data?.success) throw new Error('保存备注失败。')
    emit('saved', data); notify.success('设备备注已保存'); emit('close')
  } catch (failure) { if (!disposed) error.value = panelError(failure) }
  finally { if (!disposed) saving.value = false }
}
onMounted(load)
onUnmounted(() => { disposed = true })
</script>
