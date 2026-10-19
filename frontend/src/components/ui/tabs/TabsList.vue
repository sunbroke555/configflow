<script setup lang="ts">
import type { TabsListProps } from "reka-ui"
import type { HTMLAttributes } from "vue"
import { reactiveOmit } from "@vueuse/core"
import { TabsList } from "reka-ui"
import { cn } from "@/lib/utils"

const props = defineProps<TabsListProps & { class?: HTMLAttributes["class"]; variant?: "default" | "strip" }>()

const delegatedProps = reactiveOmit(props, "class", "variant")
</script>

<template>
  <TabsList
    data-slot="tabs-list"
    :data-variant="variant || 'default'"
    v-bind="delegatedProps"
    :class="cn(
      'cf-tabs-list bg-muted text-muted-foreground inline-flex h-9 w-fit items-center justify-center rounded-lg p-0.75',
      props.class,
    )"
  >
    <slot />
  </TabsList>
</template>

<style scoped>
@media (width < 640px) {
  .cf-tabs-list {
    height: auto;
    min-height: var(--cf-touch);
    padding-block: 0;
  }

  .cf-tabs-list[data-variant='default'] {
    position: relative;
    isolation: isolate;
    background: transparent;
  }

  .cf-tabs-list[data-variant='default']::before {
    content: '';
    position: absolute;
    z-index: -1;
    inset-inline: 0;
    inset-block: calc((var(--cf-touch) - var(--cf-choice-track)) / 2);
    border-radius: inherit;
    background: var(--muted);
    pointer-events: none;
  }

  .cf-tabs-list[data-variant='strip'] :deep([data-slot='tabs-trigger']) {
    flex: 0 0 auto;
  }
}
</style>
