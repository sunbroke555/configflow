<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { deviceGroupsApi } from '@/api/deviceGroups'
import { activeProfileId } from '@/profileContext'
import type { DeviceGroup } from '@/types/deviceGroups'
const groups = ref<DeviceGroup[]>([])
const summary = computed(() => groups.value.filter(group => group.id !== 'default' && group.mode && group.mode !== 'follow').map(group => `${group.name}${group.keep_blocking ? '仅执行其中的拦截规则' : '不按本列表处理'}`).join('；'))
let controller: AbortController | undefined, epoch = 0
watch(activeProfileId, async profile => {
  controller?.abort(); controller = new AbortController(); const current = ++epoch; groups.value = []
  try { const { data } = await deviceGroupsApi.get(profile, controller.signal); if (current === epoch) groups.value = data.device_groups?.groups || [] }
  catch { /* The rules page remains usable when this optional summary is unavailable. */ }
}, { immediate: true })
onUnmounted(() => { epoch++; controller?.abort() })
</script>
<template><p v-if="summary" class="mb-3.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground" data-device-group-rule-notice><span class="break-words">{{ summary }}。</span><RouterLink to="/device-groups" class="inline-flex min-h-11 items-center text-primary-accent underline underline-offset-4">前往设备组</RouterLink></p></template>
