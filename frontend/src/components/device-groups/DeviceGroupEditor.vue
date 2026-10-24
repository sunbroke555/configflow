<script setup lang="ts">
import { computed, ref } from 'vue'
import { Plus, Trash2 } from '@lucide/vue'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { Textarea } from '@/components/ui/textarea'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import FormField from '@/components/common/FormField.vue'
import Segmented from '@/components/common/Segmented.vue'
import OxiDNSClientSelect from '@/components/agents/OxiDNSClientSelect.vue'
import { confirm } from '@/lib/feedback'
import { normalizeClientRange } from '@/lib/oxidnsClientRange'
import { usePageReloadGuard } from '@/lib/pageReloadGuard'
import { deviceGroupCopy } from '@/lib/deviceGroupCopy'
import { memberSample } from '@/lib/deviceGroupActivity'
import type { DeviceGroup, DeviceGroupActivity, DeviceGroupsSnapshot } from '@/types/deviceGroups'

const props = defineProps<{ group: DeviceGroup; snapshot: DeviceGroupsSnapshot; notes: Record<string, string>; activity?: DeviceGroupActivity | null }>()
const emit = defineEmits<{ close: []; apply: [group: DeviceGroup, notes: Record<string, string>] }>()
const draft = ref<DeviceGroup>(JSON.parse(JSON.stringify(props.group)))
const notes = ref({ ...props.notes })
const member = ref(''), error = ref(''), confirming = ref(false)
const isDefault = computed(() => draft.value.id === 'default')
const comparable = () => JSON.stringify({ group: draft.value, notes: notes.value })
const initial = comparable()
const dirty = computed(() => comparable() !== initial || !!member.value.trim())
const samples = computed(() => Object.fromEntries(draft.value.members.map(ip => [ip, memberSample(ip, props.activity?.clients || [])])))
const recordsAvailable = computed(() => props.activity?.agents.some(agent => agent.state === 'available'))
const modeOptions = [{ value: 'follow' as const, label: '跟随全局' }, { value: 'direct' as const, label: '全部直连' }, { value: 'proxy' as const, label: '全部代理' }]
usePageReloadGuard(() => dirty.value)
async function canLeave() {
  if (confirming.value) return false
  if (!dirty.value) return true
  confirming.value = true
  try { return await confirm('设备组的修改尚未应用到草稿，关闭会丢失本次编辑。', { title: '放弃设备组修改？', confirmText: '放弃修改', cancelText: '继续编辑' }) }
  finally { confirming.value = false }
}
async function close() { if (await canLeave()) emit('close') }
function add(value: string) {
  error.value = ''
  try {
    const ip = normalizeClientRange(value)
    if (draft.value.members.includes(ip)) throw new Error('此 IP 或网段已在本设备组中。')
    const other = props.snapshot.device_groups.groups.find(group => group.id !== draft.value.id && group.members.includes(ip))
    if (other) throw new Error(`此 IP 或网段已在「${other.name}」中，请先移除原成员。`)
    draft.value.members.push(ip); member.value = ''
  } catch (failure) { error.value = failure instanceof Error ? failure.message : '请输入有效的 IP 或 CIDR 网段。' }
}
function apply() {
  error.value = ''
  if (member.value.trim()) return void (error.value = '请先添加正在输入的成员，或清空输入。')
  if (!draft.value.name.trim()) return void (error.value = '请填写设备组名称。')
  const group: DeviceGroup = { ...draft.value, name: draft.value.name.trim(), note: draft.value.note?.trim() || '' }
  if (isDefault.value) {
    delete group.mode; delete group.policy; delete group.keep_blocking; delete group.dns
  } else {
    group.mode ||= 'follow'
    if (group.mode === 'proxy' && !props.snapshot.policy_groups.some(policy => policy.name === group.policy)) return void (error.value = '请选择一个已启用的策略组。')
    if (group.mode === 'follow') { delete group.policy; delete group.keep_blocking; delete group.dns }
    else {
      group.keep_blocking = !!group.keep_blocking
      if (group.mode === 'direct') { delete group.policy; group.dns ||= 'follow' }
      else delete group.dns
    }
  }
  emit('apply', group, Object.fromEntries(Object.entries(notes.value).map(([ip, name]) => [ip, name.trim()]).filter(([, name]) => !!name)))
}
defineExpose({ canLeave })
</script>

