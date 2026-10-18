import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { effectScope, ref } from 'vue'
import { clearRecoveryParameter, isPageAssetError, recoverPage, recoveryBrowser } from '@/lib/pageRecovery'
import { usePageReloadGuard } from '@/lib/pageReloadGuard'

const oldEntry = 'assets/index-Old12345.js'
const newEntry = 'assets/index-New67890.js'
const importError = new TypeError('Failed to fetch dynamically imported module: /assets/Generate-old.js')
const storageKey = 'configflow:page-recovery'
const scopes: ReturnType<typeof effectScope>[] = []
let script: HTMLScriptElement
let fetchVersion: ReturnType<typeof vi.spyOn<typeof recoveryBrowser, 'fetch'>>
let replacePage: ReturnType<typeof vi.spyOn<typeof recoveryBrowser, 'replace'>>

function response(entry = newEntry) {
  return new Response(JSON.stringify({ entry }), { headers: { 'Content-Type': 'application/json' } })
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}

function guard(blocked: () => boolean) {
  const scope = effectScope()
  scopes.push(scope)
  scope.run(() => usePageReloadGuard(blocked))
}

beforeEach(() => {
  window.history.replaceState({ original: true }, '', '/generate?software=mihomo#settings')
  window.sessionStorage.clear()
  document.body.innerHTML = ''
  script = document.createElement('script')
  script.type = 'module'
  script.src = `/${oldEntry}`
  document.head.append(script)
  fetchVersion = vi.spyOn(recoveryBrowser, 'fetch').mockResolvedValue(response())
  replacePage = vi.spyOn(recoveryBrowser, 'replace').mockImplementation(() => undefined)
})

