import { ref } from 'vue'

const STORAGE_KEY = 'configflow.activeProfile'

const readStoredProfile = (): string => {
  try {
    return sessionStorage.getItem(STORAGE_KEY) || 'default'
  } catch {
    return 'default'
  }
}

export const activeProfileId = ref(readStoredProfile())

export const setActiveProfileId = (profileId: string): void => {
  if (!profileId) return
  activeProfileId.value = profileId
  try {
    sessionStorage.setItem(STORAGE_KEY, profileId)
  } catch {
    // Private browsing can disable sessionStorage; the in-memory context still works.
  }
}

export const clearActiveProfileId = (): void => {
  activeProfileId.value = 'default'
  try {
    sessionStorage.removeItem(STORAGE_KEY)
  } catch {
    // Keep the in-memory fallback when storage is unavailable.
  }
}

export const getActiveProfileId = (): string => activeProfileId.value
