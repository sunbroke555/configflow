import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import type { App } from 'vue'
import type { RecoveryResult } from '@/lib/pageRecovery'

vi.mock('@/App.vue', () => ({ default: { template: '<main data-normal-app>Application ready</main>' } }))
vi.mock('@/stores/theme', () => ({}))

const deferred = <T,>() => {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}
const listeners: EventListenerOrEventListenerObject[] = []
beforeEach(() => {
  vi.resetModules()
  document.body.innerHTML = '<div id="app"></div>'
  const original = document.addEventListener.bind(document)
  vi.spyOn(document, 'addEventListener').mockImplementation((event, listener, options) => {
    if (event === 'scroll') listeners.push(listener)
    original(event, listener, options)
  })
})
afterEach(() => {
  ;(document.querySelector('#app') as (HTMLElement & { __vue_app__?: App }) | null)?.__vue_app__?.unmount()
  listeners.splice(0).forEach(listener => document.removeEventListener('scroll', listener, true))
  document.body.innerHTML = ''
  vi.doUnmock('@/router')
  vi.restoreAllMocks()
})
async function start(ready: Promise<void>, recovery?: Promise<RecoveryResult>) {
  const isReady = vi.fn(() => ready)
  vi.doMock('@/router', () => ({ default: { install: vi.fn(), isReady }, pageLoadRecovery: recovery }))
  await import('@/main')
  await flushPromises()
  expect(isReady).toHaveBeenCalledOnce()
}

describe('initial page load recovery', () => {
  it('mounts the normal application after the initial route succeeds', async () => {
    await start(Promise.resolve())
    expect(document.querySelector('[data-normal-app]')?.textContent).toBe('Application ready')
    expect(document.querySelector('button')).toBeNull()
  })

  it.each<RecoveryResult>(['unavailable', 'blocked', 'stale'])('shows an actionable retry page rather than a blank screen after %s', async result => {
    const ready = deferred<void>()
    await start(ready.promise, Promise.resolve(result))
    ready.reject(new Error('initial chunk could not load'))
    await flushPromises()
    expect(document.querySelector('[data-normal-app]')).toBeNull()
    expect(document.querySelector('h1')?.textContent).toBe('页面暂时无法打开')
    const retry = document.querySelector<HTMLButtonElement>('button')
    expect(retry?.textContent).toBe('重新打开')
    expect(retry?.disabled).toBe(false)
    expect(retry).toBeInstanceOf(HTMLButtonElement)
  })

  it('also mounts a retry page if the initial failure has no recovery task', async () => {
    const ready = deferred<void>()
    await start(ready.promise)
    ready.reject(new Error('unrelated initial route failure'))
    await flushPromises()
    expect(document.querySelector('h1')?.textContent).toBe('页面暂时无法打开')
    expect(document.querySelector('button')?.textContent).toBe('重新打开')
  })

  it('waits for the version check and does not mount the failure screen during a confirmed reload', async () => {
    const ready = deferred<void>()
    const recovery = deferred<RecoveryResult>()
    await start(ready.promise, recovery.promise)
    ready.reject(new Error('initial chunk could not load'))
    await flushPromises()
    expect(document.querySelector('h1')).toBeNull()
    recovery.resolve('reloading')
    await flushPromises()
    expect(document.querySelector('h1')).toBeNull()
    expect(document.querySelector('[data-normal-app]')).toBeNull()
  })
})
