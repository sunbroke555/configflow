<template>
  <Dialog :open="true" @update:open="value => { if (!value && !saving) emit('close') }">
    <DialogContent class="max-h-[90dvh] max-w-lg overflow-y-auto" @escape-key-down="event => { if (saving) event.preventDefault() }" @interact-outside="event => { if (saving) event.preventDefault() }">
      <DialogHeader>
        <DialogTitle>加入{{ target === 'proxy' ? '代理' : '直连' }}规则集</DialogTitle>
        <DialogDescription>{{ isIP ? '将这个目标 IP 或它所在的网段加入规则集。' : '使用此 Mihomo 所属配置空间在「域名发现」中设置的默认规则集。' }}</DialogDescription>
      </DialogHeader>
      <p v-if="loading" role="status" class="text-sm text-muted-foreground">正在读取规则集…</p>
      <p v-if="error" role="alert" class="m-0 text-sm text-destructive-accent">{{ error }}</p>
      <template v-if="state">
        <div class="rounded-lg border bg-muted/40 p-3 text-sm">
          <p class="m-0 break-words">{{ state.profile_name }} · {{ selectedTarget?.name || (isIP ? '请选择 IP 规则集' : '未设置默认规则集') }}</p>
          <p v-if="isIP && !ipTargets.length" role="alert" class="mb-0 mt-2 text-xs text-warning-accent">暂无可用的 IP 规则集。请先在规则仓库添加内容型 IP 或混合规则集，并在当前配置中设置对应策略。</p>
          <p v-else-if="isIP && !selectedTarget" class="mb-0 mt-2 text-xs text-muted-foreground">默认规则集无法用于这次 IP 添加，请在下方选择。</p>
          <p v-else-if="!selectedTarget || selectedTarget.problem" role="alert" class="mb-0 mt-2 text-xs text-warning-accent">请先在此配置空间的「域名发现」设置可编辑的默认{{ target === 'proxy' ? '代理' : '直连' }}规则集。</p>
          <p v-else-if="!selectedTarget.active" class="mb-0 mt-2 text-xs text-warning-accent">此规则集尚未在当前配置中启用，保存后还需启用并发布。</p>
        </div>
        <FormField v-if="isIP && ipTargets.length" label="保存到规则集" html-for="connection-rule-ruleset">
          <Select v-model="ipRulesetId" :disabled="saving || !!result"><SelectTrigger id="connection-rule-ruleset" class="min-h-11 w-full min-w-0"><SelectValue placeholder="选择 IP 规则集" /></SelectTrigger>
            <SelectContent><SelectItem v-for="item in ipTargets" :key="item.id" :value="item.id">{{ item.name }}{{ item.active ? '' : ' · 未启用' }}</SelectItem></SelectContent>
          </Select>
        </FormField>
        <FormField :label="isIP ? '目标 IP' : '连接域名'" html-for="connection-rule-domain">
          <Select v-model="value" :disabled="saving || !!result"><SelectTrigger id="connection-rule-domain" class="min-h-11 w-full min-w-0 font-mono"><SelectValue /></SelectTrigger>
            <SelectContent><SelectItem v-for="host in values" :key="host" :value="host">{{ host }}</SelectItem></SelectContent>
          </Select>
        </FormField>
        <FormField v-if="isIP" label="匹配范围" html-for="connection-rule-scope">
          <Select v-model="ipScope" :disabled="saving || !!result"><SelectTrigger id="connection-rule-scope" class="min-h-11 w-full"><SelectValue /></SelectTrigger>
            <SelectContent><SelectItem value="host">仅此 IP</SelectItem><SelectItem value="network">所在网段</SelectItem></SelectContent>
          </Select>
        </FormField>
        <FormField v-if="isIP && ipScope === 'network'" label="子网前缀" html-for="connection-rule-prefix" hint="将匹配此网段内的所有目标 IP。">
          <Select v-model="ipPrefix" :disabled="saving || !!result"><SelectTrigger id="connection-rule-prefix" class="min-h-11 w-full"><SelectValue /></SelectTrigger>
            <SelectContent><SelectItem v-for="prefix in prefixOptions" :key="prefix" :value="String(prefix)">/{{ prefix }}{{ prefix === (maxPrefix === 32 ? 24 : 64) ? '（默认）' : '' }}</SelectItem></SelectContent>
          </Select>
        </FormField>
        <FormField v-if="!isIP" label="匹配范围" html-for="connection-rule-type">
          <Select v-model="domainRuleType" :disabled="saving || !!result"><SelectTrigger id="connection-rule-type" class="min-h-11 w-full"><SelectValue /></SelectTrigger>
            <SelectContent><SelectItem value="DOMAIN">仅此域名</SelectItem><SelectItem value="DOMAIN-SUFFIX">此域名及其子域名</SelectItem></SelectContent>
          </Select>
        </FormField>
        <p class="m-0 break-all rounded-lg bg-muted/40 p-3 font-mono text-xs">{{ rulePreview }}</p>
        <p class="m-0 text-xs leading-relaxed text-muted-foreground">将保存到共享规则集，其他引用它的配置也会更新。设备按现有规则同步机制生效，已有连接不会被断开。</p>
        <div v-if="result" role="status" class="space-y-2 text-sm">
          <p class="m-0 text-success-accent">{{ result.added.length ? '已加入规则集。' : (isIP ? '规则集已覆盖此 IP 或网段，无需重复添加。' : '规则集已包含此域名，无需重复添加。') }}</p>
          <p v-for="warning in result.warnings" :key="warning" class="m-0 text-xs text-warning-accent">{{ warning }}</p>
        </div>
      </template>
      <DialogFooter>
        <Button variant="outline" class="min-h-11" :disabled="saving" @click="emit('close')">{{ result ? '完成' : '取消' }}</Button>
        <Button v-if="error && !state" variant="outline" class="min-h-11" @click="load">重试</Button>
        <Button v-if="!result" class="min-h-11" :disabled="loading || saving || !selectedTarget || !!selectedTarget.problem || !value" @click="save"><Loader2 v-if="saving" class="size-4 animate-spin" />确认添加</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { Loader2 } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import FormField from '@/components/common/FormField.vue'
