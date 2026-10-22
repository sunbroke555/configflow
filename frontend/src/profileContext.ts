import { ref } from 'vue'

const STORAGE_KEY = 'configflow.activeProfile'

// A tab that never picked a profile follows the server's default profile once
// the profile list loads; 'default' is only the placeholder until then.
let chosen = false

const readStoredProfile = (): string => {
  try {
    const stored = sessionStorage.getItem(STORAGE_KEY)
    chosen = Boolean(stored)
    return stored || 'default'
  } catch {
    return 'default'
  }
}

export const activeProfileId = ref(readStoredProfile())

export const scopedRequests = ref(0)
export const beginScopedRequest = (): void => { scopedRequests.value++ }
export const endScopedRequest = (): void => {
  scopedRequests.value = Math.max(0, scopedRequests.value - 1)
}

export const setActiveProfileId = (profileId: string): void => {
  if (!profileId) return
  chosen = true
  activeProfileId.value = profileId
  try {
    sessionStorage.setItem(STORAGE_KEY, profileId)
  } catch {
    // Private browsing can disable sessionStorage; the in-memory context still works.
  }
}

export const clearActiveProfileId = (): void => {
  chosen = false
  activeProfileId.value = 'default'
  try {
    sessionStorage.removeItem(STORAGE_KEY)
  } catch {
    // Keep the in-memory fallback when storage is unavailable.
  }
}

export const getActiveProfileId = (): string => activeProfileId.value

export const hasChosenProfile = (): boolean => chosen
