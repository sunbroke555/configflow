<template>
  <header class="mb-6 flex flex-wrap items-end gap-x-6 gap-y-4">
    <div class="min-w-0 flex-[1_1_280px]">
      <h1
        class="m-0 text-[28px] leading-tight font-semibold tracking-[-0.025em] text-foreground max-md:text-[24px]"
      >
        {{ title }}
      </h1>
      <p v-if="description" class="mt-2 mb-0 max-w-[65ch] break-words text-sm leading-relaxed text-muted-foreground">
        {{ description }}
      </p>
    </div>

    <div v-if="$slots.actions" class="flex min-w-0 max-w-full flex-wrap items-center gap-2 max-md:w-full">
      <slot name="actions" />
    </div>
  </header>
</template>

<script setup lang="ts">
defineProps<{ title: string; description?: string }>()
</script>
