<template>
  <div>
    <PageHeader
      title="配置空间"
      description="为家庭、办公等场景分别设置策略和规则，再生成配置。资源由所有配置空间共用，修改资源会影响使用它的所有配置。此处导入导出单个配置的设置和资源选择；备份全部数据请前往系统设置。"
    >
      <template #actions>
        <Button variant="outline" class="border-border/60 bg-background/40" @click="pickImportFile">
          <Upload class="size-4" />
          导入到当前
        </Button>
        <Button @click="openCreate">
          <Plus class="size-4" />
          新建配置空间
        </Button>
        <input ref="importInput" type="file" accept="application/json" hidden @change="importProfile" />
      </template>
    </PageHeader>

    <LoadingRows v-if="loading && !profiles.length" :rows="3" />

    <SectionCard v-else-if="!profiles.length" :padded="false">
      <EmptyState
        :icon="Boxes"
        title="还没有配置空间"
        description="先创建一个配置空间，为家庭或办公等场景选择资源、设置策略和规则。"
      >
        <Button @click="openCreate">
          <Plus class="size-4" />
          新建配置空间
        </Button>
      </EmptyState>
    </SectionCard>

    <div v-else class="grid grid-cols-[repeat(auto-fill,minmax(320px,1fr))] gap-4 max-md:grid-cols-1">
      <Motion
        v-for="(profile, index) in profiles"
        :key="profile.id"
        v-bind="listItem(index)"
        :class="[
          'group relative flex flex-col gap-4 overflow-hidden rounded-xl border bg-card p-5 transition-colors duration-200',
          profile.id === activeProfileId
            ? 'border-primary-accent'
            : 'border-border'
        ]"
      >
        <header class="flex items-start justify-between gap-3">
          <div class="flex min-w-0 flex-1 items-center gap-3">
            <span
              class="relative grid size-10 shrink-0 place-items-center rounded-xl border border-border/50 bg-background/50 text-primary-accent"
            >
              <span
                class="absolute inset-0 rounded-xl bg-primary-soft"
                aria-hidden="true"
              />
              <Boxes class="relative size-5" :stroke-width="2" aria-hidden="true" />
            </span>
            <div class="min-w-0">
              <p class="m-0 truncate text-[15px] font-semibold tracking-[-0.01em] text-foreground">
                {{ profile.name }}
              </p>
              <p class="mt-0.5 mb-0 truncate font-mono text-[11.5px] text-muted-foreground">
                {{ profile.id }}
              </p>
            </div>
          </div>

          <Badge v-if="profile.id === activeProfileId" variant="brand" class="shrink-0 gap-1">
            <CircleCheck class="size-3" aria-hidden="true" />
            使用中
          </Badge>
        </header>

        <p class="m-0 min-h-10 break-words text-[12.5px] leading-relaxed text-muted-foreground">
          {{ profile.description || '暂无说明' }}
        </p>

        <footer class="mt-auto flex flex-wrap items-center gap-2 border-0 border-t border-dashed border-border/50 pt-4">
          <Button
            v-if="profile.id !== activeProfileId"
            size="sm"
            :disabled="scopedRequests > 0"
            @click="switchProfile(profile.id)"
          >
            <CircleCheck class="size-3.5" />
            使用
          </Button>
          <span v-else class="text-[12.5px] font-medium text-muted-foreground">当前正在使用</span>

          <div class="ml-auto flex items-center gap-1">
            <Button variant="ghost" size="icon-sm" title="编辑" aria-label="编辑" @click="openEdit(profile)">
              <Pencil class="size-4" />
            </Button>
            <Button variant="ghost" size="icon-sm" title="创建副本" aria-label="创建副本" @click="clone(profile)">
              <Copy class="size-4" />
            </Button>
            <Button
              variant="ghost"
              size="icon-sm"
              title="导出"
              aria-label="导出"
              @click="exportProfile(profile.id)"
            >
              <Download class="size-4" />
            </Button>
            <Button
              v-if="profile.id !== 'default'"
              variant="ghost"
              size="icon-sm"
              class="text-destructive-accent hover:bg-destructive-soft"
              title="删除"
              aria-label="删除"
              @click="remove(profile)"
            >
              <Trash2 class="size-4" />
            </Button>
          </div>
        </footer>
      </Motion>
    </div>

    <Dialog v-model:open="dialogVisible">
      <DialogContent class="max-w-[460px]">
        <DialogHeader>
          <DialogTitle>{{ editingId ? '编辑配置空间' : '新建配置空间' }}</DialogTitle>
          <DialogDescription>
            为配置空间填写名称和说明。标识 ID 用于访问配置和命名生成文件，创建后不可修改。
          </DialogDescription>
        </DialogHeader>

        <form class="flex flex-col gap-4" @submit.prevent="submit">
          <div class="flex flex-col gap-1.5">
            <Label for="profile-id">标识 ID</Label>
            <Input
              id="profile-id"
              v-model="form.id"
              class="bg-background/50 font-mono"
              :disabled="Boolean(editingId)"
              maxlength="64"
              placeholder="例如 home、work"
            />
          </div>
          <div class="flex flex-col gap-1.5">
            <Label for="profile-name">名称</Label>
            <Input id="profile-name" v-model="form.name" class="bg-background/50" maxlength="120" />
          </div>
          <div class="flex flex-col gap-1.5">
            <Label for="profile-desc">说明</Label>
            <Textarea
              id="profile-desc"
              v-model="form.description"
              class="bg-background/50"
              :rows="3"
              maxlength="500"
            />
          </div>
        </form>

        <DialogFooter>
          <Button variant="outline" @click="dialogVisible = false">取消</Button>
          <Button :disabled="saving" @click="submit">
            <Loader2 v-if="saving" class="size-4 animate-spin" />
            保存
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { Motion } from 'motion-v'
import {
  Boxes,
  CircleCheck,
  Copy,
  Download,
  Loader2,
  Pencil,
  Plus,
  Trash2,
  Upload
} from '@lucide/vue'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import EmptyState from '@/components/common/EmptyState.vue'
import LoadingRows from '@/components/common/LoadingRows.vue'
import PageHeader from '@/components/common/PageHeader.vue'
import SectionCard from '@/components/common/SectionCard.vue'
import { profileApi } from '@/api'
import { confirmDanger, notify, prompt } from '@/lib/feedback'
import { listItem } from '@/lib/motion'
import { useProfileStore, type Profile } from '@/stores/profile'

