<template>
  <div>
    <p class="m-0 mb-1 flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
      <component v-if="icon" :is="icon" class="size-3" aria-hidden="true" />
      {{ label }}
    </p>
    <div class="text-[12.5px] leading-relaxed break-words text-foreground/85">
      <slot />
    </div>
  </div>
</template>

<script setup lang="ts">
import type { Component } from 'vue'

defineProps<{ label: string; icon?: Component }>()
</script>
