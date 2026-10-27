<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { isAxiosError } from 'axios'
import { Loader2 } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import FormField from '@/components/common/FormField.vue'
import RuleDnsSelect from '@/components/rules/RuleDnsSelect.vue'
import { ruleApi } from '@/api'
import { confirm, notify } from '@/lib/feedback'
import { isOxidnsRule } from '@/lib/oxidns'
import { CHOICE_LABELS, DNS_COPY, PINNED_HINT, choiceLabel, copyKey, followRoute, isUpstreamValue, notifyRuleSaved, pinnedWithheld, stateValue } from '@/lib/ruleDns'
import type { RuleDnsSnapshot, RuleDnsState, RuleDnsValue } from '@/types'

type Item = { id: string; itemType?: string; rule_type?: string; behavior?: string; policy?: string }

/** 当前展示分组的 DNS 批量设置；覆盖单独设置前先确认影响 */
const props = defineProps<{
  open: boolean
  items: Item[]
  groupName: string
  snapshot: RuleDnsSnapshot | null
  profileId: string
}>()
const emit = defineEmits<{ 'update:open': [value: boolean]; applied: [rules: Record<string, RuleDnsState>] }>()

const value = ref<RuleDnsValue>('follow')
const saving = ref(false)
const convertible = (item: Item) => isOxidnsRule(item) && props.snapshot?.rules[item.id]?.state !== 'unsupported'
const policyRoute = computed(() => (props.items[0] ? followRoute({ itemType: 'ruleset', policy: props.items[0].policy }, props.snapshot?.engine.policy_routes) : null))
const upstreamLabel = computed(() => (isUpstreamValue(value.value)
  ? props.snapshot?.upstreams.find(item => `upstream:${item.key}` === value.value)?.label : undefined))
const label = computed(() => choiceLabel(value.value, { policyRoute: policyRoute.value, upstreamLabel: upstreamLabel.value }))

watch(() => props.open, open => {
  if (!open) return
  // 组内设置一致时以它为初值，否则默认跟随策略。
  const values = new Set(props.items.filter(convertible).map(item => stateValue(props.snapshot?.rules[item.id])))
  value.value = values.size === 1 ? [...values][0]! : 'follow'
})

// 默认自动聚焦会选中标签旁的问号，导致弹窗一打开就显示帮助。
function focusDns(event: Event) {
  const trigger = document.getElementById('rule-dns-batch')
  if (!trigger) return
  event.preventDefault()
  trigger.focus({ preventScroll: true })
}

/** 应用前的影响：会被覆盖的单独设置与不参与 DNS 的规则 */
const impact = computed(() => {
  const overwritten = new Map<string, number>()
  const skipped = new Map<string, number>()
  for (const item of props.items) {
    if (!convertible(item)) {
      if (value.value !== 'default') skipped.set(item.rule_type || item.behavior || '不支持的规则', (skipped.get(item.rule_type || item.behavior || '不支持的规则') || 0) + 1)
      continue
    }
    const current = stateValue(props.snapshot?.rules[item.id])
    if (current === 'follow' || current === 'default' || current === value.value) continue
    const name = isUpstreamValue(current) ? '指定上游' : CHOICE_LABELS[current] || current
    overwritten.set(name, (overwritten.get(name) || 0) + 1)
  }
  const count = (map: Map<string, number>) => [...map.values()].reduce((sum, item) => sum + item, 0)
  return {
    overwritten: count(overwritten), skipped: count(skipped),
    text: [
      `将把 ${props.items.length} 项的 DNS 设为「${label.value}」`,
      ...(overwritten.size ? [`将覆盖 ${count(overwritten)} 条单独设置（${[...overwritten].map(([name, total]) => `${name} ${total}`).join(' · ')}）`] : []),
      ...(skipped.size ? [`跳过 ${count(skipped)} 条 ${[...skipped.keys()].join('、')}`] : [])
    ].join('，')
  }
})

async function apply() {
  if (saving.value || !props.items.length) return
  if (!(await confirm(`${impact.value.text}。`, { title: '批量设置 DNS', confirmText: '应用' }))) return
  saving.value = true
  try {
    const routes = Object.fromEntries(props.items.map(item => [item.id, value.value]))
    const { data } = await ruleApi.setDns(routes, { skipUnsupported: true }, props.profileId)
    emit('applied', data.rules)
    const changed = props.items.length - (data.skipped?.length || 0)
    notifyRuleSaved(`已更新 ${changed} 项的 DNS`, {
      agents: props.snapshot?.engine.agents,
      scopes: [...new Set(props.items.map(item => (item.itemType === 'ruleset' ? 'library' as const : 'rules' as const)))],
      pinned: pinnedWithheld(data, isUpstreamValue(value.value)),
      extra: data.warnings
    })
    emit('update:open', false)
  } catch (error) {
    const message = isAxiosError<{ message?: string; usages?: Array<{ reason?: string }> }>(error)
      ? error.response?.data?.usages?.[0]?.reason || error.response?.data?.message : undefined
    notify.error(message || 'DNS 处理修改失败')
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <Dialog :open="open" @update:open="next => { if (!saving) emit('update:open', next) }">
    <DialogContent class="max-w-[480px]" @open-auto-focus="focusDns">
      <DialogHeader>
        <DialogTitle>批量设置 DNS</DialogTitle>
        <DialogDescription>{{ groupName }} · 共 {{ items.length }} 项。只修改这一组规则的 DNS，不参与 DNS 分流的规则会被跳过。</DialogDescription>
      </DialogHeader>
      <FormField
        label="DNS（OxiDNS）"
        html-for="rule-dns-batch"
        :hint="isUpstreamValue(value) ? PINNED_HINT : undefined"
        :help="`${DNS_COPY[copyKey(value)].hint}\n\n${DNS_COPY[copyKey(value)].help}`"
      >
        <RuleDnsSelect
          id="rule-dns-batch"
          v-model="value"
          label="批量设置 DNS"
          :upstreams="snapshot?.upstreams"
          :policy-route="policyRoute"
          :default-forward="snapshot?.engine.default_forward"
          :disabled="saving"
        />
      </FormField>
      <p class="m-0 text-[12.5px] text-muted-foreground" data-batch-impact>{{ impact.text }}</p>
      <DialogFooter>
        <Button type="button" variant="outline" :disabled="saving" @click="emit('update:open', false)">取消</Button>
        <Button type="button" :disabled="saving || !items.length" @click="apply">
          <Loader2 v-if="saving" class="size-4 animate-spin" />应用
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
