<template>
  <div class="min-w-0">
    <PageHeader title="OxiDNS 面板">
      <template #actions>
        <Button variant="outline" class="min-h-11" :disabled="loading" @click="openPreferredDevice">
          <RefreshCw class="size-4" :class="{ 'animate-spin': loading }" />刷新
        </Button>
      </template>
    </PageHeader>
    <SectionCard v-if="error">
      <EmptyState :icon="TriangleAlert" title="设备列表读取失败" :description="error">
        <Button variant="outline" class="min-h-11" :disabled="loading" @click="openPreferredDevice">重试</Button>
      </EmptyState>
    </SectionCard>
    <SectionCard v-else-if="devices && !devices.length">
      <EmptyState :icon="Server" title="暂无 OxiDNS Agent" description="接入 OxiDNS Agent 后，即可在这里查看查询记录、缓存命中率和运行统计。">
        <Button class="min-h-11" @click="router.push('/agents')">前往 Agent 接入</Button>
      </EmptyState>
    </SectionCard>
    <SectionCard v-else :padded="false"><LoadingRows :rows="3" /></SectionCard>
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { RefreshCw, Server, TriangleAlert } from '@lucide/vue'
import PageHeader from '@/components/common/PageHeader.vue'
import SectionCard from '@/components/common/SectionCard.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import LoadingRows from '@/components/common/LoadingRows.vue'
import { Button } from '@/components/ui/button'
import { oxidnsMonitorPath, preferredOxiDNSDevice, rememberOxiDNSDevice, useOxiDNSDevices } from '@/composables/useOxiDNSDevices'

const router = useRouter()
const { devices, loading, error, load } = useOxiDNSDevices()
let disposed = false
async function openPreferredDevice() {
  const available = await load()
  if (disposed || !available) return
  const selected = preferredOxiDNSDevice(available)
  if (!selected) return
  try {
    rememberOxiDNSDevice(selected.id)
    await router.replace(oxidnsMonitorPath(selected.id))
  } catch (failure: any) {
    if (!disposed) error.value = failure?.message || '暂时无法打开 OxiDNS 面板，请重试。'
  }
}
onMounted(openPreferredDevice)
onUnmounted(() => { disposed = true })
</script>
