<script setup lang="ts">
import type { DropdownMenuRootEmits, DropdownMenuRootProps } from "reka-ui"
import { DropdownMenuRoot, useForwardPropsEmits } from "reka-ui"

// 默认非模态：reka 的模态菜单会改 body 的 pointer-events 和 overflow，
// 开、关各触发一次整页样式重算，在规则、节点这类大页面上每次 0.4–0.6 s。
// 非模态菜单点击外部仍会关闭，Esc 和键盘导航不变；确需模态时显式传 :modal="true"。
const props = withDefaults(defineProps<DropdownMenuRootProps>(), {
  modal: false,
})
const emits = defineEmits<DropdownMenuRootEmits>()

const forwarded = useForwardPropsEmits(props, emits)
</script>

<template>
  <DropdownMenuRoot
    v-slot="slotProps"
    data-slot="dropdown-menu"
    v-bind="forwarded"
  >
    <slot v-bind="slotProps" />
  </DropdownMenuRoot>
</template>
