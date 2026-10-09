<template>
  <nav
    class="sticky top-[calc(var(--cf-topbar-h)+env(safe-area-inset-top))] z-10 flex h-[calc(100dvh-var(--cf-topbar-h)-env(safe-area-inset-top))] w-(--cf-rail-w) shrink-0 self-start flex-col gap-1 overflow-y-auto border-r border-border bg-card px-3 pt-5 pb-6"
    aria-label="主导航"
  >
    <template v-for="group in groups" :key="group.scope">
      <div
        v-if="group.title"
        class="mt-6 mb-2 px-2.5 text-xs font-medium text-muted-foreground first:mt-1"
      >
        <span class="block">{{ group.title }}</span>
        <span v-if="group.scope === 'profile'" class="mt-1 block break-words text-[13px] font-semibold leading-5 text-foreground">{{ profileName }}</span>
      </div>

      <router-link
        v-for="item in visibleItems(group)"
        :key="item.path"
        :to="item.path"
        :class="cn(
          'group relative flex min-h-11 items-center gap-2.5 rounded-lg px-3 text-[13px] font-medium text-muted-foreground no-underline transition-colors duration-150 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring focus-visible:outline-none',
          activePath === item.path
            ? 'bg-primary-soft text-primary-accent'
            : 'hover:bg-muted hover:text-foreground'
        )"
        :aria-current="activePath === item.path ? 'page' : undefined"
      >
        <span
          v-if="activePath === item.path"
          class="absolute top-1/2 left-0 h-5 w-0.5 -translate-y-1/2 rounded-r-full bg-primary-accent"
          aria-hidden="true"
        />
        <component
          :is="iconOf(item.icon)"
          :class="cn(
            'relative size-4 shrink-0 transition-colors',
            activePath === item.path ? 'text-primary-accent' : 'group-hover:text-foreground'
          )"
          :stroke-width="2"
          aria-hidden="true"
        />
        <span class="relative truncate">{{ item.label }}</span>
      </router-link>
    </template>

  </nav>
</template>

<script setup lang="ts">
import { NAV_GROUPS, type NavGroup, type NavItem } from '@/navigation'
import { iconOf } from '@/lib/icons'
import { cn } from '@/lib/utils'

const props = defineProps<{
  activePath: string
  profileName: string
  subscriptionAggregationEnabled: boolean
}>()

const groups = NAV_GROUPS

const visibleItems = (group: NavGroup): NavItem[] =>
  group.items.filter(
    item => item.flag !== 'subscriptionAggregation' || props.subscriptionAggregationEnabled
  )

</script>
