<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { CircleCheck, CircleDashed, CircleX, Loader2, TriangleAlert } from '@lucide/vue'
import { agentApi } from '@/api'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import FormField from '@/components/common/FormField.vue'
import type { DockerHANetwork, DockerHAPreflightReport, DockerHAPreflightStatus } from '@/types/dockerHAGuide'

const props = defineProps<{ profileId: string; network: DockerHANetwork }>()
const outputs = ref(['', ''])
const reports = ref<(DockerHAPreflightReport | null)[]>([null, null])
const errors = ref(['', ''])
const busy = ref([false, false])
const requests = [0, 0]
let disposed = false
const states = {
  pass: { label: '通过', icon: CircleCheck, color: 'text-success-accent' },
  warning: { label: '需注意', icon: TriangleAlert, color: 'text-warning-accent' },
  fail: { label: '未通过', icon: CircleX, color: 'text-destructive-accent' },
  unknown: { label: '无法判断', icon: CircleDashed, color: 'text-warning-accent' },
}
const isStatus = (status: unknown): status is DockerHAPreflightStatus => typeof status === 'string' && Object.prototype.hasOwnProperty.call(states, status)
function isReport(value: unknown): value is DockerHAPreflightReport {
  if (!value || typeof value !== 'object') return false
  const report = value as DockerHAPreflightReport
  return isStatus(report.status) && typeof report.summary === 'string' && Array.isArray(report.checks) && report.checks.length > 0
    && report.checks.every(check => check && typeof check.id === 'string' && typeof check.label === 'string'
      && isStatus(check.status) && typeof check.detail === 'string' && (check.remedy === undefined || typeof check.remedy === 'string'))
}
function invalidate(index: number) {
  requests[index]++
  reports.value[index] = null
  errors.value[index] = ''
  busy.value[index] = false
}
for (const index of [0, 1]) watch(() => outputs.value[index], () => invalidate(index), { flush: 'sync' })
watch(() => [props.network, props.profileId], () => { invalidate(0); invalidate(1) }, { flush: 'sync' })
const duplicate = computed(() => {
  const left = outputs.value[0]?.trim()
  const right = outputs.value[1]?.trim()
  if (left && left === right) return '两份检查输出相同，可能粘贴了同一台主机的结果。请分别在两台宿主机重新执行并粘贴；不能据此确认两台独立主机。'
  const fingerprint = reports.value[0]?.host_fingerprint
  if (typeof fingerprint === 'string' && fingerprint && fingerprint === reports.value[1]?.host_fingerprint) return '两份结果的宿主指纹相同。请确认使用两台不同宿主机，并分别重新采集；不能据此确认两台独立主机。'
  return ''
})
async function check(index: number) {
  if (!outputs.value[index]?.trim() || busy.value[index]) return
  const request = ++requests[index]
  busy.value[index] = true
  errors.value[index] = ''
  reports.value[index] = null
  try {
    if (new TextEncoder().encode(outputs.value[index]).byteLength > 96 * 1024) throw new Error('检查输出超过 96 KiB，请只粘贴本页命令的完整输出，移除无关终端历史后重试。')
    const { data } = await agentApi.generateDockerHAGuide({ action: 'check', output: outputs.value[index], network_mode: props.network }, props.profileId)
    if (disposed || request !== requests[index]) return
    if (!data.success) throw new Error(data.message || '检测失败，请重试。')
    if (!isReport(data.preflight_report)) throw new Error('未收到完整检测结果，请重新复制本页新版检查命令，运行后粘贴完整输出再试。')
    reports.value[index] = data.preflight_report
  } catch (error: any) {
    if (!disposed && request === requests[index]) errors.value[index] = error?.response?.data?.message || error?.response?.data?.error || error?.message || '无法完成检测，请重试。'
  } finally {
    if (!disposed && request === requests[index]) busy.value[index] = false
  }
}
onUnmounted(() => { disposed = true; requests.forEach((_, index) => requests[index]++) })
</script>

