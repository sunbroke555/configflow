<script setup lang="ts">
import { computed } from 'vue'
import { ChevronRight, Plus } from '@lucide/vue'
import FieldHelp from '@/components/common/FieldHelp.vue'
import type { OxidnsNavigationItem } from './OxidnsConfigNavigation.vue'

const props = withDefaults(defineProps<{
  item: OxidnsNavigationItem
  selectedId: string
  ancestorIds: Set<string>
  expanded: Set<string>
  searchActive: boolean
  disabled?: boolean
  depth?: number
}>(), { depth: 0 })
const emit = defineEmits<{ select: [id: string]; toggle: [id: string] }>()
const open = computed(() => props.searchActive || props.expanded.has(props.item.id))
const blocked = computed(() => props.disabled || props.item.disabled)
const accessibleLabel = computed(() => props.item.code ? `${props.item.label}（${props.item.code}）` : props.item.label)
const shortCode = computed(() => props.item.kind === 'section' ? props.item.code || '' : props.item.code?.split('.').pop() || '')

function select() {
  if (!blocked.value) emit('select', props.item.id)
}
function toggle() {
  if (!blocked.value && !props.searchActive) emit('toggle', props.item.id)
}
</script>

<template>
  <li class="min-w-0">
    <div
      class="flex min-w-0 items-stretch rounded-md"
      :class="selectedId === item.id ? 'bg-primary-soft text-primary-accent' : ancestorIds.has(item.id) ? 'text-foreground' : 'text-muted-foreground'"
      :data-contains-selection="ancestorIds.has(item.id) || undefined"
    >
      <button
        v-if="item.children?.length"
        type="button"
        :aria-label="`${open ? '收起' : '展开'} ${accessibleLabel}`"
        :aria-expanded="open"
        :disabled="blocked || searchActive"
        class="flex min-h-11 w-11 shrink-0 items-center justify-center rounded-md outline-none hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-primary-accent disabled:pointer-events-none md:min-h-9 md:w-8 [@media(pointer:coarse)]:min-h-11 [@media(pointer:coarse)]:w-11"
        :class="blocked ? 'opacity-50' : ''"
        @click="toggle"
      >
        <ChevronRight class="size-3.5" :class="open ? 'rotate-90' : ''" aria-hidden="true" />
      </button>
      <span v-else-if="!item.available" class="w-11 shrink-0 md:w-8 [@media(pointer:coarse)]:w-11" aria-hidden="true" />
      <button
        type="button"
        :aria-label="`${item.available ? '添加' : '编辑'} ${accessibleLabel}`"
        :aria-current="selectedId === item.id ? 'true' : undefined"
        :title="accessibleLabel"
        :disabled="blocked"
        class="flex min-h-11 min-w-0 flex-1 items-center rounded-md py-1.5 pr-2 text-left text-xs leading-snug outline-none hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-primary-accent disabled:pointer-events-none disabled:opacity-50 md:min-h-9 [@media(pointer:coarse)]:min-h-11"
        :class="item.kind === 'section' || selectedId === item.id || ancestorIds.has(item.id) ? 'font-medium' : ''"
        @click="select"
      >
        <span v-if="item.available && !item.children?.length" class="flex w-11 shrink-0 items-center justify-center md:w-8 [@media(pointer:coarse)]:w-11" aria-hidden="true">
          <Plus class="size-3.5" />
        </span>
        <span class="block min-w-0 flex-1">
          <span class="block [overflow-wrap:anywhere]">{{ item.label }}</span>
          <span v-if="item.available || (shortCode && shortCode !== item.label)" class="mt-0.5 flex min-w-0 flex-wrap items-center gap-x-2 text-[11px] font-normal text-muted-foreground">
            <code v-if="shortCode && shortCode !== item.label" class="min-w-0 truncate">{{ shortCode }}</code>
            <span v-if="item.available" class="shrink-0">未配置</span>
          </span>
        </span>
      </button>
      <span v-if="item.help" class="flex shrink-0 items-center pr-0.5">
        <FieldHelp :label="item.label" :text="item.help" />
      </span>
    </div>
    <ul v-if="item.children?.length && open" class="min-w-0 space-y-0.5" :class="depth < 4 ? 'ml-4 border-l border-border pl-1' : ''">
      <OxidnsConfigNavigationItem
        v-for="child in item.children"
        :key="child.id"
        :item="child"
        :selected-id="selectedId"
        :ancestor-ids="ancestorIds"
        :expanded="expanded"
        :search-active="searchActive"
        :disabled="blocked"
        :depth="depth + 1"
        @select="emit('select', $event)"
        @toggle="emit('toggle', $event)"
      />
    </ul>
  </li>
</template>
