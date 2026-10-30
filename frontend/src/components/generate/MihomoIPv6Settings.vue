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
  <div class="min-w-0 shrink-0 border-b border-border px-4 py-3 md:px-5" data-mihomo-ipv6-settings>
    <div class="grid gap-x-6 gap-y-1 sm:grid-cols-2">
      <div v-for="field in fields" :key="field.key" class="min-w-0">
        <div class="flex min-h-11 items-center gap-1">
          <label :for="`${id}-${field.key}`" class="flex min-h-11 min-w-0 flex-1 cursor-pointer items-center gap-3 text-[13px] font-medium">
            <span class="min-w-0 flex-1">{{ field.label }}</span>
            <Switch :id="`${id}-${field.key}`" :model-value="state[field.key].value === true" :disabled="disabled || busy || !state[field.key].editable" @update:model-value="change(field.key, $event)" />
          </label>
          <FieldHelp :label="field.label" :text="field.help" />
        </div>
        <p class="m-0 text-xs" :class="state[field.key].source === 'invalid' ? 'text-warning-accent' : 'text-muted-foreground'">{{ state.error ? '请先修正下方配置' : state[field.key].message }}</p>
        <p class="mb-0 mt-1 text-xs leading-relaxed text-muted-foreground">{{ field.hint }}</p>
      </div>
    </div>
    <p v-if="state.error || error" class="mb-0 mt-2 break-words text-xs text-warning-accent" role="alert">{{ error || state.error }}</p>
    <div class="mt-2 flex min-w-0 flex-wrap items-center justify-between gap-2">
      <p class="m-0 text-xs text-muted-foreground">开关与下方 YAML 同步，保存并发布后应用到 Mihomo。</p>
      <Button type="button" variant="ghost" class="min-h-11 shrink-0" :aria-expanded="expanded" :aria-controls="`${id}-checks`" :disabled="disabled || busy" @click="expanded = !expanded">设备 IPv6 检查<ChevronDown class="size-3.5" :class="expanded && 'rotate-180'" /></Button>
    </div>
    <div v-if="expanded" :id="`${id}-checks`" class="mt-2 border-t border-border pt-3">
      <MihomoIPv6Readiness :open="open && expanded" :profile-id="profileId" :preferred-agent-id="preferredAgentId" :disabled="disabled" :dirty="dirty" @busy="busy = $event" />
    </div>
  </div>
</template>
