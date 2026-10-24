<script setup lang="ts">
import { onUnmounted, ref } from 'vue'
import { Loader2 } from '@lucide/vue'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { deviceGroupsApi } from '@/api/deviceGroups'
import { deviceGroupModeLabel, type DeviceGroupLookup } from '@/types/deviceGroups'
const props = defineProps<{ profileId: string }>()
const emit = defineEmits<{ close: [] }>()
const ip = ref(''), loading = ref(false), error = ref(''), result = ref<DeviceGroupLookup | null>(null)
let controller: AbortController | undefined
let epoch = 0
async function lookup() {
  controller?.abort()
  const current = ++epoch
  controller = new AbortController(); loading.value = true; error.value = ''; result.value = null
  try { const { data } = await deviceGroupsApi.lookup(props.profileId, ip.value.trim(), controller.signal); if (current === epoch) result.value = data }
  catch (failure: any) { if (current === epoch && !controller.signal.aborted) error.value = failure.response?.data?.message || failure.message || '暂时无法查询 IP 归属。' }
  finally { if (current === epoch) loading.value = false }
}
onUnmounted(() => { epoch++; controller?.abort() })
</script>
<template>
  <Dialog :open="true" @update:open="value => { if (!value) emit('close') }"><DialogContent :show-close-button="false" class="max-w-[560px]"><DialogHeader><DialogTitle>查询 IP 归属</DialogTitle><DialogDescription>查询已保存草稿中的归属与策略路由来源；设备实际生效还取决于发布与真实源 IP。</DialogDescription></DialogHeader><form class="flex min-w-0 gap-2" @submit.prevent="lookup"><Input v-model="ip" class="min-h-11 min-w-0 flex-1 font-mono" aria-label="要查询的 IP" placeholder="输入单个 IPv4 或 IPv6" /><Button type="submit" class="min-h-11" :disabled="!ip.trim() || loading"><Loader2 v-if="loading" class="size-4 animate-spin" />查询</Button></form><p v-if="error" role="alert" class="m-0 text-sm text-destructive-accent">{{ error }}</p><dl v-if="result" class="m-0 space-y-3 text-sm"><div><dt class="text-xs text-muted-foreground">所在设备组</dt><dd class="m-0 break-words">{{ result.group_name }} · {{ deviceGroupModeLabel(result.mode) }}</dd></div><div><dt class="text-xs text-muted-foreground">命中成员</dt><dd class="m-0 break-all font-mono">{{ result.matched_member || '未归组，默认兜底' }}</dd></div><div v-if="result.overridden_members.length"><dt class="text-xs text-muted-foreground">被更具体成员覆盖的范围</dt><dd v-for="entry in result.overridden_members" :key="`${entry.group_name}:${entry.member}`" class="m-0 break-words">{{ entry.group_name }} · <span class="font-mono">{{ entry.member }}</span></dd></div><div v-if="result.infrastructure_exclusion"><dt class="text-xs text-muted-foreground">基础设施排除</dt><dd class="m-0 text-warning-accent">{{ result.infrastructure_exclusion }}</dd></div><div><dt class="text-xs text-muted-foreground">RouterOS 策略路由来源</dt><dd class="m-0">{{ result.policy_routing_included ? '在有效来源内' : '不在有效来源内' }}</dd></div></dl><DialogFooter><Button variant="outline" class="min-h-11" @click="emit('close')">关闭</Button></DialogFooter></DialogContent></Dialog>
</template>
