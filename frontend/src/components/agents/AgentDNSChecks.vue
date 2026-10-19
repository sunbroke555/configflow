<script setup lang="ts">
import { computed } from 'vue'
import AgentStatusRow from './AgentStatusRow.vue'
import type { AgentDNSCheck } from '@/composables/useAgentDeployments'

const props = withDefaults(defineProps<{ checks?: AgentDNSCheck[]; checkedAt?: string; compact?: boolean }>(), { compact: false })
const issues = computed(() => props.checks?.filter(check => check.level !== 'pass') || [])
const passed = computed(() => props.checks?.filter(check => check.level === 'pass') || [])
const passedSummary = computed(() => {
  const groups = new Map<string, { name: string; message: string; protocols: string[] }>()
  return passed.value.flatMap(check => {
    const binding = check.name.match(/^(.+) (tcp|udp)\/(\d+)$/)
    if (!binding) return [{ ...check, protocols: [] }]
    const [, name, protocol, port] = binding
    const key = JSON.stringify([name, port, check.message, check.fix])
    const previous = groups.get(key)
    if (previous && !previous.protocols.includes(protocol)) {
      previous.protocols.push(protocol)
      previous.name = `${name} ${previous.protocols.join('/').toUpperCase()} ${port}`
      return []
    }
    // The shared report note already explains the scope of this known probe result.
    const message = name === 'DNS 当前解析' && check.message === '当前受管监听解析正常；此结果不代表待发布配置已生效'
      ? '当前受管监听解析正常' : check.message
    const summary = { name: check.name, message, protocols: [protocol] }
    groups.set(key, summary)
    return [summary]
  })
})
const errors = computed(() => issues.value.filter(check => check.level === 'error'))
const statusLabel = computed(() => errors.value.length ? `${errors.value.length} 项错误`
  : issues.value.length ? `${issues.value.length} 项提醒` : `${passed.value.length} 项通过`)
const statusTone = computed(() => errors.value.length ? 'danger' : issues.value.length ? 'warning' : 'success')
const repairs = computed(() => issues.value.filter(check => check.fix))
const checkedTime = computed(() => {
  if (!props.checkedAt) return ''
  const date = new Date(props.checkedAt)
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString()
})
</script>

<template>
  <section v-if="checks?.length" aria-label="DNS 检查结果" class="min-w-0 text-xs leading-relaxed text-foreground [overflow-wrap:anywhere]" :class="!compact && 'space-y-2 rounded-md border border-border bg-background/50 p-3'">
    <AgentStatusRow v-if="compact" label="DNS 检查" :status="statusLabel" :tone="statusTone">
      <template v-if="issues.length" #notice>
        <ul class="m-0 list-none space-y-2 p-0">
          <li v-for="(check, index) in issues" :key="`${check.name}:${index}`" class="min-w-0 border-l-2 pl-3" :class="check.level === 'error' ? 'border-destructive-accent' : 'border-warning-accent'">
            <p class="m-0 text-[13px] font-semibold" :class="check.level === 'error' ? 'text-destructive-accent' : 'text-warning-accent'">{{ check.level === 'error' ? '错误' : '警告' }} · {{ check.name }}</p>
            <p class="m-0 whitespace-pre-wrap">{{ check.message }}</p>
          </li>
        </ul>
      </template>
      <div class="min-w-0 space-y-2 text-muted-foreground">
        <ul v-if="repairs.length" class="m-0 list-none space-y-2 p-0">
          <li v-for="(check, index) in repairs" :key="`${check.name}:${index}`" class="whitespace-pre-wrap"><span class="font-medium"><template v-if="issues.length > 1">{{ check.name }} · </template>修复建议：</span>{{ check.fix }}</li>
        </ul>
        <ul v-if="passed.length" class="m-0 list-none space-y-2 p-0">
          <li v-for="(check, index) in passedSummary" :key="`${check.name}:${index}`" class="whitespace-pre-wrap"><span class="font-medium">{{ check.name }}：</span>{{ check.message }}</li>
        </ul>
        <p class="m-0">解析探测针对检查时运行的 DNS 服务；新配置仍需发布后的服务检查。<time v-if="checkedTime" :datetime="checkedAt" class="mt-1 block">检查时间：{{ checkedTime }}</time></p>
      </div>
    </AgentStatusRow>
    <template v-else>
      <template v-if="issues.length">
        <p class="m-0 font-medium">DNS 检查</p>
        <ul class="m-0 list-none space-y-3 p-0">
          <li v-for="(check, index) in issues" :key="`${check.name}:${index}`" class="min-w-0">
            <p class="m-0 font-medium" :class="check.level === 'error' ? 'text-destructive-accent' : 'text-warning-accent'">{{ check.level === 'error' ? '错误' : '警告' }} · {{ check.name }}</p>
            <p class="m-0 whitespace-pre-wrap">{{ check.message }}</p>
            <p v-if="check.fix" class="mb-0 mt-1 whitespace-pre-wrap text-muted-foreground">修复建议：{{ check.fix }}</p>
          </li>
        </ul>
      </template>
      <details v-if="passed.length" class="min-w-0">
        <summary class="flex min-h-11 cursor-pointer items-center font-medium text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-accent">{{ issues.length ? '查看通过项目' : 'DNS 检查' }} · {{ passed.length }} 项通过</summary>
        <ul class="m-0 list-none space-y-2 p-0 pb-2 text-muted-foreground">
          <li v-for="(check, index) in passedSummary" :key="`${check.name}:${index}`" class="whitespace-pre-wrap"><span class="font-medium">{{ check.name }}：</span>{{ check.message }}</li>
        </ul>
      </details>
      <p class="m-0 text-muted-foreground">解析探测针对检查时运行的 DNS 服务；新配置仍需发布后的服务检查。<time v-if="checkedTime" :datetime="checkedAt" class="mt-1 block">检查时间：{{ checkedTime }}</time></p>
    </template>
  </section>
</template>
