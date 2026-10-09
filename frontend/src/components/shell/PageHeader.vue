<template>
  <header class="mb-6 flex flex-wrap items-start gap-4">
    <div class="min-w-0 flex-[1_1_260px]">
      <h1 class="m-0 text-2xl leading-tight font-semibold tracking-[-0.025em] break-words text-foreground">
        {{ title }}
      </h1>
      <p v-if="description" class="mt-2 mb-0 max-w-[65ch] text-[13px] leading-relaxed text-muted-foreground">
        {{ description }}
      </p>
    </div>
    <!-- 一个视图只有一个主操作，其余降级为次要/图标操作 -->
    <div v-if="$slots.actions" class="flex max-w-full shrink-0 flex-wrap items-center gap-2 max-md:w-full">
      <slot name="actions" />
    </div>
  </header>
</template>

<script setup lang="ts">
defineProps<{ title: string; description?: string }>()
</script>
