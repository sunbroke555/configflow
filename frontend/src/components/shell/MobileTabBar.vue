<template>
  <nav
    class="fixed inset-x-0 bottom-0 z-40 hidden grid-cols-4 border-t border-border bg-card pb-[env(safe-area-inset-bottom)] max-[900px]:grid"
    aria-label="主导航"
  >
    <button
      v-for="group in groups"
      :key="group.scope"
      type="button"
      :class="cn(
        'relative flex min-h-(--cf-tabbar-h) cursor-pointer flex-col items-center justify-center gap-1 border-0 px-1 py-2 text-[11px] font-medium transition-colors duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring',
        group.scope === activeScope ? 'bg-primary-soft text-primary-accent' : 'bg-transparent text-muted-foreground hover:bg-muted hover:text-foreground'
      )"
      :aria-current="group.scope === activeScope ? 'page' : undefined"
      @click="$emit('select', group)"
    >
      <span
        v-if="group.scope === activeScope"
        class="absolute top-0 h-0.5 w-9 rounded-full bg-primary-accent"
        aria-hidden="true"
      />
      <component :is="iconOf(group.tabIcon)" class="size-5" :stroke-width="2" aria-hidden="true" />
      <span>{{ group.tabLabel }}</span>
    </button>
  </nav>
</template>

<script setup lang="ts">
import { NAV_GROUPS, type NavGroup, type NavScope } from '@/navigation'
import { iconOf } from '@/lib/icons'
import { cn } from '@/lib/utils'

defineProps<{ activeScope?: NavScope }>()
defineEmits<{ (e: 'select', group: NavGroup): void }>()

const groups = NAV_GROUPS
</script>
