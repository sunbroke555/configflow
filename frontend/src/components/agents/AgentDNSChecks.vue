<script setup lang="ts">
import { computed } from 'vue'
import type { AgentDNSCheck } from '@/composables/useAgentDeployments'

const props = defineProps<{ checks?: AgentDNSCheck[]; checkedAt?: string }>()
const issues = computed(() => props.checks?.filter(check => check.level !== 'pass') || [])
const passed = computed(() => props.checks?.filter(check => check.level === 'pass') || [])
const checkedTime = computed(() => {
  if (!props.checkedAt) return ''
  const date = new Date(props.checkedAt)
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString()
})
</script>

<template>
  <section v-if="checks?.length" aria-label="DNS 检查结果" class="min-w-0 space-y-2 rounded-md border border-border bg-background/50 p-3 text-xs leading-relaxed text-foreground [overflow-wrap:anywhere]">
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
        <li v-for="(check, index) in passed" :key="`${check.name}:${index}`" class="whitespace-pre-wrap"><span class="font-medium">{{ check.name }}：</span>{{ check.message }}</li>
      </ul>
    </details>
    <p class="m-0 text-muted-foreground">解析探测针对检查时运行的 DNS 服务；新配置仍需发布后的服务检查。<time v-if="checkedTime" :datetime="checkedAt" class="mt-1 block">检查时间：{{ checkedTime }}</time></p>
  </section>
</template>
