<script setup lang="ts">
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import FieldHelp from '@/components/common/FieldHelp.vue'
import { optionHelp } from '@/lib/optionCopy'

const interval = defineModel<number | string>({ required: true })
</script>

<template>
  <div class="grid shrink-0 grid-cols-[minmax(0,1fr)_8rem] items-center gap-x-4 gap-y-1 border-b border-border px-4 py-2 sm:grid-cols-[13rem_10rem_minmax(0,1fr)] md:px-5" data-mihomo-sync-interval>
    <div class="flex min-w-0 items-center gap-1"><Label for="mihomo-provider-interval" class="min-h-11 text-[13px]">订阅同步间隔</Label><FieldHelp label="订阅同步间隔" :text="optionHelp('希望 Mihomo 定时从面板获取订阅与聚合的最新内容。', '此间隔控制核心与面板同步，0 表示仅手动同步。原始订阅是否重新获取仍按订阅来源的刷新间隔判断，两项时间各自生效。')" /></div>
    <div class="flex min-h-11 items-center gap-2">
      <Input id="mihomo-provider-interval" v-model.number="interval" type="number" :min="0" :max="604800" :step="60" class="h-11 w-24" />
      <span class="text-xs text-muted-foreground">秒</span>
    </div>
    <p class="col-span-2 m-0 text-xs leading-relaxed text-muted-foreground sm:col-span-1">0 为仅手动同步；原始订阅按来源设置刷新。</p>
  </div>
</template>