<template>
  <section class="flex min-w-0 flex-col gap-4 border-t border-border pt-4" aria-label="粘贴输出检查环境">
    <div class="space-y-2">
      <h4 class="m-0 text-sm font-semibold">粘贴输出，逐项检查环境（可选）</h4>
      <p id="docker-ha-preflight-scope" class="m-0 text-[13px] leading-relaxed text-muted-foreground">在两台宿主机分别运行上方新版命令，将完整输出粘贴到对应位置。点击检测后才会提交给 ConfigFlow 分析；输出不保存，也不会执行安装或自动修复。</p>
      <p class="m-0 text-[12px] leading-relaxed text-muted-foreground">这里只根据粘贴时的系统、Docker、网络工具和端口信息判断前置条件，不能替代安装后的 Agent、容器权限、VIP 与 DNS 配置检查。旧版无结构输出或截断内容请用本页新版命令重新采集。</p>
    </div>
    <p v-if="duplicate" role="alert" class="m-0 rounded-md border border-warning/30 bg-warning-soft p-3 text-[13px] leading-relaxed [overflow-wrap:anywhere]">{{ duplicate }}</p>
    <div class="grid min-w-0 grid-cols-1 gap-6 sm:grid-cols-2">
      <section v-for="index in [0, 1]" :key="index" class="flex min-w-0 flex-col gap-3" :aria-label="`主机 ${index + 1} 环境预检`" :aria-busy="busy[index]">
        <FormField :label="`主机 ${index + 1} 的检查输出`" :html-for="`docker-ha-preflight-output-${index + 1}`">
          <Textarea :id="`docker-ha-preflight-output-${index + 1}`" v-model="outputs[index]" class="h-32 min-h-28 max-h-64 min-w-0 resize-y overflow-auto font-mono text-xs [field-sizing:fixed] [overflow-wrap:anywhere]" :aria-describedby="`docker-ha-preflight-scope${errors[index] ? ` docker-ha-preflight-error-${index + 1}` : ''}`" :aria-invalid="!!errors[index]" placeholder="粘贴这台主机的完整检查输出" spellcheck="false" autocapitalize="off" autocomplete="off" />
        </FormField>
        <Button variant="outline" class="min-h-11 self-start" :disabled="!outputs[index]?.trim() || busy[index]" @click="check(index)"><Loader2 v-if="busy[index]" class="size-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />检测主机 {{ index + 1 }}</Button>
        <p v-if="errors[index]" :id="`docker-ha-preflight-error-${index + 1}`" role="alert" class="m-0 text-[13px] leading-relaxed text-destructive-accent [overflow-wrap:anywhere]">{{ errors[index] }}</p>
        <div v-if="reports[index]" :data-preflight-report="index + 1" role="status" aria-live="polite" class="min-w-0 space-y-3 text-[13px] leading-relaxed [overflow-wrap:anywhere]">
          <div class="flex min-w-0 items-start gap-2 font-semibold" :class="states[reports[index]!.status].color"><component :is="states[reports[index]!.status].icon" class="mt-0.5 size-4 shrink-0" aria-hidden="true" /><span>{{ states[reports[index]!.status].label }} · {{ reports[index]!.summary }}</span></div>
          <ul class="m-0 list-none divide-y divide-border p-0">
            <li v-for="(item, position) in reports[index]!.checks" :key="`${item.id}-${position}`" :data-preflight-check="item.id" class="min-w-0 space-y-1 py-3 first:pt-0">
              <div class="flex min-w-0 items-start gap-2"><component :is="states[item.status].icon" class="mt-0.5 size-4 shrink-0" :class="states[item.status].color" aria-hidden="true" /><span class="min-w-0 flex-1 font-medium">{{ item.label }}</span><span class="shrink-0 text-xs" :class="states[item.status].color">{{ states[item.status].label }}</span></div>
              <p class="m-0 whitespace-pre-wrap">{{ item.detail }}</p>
              <p v-if="item.remedy" class="m-0 whitespace-pre-wrap text-muted-foreground"><span class="font-medium text-foreground">处理办法：</span>{{ item.remedy }}</p>
            </li>
          </ul>
        </div>
      </section>
    </div>
  </section>
</template>
