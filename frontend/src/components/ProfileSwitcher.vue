<template>
  <div class="flex min-w-0 items-center gap-1">
    <span class="shrink-0 text-xs text-muted-foreground max-[1100px]:hidden">当前配置</span>
    <Select v-model="selectedProfileId" :disabled="loading || scopedRequests > 0" @update:model-value="handleChange">
      <SelectTrigger
        size="sm"
        class="w-[144px] min-w-0 gap-1.5 border-border/80 bg-background px-2.5 text-[13px] font-medium shadow-none transition-colors hover:border-border-strong hover:text-foreground max-[1100px]:w-[132px] max-[700px]:data-[size=sm]:h-11 max-[700px]:flex-1 max-[700px]:px-3"
        aria-label="当前配置空间"
      >
        <Boxes class="size-4 shrink-0 text-primary-accent" />
        <SelectValue :title="currentLabel" :class="cn('min-w-0 flex-1 truncate text-left', !currentLabel && 'text-muted-foreground')">
          {{ currentLabel || '选择配置空间' }}
        </SelectValue>
      </SelectTrigger>
      <SelectContent align="end" class="min-w-[220px] max-w-[calc(100vw-24px)]">
        <SelectItem v-for="profile in profiles" :key="profile.id" :value="profile.id">
          <span class="flex min-w-0 flex-col whitespace-normal break-all leading-snug">
            <span class="text-[13px] font-medium">{{ profile.name }}</span>
            <span class="font-mono text-[11px] text-muted-foreground">{{ profile.id }}</span>
          </span>
        </SelectItem>
      </SelectContent>
    </Select>

    <Button
      variant="ghost"
      size="icon-sm"
      class="size-8 shrink-0 text-muted-foreground max-[700px]:size-11"
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

watch(activeProfileId, value => {
  selectedProfileId.value = value
})

const handleChange = async (profileId: unknown) => {
  const id = String(profileId)
  if (!id || id === activeProfileId.value) return
  await switchProfile(id)
  selectedProfileId.value = activeProfileId.value
}
</script>
