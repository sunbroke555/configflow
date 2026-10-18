<template>
  <Dialog :open="open" @update:open="value => emit('update:open', value)">
    <DialogContent class="max-h-[90dvh] max-w-[520px] overflow-y-auto max-md:[&_[data-slot=dialog-close]]:min-h-11 max-md:[&_[data-slot=dialog-close]]:min-w-11">
      <DialogHeader>
        <DialogTitle>代理设置</DialogTitle>
        <DialogDescription>只影响当前浏览器里的显示与切换行为，不会改动 Mihomo 配置。</DialogDescription>
      </DialogHeader>
      <div class="divide-y divide-border">
        <div class="flex min-h-14 items-center justify-between gap-4">
          <span class="text-[13.5px]">排序方式</span>
          <Select :model-value="settings.sort" @update:model-value="update('sort', $event as MemberSort)">
            <SelectTrigger class="min-h-11 w-36" aria-label="节点排序方式"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="config">按配置排序</SelectItem>
              <SelectItem value="delay">按延迟排序</SelectItem>
              <SelectItem value="name">按名称排序</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <label v-for="item in SWITCHES" :key="item.key" class="flex min-h-14 cursor-pointer items-center justify-between gap-4">
          <span class="min-w-0">
            <span class="block text-[13.5px]">{{ item.label }}</span>
            <span class="block text-xs text-muted-foreground">{{ item.hint }}</span>
          </span>
          <Switch :model-value="settings[item.key]" :aria-label="item.label" @update:model-value="update(item.key, $event)" />
        </label>
        <div class="flex min-h-14 items-center justify-between gap-4">
          <span class="text-[13.5px]">节点卡片最小宽度</span>
          <span class="flex items-center gap-2">
            <Input type="number" class="min-h-11 w-24 text-right" :min="CARD_MIN_WIDTH_RANGE[0]" :max="CARD_MIN_WIDTH_RANGE[1]"
              :model-value="settings.cardMinWidth" aria-label="节点卡片最小宽度（像素）"
              @change="(event: Event) => update('cardMinWidth', clampCardWidth((event.target as HTMLInputElement).value))" />
            <Button variant="outline" class="min-h-11" :disabled="settings.cardMinWidth === DEFAULT_CARD_MIN_WIDTH"
              @click="update('cardMinWidth', DEFAULT_CARD_MIN_WIDTH)">重置</Button>
          </span>
        </div>
      </div>
    </DialogContent>
  </Dialog>
</template>

<script setup lang="ts">
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import {
  CARD_MIN_WIDTH_RANGE, DEFAULT_CARD_MIN_WIDTH, clampCardWidth, type MemberSort, type ProxyPanelSettings
} from '@/types/mihomo'

type SwitchKey = 'groupByProvider' | 'hideUnavailable' | 'showHiddenGroups' | 'closeConnectionsOnSwitch' | 'showFinalNode' | 'disableTextSelection'
const SWITCHES: Array<{ key: SwitchKey; label: string; hint: string }> = [
  { key: 'groupByProvider', label: '节点根据提供商分组', hint: '展开的策略组里按代理集合分段列出节点' },
  { key: 'hideUnavailable', label: '隐藏不可用节点', hint: '测速超时或判定离线的节点不显示，当前选中的除外' },
  { key: 'showHiddenGroups', label: '管理隐藏代理组', hint: '显示配置里标记 hidden 的策略组' },
  { key: 'closeConnectionsOnSwitch', label: '切换节点时自动断开连接', hint: '断开经过该策略组的现有连接，让新连接走新节点' },
  { key: 'showFinalNode', label: '显示最终出口节点', hint: '策略组嵌套时，在组标题上显示最终出站的节点' },
  { key: 'disableTextSelection', label: '禁用代理页文字选中', hint: '避免点选节点时误选中文字' }
]

const props = defineProps<{ open: boolean; settings: ProxyPanelSettings }>()
const emit = defineEmits<{ 'update:open': [value: boolean]; 'update:settings': [value: ProxyPanelSettings] }>()

function update<K extends keyof ProxyPanelSettings>(key: K, value: ProxyPanelSettings[K] | unknown) {
  emit('update:settings', { ...props.settings, [key]: value })
}
</script>