import { clientToolsApi } from '@/api'
import { deviceAddress, type ConnectionRuleTargets, type ConnectionRuleType } from '@/lib/device-notes'
import { normalizeClientRange } from '@/lib/oxidnsClientRange'
import { panelError } from '@/types/mihomo'
const props = defineProps<{ agentId: string; values: string[]; target: 'direct' | 'proxy' }>()
const emit = defineEmits<{ close: [] }>()
const state = ref<ConnectionRuleTargets | null>(null)
const value = ref(props.values[0] || '')
const isIP = computed(() => !!deviceAddress(value.value))
const maxPrefix = computed(() => value.value.includes(':') ? 128 : 32)
const ipScope = ref<'host' | 'network'>('host')
const ipPrefix = ref(maxPrefix.value === 32 ? '24' : '64')
const prefixOptions = computed(() => Array.from({ length: maxPrefix.value + 1 }, (_, index) => maxPrefix.value - index))
watch(maxPrefix, maximum => { ipPrefix.value = maximum === 32 ? '24' : '64' })
const ipCidr = computed(() => {
  if (!isIP.value) return ''
  const prefix = ipScope.value === 'network' ? ipPrefix.value : String(maxPrefix.value)
  const network = normalizeClientRange(`${value.value}/${prefix}`)
  return network.includes('/') ? network : `${network}/${prefix}`
})
const ipTargets = computed(() => state.value?.ip_rulesets?.filter(item => item.targets.includes(props.target)) || [])
const ipRulesetId = ref('')
const selectedTarget = computed(() => isIP.value ? ipTargets.value.find(item => item.id === ipRulesetId.value) : state.value?.[props.target])
const domainRuleType = ref<'DOMAIN' | 'DOMAIN-SUFFIX'>('DOMAIN')
const ruleType = computed<ConnectionRuleType>(() => isIP.value ? (value.value.includes(':') ? 'IP-CIDR6' : 'IP-CIDR') : domainRuleType.value)
const rulePreview = computed(() => {
  if (!isIP.value) return `${ruleType.value},${value.value}`
  return (selectedTarget.value as { behavior?: string })?.behavior === 'ipcidr' ? ipCidr.value : `${ruleType.value},${ipCidr.value},no-resolve`
})
const result = ref<{ added: unknown[]; skipped: unknown[]; warnings: string[] } | null>(null)
const error = ref('')
const loading = ref(true)
const saving = ref(false)
let disposed = false
async function load() {
  loading.value = true; error.value = ''
  try {
    const { data } = await clientToolsApi.ruleTargets(props.agentId)
    if (disposed) return
    if (!data?.success) throw new Error('读取默认规则集失败。')
    state.value = data
    const defaultId = state.value?.[props.target]?.id
    ipRulesetId.value = ipTargets.value.find(item => item.id === defaultId)?.id || ''
  } catch (failure) { if (!disposed) error.value = panelError(failure) }
  finally { if (!disposed) loading.value = false }
}
async function save() {
  if (!state.value || !selectedTarget.value || selectedTarget.value.problem || saving.value || result.value) return
  saving.value = true; error.value = ''
  try {
    const { data } = await clientToolsApi.addRule(props.agentId, { profile_id: state.value.profile_id, ruleset_id: selectedTarget.value.id, target: props.target, value: isIP.value && ipScope.value === 'network' ? ipCidr.value : value.value, rule_type: ruleType.value })
    if (disposed) return
    if (!data?.success) throw new Error('保存规则失败。')
    result.value = data
  } catch (failure) { if (!disposed) error.value = panelError(failure) }
  finally { if (!disposed) saving.value = false }
}
onMounted(load)
onUnmounted(() => { disposed = true })
</script>
