<script setup lang="ts">
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import FieldHelp from '@/components/common/FieldHelp.vue'
import { optionHelp } from '@/lib/optionCopy'

const interval = defineModel<number | string>({ required: true })
</script>

<template>
  <div class="grid shrink-0 gap-2 border-b border-border px-4 py-3 md:px-5" data-mihomo-sync-interval>
    <div class="flex items-center gap-1"><Label for="mihomo-provider-interval">订阅同步间隔</Label><FieldHelp label="订阅同步间隔" :text="optionHelp('希望 Mihomo 定时从面板获取订阅与聚合的最新内容。', '此间隔控制核心与面板同步，0 表示仅手动同步。原始订阅是否重新获取仍按订阅来源的刷新间隔判断，两项时间各自生效。')" /></div>
    <div class="flex items-center gap-2">
      <Input id="mihomo-provider-interval" v-model.number="interval" type="number" :min="0" :max="604800" :step="60" class="h-11 w-40" />
      <span class="text-xs text-muted-foreground">秒</span>
    </div>
  </div>
</template>
