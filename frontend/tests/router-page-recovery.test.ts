import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h, ref } from 'vue'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { RouterView, onBeforeRouteLeave, isNavigationFailure, NavigationFailureType } from 'vue-router'
import router, { pageLoadRecovery } from '@/router'
import { notify } from '@/lib/feedback'
import { recoveryBrowser } from '@/lib/pageRecovery'
import { usePageReloadGuard } from '@/lib/pageReloadGuard'

// Exercise real Vue Router guards, error handling, mounted drafts and recovery.
// Authentication and browser navigation are the only unrelated boundaries.
vi.mock('@/api', () => ({ default: { get: vi.fn(async () => ({ data: { enabled: true } })) } }))
vi.mock('@/authSession', () => ({ checkNavigationAuth: vi.fn(async () => true), invalidateAuthSession: vi.fn() }))
vi.mock('@/views/Login.vue', () => ({ default: { render: () => null } }))

const dirty = ref(false)
const preventLeave = ref(false)
const editor = defineComponent({ setup() {
  const value = ref('Saved content')
  usePageReloadGuard(() => dirty.value)
  onBeforeRouteLeave(() => !preventLeave.value)
  return () => h('input', { 'aria-label': 'Draft content', value: value.value, onInput: (event: Event) => {
    value.value = (event.target as HTMLInputElement).value
    dirty.value = value.value !== 'Saved content'
  } })
} })
const otherPage = { render: () => h('p', 'Other page') }
const assetError = () => new Error('Failed to fetch dynamically imported module: /assets/old-page.js')
const deferred = <T,>() => {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}
const version = (entry = 'assets/index-new.js') => ({ ok: true, json: async () => ({ entry }) } as Response)
let wrapper: VueWrapper | undefined
const removals: Array<() => void> = []
router.addRoute({ path: '/recovery-editor', component: editor })
router.addRoute({ path: '/recovery-other', component: otherPage })

