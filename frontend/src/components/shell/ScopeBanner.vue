<template>
  <div class="mb-5 flex items-start gap-2.5 border-b border-border pb-3 text-xs leading-5" role="note">
    <component :is="scopeIcon" class="mt-0.5 size-4 shrink-0 text-muted-foreground" :stroke-width="2" aria-hidden="true" />
    <div class="min-w-0 break-words">
      <b class="font-semibold text-foreground">{{ title }}</b>
      <span v-if="description" class="mt-0.5 block text-muted-foreground">{{ description }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { IdCard, Layers, Server } from '@lucide/vue'

const props = defineProps<{
  /**
   * resource = 所有配置共用的订阅/节点/聚合/规则库
   * profile  = 独立的策略、代理链、规则与生成参数
   * system   = 全局服务、备份、Agent 与配置空间管理
   */
  scope: 'resource' | 'profile' | 'system'
  profileName?: string
  description?: string
}>()

const scopeIcon = computed(() => ({ resource: Layers, profile: IdCard, system: Server })[props.scope])

const title = computed(() => {
  if (props.scope === 'system') return '系统设置 · 适用于所有配置'
  if (props.scope === 'resource') return '共享资源 · 所有配置均可使用'
  return `当前配置 · ${props.profileName || '未选择'}`
})
</script>
