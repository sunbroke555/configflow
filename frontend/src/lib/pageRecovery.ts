import { canReloadPage } from './pageReloadGuard'

const reloadParameter = '__cf_reload'
const recoveryKey = 'configflow:page-recovery'
export type RecoveryResult = 'reloading' | 'blocked' | 'unavailable' | 'stale'

export function isPageAssetError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error)
  return /Failed to fetch dynamically imported module|error loading dynamically imported module|Importing a module script failed|Unable to preload CSS/i.test(message)
}

/** Remove our cache-busting parameter before Vue Router reads the destination. */
export function clearRecoveryParameter(): void {
  const url = new URL(window.location.href)
  if (!url.searchParams.has(reloadParameter)) return
  url.searchParams.delete(reloadParameter)
  window.history.replaceState(window.history.state, '', url.pathname + url.search + url.hash)
}

// Keep browser effects together so tests can exercise recovery without navigating.
export const recoveryBrowser = {
  fetch: (url: string, options: RequestInit) => window.fetch(url, options),
  replace: (url: string) => window.location.replace(url),
  storage: () => window.sessionStorage,
}

/** Recover only a confirmed deployment change, once per deployed entry in this tab. */
export async function recoverPage(error: unknown, fullPath: string, isCurrent: () => boolean): Promise<RecoveryResult> {
  if (!isPageAssetError(error)) return 'unavailable'
  const entry = document.querySelector<HTMLScriptElement>('script[type="module"][src]')
  if (!entry) return 'unavailable'
  const current = new URL(entry.src, window.location.href)
  if (current.origin !== window.location.origin || !current.pathname.startsWith('/assets/')) return 'unavailable'

  const controller = new AbortController()
  const timeout = window.setTimeout(() => controller.abort(), 3000)
  try {
    const response = await recoveryBrowser.fetch('/frontend-version.json', { cache: 'no-store', signal: controller.signal })
    if (!response.ok) return 'unavailable'
    const version: unknown = await response.json()
    if (!version || typeof version !== 'object' || !('entry' in version)
      || typeof version.entry !== 'string' || !/^assets\/[\w-]+\.js$/.test(version.entry)) return 'unavailable'
    if (!isCurrent()) return 'stale'
    if (`/${version.entry}` === current.pathname) return 'unavailable'
    if (!canReloadPage()) return 'blocked'

    const target = new URL(fullPath, window.location.href)
    if (target.origin !== window.location.origin) return 'unavailable'
    // If storage is unavailable, stay on the page rather than risk a reload loop.
    const storage = recoveryBrowser.storage()
    if (storage.getItem(recoveryKey) === version.entry) return 'unavailable'
    storage.setItem(recoveryKey, version.entry)
    target.searchParams.set(reloadParameter, version.entry)
    recoveryBrowser.replace(target.pathname + target.search + target.hash)
    return 'reloading'
  } catch {
    return isCurrent() ? 'unavailable' : 'stale'
  } finally {
    window.clearTimeout(timeout)
  }
}
