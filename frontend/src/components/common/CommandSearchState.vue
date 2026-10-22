<script setup lang="ts">
/* 读取所在 Command 的搜索词，交给插槽使用；搜索词由 Command 内部维护，选中项后也会被清空 */
import { useCommand } from '@/components/ui/command'

const { filterState } = useCommand()
</script>

<template>
  <slot :search="filterState.search" />
</template>
