<template>
  <Dialog :open="true" @update:open="value => { if (!value) close() }">
    <DialogContent :show-close-button="false">
      <DialogHeader><DialogTitle>{{ sourceMode ? (source ? '编辑包含来源' : '新增包含来源') : policy ? '编辑来源设备' : '新增来源设备' }}</DialogTitle><DialogDescription>修改当前配置空间的来源草稿，保存并预览后再发布。</DialogDescription></DialogHeader>
      <form id="policy-source-form" class="space-y-4" @submit.prevent="apply">
        <div class="space-y-2"><Label for="source-edit-ip">客户端 IP / 网段</Label><Input id="source-edit-ip" v-model="draft.ip" class="min-h-11 font-mono" placeholder="192.168.0.105 或 192.168.0.0/24" /></div>
        <div v-if="!sourceMode" class="space-y-2"><Label for="source-edit-name">备注</Label><Input id="source-edit-name" v-model="draft.name" class="min-h-11" maxlength="80" /></div>
        <div v-if="!sourceMode" class="space-y-2"><Label for="source-edit-mode">策略</Label><Select v-model="draft.mode"><SelectTrigger id="source-edit-mode" class="min-h-11"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="smart">智能分流</SelectItem><SelectItem value="direct">直连</SelectItem></SelectContent></Select></div>
        <p v-if="error" role="alert" class="m-0 text-sm text-destructive-accent">{{ error }}</p>
      </form>
      <DialogFooter><Button variant="outline" class="min-h-11" @click="close">取消</Button><Button type="submit" form="policy-source-form" class="min-h-11">应用到草稿</Button></DialogFooter>
    </DialogContent>
  </Dialog>
</template>
<script setup lang="ts">
import { ref } from 'vue'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { normalizeClientRange } from '@/lib/oxidnsClientRange'
import { confirm } from '@/lib/feedback'
import type { OxiDNSDevicePolicy } from '@/types/oxidnsMonitor'
const props = withDefaults(defineProps<{ policy?: OxiDNSDevicePolicy; policies?: OxiDNSDevicePolicy[]; sourceMode?: boolean; source?: string; include?: string[] }>(), { policies: () => [], include: () => [] })
const emit = defineEmits<{ close: []; apply: [value: OxiDNSDevicePolicy]; 'apply-source': [value: string] }>()
const initial = { ip: props.source || '', name: '', mode: 'smart' as const, ...props.policy }
const draft = ref<OxiDNSDevicePolicy>({ ...initial }), error = ref('')
async function canLeave() {
  return JSON.stringify(draft.value) === JSON.stringify(initial) || await confirm('放弃尚未应用的来源修改？', { confirmText: '放弃修改' })
}
async function close() { if (await canLeave()) emit('close') }
function apply() {
  try {
    const ip = normalizeClientRange(draft.value.ip)
    if (props.sourceMode) {
      if (props.include.some(value => value !== props.source && normalizeClientRange(value) === ip)) { error.value = '此 IP 或网段已存在，请编辑已有来源。'; return }
      emit('apply-source', ip); return
    }
    if (props.policies.some(row => row.ip !== props.policy?.ip && normalizeClientRange(row.ip) === ip)) {
      error.value = '此 IP 或网段已存在，请编辑已有来源。'; return
    }
    if (!props.policy && props.policies.length >= 512) { error.value = '来源设备最多 512 项。'; return }
    emit('apply', { ...draft.value, ip, name: draft.value.name?.trim() || '' })
  } catch { error.value = '请输入有效的 IP 或 CIDR 网段。' }
}
defineExpose({ canLeave })
</script>
