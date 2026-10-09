<template>
  <div class="flex flex-col items-center justify-center gap-3 px-6 py-14 text-center">
    <div
      class="flex size-11 items-center justify-center rounded-lg bg-muted text-muted-foreground"
    >
      <component :is="icon ?? Inbox" class="size-5" :stroke-width="1.75" aria-hidden="true" />
    </div>
    <p class="m-0 text-[14px] font-medium text-foreground">{{ title }}</p>
    <p v-if="description" class="m-0 max-w-[46ch] text-[13px] leading-relaxed text-muted-foreground">
      {{ description }}
    </p>
    <div v-if="$slots.default" class="mt-2 flex flex-wrap justify-center gap-2">
      <slot />
    </div>
  </div>
</template>

<script setup lang="ts">
import type { Component } from 'vue'
import { Inbox } from '@lucide/vue'

defineProps<{ title: string; description?: string; icon?: Component }>()
</script>
