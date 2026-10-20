<script setup lang="ts">
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import FieldHelp from '@/components/common/FieldHelp.vue'
import { optionHelp } from '@/lib/optionCopy'

const interval = defineModel<number | string>({ required: true })
</script>

<template>
  <div class="grid gap-2 border-b border-border px-4 py-3 md:px-5">
    <div class="flex items-center gap-1"><Label for="mihomo-provider-interval">订阅同步间隔</Label><FieldHelp label="订阅同步间隔" :text="optionHelp('希望 Mihomo 定时从面板获取订阅与聚合的最新内容。', '此间隔控制核心与面板同步，0 表示仅手动同步。原始订阅是否重新获取仍按订阅来源的刷新间隔判断，两项时间各自生效。')" /></div>
    <div class="flex items-center gap-2">
      <Input id="mihomo-provider-interval" v-model.number="interval" type="number" :min="0" :max="604800" :step="60" class="w-40" />
      <span class="text-xs text-muted-foreground">秒，0 为仅手动同步</span>
    </div>
    <p class="m-0 text-xs text-muted-foreground">定时从面板同步内容，原始订阅仍按来源设置刷新。</p>
  </div>
</template>
