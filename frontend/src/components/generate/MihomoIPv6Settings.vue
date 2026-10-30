<script setup lang="ts">
import { computed, onUnmounted, ref, useId, watch } from 'vue'
import { ScanLine } from '@lucide/vue'
import { Switch } from '@/components/ui/switch'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/dialog'
import FieldHelp from '@/components/common/FieldHelp.vue'
import { optionHelp } from '@/lib/optionCopy'
import { readMihomoIPv6, setMihomoIPv6WithDetails, readMihomoFakeIPv6Range, setMihomoFakeIPv6Range, type MihomoIPv6Field } from '@/lib/mihomoIPv6'
import MihomoIPv6Readiness from './MihomoIPv6Readiness.vue'

const content = defineModel<string>({ required: true })
const props = defineProps<{ open: boolean; profileId: string; preferredAgentId?: string; defaultConfig?: string; disabled?: boolean; dirty?: boolean }>()
const emit = defineEmits<{ busy: [value: boolean]; validation: [value: { invalid: boolean; dirty: boolean }] }>()
const checksOpen = ref(false)
const busy = ref(false)
const error = ref('')
const changeNotice = ref('')
const id = useId()
const state = computed(() => readMihomoIPv6(content.value, props.defaultConfig))
const fakeRange = computed(() => readMihomoFakeIPv6Range(content.value, props.defaultConfig))
const rangeInput = ref('')
const rangeError = ref('')
const rangeDirty = ref(false)
const rangeHelp = optionHelp('用于生成客户端收到的 IPv6 Fake-IP，通常保留默认值即可。', '填写 IPv6 内网网段，例如 fc00::1/64，并避开局域网正在使用的网段。修改后保存，到「上级路由」预览并发布对应路由。关闭 DNS IPv6 会保留此值，重新开启时继续使用。')
watch(() => [content.value, props.defaultConfig, props.open, props.profileId] as const, () => {
  rangeInput.value = fakeRange.value.value
  rangeError.value = fakeRange.value.error
  rangeDirty.value = false
}, { immediate: true })
watch(() => [props.open, fakeRange.value.visible, rangeError.value, rangeDirty.value] as const, () => {
  emit('validation', { invalid: props.open && fakeRange.value.visible && !!rangeError.value,
    dirty: props.open && fakeRange.value.visible && rangeDirty.value })
}, { immediate: true })
onUnmounted(() => emit('validation', { invalid: false, dirty: false }))
function changeRange(value: string | number) {
  if (props.disabled || busy.value || !fakeRange.value.editable) return
  rangeInput.value = String(value)
  rangeDirty.value = rangeInput.value !== fakeRange.value.value
  try {
    const changed = setMihomoFakeIPv6Range(content.value, rangeInput.value, props.defaultConfig)
    content.value = changed
    rangeError.value = ''
    rangeDirty.value = false
    changeNotice.value = '网段已更新；保存后到「上级路由」查看差异并发布路由。'
  } catch (failure) {
    rangeError.value = failure instanceof Error ? failure.message : '请填写有效的 IPv6 内网网段。'
    changeNotice.value = ''
  }
}
const applyHint = '开关与 YAML 同步，保存并发布后生效。'
const fields: Array<{ key: MihomoIPv6Field; label: string; help: string }> = [
  { key: 'core', label: 'Mihomo 使用 IPv6', help: optionHelp('需要 Mihomo 通过 IPv6 连接节点或访问网站时开启。', '设备本身需有可用的 IPv6 网络。客户端仍可使用 IPv4 分流，不必同时开启「DNS 返回 IPv6 地址」。') },
  { key: 'dns', label: 'DNS 返回 IPv6 地址', help: optionHelp('希望 DNS 给手机、电脑返回 IPv6 地址时开启；客户端只走 IPv4 分流时可关闭。', 'Fake-IP 模式下开启后可在下方填写 IPv6 Fake-IP 网段，已有网段沿用。保存后到「上级路由」查看差异并发布对应路由；TUN 需已开启。若用 OxiDNS，注意「优先 IPv4」可能压下答案。') },
]
function change(field: MihomoIPv6Field, value: boolean) {
  if (props.disabled || busy.value) return
  try {
    const changed = setMihomoIPv6WithDetails(content.value, field, value, props.defaultConfig)
    content.value = changed.content
    changeNotice.value = changed.notice
    error.value = ''
  } catch (failure) {
    error.value = failure instanceof Error ? failure.message : '无法修改，请在 YAML 中调整。'
  }
}
function setChecksOpen(value: boolean) {
  if (busy.value || (value && (props.disabled || !props.open))) return
  checksOpen.value = value
}
function guardDismiss(event: Event) {
  if (busy.value) event.preventDefault()
}
watch(() => [props.open, props.profileId, props.preferredAgentId], () => { checksOpen.value = false; error.value = ''; changeNotice.value = '' })
watch(busy, value => emit('busy', value))
</script>

