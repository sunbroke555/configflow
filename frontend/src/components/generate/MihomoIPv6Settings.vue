<script setup lang="ts">
import { computed, ref, useId, watch } from 'vue'
import { ChevronDown } from '@lucide/vue'
import { Switch } from '@/components/ui/switch'
import { Button } from '@/components/ui/button'
import FieldHelp from '@/components/common/FieldHelp.vue'
import { optionHelp } from '@/lib/optionCopy'
import { readMihomoIPv6, setMihomoIPv6, type MihomoIPv6Field } from '@/lib/mihomoIPv6'
import MihomoIPv6Readiness from './MihomoIPv6Readiness.vue'

const content = defineModel<string>({ required: true })
const props = defineProps<{ open: boolean; profileId: string; preferredAgentId?: string; defaultConfig?: string; disabled?: boolean; dirty?: boolean }>()
const emit = defineEmits<{ busy: [value: boolean] }>()
const expanded = ref(false)
const busy = ref(false)
const error = ref('')
const id = useId()
const state = computed(() => readMihomoIPv6(content.value, props.defaultConfig))
const fields: Array<{ key: MihomoIPv6Field; label: string; hint: string; help: string }> = [
  { key: 'core', label: 'Mihomo 使用 IPv6', hint: '让 Mihomo 能连接 IPv6 节点或网站。', help: optionHelp('需要通过 IPv6 连接代理节点或接收 IPv6 流量。', '修改顶层 ipv6。设备还需有可用的 IPv6 地址和出口；开关不会自动把客户端从 IPv4 分流切换成 IPv6。') },
  { key: 'dns', label: 'DNS 返回 IPv6 地址', hint: '客户端走 IPv4 分流时可关闭。', help: optionHelp('希望 DNS 给手机、电脑返回 IPv6 地址时开启。', '开启还需 IPv6 路由可用；若用 OxiDNS，注意「优先 IPv4」可能压下答案。关闭仍可用 IPv6 节点，需「Mihomo 使用 IPv6」开启且设备网络可用。') },
]
function change(field: MihomoIPv6Field, value: boolean) {
  if (props.disabled || busy.value) return
  try {
    content.value = setMihomoIPv6(content.value, field, value, props.defaultConfig)
    error.value = ''
  } catch (failure) {
    error.value = failure instanceof Error ? failure.message : '无法修改，请在 YAML 中调整。'
  }
}
watch(() => [props.open, props.profileId, props.preferredAgentId], () => { expanded.value = false; error.value = '' })
watch(busy, value => emit('busy', value))
</script>

<template>
  <div class="min-w-0 shrink-0 border-b border-border px-4 md:px-5" data-mihomo-ipv6-settings>
    <div class="divide-y divide-border/70">
      <div v-for="field in fields" :key="field.key" class="grid min-w-0 grid-cols-[minmax(0,1fr)_8rem] items-center gap-x-4 gap-y-1 py-2 sm:grid-cols-[13rem_10rem_minmax(0,1fr)]">
        <div class="flex min-w-0 items-center gap-1">
          <label :for="`${id}-${field.key}`" class="inline-flex min-h-11 min-w-0 cursor-pointer items-center text-[13px] font-medium">{{ field.label }}</label>
          <FieldHelp :label="field.label" :text="field.help" />
        </div>
        <label :for="`${id}-${field.key}`" class="flex size-11 cursor-pointer items-center">
          <Switch :id="`${id}-${field.key}`" :model-value="state[field.key].value === true" :disabled="disabled || busy || !state[field.key].editable" :aria-describedby="`${id}-${field.key}-description`" @update:model-value="change(field.key, $event)" />
        </label>
        <div :id="`${id}-${field.key}-description`" class="col-span-2 min-w-0 text-xs leading-relaxed sm:col-span-1">
          <p class="m-0 text-muted-foreground">{{ field.hint }}</p>
          <p v-if="!state.error && (state[field.key].source !== 'explicit' || !state[field.key].editable)" class="mb-0 mt-1 break-words" :class="state[field.key].source === 'invalid' || !state[field.key].editable ? 'text-warning-accent' : 'text-muted-foreground'">{{ state[field.key].message }}</p>
        </div>
      </div>
    </div>
    <p class="mb-3 mt-0 text-xs leading-relaxed text-muted-foreground">开关与 YAML 同步，保存并发布后生效。</p>
    <p v-if="state.error || error" class="mb-3 mt-0 break-words text-xs text-warning-accent" role="alert">{{ error || state.error }}</p>
    <div class="border-t border-border/70">
      <Button type="button" variant="ghost" class="min-h-11 w-full justify-between px-0 text-[13px] has-[>svg]:px-0" :aria-expanded="expanded" :aria-controls="`${id}-checks`" :disabled="disabled || busy" @click="expanded = !expanded">设备 IPv6 检查<ChevronDown class="size-3.5" :class="expanded && 'rotate-180'" /></Button>
    </div>
    <div v-if="expanded" :id="`${id}-checks`" class="border-t border-border pb-3 pt-3">
      <MihomoIPv6Readiness :open="open && expanded" :profile-id="profileId" :preferred-agent-id="preferredAgentId" :disabled="disabled" :dirty="dirty" @busy="busy = $event" />
    </div>
  </div>
</template>
