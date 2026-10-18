import { onScopeDispose } from 'vue'

const blockers = new Set<() => boolean>()

/** Protect in-memory edits only when recovering from an outdated page bundle. */
export function usePageReloadGuard(blocked: () => boolean): void {
  blockers.add(blocked)
  onScopeDispose(() => blockers.delete(blocked))
}

/** A submitted write outlives its page; release this guard when the request settles. */
export async function withPageReloadBlocked<T>(operation: () => Promise<T>): Promise<T> {
  const blocked = () => true
  blockers.add(blocked)
  try {
    return await operation()
  } finally {
    blockers.delete(blocked)
  }
}

export function canReloadPage(): boolean {
  if ([...blockers].some(blocked => blocked())) return false
  // Dialogs also contain forms that do not need ordinary navigation guards.
  if (document.querySelector('[role="dialog"][data-state="open"], [role="alertdialog"][data-state="open"]')) return false
  // Reuse existing draft guards without asking the browser to actually unload.
  return window.dispatchEvent(new Event('beforeunload', { cancelable: true }))
}