const profileStore = useProfileStore()
const { profiles, loading, scopedRequests, activeProfileId, refreshProfiles, switchProfile } = profileStore
const dialogVisible = ref(false)
const saving = ref(false)
const editingId = ref('')
const importInput = ref<HTMLInputElement>()
const form = reactive({ id: '', name: '', description: '' })

const PROFILE_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/

const openCreate = () => {
  editingId.value = ''
  Object.assign(form, { id: '', name: '', description: '' })
  dialogVisible.value = true
}

const openEdit = (profile: Profile) => {
  editingId.value = profile.id
  Object.assign(form, {
    id: profile.id,
    name: profile.name,
    description: profile.description || ''
  })
  dialogVisible.value = true
}

const submit = async () => {
  if (!form.name.trim() || (!editingId.value && !form.id.trim())) {
    notify.warning('请填写标识 ID 和名称')
    return
  }
  saving.value = true
  try {
    if (editingId.value) {
      await profileApi.update(editingId.value, { name: form.name, description: form.description })
    } else {
      await profileApi.create({ id: form.id.trim(), name: form.name, description: form.description })
    }
    await refreshProfiles()
    dialogVisible.value = false
    notify.success('配置空间已保存')
  } catch (error: any) {
    notify.error(error.response?.data?.message || '配置空间保存失败')
  } finally {
    saving.value = false
  }
}


const clone = async (profile: Profile) => {
  const id = await prompt({
    title: `复制 ${profile.name}`,
    description: '填写新副本的标识 ID。副本可单独设置策略和规则，资源仍与其他配置空间共用。',
    defaultValue: `${profile.id}-copy`,
    confirmText: '创建副本',
    validate: value =>
      PROFILE_ID.test(value.trim()) ? '' : 'ID 须为 1–64 个字符，以字母或数字开头，仅含字母、数字、下划线和短横线'
  })
  if (id === null) return

  try {
    await profileApi.clone(profile.id, { id: id.trim(), name: `${profile.name} 副本` })
    await refreshProfiles()
    notify.success('配置空间副本已创建')
  } catch (error: any) {
    notify.error(error.response?.data?.message || '创建配置空间副本失败')
  }
}

const remove = async (profile: Profile) => {
  const ok = await confirmDanger(
    `确定删除配置空间「${profile.name}」及其缓存和生成文件吗？此操作不可撤销。`,
    { title: '删除配置空间' }
  )
  if (!ok) return

  try {
    await profileApi.delete(profile.id)
    await refreshProfiles()
    notify.success('配置空间已删除')
  } catch (error: any) {
    notify.error(error.response?.data?.message || '配置空间删除失败')
  }
}

const exportProfile = async (profileId: string) => {
  try {
    const response = await profileApi.export(profileId)
    const url = URL.createObjectURL(new Blob([response.data], { type: 'application/json' }))
    const link = document.createElement('a')
    link.href = url
    link.download = `${profileId}.json`
    link.click()
    URL.revokeObjectURL(url)
  } catch (error: any) {
    notify.error(error.response?.data?.message || '配置空间导出失败')
  }
}

const pickImportFile = () => importInput.value?.click()

const importProfile = async (event: Event) => {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  const profileId = activeProfileId.value
  try {
    const data = JSON.parse(await file.text())
    await profileApi.import(profileId, data)
    await refreshProfiles()
    notify.success('配置已导入当前配置空间')
  } catch (error: any) {
    notify.error(error.response?.data?.message || '配置空间导入失败')
  } finally {
    input.value = ''
  }
}

onMounted(() => {
  refreshProfiles().catch(() => notify.error('加载配置空间失败'))
})
</script>
