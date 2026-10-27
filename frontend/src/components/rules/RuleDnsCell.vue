<script setup lang="ts">
import { computed } from 'vue'
import { ArrowRightLeft, Globe, Lock, Server, TriangleAlert } from '@lucide/vue'
import FieldHelp from '@/components/common/FieldHelp.vue'
import RuleDnsSelect from '@/components/rules/RuleDnsSelect.vue'
import { cn } from '@/lib/utils'
import { chipClass, choiceLabel, dnsChip, followRoute, stateValue } from '@/lib/ruleDns'
import type { RuleDnsSnapshot, RuleDnsValue } from '@/types'

/** 列表与卡片中一条规则的 DNS：只读状态直接显示，可修改时为轻量文字选择器。 */
const props = defineProps<{
  item: { id: string; itemType?: string; rule_type?: string; value?: string; name?: string; policy?: string }
  snapshot: RuleDnsSnapshot | null
  /** DNS 状态加载中：只显示骨架，绝不显示「默认上游」 */
  loading?: boolean
  /** 快照读取之后新增或修改过这条规则：快照中的值（或缺省的默认上游）可能已过时 */
  stale?: boolean
  /** 正在保存的新选择 */
  pending?: RuleDnsValue | null
  /** 排序模式等不可交互的场景 */
  disabled?: boolean
  class?: string
}>()
const emit = defineEmits<{ change: [value: RuleDnsValue] }>()

const entry = computed(() => props.snapshot?.rules[props.item.id])
const engine = computed(() => props.snapshot?.engine)
const chip = computed(() => engine.value && dnsChip(entry.value, engine.value))
const name = computed(() => props.item.itemType === 'ruleset' ? props.item.name || props.item.id : props.item.value || props.item.id)
const policyRoute = computed(() => followRoute(props.item, engine.value?.policy_routes))
const pendingText = computed(() => props.pending && choiceLabel(props.pending, { policyRoute: policyRoute.value }))
const icons = { direct: ArrowRightLeft, proxy: Globe, upstream: Server, drift: TriangleAlert, lock: Lock }
const readOnly = computed(() => !chip.value || entry.value?.state === 'unsupported' || entry.value?.state === 'disabled' || !engine.value?.editable)
</script>

<template>
  <span :class="cn('inline-flex w-full min-w-0 max-w-full items-center', props.class)">
    <span
      v-if="loading && (!snapshot || stale)"
      role="status"
      aria-label="DNS 状态加载中"
      class="inline-block h-6 w-24 max-w-full animate-pulse rounded-full bg-primary/10"
    />
    <span v-else-if="!chip || stale" class="px-2 text-[12px] text-muted-foreground" title="DNS 状态读取失败" aria-label="DNS 状态读取失败">—</span>
    <span
      v-else-if="entry?.state === 'unsupported'"
      class="truncate px-2 text-[12px] text-muted-foreground"
      :title="chip.title"
      :aria-label="chip.title"
    >{{ chip.text }}</span>
    <span v-else-if="readOnly" class="inline-flex min-w-0 max-w-full items-center gap-0.5">
      <span :class="cn('inline-flex h-[30px] min-w-0 max-w-full items-center gap-1.5 px-2 text-[12px]', chipClass(chip.tone))" :title="chip.title" :aria-label="`${name} 的 DNS：${chip.title}`">
        <component :is="icons[chip.icon]" v-if="chip.icon" class="size-3.5 shrink-0" aria-hidden="true" />
        <span class="min-w-0 flex-1 truncate" :class="chip.icon === 'upstream' && 'font-mono'">{{ chip.text }}</span>
      </span>
      <FieldHelp v-if="chip.icon === 'lock' && engine?.reason" :label="`${name} 的 DNS`" :text="engine.reason" />
    </span>
    <RuleDnsSelect
      v-else
      variant="cell"
      :model-value="pending || stateValue(entry)"
      :label="`${name} 的 DNS：${pendingText || chip.title}，点击修改`"
      :upstreams="snapshot?.upstreams"
      :upstream-label="entry?.upstream_label"
      :policy-route="policyRoute"
      :default-forward="engine?.default_forward"
      :disabled="disabled"
      :loading="!!pending"
      :trigger-class="chipClass(pending ? 'neutral' : chip.tone)"
      @update:model-value="value => { if (value !== stateValue(entry) || entry?.state === 'follow_drift') emit('change', value) }"
    >
      <template v-if="pending">
        <span class="truncate">{{ pendingText }}</span>
      </template>
      <template v-else>
        <component :is="icons[chip.icon]" v-if="chip.icon" class="size-3.5 shrink-0" aria-hidden="true" />
        <span class="min-w-0 flex-1 truncate" :class="chip.icon === 'upstream' && 'font-mono'">{{ chip.text }}</span>
      </template>
    </RuleDnsSelect>
  </span>
</template>
