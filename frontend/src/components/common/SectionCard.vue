<template>
  <section
    :class="cn(
      'relative overflow-hidden rounded-[18px] border border-border bg-card/90 shadow-surface transition-[border-color,transform] duration-350 ease-(--ease-flow)',
      interactive && 'hover:-translate-y-[3px] hover:border-border-strong',
      padded && 'p-5 max-md:p-4'
    )"
  >
    <header v-if="title || description || $slots.actions" :class="cn('cf-section-header flex flex-wrap items-center gap-3', padded ? 'mb-4' : 'px-5 pt-5 pb-3 max-md:px-4')">
      <div class="min-w-0 flex-1">
        <h2 v-if="title" class="m-0 flex max-sm:flex-wrap items-center gap-2 text-[14px] font-semibold tracking-[-0.01em] text-foreground">
          <component v-if="icon" :is="icon" class="size-4 text-primary-accent" :stroke-width="2.2" aria-hidden="true" />
          {{ title }}
          <FieldHelp v-if="help" :label="title" :text="help" />
          <slot name="title-extra" />
        </h2>
        <p v-if="description" class="mt-1 mb-0 text-[12px] leading-relaxed text-muted-foreground">
          {{ description }}
        </p>
      </div>
      <div v-if="$slots.actions" class="cf-section-actions flex min-w-0 max-w-full flex-wrap items-center gap-2">
        <slot name="actions" />
      </div>
    </header>

    <slot />
  </section>
</template>

<script setup lang="ts">
import type { Component } from 'vue'
import { cn } from '@/lib/utils'
import FieldHelp from './FieldHelp.vue'

withDefaults(
  defineProps<{
    title?: string
    description?: string
    help?: string
    icon?: Component
    /** 内容自带内边距时（例如整块是表格）传 false */
    padded?: boolean
    interactive?: boolean
  }>(),
  { padded: true, interactive: false }
)
</script>
