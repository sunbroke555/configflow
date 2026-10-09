import { computed, ref } from 'vue'
import { profileApi } from '@/api'
import { activeProfileId, scopedRequests, setActiveProfileId } from '@/profileContext'
import { confirm, notify } from '@/lib/feedback'

export interface Profile {
  id: string
  name: string
  description?: string
  created_at?: string
  updated_at?: string
}

const profiles = ref<Profile[]>([])
const loading = ref(false)
let refreshing: Promise<Profile[]> | undefined

const refreshProfiles = (): Promise<Profile[]> => {
  if (refreshing) return refreshing
  loading.value = true
  refreshing = (async () => {
    try {
      const { data } = await profileApi.list()
      profiles.value = data
      if (!profiles.value.some(profile => profile.id === activeProfileId.value)) {
        setActiveProfileId(profiles.value.find(profile => profile.id === 'default')?.id || profiles.value[0]?.id || 'default')
      }
      return profiles.value
    } finally {
      loading.value = false
      refreshing = undefined
    }
  })()
  return refreshing
}

const switchProfile = async (profileId: string): Promise<boolean> => {
  if (profileId === activeProfileId.value) return true
  if (!profiles.value.some(profile => profile.id === profileId)) return false
  if (scopedRequests.value) {
    notify.warning('当前配置正在加载或保存，请完成后再切换')
    return false
  }
  const accepted = await confirm('切换将关闭当前配置页面，未保存的编辑会丢失。共享资源和系统设置不受影响。继续吗？', {
    title: '切换配置空间', confirmText: '切换'
  })
  if (!accepted) return false
  if (scopedRequests.value) {
    notify.warning('当前配置正在加载或保存，请完成后再切换')
    return false
  }
  setActiveProfileId(profileId)
  return true
}

const activeProfile = computed(() =>
  profiles.value.find(profile => profile.id === activeProfileId.value)
)

const profileName = (profileId: string): string => {
  return profiles.value.find(profile => profile.id === profileId)?.name || profileId
}

export const useProfileStore = () => ({
  profiles,
  loading,
  scopedRequests,
  activeProfileId,
  activeProfile,
  refreshProfiles,
  switchProfile,
  profileName,
})
