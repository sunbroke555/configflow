<script setup lang="ts">
import { computed } from 'vue'
import { Copy } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { copyText } from '@/lib/clipboard'
import { notify } from '@/lib/feedback'
import { forwardFilterWarning, type ForwardFilter, type ForwardFilterGroup } from '@/lib/forwardFilter'

// 完整 TUN 主机的转发过滤提醒：只展示 Agent 给出的放行命令，不改动任何状态或完成判定。
const props = defineProps<{ filter?: ForwardFilter | null }>()
const warning = computed(() => forwardFilterWarning(props.filter))

async function copyGroup(group: ForwardFilterGroup) {
  try {
    await copyText(group.text)
    notify.success('放行命令已复制。')
  } catch {
    notify.error('复制失败，请手动选择命令。')
  }
}
</script>

<template>
  <div v-if="warning" class="min-w-0 space-y-3 text-xs leading-relaxed" data-forward-filter-notice>
    <p class="m-0 text-foreground">{{ warning.hint }}</p>
    <div v-for="group in warning.groups" :key="group.key" class="min-w-0 space-y-1.5" data-forward-filter-group>
      <div class="flex min-h-8 items-center justify-between gap-2 max-md:min-h-11">
        <p class="m-0 min-w-0 font-medium text-foreground [overflow-wrap:anywhere]">{{ group.label }}</p>
        <Button variant="ghost" size="sm" class="shrink-0 px-2 text-xs max-md:min-h-11" :aria-label="`复制放行命令（${group.source}）`" @click="copyGroup(group)">
          <Copy class="size-3.5" aria-hidden="true" />复制
        </Button>
      </div>
      <pre class="m-0 min-w-0 overflow-x-auto whitespace-pre rounded-md border border-border bg-muted px-3 py-2 font-mono text-xs leading-relaxed text-foreground">{{ group.text }}</pre>
      <p v-if="group.persistNote" class="m-0 text-muted-foreground">{{ group.persistNote }}</p>
    </div>
    <p v-if="warning.verify" class="m-0 text-muted-foreground">{{ warning.verify }}</p>
    <details v-if="warning.details.length" class="text-muted-foreground">
      <summary class="min-h-11 cursor-pointer content-center py-2 font-medium outline-none focus-visible:ring-2 focus-visible:ring-primary-accent">检查详情</summary>
      <ul class="mt-0 mb-2 list-disc space-y-1 pl-5"><li v-for="item in warning.details" :key="item" class="break-words [overflow-wrap:anywhere]">{{ item }}</li></ul>
    </details>
  </div>
</template>