<template>
  <Sheet :open="true" @update:open="value => { if (!value) close() }">
    <SheetContent :show-close-button="false" class="h-dvh w-full max-w-none gap-0 p-0 sm:w-[560px] sm:max-w-[560px]" @escape-key-down.prevent="close" @pointer-down-outside.prevent="close">
      <SheetHeader class="shrink-0 border-b border-border p-3 sm:p-5">
        <SheetTitle>{{ isDefault ? '默认组例外成员' : '编辑设备组' }}<span v-if="group.name" class="ml-2">「{{ group.name }}」</span></SheetTitle>
        <SheetDescription>应用到页面草稿后，预览生成差异再保存。</SheetDescription>
      </SheetHeader>
      <form id="device-group-editor" class="flex min-h-0 flex-1 flex-col" @submit.prevent="apply">
        <div class="min-h-0 flex-1 space-y-5 overflow-y-auto p-3 sm:p-5">
          <FormField label="名称" html-for="device-group-name"><Input id="device-group-name" v-model="draft.name" maxlength="80" :disabled="isDefault" class="min-h-11" /></FormField>
          <FormField label="备注" html-for="device-group-note"><Textarea id="device-group-note" v-model="draft.note" maxlength="500" :rows="2" placeholder="设备组用途（可选）" /></FormField>
          <FormField v-if="!isDefault" label="模式" v-bind="deviceGroupCopy.mode">
            <Segmented :model-value="draft.mode || 'follow'" :options="modeOptions" label="设备组模式" block @update:model-value="value => draft.mode = value" />
          </FormField>
          <p v-if="isDefault || !draft.mode || draft.mode === 'follow'" class="m-0 text-sm text-muted-foreground">按规则列表处理，与未归组设备相同。</p>
          <template v-else>
            <FormField v-if="draft.mode === 'proxy'" label="策略组" html-for="device-group-policy">
              <Select v-model="draft.policy"><SelectTrigger id="device-group-policy" class="min-h-11"><SelectValue placeholder="选择已启用的策略组" /></SelectTrigger><SelectContent><SelectItem v-for="policy in snapshot.policy_groups" :key="policy.id" :value="policy.name">{{ policy.name }}</SelectItem></SelectContent></Select>
            </FormField>
            <FormField label="仍执行 DNS 拦截" v-bind="deviceGroupCopy.blocking">
              <label for="device-group-blocking" class="flex min-h-11 cursor-pointer items-center gap-3"><Switch id="device-group-blocking" :model-value="!!draft.keep_blocking" :disabled="!snapshot.capabilities.keep_blocking_editable" aria-label="仍执行 DNS 拦截" @update:model-value="value => draft.keep_blocking = value" /><span class="text-sm">{{ draft.keep_blocking ? '已开启' : '已关闭' }}</span></label>
              <p v-if="!snapshot.capabilities.keep_blocking_editable" class="m-0 text-xs text-warning-accent" role="status">{{ snapshot.capabilities.keep_blocking_reason || '完整参数模式暂不支持复制规则列表中的 DNS 拦截。' }}</p>
            </FormField>
            <FormField v-if="draft.mode === 'direct'" label="直连 DNS" html-for="device-group-dns" v-bind="deviceGroupCopy.dns">
              <Select :model-value="draft.dns || 'follow'" @update:model-value="value => draft.dns = value as 'follow' | 'direct_full'"><SelectTrigger id="device-group-dns" class="min-h-11 w-full min-w-0 [&_[data-slot=select-value]]:truncate"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="follow">直连 DNS</SelectItem><SelectItem value="direct_full">直连 DNS（完整，带缓存和路由器联动）</SelectItem></SelectContent></Select>
            </FormField>
          </template>
          <FormField :label="isDefault ? '显式例外成员' : '成员'" v-bind="deviceGroupCopy.members">
            <OxiDNSClientSelect model-value="" agent-id="" recorder="" range="all" :allow-all="false" :notes="notes" :external-clients="activity?.clients || []" :external-available="!!activity?.agents.some(agent => agent.state === 'available')" placeholder="从查询记录选择…" @update:model-value="add" />
            <div class="mt-2 flex min-w-0 gap-2"><Input id="device-group-member" v-model="member" class="min-h-11 min-w-0 flex-1 font-mono" aria-label="成员 IP 或网段" placeholder="192.168.50.0/24 或 IPv6" @keydown.enter.prevent="add(member)" /><Button type="button" variant="outline" class="min-h-11 shrink-0" :disabled="!member.trim()" @click="add(member)"><Plus class="size-4" />添加</Button></div>
          </FormField>
          <div v-if="draft.members.length" class="cf-table-wrap"><table class="cf-table cf-mobile-records w-full" role="table"><thead><tr><th>成员</th><th>设备备注</th><th class="text-right">操作</th></tr></thead><tbody><tr v-for="ip in draft.members" :key="ip"><td class="cf-record-wide break-all font-mono text-xs" data-label="成员">{{ ip }}<span v-if="ip.includes(':')" class="mt-1 block font-sans text-warning-accent">IPv6 路径需单独核查；隐私地址可能轮换</span><span class="mt-1 block font-sans text-muted-foreground">{{ recordsAvailable ? samples[ip]?.clients ? `${ip.includes('/') ? '范围内' : ''}样本内 ${samples[ip].queries} 次查询${ip.includes('/') ? ` · ${samples[ip].clients} 个客户端` : ''}` : '样本内未发现查询' : '无查询记录' }}</span><span v-for="overlap in snapshot.effective_groups.find(group => group.id === draft.id)?.member_overlaps?.filter(item => item.member === ip) || []" :key="overlap.group_id" class="mt-1 block font-sans text-warning-accent">部分被「{{ overlap.group_name }}」的更具体成员覆盖</span></td><td data-label="设备备注"><Input v-model="notes[ip]" class="min-h-11" maxlength="80" :aria-label="`${ip} 设备备注`" /></td><td class="text-right"><Button type="button" variant="ghost" size="icon" class="size-11" :aria-label="`移除 ${ip}`" @click="draft.members = draft.members.filter(value => value !== ip)"><Trash2 class="size-4" /></Button></td></tr></tbody></table></div>
          <p class="m-0 text-xs text-muted-foreground">设备备注与同一配置空间的其他页面共用。样本按填写范围统计，实际归属以预览和 IP 查询为准。建议为成员设备设置静态租约。</p>
          <p v-if="error" role="alert" class="m-0 break-words text-sm text-destructive-accent">{{ error }}</p>
        </div>
        <div class="flex shrink-0 justify-end gap-2 border-t border-border p-3 pb-[max(.75rem,env(safe-area-inset-bottom))] sm:p-5"><Button type="button" variant="outline" class="min-h-11" :disabled="confirming" @click="close">取消</Button><Button type="submit" class="min-h-11">应用到草稿</Button></div>
      </form>
    </SheetContent>
  </Sheet>
</template>
