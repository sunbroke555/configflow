<template>
  <span v-if="labels.length" class="ml-1.5 inline-flex flex-wrap gap-1 align-middle">
    <span class="sr-only">来源：</span>
    <span v-for="label in labels" :key="label" class="chip h-5 px-1.5 text-[11px]">{{ label }}</span>
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { ClientSighting } from '@/api'

/** 路由器在哪些表里看到了这个地址：ARP、DHCP 租约、iKuai 在线终端 */
const props = defineProps<{ sources?: ClientSighting[] }>()
const NAMES: Record<ClientSighting, string> = { arp: 'ARP', lease: '租约', online: '在线终端' }
const labels = computed(() => (props.sources || []).map(source => NAMES[source] || source))
</script>
