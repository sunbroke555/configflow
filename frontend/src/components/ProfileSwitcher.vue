<template>
  <div :class="cn('flex min-w-0 items-center gap-1', rail ? 'w-full' : 'max-sm:gap-0', compact && 'justify-center')">
    <Select v-model="selectedProfileId" :disabled="loading || scopedRequests > 0" @update:model-value="handleChange">
      <SelectTrigger
        :class="cn(
          'gap-2 border-border/60 bg-background/40 text-[13px] font-medium transition-colors hover:border-border-strong',
          rail
            ? compact
              ? 'size-11! shrink-0 justify-center gap-0 rounded-lg border-transparent bg-transparent p-0 shadow-none hover:bg-accent [&>svg]:hidden'
              : 'h-11! min-w-0 flex-1 rounded-lg bg-card/50 px-2.5 py-0 shadow-none'
            : 'h-8 w-[190px] max-sm:w-full max-sm:flex-1 max-sm:gap-1.5 max-sm:px-2'
        )"
        aria-label="当前配置空间"
        :aria-description="currentLabel || '选择配置空间'"
        :title="currentLabel || '选择配置空间'"
      >
        <span
          v-if="rail"
          :class="cn('font-display grid shrink-0 place-items-center rounded-md bg-primary-soft text-[13px] text-primary-accent', compact ? 'size-7' : 'size-6')"
          aria-hidden="true"
        >
          {{ initial }}
        </span>
        <Boxes v-else class="size-4 shrink-0 text-primary-accent max-[359px]:hidden" />
        <SelectValue :class="cn('min-w-0 flex-1 truncate text-left', rail && 'text-[13px] font-medium', compact && 'cf-sr', !currentLabel && 'text-muted-foreground')">
          <span class="min-w-0 truncate">{{ currentLabel || '选择配置空间' }}</span>
        </SelectValue>
      </SelectTrigger>
      <SelectContent :align="rail ? 'start' : 'end'" class="glass-strong w-[min(300px,calc(100vw-2rem))] min-w-0 max-w-[calc(100vw-2rem)] [&_[data-reka-select-viewport]]:min-w-0">
        <SelectItem
          v-for="profile in profiles"
          :key="profile.id"
          :value="profile.id"
          :text-value="profile.name || profile.id"
          class="[&>span:last-child]:min-w-0 [&>span:last-child]:flex-1"
        >
          <span
            class="font-display grid size-6 shrink-0 place-items-center rounded-md bg-primary-soft text-[12px] text-primary-accent"
            aria-hidden="true"
          >
            {{ initialOf(profile.name || profile.id) }}
          </span>
          <span class="flex min-w-0 flex-1 flex-col leading-snug">
            <span class="truncate text-[13px] font-medium" :title="profile.name || profile.id">{{ profile.name || profile.id }}</span>
            <span class="truncate font-mono text-[11px] text-muted-foreground" :title="profile.id">{{ profile.id }}</span>
          </span>
        </SelectItem>
      </SelectContent>
    </Select>

    <Button
      v-if="!rail"
      variant="ghost"
      size="icon-sm"
      class="shrink-0 text-primary-accent"
      title="管理配置空间"
      aria-label="管理配置空间"
      @click="router.push('/profiles')"
    >
      <Settings class="size-4" />
    </Button>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Boxes, Settings } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue
} from '@/components/ui/select'
import { useProfileStore } from '@/stores/profile'

const props = defineProps<{ variant?: 'bar' | 'rail'; collapsed?: boolean }>()
const rail = computed(() => props.variant === 'rail')
const compact = computed(() => rail.value && props.collapsed)

const router = useRouter()
const profileStore = useProfileStore()
const { profiles, loading, scopedRequests, activeProfileId, switchProfile } = profileStore
const selectedProfileId = ref(activeProfileId.value)

/* 配置空间列表尚未加载（或接口不可用）时，回退显示当前生效的 id，
 * 避免顶栏错误地呈现为「未选择」。
 */
const currentLabel = computed(
  () =>
    profiles.value.find(p => p.id === selectedProfileId.value)?.name ||
    selectedProfileId.value ||
    ''
)

/** 配置空间名首字，作为头像 */
const initialOf = (name: string) => Array.from(String(name || '').trim())[0]?.toUpperCase() || '·'
const initial = computed(() => initialOf(currentLabel.value))

watch(activeProfileId, value => {
  selectedProfileId.value = value
})

const handleChange = async (profileId: unknown) => {
  const id = String(profileId)
  if (!id || id === activeProfileId.value) return
  await switchProfile(id)
  // 切换被拒（例如仍有进行中的请求）时回到当前生效的配置空间
  selectedProfileId.value = activeProfileId.value
}
</script>
