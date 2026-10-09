<template>
  <!-- 当前分组内的页面切换：横向分段控件，就地切换不弹层。
       仅移动端出现；桌面由左侧 rail 承担分组导航。 -->
  <nav
    v-if="items.length > 1"
    class="-mx-4 mb-5 hidden gap-2 overflow-x-auto px-4 py-1 [scroll-snap-type:x_proximity] max-[900px]:flex"
    aria-label="分组内页面"
  >
    <router-link
      v-for="item in items"
      :key="item.path"
      :to="item.path"
      :class="cn(
        'relative inline-flex min-h-11 shrink-0 items-center gap-2 rounded-lg border px-3.5 text-[13px] font-medium whitespace-nowrap no-underline [scroll-snap-align:start] transition-colors duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring',
        activePath === item.path
          ? 'border-primary-accent/30 bg-primary-soft text-primary-accent'
          : 'border-border bg-card text-muted-foreground hover:bg-muted hover:text-foreground'
      )"
      :aria-current="activePath === item.path ? 'page' : undefined"
    >
      <component :is="iconOf(item.icon)" class="size-3.5" :stroke-width="2" aria-hidden="true" />
      <span>{{ item.label }}</span>
    </router-link>
  </nav>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { NAV_GROUPS, scopeOfPath, type NavItem } from '@/navigation'
import { iconOf } from '@/lib/icons'
import { cn } from '@/lib/utils'

const props = defineProps<{
  activePath: string
  subscriptionAggregationEnabled: boolean
}>()

const items = computed<NavItem[]>(() => {
  const scope = scopeOfPath(props.activePath)
  const group = NAV_GROUPS.find(g => g.scope === scope)
  if (!group) return []
  return group.items.filter(
    item => item.flag !== 'subscriptionAggregation' || props.subscriptionAggregationEnabled
  )
})
</script>
