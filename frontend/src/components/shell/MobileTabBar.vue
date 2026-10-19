<template>
  <nav
    class="glass-strong fixed inset-x-0 bottom-0 z-30 hidden h-[calc(var(--cf-tabbar-h)+env(safe-area-inset-bottom))] grid-cols-5 gap-1 border-t border-border-strong px-2 pt-1 pb-[calc(env(safe-area-inset-bottom)+4px)] max-[900px]:grid"
    aria-label="主导航"
  >
    <component
      :is="tab.path ? 'router-link' : 'button'"
      v-for="tab in TABS"
      :key="tab.label"
      :to="tab.path"
      :type="tab.path ? undefined : 'button'"
      :class="cn(
        'relative flex min-h-11 cursor-pointer flex-col items-center justify-center gap-0.5 rounded-lg border-0 bg-transparent px-0 text-[10.5px] font-semibold no-underline transition-colors duration-200 focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none',
        isActive(tab) ? 'text-primary-accent' : 'text-muted-foreground'
      )"
      :aria-current="isActive(tab) ? 'page' : undefined"
      @click="!tab.path && emit('more')"
    >
      <Motion
        v-if="isActive(tab)"
        layout-id="tab-active"
        class="absolute inset-0 rounded-lg bg-primary-soft"
        :transition="SPRING"
        aria-hidden="true"
      />
      <component :is="tab.icon" class="relative size-5" :stroke-width="2" aria-hidden="true" />
      <span class="relative">{{ tab.label }}</span>
    </component>
  </nav>
</template>

<script setup lang="ts">
import type { Component } from 'vue'
import { Motion } from 'motion-v'
import { LayoutDashboard, Link2, MoreHorizontal, Network, SlidersHorizontal } from '@lucide/vue'
import { scopeOfPath } from '@/navigation'
import { SPRING } from '@/lib/motion'
import { cn } from '@/lib/utils'

/* 配置入口覆盖当前配置的所有页面，默认仍直达配置生成；其它入口保持直达。 */
interface Tab {
  label: string
  icon: Component
  path?: string
}

const TABS: Tab[] = [
  { label: '总览', icon: LayoutDashboard, path: '/dashboard' },
  { label: '订阅', icon: Link2, path: '/subscriptions' },
  { label: '节点', icon: Network, path: '/nodes' },
  { label: '配置', icon: SlidersHorizontal, path: '/generate' },
  { label: '更多', icon: MoreHorizontal }
]

const props = defineProps<{ activePath: string }>()
const emit = defineEmits<{ (e: 'more'): void }>()

const isActive = (tab: Tab) => !!tab.path && (props.activePath === tab.path
  || tab.path === '/dashboard' && scopeOfPath(props.activePath) === 'overview'
  || tab.path === '/generate' && scopeOfPath(props.activePath) === 'profile')
</script>