<template>
  <Dialog :open="checksOpen" @update:open="setChecksOpen">
    <div class="min-w-0 shrink-0 border-b border-border px-4 py-3 md:px-5" data-mihomo-ipv6-settings>
      <div class="grid gap-x-6 gap-y-1 sm:grid-cols-2">
        <div v-for="field in fields" :key="field.key" class="min-w-0">
          <div class="flex min-h-11 flex-wrap items-center gap-x-3 gap-y-1">
            <div class="flex min-w-0 items-center gap-1">
              <label :for="`${id}-${field.key}`" class="flex min-h-11 min-w-0 cursor-pointer items-center gap-3 text-[13px] font-medium">
                <span class="min-w-0">{{ field.label }}</span>
                <Switch :id="`${id}-${field.key}`" :model-value="state[field.key].value === true" :disabled="disabled || busy || !state[field.key].editable" :aria-describedby="`${id}-${field.key}-description`" @update:model-value="change(field.key, $event)" />
              </label>
              <FieldHelp :label="field.label" :text="`${field.help}\n\n${applyHint}`" />
            </div>
            <DialogTrigger v-if="field.key === 'core'" as-child>
              <Button type="button" variant="outline" class="min-h-11 shrink-0 border-border-strong text-[13px]" aria-label="检查设备 IPv6" :disabled="disabled || busy"><ScanLine class="size-4" aria-hidden="true" />检查设备</Button>
            </DialogTrigger>
          </div>
          <div v-if="field.key === 'dns' && fakeRange.visible" class="min-w-0 max-w-sm space-y-1 pb-2 pt-1" data-ipv6-fakeip-range>
            <div class="flex min-h-8 items-center gap-1">
              <label :for="`${id}-fakeip-range`" class="text-[13px] font-medium">IPv6 Fake-IP 网段</label>
              <FieldHelp label="IPv6 Fake-IP 网段" :text="rangeHelp" />
            </div>
            <Input :id="`${id}-fakeip-range`" :model-value="rangeInput" placeholder="fc00::1/64" class="min-h-11 font-mono"
              :disabled="disabled || busy || !fakeRange.editable" :aria-invalid="!!rangeError" :aria-describedby="`${id}-fakeip-range-message`"
              autocomplete="off" autocapitalize="off" :spellcheck="false" @update:model-value="changeRange" />
            <p :id="`${id}-fakeip-range-message`" class="m-0 break-words text-xs leading-relaxed"
              :class="rangeError ? 'text-destructive-accent' : 'text-muted-foreground'" :role="rangeError ? 'alert' : undefined">{{ rangeError || fakeRange.message }}</p>
          </div>
          <div :id="`${id}-${field.key}-description`" class="min-w-0 text-xs leading-relaxed">
            <p v-if="!state.error && (state[field.key].source !== 'explicit' || !state[field.key].editable)" class="m-0 break-words" :class="state[field.key].source === 'invalid' || !state[field.key].editable ? 'text-warning-accent' : 'text-muted-foreground'">{{ state[field.key].message }}</p>
          </div>
        </div>
      </div>
      <p v-if="changeNotice" class="mb-0 mt-2 break-words text-xs text-muted-foreground" role="status">{{ changeNotice }}</p>
      <p v-if="state.error || error" class="mb-0 mt-2 break-words text-xs text-warning-accent" role="alert">{{ error || state.error }}</p>
    </div>
    <DialogContent v-if="checksOpen" class="flex min-h-0 max-w-2xl flex-col gap-0 overflow-hidden p-0 max-sm:p-0" :show-close-button="!busy" :aria-describedby="undefined" data-mihomo-ipv6-dialog @escape-key-down="guardDismiss" @interact-outside="guardDismiss">
      <DialogHeader class="shrink-0 border-b border-border px-4 py-4 pr-14 sm:px-5 sm:pr-14"><DialogTitle>设备 IPv6 检查</DialogTitle></DialogHeader>
      <div class="min-h-0 overflow-y-auto overscroll-contain px-4 py-4 sm:px-5">
        <MihomoIPv6Readiness :open="open && checksOpen" :profile-id="profileId" :preferred-agent-id="preferredAgentId" :disabled="disabled" :dirty="dirty" @busy="busy = $event" />
      </div>
      <DialogFooter class="shrink-0 border-t border-border px-4 py-3 sm:px-5"><Button type="button" variant="outline" class="min-h-11" :disabled="busy" @click="setChecksOpen(false)">返回设置</Button></DialogFooter>
    </DialogContent>
  </Dialog>
</template>
