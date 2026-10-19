<template>
  <div :class="cn('flex min-w-0 items-center gap-1', rail ? 'w-full gap-1.5' : 'max-sm:gap-0')">
    <Select v-model="selectedProfileId" :disabled="loading || scopedRequests > 0" @update:model-value="handleChange">
      <SelectTrigger
        :class="cn(
          'gap-2 border-border/60 bg-background/40 text-[13px] font-medium transition-colors hover:border-border-strong',
          rail
            ? 'h-auto! min-w-0 flex-1 rounded-xl bg-card/70 px-2.5 py-2'
            : 'h-8 w-[190px] max-sm:w-full max-sm:flex-1 max-sm:gap-1.5 max-sm:px-2'
        )"
        aria-label="当前配置空间"
        :title="currentLabel"
      >
        <span
          v-if="rail"
          class="font-display grid size-7 shrink-0 place-items-center rounded-lg bg-primary-soft text-[14px] text-primary-accent"
          aria-hidden="true"
        >
          {{ initial }}
        </span>
        <Boxes v-else class="size-4 shrink-0 text-primary-accent max-[359px]:hidden" />
        <span v-if="rail" class="flex min-w-0 flex-1 flex-col text-left leading-tight">
          <SelectValue :class="cn('truncate text-[13px] font-semibold', !currentLabel && 'text-muted-foreground')">
            {{ currentLabel || '选择配置空间' }}
          </SelectValue>
          <span class="text-[11px] font-normal text-muted-foreground">配置空间</span>
        </span>
        <SelectValue v-else :class="cn('truncate', !currentLabel && 'text-muted-foreground')">
          {{ currentLabel || '选择配置空间' }}
        </SelectValue>
      </SelectTrigger>
      <SelectContent :align="rail ? 'start' : 'end'" class="glass-strong min-w-[220px]">
        <SelectItem v-for="profile in profiles" :key="profile.id" :value="profile.id">
          <span
            class="font-display grid size-6 shrink-0 place-items-center rounded-md bg-primary-soft text-[12px] text-primary-accent"
            aria-hidden="true"
          >
            {{ initialOf(profile.name || profile.id) }}
          </span>
          <span class="flex flex-col leading-snug">
            <span class="text-[13px] font-medium">{{ profile.name }}</span>
            <span class="font-mono text-[11px] text-muted-foreground">{{ profile.id }}</span>
          </span>
        </SelectItem>
      </SelectContent>
    </Select>

    <Button
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

const props = defineProps<{ variant?: 'bar' | 'rail' }>()
const rail = computed(() => props.variant === 'rail')

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