afterEach(() => {
  scopes.splice(0).forEach(scope => scope.stop())
  script.remove()
  document.body.innerHTML = ''
  window.sessionStorage.clear()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('confirmed deployment recovery', () => {
  it('reloads the requested page once, preserving its query values and anchor', async () => {
    const destination = '/agents/dns%20one/oxidns?tab=clients&filter=a%2Bb&filter=two#client-list'
    expect(await recoverPage(importError, destination, () => true)).toBe('reloading')
    expect(fetchVersion).toHaveBeenCalledWith('/frontend-version.json', {
      cache: 'no-store', signal: expect.any(AbortSignal)
    })
    expect(replacePage).toHaveBeenCalledOnce()
    const next = new URL(replacePage.mock.calls[0]![0], window.location.href)
    expect(next.pathname).toBe('/agents/dns%20one/oxidns')
    expect(next.searchParams.get('tab')).toBe('clients')
    expect(next.searchParams.getAll('filter')).toEqual(['a+b', 'two'])
    expect(next.hash).toBe('#client-list')
    expect(next.searchParams.get('__cf_reload')).toBe(newEntry)
    expect(window.sessionStorage.getItem(storageKey)).toBe(newEntry)

    // Simulate the old page remaining open after a failed reload or another lazy import failing.
    fetchVersion.mockResolvedValueOnce(response())
    expect(await recoverPage(importError, '/routers', () => true)).toBe('unavailable')
    expect(replacePage).toHaveBeenCalledOnce()
  })

  it('does not retry when this tab already attempted the same deployed entry', async () => {
    window.sessionStorage.setItem(storageKey, newEntry)
    expect(await recoverPage(importError, '/oxidns', () => true)).toBe('unavailable')
    expect(replacePage).not.toHaveBeenCalled()
  })

  it('can recover a later deployment without a permanent tab-wide lockout', async () => {
    window.sessionStorage.setItem(storageKey, 'assets/index-Previous.js')
    expect(await recoverPage(importError, '/oxidns', () => true)).toBe('reloading')
    expect(window.sessionStorage.getItem(storageKey)).toBe(newEntry)
    expect(replacePage).toHaveBeenCalledOnce()
  })

  it('does not reload if the server reports the current entry', async () => {
    fetchVersion.mockResolvedValueOnce(response(oldEntry))
    expect(await recoverPage(importError, '/oxidns', () => true)).toBe('unavailable')
    expect(replacePage).not.toHaveBeenCalled()
    expect(window.sessionStorage.length).toBe(0)
  })

  it.each([
    new Error('Request failed with status code 500'),
    new TypeError('Failed to fetch'),
    new Error('Cannot read properties of undefined'),
    new Error('Navigation aborted')
  ])('leaves ordinary application or connection errors alone: %s', async error => {
    expect(await recoverPage(error, '/oxidns', () => true)).toBe('unavailable')
    expect(fetchVersion).not.toHaveBeenCalled()
    expect(replacePage).not.toHaveBeenCalled()
  })

  it.each([
    'Failed to fetch dynamically imported module: http://example.test/assets/old.js',
    'error loading dynamically imported module',
    'Importing a module script failed.',
    'Unable to preload CSS for /assets/Generate-old.css'
  ])('recognizes browser asset failures: %s', message => {
    expect(isPageAssetError(new Error(message))).toBe(true)
  })

  it('requires a recognizable, same-origin module entry before checking the server', async () => {
    script.remove()
    expect(await recoverPage(importError, '/oxidns', () => true)).toBe('unavailable')
    script.src = 'https://other.example/assets/index-old.js'
    document.head.append(script)
    expect(await recoverPage(importError, '/oxidns', () => true)).toBe('unavailable')
    script.src = '/src/main.ts'
    expect(await recoverPage(importError, '/oxidns', () => true)).toBe('unavailable')
    expect(fetchVersion).not.toHaveBeenCalled()
    expect(replacePage).not.toHaveBeenCalled()
  })

  it('does not redirect to an external URL', async () => {
    expect(await recoverPage(importError, 'https://other.example/oxidns', () => true)).toBe('unavailable')
    expect(replacePage).not.toHaveBeenCalled()
    expect(window.sessionStorage.length).toBe(0)
  })
})

describe('unavailable version metadata', () => {
  it.each([404, 502])('does not reload after HTTP %i', async status => {
    fetchVersion.mockResolvedValueOnce(new Response('Unavailable', { status }))
    expect(await recoverPage(importError, '/oxidns', () => true)).toBe('unavailable')
    expect(replacePage).not.toHaveBeenCalled()
    expect(window.sessionStorage.length).toBe(0)
  })

  it('handles an old server or service worker returning HTML instead of the version', async () => {
    fetchVersion.mockResolvedValueOnce(new Response('<!doctype html><html><body>ConfigFlow</body></html>', {
      headers: { 'Content-Type': 'text/html' }
    }))
    expect(await recoverPage(importError, '/oxidns', () => true)).toBe('unavailable')
    expect(replacePage).not.toHaveBeenCalled()
    expect(window.sessionStorage.length).toBe(0)
  })

  it.each([null, {}, { entry: 42 }, { entry: 'index.html' }, { entry: '/assets/index-new.js' }, { entry: 'https://other.example/index.js' }])(
    'does not treat malformed metadata as a confirmed update: %j', async body => {
      fetchVersion.mockResolvedValueOnce(new Response(JSON.stringify(body)))
      expect(await recoverPage(importError, '/oxidns', () => true)).toBe('unavailable')
      expect(replacePage).not.toHaveBeenCalled()
      expect(window.sessionStorage.length).toBe(0)
    }
  )

  it('leaves the page intact when the metadata request cannot connect', async () => {
    fetchVersion.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    expect(await recoverPage(importError, '/oxidns', () => true)).toBe('unavailable')
    expect(replacePage).not.toHaveBeenCalled()
    expect(window.sessionStorage.length).toBe(0)
  })

  it('aborts a stalled metadata request after three seconds and does not reload', async () => {
    vi.useFakeTimers()
    let signal!: AbortSignal
    fetchVersion.mockImplementationOnce((_url, options) => new Promise((_resolve, reject) => {
      signal = options.signal as AbortSignal
      signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))
    }))
    const result = recoverPage(importError, '/oxidns', () => true)
    await vi.advanceTimersByTimeAsync(2999)
    expect(signal.aborted).toBe(false)
    await vi.advanceTimersByTimeAsync(1)
    expect(signal.aborted).toBe(true)
    expect(await result).toBe('unavailable')
    expect(replacePage).not.toHaveBeenCalled()
    expect(window.sessionStorage.length).toBe(0)
    expect(vi.getTimerCount()).toBe(0)
  })

  it.each(['access', 'read', 'write'])('does not reload when session storage fails during %s', async stage => {
    const denied = () => { throw new DOMException('Storage unavailable', 'SecurityError') }
    if (stage === 'access') vi.spyOn(recoveryBrowser, 'storage').mockImplementation(denied)
    else vi.spyOn(Storage.prototype, stage === 'read' ? 'getItem' : 'setItem').mockImplementation(denied)
    expect(await recoverPage(importError, '/oxidns', () => true)).toBe('unavailable')
    expect(replacePage).not.toHaveBeenCalled()
  })
})

