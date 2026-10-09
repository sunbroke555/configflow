<template>
  <section
    :class="cn(
      'min-w-0 overflow-hidden rounded-xl border border-border bg-card',
      interactive && 'transition-colors hover:border-border-strong motion-reduce:transition-none',
      padded && 'p-5 max-md:p-4'
    )"
  >
    <header v-if="title || description || $slots.actions" :class="cn('flex flex-wrap items-center gap-3', padded ? 'mb-4' : 'px-5 pt-5 pb-4 max-md:px-4')">
      <div class="min-w-0 flex-1">
        <h2 v-if="title" class="m-0 flex items-center gap-2 text-sm font-semibold text-foreground">
          <component v-if="icon" :is="icon" class="size-4 shrink-0 text-muted-foreground" :stroke-width="2" aria-hidden="true" />
          {{ title }}
        </h2>
        <p v-if="description" class="mt-1 mb-0 break-words text-[13px] leading-relaxed text-muted-foreground">
          {{ description }}
        </p>
      </div>
      <div v-if="$slots.actions" class="flex min-w-0 max-w-full flex-wrap items-center gap-2">
        <slot name="actions" />
      </div>
    </header>

    <slot />
  </section>
</template>

<script setup lang="ts">
import type { Component } from 'vue'
import { cn } from '@/lib/utils'

withDefaults(
  defineProps<{
    title?: string
    description?: string
    icon?: Component
    /** 内容自带内边距时（例如整块是表格）传 false */
    padded?: boolean
    interactive?: boolean
  }>(),
  { padded: true, interactive: false }
)
</script>
