<template>
  <SectionCard :padded="false" role="region" aria-label="运行状态">
    <header class="flex flex-wrap items-center gap-3 px-5 pt-5 pb-3 max-md:px-4">
      <h2 class="m-0 flex items-center gap-2 text-[14px] font-semibold text-foreground">
        <Activity class="size-4 text-muted-foreground" :stroke-width="2" aria-hidden="true" />
        运行状态
      </h2>
      <Tabs
        :model-value="active"
        class="ml-auto min-w-0 max-w-full max-sm:ml-0 max-sm:w-full"
        @update:model-value="$emit('update:active', String($event))"
      >
        <TabsList class="h-auto min-h-9 flex-wrap justify-start bg-muted" aria-label="运行记录类型">
          <TabsTrigger v-for="tab in tabs" :key="tab" :value="tab" class="min-h-8 text-xs">
            {{ tab }}
          </TabsTrigger>
        </TabsList>
      </Tabs>
    </header>

    <Separator />

    <EmptyState
      v-if="!rows.length"
      :icon="Activity"
      title="暂无运行记录"
      description="试试切换上方分类。更新订阅或生成配置后，也可在这里查看结果。"
    />

    <ol v-else class="m-0 list-none px-5 py-3 max-md:px-4">
      <li
        v-for="(row, i) in rows"
        :key="`${row.time}-${i}`"
        class="relative grid grid-cols-[14px_minmax(0,1fr)_auto] items-start gap-x-3 py-3 max-sm:grid-cols-[14px_minmax(0,1fr)]"
      >
        <!-- 主干线：最后一项不再向下延伸 -->
        <span
          v-if="i < rows.length - 1"
          class="absolute top-6 bottom-0 left-[6px] w-px bg-border/70"
          aria-hidden="true"
        />
        <span
          class="relative mt-1.5 size-3 rounded-full border-2 border-background"
          :class="dotTone(row.level)"
          aria-hidden="true"
        />

        <div class="min-w-0">
          <div class="flex flex-wrap items-center gap-2">
            <span class="text-[13px] font-medium text-foreground">{{ row.task }}</span>
            <Badge :variant="badgeTone(row.level)" class="h-5 px-1.5 text-[10.5px]">
              {{ row.status }}
            </Badge>
          </div>
          <p class="mt-1 mb-0 text-[13px] leading-relaxed [overflow-wrap:anywhere] text-muted-foreground">
            {{ row.detail }}
          </p>
        </div>

        <span class="num mt-0.5 shrink-0 text-[11.5px] whitespace-nowrap text-muted-foreground max-sm:col-start-2 max-sm:mt-1.5">
          {{ row.time }}
        </span>
      </li>
    </ol>

    <template v-if="rows.length">
      <Separator />
      <footer class="px-5 py-2.5 text-xs text-muted-foreground max-md:px-4">共 {{ rows.length }} 条</footer>
    </template>
  </SectionCard>
</template>

<script setup lang="ts">
import { Activity } from '@lucide/vue'
import { Badge } from '@/components/ui/badge'
import { Separator } from '@/components/ui/separator'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import EmptyState from '@/components/common/EmptyState.vue'
import SectionCard from '@/components/common/SectionCard.vue'

export interface ActivityRow {
  task: string
  detail: string
  status: string
  level: 'ok' | 'warn' | 'err'
  time: string
}

defineProps<{ rows: ActivityRow[]; tabs: string[]; active: string }>()
defineEmits<{ (e: 'update:active', tab: string): void }>()

const dotTone = (level: ActivityRow['level']): string =>
  level === 'ok'
    ? 'bg-success-accent'
    : level === 'warn'
      ? 'bg-warning-accent'
      : 'bg-destructive-accent'

const badgeTone = (level: ActivityRow['level']) =>
  level === 'ok' ? ('success' as const) : level === 'warn' ? ('warning' as const) : ('danger' as const)
</script>
