import { computed, ref } from 'vue'
import { profileApi, type ProfileAgentBinding } from '@/api'
import { activeProfileId, scopedRequests, setActiveProfileId } from '@/profileContext'
import { confirm, notify } from '@/lib/feedback'

export interface Profile {
  id: string
  name: string
  description?: string
  created_at?: string
  updated_at?: string
  /** 配置内容的修订号，每次内容变化 +1 */
  revision?: number
  /** 是否为默认配置空间（未指定配置空间时使用；删除默认配置时可切换给其他配置空间） */
  is_default?: boolean
  bound_agents?: ProfileAgentBinding[]
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
        setActiveProfileId(defaultProfileId.value || profiles.value[0]?.id || 'default')
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

// 旧后端不返回 is_default，此时 default 即默认配置空间
export const isDefaultProfile = (profile: Profile): boolean => profile.is_default ?? profile.id === 'default'

const defaultProfileId = computed(() =>
  profiles.value.find(isDefaultProfile)?.id || 'default'
)

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
  defaultProfileId,
  refreshProfiles,
  switchProfile,
  profileName,
})
