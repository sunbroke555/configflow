import { computed, ref } from 'vue'
import { profileApi } from '@/api'
import { activeProfileId, setActiveProfileId } from '@/profileContext'

export interface Profile {
  id: string
  name: string
  description?: string
  created_at?: string
  updated_at?: string
}

const profiles = ref<Profile[]>([])
const loading = ref(false)

const refreshProfiles = async (): Promise<Profile[]> => {
  loading.value = true
  try {
    const { data } = await profileApi.list()
    profiles.value = Array.isArray(data) ? data : data?.profiles || []
    if (!profiles.value.some(profile => profile.id === activeProfileId.value)) {
      setActiveProfileId(profiles.value[0]?.id || 'default')
    }
    return profiles.value
  } finally {
    loading.value = false
  }
}

const switchProfile = (profileId: string): void => {
  if (profiles.value.some(profile => profile.id === profileId)) {
    setActiveProfileId(profileId)
  }
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
  activeProfileId,
  activeProfile,
  refreshProfiles,
  switchProfile,
  profileName,
})
