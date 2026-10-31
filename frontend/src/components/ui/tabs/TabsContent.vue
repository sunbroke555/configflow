<script setup lang="ts">
import type { TabsContentProps } from "reka-ui"
import type { HTMLAttributes } from "vue"
import { computed, inject, provide, ref, watchEffect } from "vue"
import { reactiveOmit } from "@vueuse/core"
import { injectTabsRootContext, TabsContent } from "reka-ui"
import { cn } from "@/lib/utils"
import { TABS_CONTENT_ACTIVE, TABS_KEEP_VISITED } from "./keepVisited"

const props = defineProps<TabsContentProps & { class?: HTMLAttributes["class"] }>()

const delegatedProps = reactiveOmit(props, "class")

// keep-visited 时面板常驻（未选中用 hidden 隐藏），内容在第一次选中后挂载并一直保留。
const keepVisited = inject(TABS_KEEP_VISITED, ref(false))
const root = injectTabsRootContext()
const visited = ref(false)
watchEffect(() => { if (root.modelValue.value === props.value) visited.value = true })
// 内容用 useTabActive 读取是否为当前标签，不必由父组件经 props 传入
const outer = inject(TABS_CONTENT_ACTIVE, null)
provide(TABS_CONTENT_ACTIVE, computed(() => (outer?.value ?? true) && root.modelValue.value === props.value))
</script>

<template>
  <TabsContent
    data-slot="tabs-content"
    :class="cn('flex-1 outline-none', keepVisited && 'data-[state=inactive]:hidden', props.class)"
    v-bind="delegatedProps"
    :force-mount="keepVisited || forceMount"
  >
    <slot v-if="!keepVisited || visited" />
  </TabsContent>
</template>
