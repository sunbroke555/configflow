<script lang="ts" setup>
import type { ToasterProps } from "vue-sonner"
import { CircleCheckIcon, InfoIcon, Loader2Icon, OctagonXIcon, TriangleAlertIcon, XIcon } from "@lucide/vue"
import { Toaster as Sonner } from "vue-sonner"
import { cn } from "@/lib/utils"

const props = defineProps<ToasterProps>()
</script>

<template>
  <Sonner
    :class="cn('toaster group', props.class)"
    :style="{
      /* 反色胶囊：深色主题下是象牙底黑字，浅色主题下是炭黑底白字 */
      '--normal-bg': 'var(--foreground)',
      '--normal-text': 'var(--background)',
      '--normal-border': 'transparent',
      '--border-radius': '12px',
    }"
    v-bind="props"
  >
    <template #success-icon>
      <!-- 反色底使用状态色本身，避免当前主题的 accent 在反色底上对比不足。 -->
      <CircleCheckIcon class="size-4 text-success" />
    </template>
    <template #info-icon>
      <InfoIcon class="size-4 text-info" />
    </template>
    <template #warning-icon>
      <TriangleAlertIcon class="size-4 text-warning" />
    </template>
    <template #error-icon>
      <OctagonXIcon class="size-4 text-destructive" />
    </template>
    <template #loading-icon>
      <div>
        <Loader2Icon class="size-4 animate-spin text-primary" />
      </div>
    </template>
    <template #close-icon>
      <XIcon class="size-4" />
    </template>
  </Sonner>
</template>

<style>
/* 说明文字跟随反色通知的正文色，覆盖 Sonner 默认的固定灰色。 */
.toaster[data-sonner-toaster] [data-sonner-toast][data-styled='true'] [data-description] {
  color: inherit;
}
</style>
