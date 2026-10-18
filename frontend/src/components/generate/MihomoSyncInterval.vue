<script setup lang="ts">
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

const interval = defineModel<number | string>({ required: true })
</script>

<template>
  <div class="grid gap-2 border-b border-border px-4 py-3 md:px-5">
    <Label for="mihomo-provider-interval">订阅同步间隔</Label>
    <div class="flex items-center gap-2">
      <Input id="mihomo-provider-interval" v-model.number="interval" type="number" :min="0" :max="604800" :step="60" class="w-40" />
      <span class="text-xs text-muted-foreground">秒，0 为仅手动同步</span>
    </div>
    <p class="m-0 text-xs text-muted-foreground">控制 Mihomo 从面板同步订阅与聚合；订阅源是否刷新，仍按「订阅来源」中的间隔判断。</p>
  </div>
</template>
