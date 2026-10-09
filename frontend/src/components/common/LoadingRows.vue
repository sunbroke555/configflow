<template>
  <div class="space-y-2 p-4" role="status" aria-label="正在加载">
    <Skeleton v-for="i in rows" :key="i" class="h-11 w-full rounded-lg motion-reduce:animate-none" />
  </div>
</template>

<script setup lang="ts">
import { Skeleton } from '@/components/ui/skeleton'

withDefaults(defineProps<{ rows?: number }>(), { rows: 5 })
</script>
