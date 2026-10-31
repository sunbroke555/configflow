<script setup lang="ts">
import type { TabsRootEmits, TabsRootProps } from "reka-ui"
import type { HTMLAttributes } from "vue"
import { provide, toRef } from "vue"
import { reactiveOmit } from "@vueuse/core"
import { TabsRoot, useForwardPropsEmits } from "reka-ui"
import { cn } from "@/lib/utils"
import { TABS_KEEP_VISITED } from "./keepVisited"

const props = defineProps<TabsRootProps & { class?: HTMLAttributes["class"]; keepVisited?: boolean }>()
const emits = defineEmits<TabsRootEmits>()

const delegatedProps = reactiveOmit(props, "class", "keepVisited")
const forwarded = useForwardPropsEmits(delegatedProps, emits)
// keep-visited：每个标签第一次选中时才挂载内容，之后切走只隐藏不卸载，切回不再重建、重新请求。
provide(TABS_KEEP_VISITED, toRef(props, "keepVisited"))
</script>

<template>
  <TabsRoot
    v-slot="slotProps"
    data-slot="tabs"
    v-bind="forwarded"
    :class="cn('flex flex-col gap-2', props.class)"
  >
    <slot v-bind="slotProps" />
  </TabsRoot>
</template>
