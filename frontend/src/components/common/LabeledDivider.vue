<template>
  <div class="flex items-center gap-3 pt-1">
    <span class="text-xs font-semibold text-muted-foreground">
      {{ label }}
    </span>
    <span class="h-px flex-1 bg-border/60" aria-hidden="true" />
  </div>
</template>

<script setup lang="ts">
defineProps<{ label: string }>()
</script>
