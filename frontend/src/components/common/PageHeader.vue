<template>
  <header class="cf-page-header relative mb-6 flex flex-wrap items-end gap-4 max-md:mb-4">
    <div v-if="$slots['title-extra']" class="cf-page-heading flex min-w-0 flex-[1_1_240px] items-center gap-3">
      <h1 class="font-display m-0 shrink-0 text-[34px] leading-[1.12] text-foreground max-md:text-[26px]">
        {{ title }}
      </h1>
      <slot name="title-extra" />
    </div>
    <h1
      v-else
      class="font-display m-0 min-w-0 flex-[1_1_240px] text-[34px] leading-[1.12] text-foreground max-md:text-[26px]"
    >
      {{ title }}
    </h1>

    <div v-if="$slots.actions" class="cf-page-actions flex min-w-0 max-w-full flex-wrap items-center gap-2 max-md:w-full">
      <slot name="actions" />
    </div>
  </header>
</template>

<script setup lang="ts">
defineProps<{ title: string }>()
</script>
