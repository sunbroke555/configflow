import { ref } from 'vue'

const STORAGE_KEY = 'configflow.sidebarCollapsed'

const readCollapsed = (): boolean => {
  try {
    return localStorage.getItem(STORAGE_KEY) === 'true'
  } catch {
    return false
  }
}

/** Each mounted rail restores this browser's preference without sharing component state. */
export const useRailCollapse = () => {
  const collapsed = ref(readCollapsed())

  const toggle = (): void => {
    collapsed.value = !collapsed.value
    try {
      localStorage.setItem(STORAGE_KEY, String(collapsed.value))
    } catch {
      // Storage may be blocked or full; the current sidebar remains usable.
    }
  }

  return { collapsed, toggle }
}
