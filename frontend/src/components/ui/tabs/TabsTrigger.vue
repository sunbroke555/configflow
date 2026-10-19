<script setup lang="ts">
import type { TabsTriggerProps } from "reka-ui"
import type { HTMLAttributes } from "vue"
import { reactiveOmit } from "@vueuse/core"
import { TabsTrigger, useForwardProps } from "reka-ui"
import { cn } from "@/lib/utils"

const props = defineProps<TabsTriggerProps & { class?: HTMLAttributes["class"] }>()

const delegatedProps = reactiveOmit(props, "class")

const forwardedProps = useForwardProps(delegatedProps)
</script>

<template>
  <TabsTrigger
    data-slot="tabs-trigger"
    :class="cn(
      `data-[state=active]:border-primary-accent data-[state=active]:bg-primary-soft data-[state=active]:text-primary-accent dark:data-[state=active]:text-foreground focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:outline-ring dark:data-[state=active]:border-input dark:data-[state=active]:bg-input/30 text-foreground dark:text-muted-foreground inline-flex h-[calc(100%-1px)] flex-1 items-center justify-center gap-1.5 rounded-md border border-transparent px-2 py-1 text-sm font-medium whitespace-nowrap transition-[color,box-shadow] focus-visible:ring-3 focus-visible:outline-1 disabled:pointer-events-none disabled:opacity-50 data-[state=active]:shadow-sm [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4`,
      'cf-tabs-trigger max-sm:data-[state=active]:before:shadow-sm',
      props.class,
    )"
    v-bind="forwardedProps"
  >
    <slot />
  </TabsTrigger>
</template>

<style scoped>
@media (width < 640px) {
  .cf-tabs-trigger {
    position: relative;
    isolation: isolate;
    height: var(--cf-touch);
    min-height: var(--cf-touch);
    min-width: var(--cf-touch);
    flex-shrink: 0;
    border: 0;
    background: transparent;
    box-shadow: none;
  }

  .cf-tabs-trigger::before {
    content: '';
    position: absolute;
    z-index: -1;
    inset-inline: 0;
    top: calc((var(--cf-touch) - var(--cf-choice-selected)) / 2);
    height: var(--cf-choice-selected);
    border: 1px solid transparent;
    border-radius: inherit;
    pointer-events: none;
  }

  .cf-tabs-trigger[data-state='active']::before {
    border-color: var(--primary-accent);
    background: var(--primary-soft);
  }

  [data-theme='dark'] .cf-tabs-trigger[data-state='active']::before {
    border-color: var(--input);
    background: color-mix(in oklab, var(--input) 30%, transparent);
  }

  .cf-tabs-trigger:focus-visible {
    outline: 2px solid var(--ring);
    outline-offset: -2px;
  }
}
</style>
