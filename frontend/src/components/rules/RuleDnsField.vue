<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ExternalLink, Info, TriangleAlert } from '@lucide/vue'
import FormField from '@/components/common/FormField.vue'
import { Button } from '@/components/ui/button'
import RuleDnsSelect from '@/components/rules/RuleDnsSelect.vue'
import type { DnsWarning } from '@/lib/ruleDns'
import type { RuleDnsUpstream, RuleDnsValue } from '@/types'

/** 表单中的 DNS 字段：分组选择器加一个警告槽，同一时间只显示一条，其余收进「另有 N 条提示」 */
const props = defineProps<{
  modelValue: RuleDnsValue
  hint: string
  help: string
  /** 不可转换的类型、未加载等需要直接可见的提示 */
  persistentHint?: boolean
  warnings: DnsWarning[]
  upstreams?: RuleDnsUpstream[]
  upstreamLabel?: string
  policyRoute?: 'direct' | 'proxy' | null
  defaultForward?: string
  disabled?: boolean
  loading?: boolean
}>()
const emit = defineEmits<{ 'update:modelValue': [value: RuleDnsValue]; action: [kind: NonNullable<DnsWarning['action']>] }>()

const expanded = ref(false)
const visible = computed(() => (expanded.value ? props.warnings : props.warnings.slice(0, 1)))
watch(() => props.warnings.length, count => { if (count < 2) expanded.value = false })
</script>

<template>
  <FormField label="DNS（OxiDNS）" html-for="rule-dns" :hint="hint" :help="help" :persistent-hint="persistentHint">
    <RuleDnsSelect
      id="rule-dns"
      :model-value="modelValue"
      label="DNS（OxiDNS）"
      :upstreams="upstreams"
      :upstream-label="upstreamLabel"
      :policy-route="policyRoute"
      :default-forward="defaultForward"
      :disabled="disabled"
      :loading="loading"
      @update:model-value="value => emit('update:modelValue', value)"
    />
    <div v-if="warnings.length" class="flex flex-col gap-1.5" data-dns-warnings>
      <div
        v-for="(warning, index) in visible"
        :key="`${warning.kind}-${index}`"
        role="note"
        :class="[
          'flex items-start gap-2 rounded-lg border px-3 py-2 text-[12.5px] leading-relaxed',
          warning.tone === 'warning' ? 'border-warning-accent/30 bg-warning-soft/40 text-foreground' : 'border-border bg-muted text-muted-foreground'
        ]"
      >
        <component
          :is="warning.tone === 'warning' ? TriangleAlert : Info"
          :class="['mt-0.5 size-3.5 shrink-0', warning.tone === 'warning' ? 'text-warning-accent' : 'text-primary-accent']"
          aria-hidden="true"
        />
        <div class="flex min-w-0 flex-1 flex-wrap items-center gap-x-2 gap-y-1">
          <span class="min-w-0 break-words">{{ warning.text }}</span>
          <a
            v-if="warning.action === 'open-config'"
            href="/generate?oxidns=config"
            target="_blank"
            rel="noopener"
            class="inline-flex min-h-6 items-center gap-1 text-primary-accent underline-offset-4 hover:underline max-sm:min-h-11"
          >{{ warning.actionLabel }}<ExternalLink class="size-3" aria-hidden="true" /></a>
          <Button
            v-else-if="warning.action"
            type="button"
            variant="ghost"
            size="xs"
            class="h-6 px-1.5 text-primary-accent max-sm:h-11"
            @click="emit('action', warning.action!)"
          >{{ warning.actionLabel }}</Button>
        </div>
        <Button
          v-if="index === 0 && warnings.length > 1 && !expanded"
          type="button"
          variant="ghost"
          size="xs"
          class="h-6 shrink-0 px-1.5 text-muted-foreground max-sm:h-11"
          :aria-expanded="false"
          @click="expanded = true"
        >另有 {{ warnings.length - 1 }} 条提示</Button>
      </div>
    </div>
  </FormField>
</template>
