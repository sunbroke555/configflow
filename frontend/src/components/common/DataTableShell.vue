<template>
  <SectionCard :padded="false">
    <!-- 宽表在自身容器内滚动，页面不产生横向滚动 -->
    <div class="overflow-x-auto focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring" tabindex="0" role="region" aria-label="数据表格">
      <Table>
        <slot />
      </Table>
    </div>
    <template v-if="footer">
      <Separator />
      <p class="m-0 bg-muted px-5 py-3 text-xs text-muted-foreground max-md:px-4">{{ footer }}</p>
    </template>
  </SectionCard>
</template>

<script setup lang="ts">
import { Separator } from '@/components/ui/separator'
import { Table } from '@/components/ui/table'
import SectionCard from './SectionCard.vue'

defineProps<{ footer?: string }>()
</script>
