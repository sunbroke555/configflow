<script setup lang="ts">
import { computed, ref, useId, watch } from 'vue'
import { ChevronDown, ScanLine } from '@lucide/vue'
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
const applyHint = '开关与 YAML 同步，保存并发布后生效。'
const fields: Array<{ key: MihomoIPv6Field; label: string; help: string }> = [
  { key: 'core', label: 'Mihomo 使用 IPv6', help: optionHelp('需要 Mihomo 通过 IPv6 连接节点或访问网站时开启。', '设备本身需有可用的 IPv6 网络。客户端仍可使用 IPv4 分流，不必同时开启「DNS 返回 IPv6 地址」。') },
  { key: 'dns', label: 'DNS 返回 IPv6 地址', help: optionHelp('希望 DNS 给手机、电脑返回 IPv6 地址时开启；客户端只走 IPv4 分流时可关闭。', '开启还需 IPv6 路由可用；若用 OxiDNS，注意「优先 IPv4」可能压下答案。关闭仍可用 IPv6 节点，需「Mihomo 使用 IPv6」开启且设备网络可用。') },
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
          <label :for="`${id}-${field.key}`" class="flex min-h-11 min-w-0 cursor-pointer items-center gap-3 text-[13px] font-medium">
            <span class="min-w-0">{{ field.label }}</span>
            <Switch :id="`${id}-${field.key}`" :model-value="state[field.key].value === true" :disabled="disabled || busy || !state[field.key].editable" :aria-describedby="`${id}-${field.key}-description`" @update:model-value="change(field.key, $event)" />
          </label>
          <FieldHelp :label="field.label" :text="`${field.help}\n\n${applyHint}`" />
        </div>
        <div :id="`${id}-${field.key}-description`" class="min-w-0 text-xs leading-relaxed">
          <p v-if="!state.error && (state[field.key].source !== 'explicit' || !state[field.key].editable)" class="m-0 break-words" :class="state[field.key].source === 'invalid' || !state[field.key].editable ? 'text-warning-accent' : 'text-muted-foreground'">{{ state[field.key].message }}</p>
        </div>
      </div>
    </div>
    <p v-if="state.error || error" class="mb-0 mt-2 break-words text-xs text-warning-accent" role="alert">{{ error || state.error }}</p>
    <div class="mt-3 flex">
      <Button type="button" variant="outline" class="min-h-11 shrink-0 border-border-strong text-[13px]" :class="expanded && 'border-primary-accent bg-primary-soft text-primary-accent'" :aria-expanded="expanded" :aria-controls="`${id}-checks`" :disabled="disabled || busy" @click="expanded = !expanded"><ScanLine class="size-4" aria-hidden="true" />设备 IPv6 检查<ChevronDown class="size-3.5" :class="expanded && 'rotate-180'" aria-hidden="true" /></Button>
    </div>
    <div v-if="expanded" :id="`${id}-checks`" class="mt-2 border-t border-border pt-3">
      <MihomoIPv6Readiness :open="open && expanded" :profile-id="profileId" :preferred-agent-id="preferredAgentId" :disabled="disabled" :dirty="dirty" @busy="busy = $event" />
    </div>
  </div>
</template>