describe('preserving edits and newer navigation', () => {
  it('keeps unsaved content and allows recovery only after it has been saved', async () => {
    const dirty = ref(true)
    guard(() => dirty.value)
    document.body.innerHTML = '<textarea id="draft"></textarea>'
    const editor = document.querySelector<HTMLTextAreaElement>('#draft')!
    editor.value = 'unfinished user configuration'
    expect(await recoverPage(importError, '/oxidns', () => true)).toBe('blocked')
    expect(editor.value).toBe('unfinished user configuration')
    expect(replacePage).not.toHaveBeenCalled()
    expect(window.sessionStorage.length).toBe(0)
    dirty.value = false
    fetchVersion.mockResolvedValueOnce(response())
    expect(await recoverPage(importError, '/oxidns', () => true)).toBe('reloading')
    expect(replacePage).toHaveBeenCalledOnce()
  })

  it.each(['dialog', 'alertdialog'])('keeps an open %s and its form contents', async role => {
    document.body.innerHTML = `<section role="${role}" data-state="open"><input value="unfinished" /></section>`
    expect(await recoverPage(importError, '/oxidns', () => true)).toBe('blocked')
    expect(document.querySelector('input')!.value).toBe('unfinished')
    expect(replacePage).not.toHaveBeenCalled()
    expect(window.sessionStorage.length).toBe(0)
  })

  it('respects an existing beforeunload draft guard without opening a browser confirmation', async () => {
    const unload = (event: Event) => event.preventDefault()
    const confirm = vi.spyOn(window, 'confirm')
    window.addEventListener('beforeunload', unload)
    try {
      expect(await recoverPage(importError, '/oxidns', () => true)).toBe('blocked')
      expect(replacePage).not.toHaveBeenCalled()
      expect(confirm).not.toHaveBeenCalled()
    } finally { window.removeEventListener('beforeunload', unload) }
  })

  it('checks edits made while the version request was in progress', async () => {
    const dirty = ref(false)
    guard(() => dirty.value)
    const pending = deferred<Response>()
    fetchVersion.mockReturnValueOnce(pending.promise)
    const result = recoverPage(importError, '/oxidns', () => true)
    dirty.value = true
    pending.resolve(response())
    expect(await result).toBe('blocked')
    expect(replacePage).not.toHaveBeenCalled()
  })

  it('does not return to an obsolete destination after the user clicks another page', async () => {
    const pending = deferred<Response>()
    fetchVersion.mockReturnValueOnce(pending.promise)
    let current = true
    const result = recoverPage(importError, '/oxidns', () => current)
    current = false
    pending.resolve(response())
    expect(await result).toBe('stale')
    expect(replacePage).not.toHaveBeenCalled()
    expect(window.sessionStorage.length).toBe(0)
  })

  it('also ignores a request superseded while its response body was being read', async () => {
    const body = deferred<{ entry: string }>()
    const json = vi.fn(() => body.promise)
    fetchVersion.mockResolvedValueOnce({ ok: true, json } as unknown as Response)
    let current = true
    const result = recoverPage(importError, '/oxidns', () => current)
    await Promise.resolve()
    expect(json).toHaveBeenCalledOnce()
    current = false
    body.resolve({ entry: newEntry })
    expect(await result).toBe('stale')
    expect(replacePage).not.toHaveBeenCalled()
  })
})

describe('recovery URL cleanup', () => {
  it('removes only the temporary version parameter, retaining history state and the user URL', () => {
    const state = { back: '/dashboard', position: 7, scroll: { left: 0, top: 400 } }
    window.history.replaceState(state, '', '/generate?software=mihomo&filter=a%2Bb&__cf_reload=assets%2Findex-new.js&filter=two#settings')
    const replace = vi.spyOn(window.history, 'replaceState')
    clearRecoveryParameter()
    expect(window.location.pathname).toBe('/generate')
    const url = new URL(window.location.href)
    expect(url.searchParams.getAll('filter')).toEqual(['a+b', 'two'])
    expect(url.searchParams.get('software')).toBe('mihomo')
    expect(url.searchParams.has('__cf_reload')).toBe(false)
    expect(url.hash).toBe('#settings')
    expect(window.history.state).toEqual(state)
    expect(replace).toHaveBeenCalledOnce()
    clearRecoveryParameter()
    expect(replace).toHaveBeenCalledOnce()
  })

  it('does not rewrite ordinary navigation URLs or create a new history entry', () => {
    const replace = vi.spyOn(window.history, 'replaceState')
    const push = vi.spyOn(window.history, 'pushState')
    clearRecoveryParameter()
    expect(replace).not.toHaveBeenCalled()
    expect(push).not.toHaveBeenCalled()
  })
})