beforeEach(async () => {
  sessionStorage.clear()
  dirty.value = false
  preventLeave.value = false
  document.head.innerHTML = '<script type="module" src="/assets/index-old.js"></script>'
  vi.spyOn(recoveryBrowser, 'fetch').mockResolvedValue(version())
  vi.spyOn(recoveryBrowser, 'replace').mockImplementation(() => undefined)
  vi.spyOn(notify, 'error').mockImplementation(() => 0)
  vi.spyOn(notify, 'loading').mockImplementation(() => 0)
  vi.spyOn(notify, 'dismiss').mockImplementation(() => undefined)
  await router.push('/recovery-editor')
  wrapper = mount(defineComponent({ render: () => h(RouterView) }), { attachTo: document.body, global: { plugins: [router] } })
  await flushPromises()
})
afterEach(async () => {
  wrapper?.unmount()
  wrapper = undefined
  await router.push('/login')
  removals.splice(0).forEach(remove => remove())
  sessionStorage.clear()
  document.head.innerHTML = ''
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

function failingPage(path = '/recovery-missing', failure: Error = assetError()) {
  const imported = vi.fn(async () => { throw failure })
  removals.push(router.addRoute({ path, component: imported }))
  return imported
}
async function failNavigation(path = '/recovery-missing') {
  await expect(router.push(path)).rejects.toThrow('Failed to fetch dynamically imported module')
  await flushPromises()
}

describe('deployed-version recovery with the actual router', () => {
  it('recovers the failed destination including query and anchor after confirming a new version', async () => {
    failingPage()
    await failNavigation('/recovery-missing?tab=clients#latest')
    expect(await pageLoadRecovery).toBe('reloading')
    expect(recoveryBrowser.replace).toHaveBeenCalledWith('/recovery-missing?tab=clients&__cf_reload=assets%2Findex-new.js#latest')
    expect(notify.error).not.toHaveBeenCalled()
    expect(router.currentRoute.value.path).toBe('/recovery-editor')
  })

  it('does not repeat a reload for the same deployment in this tab', async () => {
    failingPage()
    await failNavigation()
    expect(await pageLoadRecovery).toBe('reloading')
    await failNavigation()
    expect(await pageLoadRecovery).toBe('unavailable')
    expect(recoveryBrowser.replace).toHaveBeenCalledOnce()
    expect(notify.error).toHaveBeenCalledWith('页面打开失败，请重试', expect.any(String))
  })

  it.each(['/recovery-other', '/recovery-editor'])('does not redirect after the user chooses %s during the version check', async destination => {
    const pending = deferred<Response>()
    vi.mocked(recoveryBrowser.fetch).mockReturnValueOnce(pending.promise)
    failingPage()
    await failNavigation()
    expect(recoveryBrowser.fetch).toHaveBeenCalledOnce()
    await router.push(destination)
    pending.resolve(version())
    expect(await pageLoadRecovery).toBe('stale')
    expect(router.currentRoute.value.path).toBe(destination)
    expect(recoveryBrowser.replace).not.toHaveBeenCalled()
    expect(sessionStorage.getItem('configflow:page-recovery')).toBeNull()
    expect(notify.error).not.toHaveBeenCalled()
  })


  it('retires an old version check even when the next navigation is rejected by a leave guard', async () => {
    const pending = deferred<Response>()
    vi.mocked(recoveryBrowser.fetch).mockReturnValueOnce(pending.promise)
    failingPage()
    await failNavigation()
    preventLeave.value = true
    const failure = await router.push('/recovery-other')
    expect(isNavigationFailure(failure, NavigationFailureType.aborted)).toBe(true)
    pending.resolve(version())
    expect(await pageLoadRecovery).toBe('stale')
    expect(router.currentRoute.value.path).toBe('/recovery-editor')
    expect(recoveryBrowser.replace).not.toHaveBeenCalled()
    expect(sessionStorage.getItem('configflow:page-recovery')).toBeNull()
    expect(notify.error).not.toHaveBeenCalled()
  })

  it('does not let an old version response cancel a newer page import still in progress', async () => {
    const pendingVersion = deferred<Response>()
    const pendingPage = deferred<typeof otherPage>()
    vi.mocked(recoveryBrowser.fetch).mockReturnValueOnce(pendingVersion.promise)
    failingPage()
    removals.push(router.addRoute({ path: '/recovery-slow-new', component: () => pendingPage.promise }))
    await failNavigation()
    const newerNavigation = router.push('/recovery-slow-new')
    await flushPromises()
    pendingVersion.resolve(version())
    expect(await pageLoadRecovery).toBe('stale')
    expect(recoveryBrowser.replace).not.toHaveBeenCalled()
    pendingPage.resolve(otherPage)
    await newerNavigation
    expect(router.currentRoute.value.path).toBe('/recovery-slow-new')
    expect(notify.error).not.toHaveBeenCalled()
  })

  it('keeps the mounted editor and draft, then recovers on retry after the draft is saved', async () => {
    failingPage()
    await wrapper!.get('input').setValue('Unsaved settings')
    const originalInput = wrapper!.get('input').element
    await failNavigation()
    expect(await pageLoadRecovery).toBe('blocked')
    expect(recoveryBrowser.replace).not.toHaveBeenCalled()
    expect(wrapper!.get('input').element).toBe(originalInput)
    expect(wrapper!.get<HTMLInputElement>('input').element.value).toBe('Unsaved settings')
    expect(sessionStorage.getItem('configflow:page-recovery')).toBeNull()
    expect(notify.error).toHaveBeenCalledWith('页面已更新，当前内容已保留', expect.any(String))
    dirty.value = false
    await failNavigation()
    expect(await pageLoadRecovery).toBe('reloading')
    expect(recoveryBrowser.replace).toHaveBeenCalledOnce()
  })

  it('checks for edits made while the version request is still pending', async () => {
    const pending = deferred<Response>()
    vi.mocked(recoveryBrowser.fetch).mockReturnValueOnce(pending.promise)
    failingPage()
    await failNavigation()
    await wrapper!.get('input').setValue('Typed during version check')
    pending.resolve(version())
    expect(await pageLoadRecovery).toBe('blocked')
    expect(wrapper!.get<HTMLInputElement>('input').element.value).toBe('Typed during version check')
    expect(recoveryBrowser.replace).not.toHaveBeenCalled()
  })

  it('keeps a newly opened edit dialog while the version request is pending', async () => {
    const pending = deferred<Response>()
    vi.mocked(recoveryBrowser.fetch).mockReturnValueOnce(pending.promise)
    failingPage()
    await failNavigation()
    const dialog = document.createElement('section')
    dialog.setAttribute('role', 'dialog')
    dialog.setAttribute('data-state', 'open')
    dialog.innerHTML = '<input value="Unsaved dialog content" />'
    document.body.append(dialog)
    pending.resolve(version())
    expect(await pageLoadRecovery).toBe('blocked')
    expect(dialog.querySelector('input')!.value).toBe('Unsaved dialog content')
    expect(recoveryBrowser.replace).not.toHaveBeenCalled()
  })

  it('reports an ordinary route error without checking deployment or refreshing', async () => {
    failingPage('/recovery-ordinary-error', new Error('unrelated page error'))
    await expect(router.push('/recovery-ordinary-error')).rejects.toThrow('unrelated page error')
    expect(await pageLoadRecovery).toBe('unavailable')
    expect(recoveryBrowser.fetch).not.toHaveBeenCalled()
    expect(recoveryBrowser.replace).not.toHaveBeenCalled()
    expect(wrapper!.get<HTMLInputElement>('input').element.value).toBe('Saved content')
    expect(notify.error).toHaveBeenCalledWith('页面打开失败，请重试', expect.any(String))
  })
})
