<script setup lang="ts">
import type { LinuxGatewayStatus } from '@/api/linuxGateway'
defineProps<{ detection: NonNullable<LinuxGatewayStatus['detection']> }>()
</script>

<template>
  <details class="text-xs text-muted-foreground"><summary class="min-h-11 cursor-pointer content-center font-medium">查看实际网卡与默认路由</summary><dl class="m-0 grid grid-cols-1 gap-3 pb-3 sm:grid-cols-2"><div><dt class="mb-1 font-medium text-foreground">实际网卡与地址</dt><dd v-for="item in detection.interfaces" :key="item.name" class="m-0 break-all font-mono">{{ item.name }}：{{ item.addresses?.join('、') || '无 IPv4 地址' }}</dd></div><div><dt class="mb-1 font-medium text-foreground">当前默认路由</dt><dd v-for="item in detection.default_routes" :key="item.interface + item.gateway" class="m-0 break-all font-mono">{{ item.interface }} → {{ item.gateway }}</dd><dd v-if="!detection.default_routes?.length" class="m-0">未发现默认路由</dd></div></dl></details>
</template>
