import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import type { AxiosAdapter, AxiosResponse, InternalAxiosRequestConfig } from 'axios'
import api, { customConfigApi, generateApi, profileApi, proxyGroupApi } from '@/api'
import { useProfileStore } from '@/stores/profile'
import { activeProfileId, beginScopedRequest, clearActiveProfileId, endScopedRequest, scopedRequests, setActiveProfileId } from '@/profileContext'
import { confirmState, notify, settleConfirm } from '@/lib/feedback'

vi.mock('@/router', () => ({ default: { push: vi.fn() } }))

const originalAdapter = api.defaults.adapter
const response = (config: InternalAxiosRequestConfig, data: unknown = {}): AxiosResponse => ({
  config, data, status: 200, statusText: 'OK', headers: {}
})
const store = useProfileStore()

beforeEach(async () => {
  sessionStorage.clear()
  setActiveProfileId('default')
  scopedRequests.value = 0
  const adapter: AxiosAdapter = async config => response(config, config.url === '/profiles'
    ? [{ id: 'default', name: 'Default' }, { id: 'second', name: 'Second' }]
    : {})
  api.defaults.adapter = adapter
  await store.refreshProfiles()
  vi.spyOn(notify, 'warning').mockImplementation(() => 0)
})
afterEach(() => {
  settleConfirm('cancel')
  api.defaults.adapter = originalAdapter
  scopedRequests.value = 0
  vi.restoreAllMocks()
})

describe('local profile switching and request ownership', () => {
  it('persists a confirmed choice only in this tab, without server activation', async () => {
    const requests: string[] = []
    api.defaults.adapter = async config => {
      requests.push(config.url || '')
      return response(config)
    }
    const switching = store.switchProfile('second')
    expect(confirmState.open).toBe(true)
    expect(activeProfileId.value).toBe('default')
    settleConfirm('confirm')
    expect(await switching).toBe(true)
    expect(activeProfileId.value).toBe('second')
    expect(sessionStorage.getItem('configflow.activeProfile')).toBe('second')
    expect(requests).toEqual([])
  })

  it('leaves the active profile unchanged after cancelling a switch', async () => {
    const switching = store.switchProfile('second')
    settleConfirm('cancel')
    expect(await switching).toBe(false)
    expect(activeProfileId.value).toBe('default')
  })

  it('blocks switching while a scoped save is pending and releases after it finishes', async () => {
    let finish!: () => void
    let capturedProfile: unknown
    api.defaults.adapter = config => {
      capturedProfile = config.headers.get('X-ConfigFlow-Profile')
      return new Promise<AxiosResponse>(resolve => { finish = () => resolve(response(config)) })
    }
    const saving = proxyGroupApi.update('group', { name: 'Updated' })
    expect(scopedRequests.value).toBe(1)
    expect(await store.switchProfile('second')).toBe(false)
    expect(capturedProfile).toBe('default')
    expect(activeProfileId.value).toBe('default')
    finish()
    await saving
    expect(scopedRequests.value).toBe(0)
  })

  it('rechecks pending requests after the confirmation is accepted', async () => {
    const switching = store.switchProfile('second')
    beginScopedRequest()
    settleConfirm('confirm')
    expect(await switching).toBe(false)
    expect(activeProfileId.value).toBe('default')
    endScopedRequest()
  })

  it('preserves a mounted editor explicit profile after another selection', async () => {
    let capturedProfile: unknown
    api.defaults.adapter = async config => {
      capturedProfile = config.headers.get('X-ConfigFlow-Profile')
      return response(config)
    }
    setActiveProfileId('second')
    await proxyGroupApi.update('group', { name: 'Old editor' }, 'default')
    expect(capturedProfile).toBe('default')
    expect(scopedRequests.value).toBe(0)
  })

  it('releases a rejected scoped request rather than locking selection', async () => {
    api.defaults.adapter = async config => { throw { config, response: { status: 409, data: { message: 'Referenced resource' } } } }
    await expect(proxyGroupApi.update('group', { name: 'Updated' }, 'default')).rejects.toMatchObject({ response: { status: 409 } })
    expect(scopedRequests.value).toBe(0)
  })

  it.each([
    ['header', () => customConfigApi.getMihomo('removed')],
    ['path', () => generateApi.previewMihomo('removed')],
    ['query', () => api.get('/rules', { params: { profile: 'removed' } })],
  ] as const)('preserves the active profile when an explicit %s selects a missing profile', async (_selection, request) => {
    setActiveProfileId('second')
    const requests: string[] = []
    api.defaults.adapter = async config => {
      requests.push(config.url || '')
      if (config.url === '/profiles') return response(config, [{ id: 'default', name: 'Default' }])
      throw { config, response: { status: 404, data: { message: 'Profile not found: removed' } } }
    }

    await expect(request()).rejects.toMatchObject({ response: { status: 404 } })
    await flushPromises()

    expect(activeProfileId.value).toBe('second')
    expect(sessionStorage.getItem('configflow.activeProfile')).toBe('second')
    expect(requests).not.toContain('/profiles')
    expect(scopedRequests.value).toBe(0)
  })

  it('recovers the current profile when that profile is missing', async () => {
    setActiveProfileId('second')
    const requests: string[] = []
    api.defaults.adapter = async config => {
      requests.push(config.url || '')
      if (config.url === '/profiles') return response(config, [{ id: 'default', name: 'Default', is_default: true }])
      throw { config, response: { status: 404, data: { message: 'Profile not found: second' } } }
    }

    await expect(customConfigApi.getMihomo()).rejects.toMatchObject({ response: { status: 404 } })
    await flushPromises()

    expect(activeProfileId.value).toBe('default')
    expect(sessionStorage.getItem('configflow.activeProfile')).toBeNull()
    expect(requests.filter(path => path === '/profiles')).toHaveLength(1)
    expect(scopedRequests.value).toBe(0)
  })

  it('ignores a missing-profile response after the user has selected another profile', async () => {
    let rejectRequest!: () => void
    const requests: string[] = []
    api.defaults.adapter = config => {
      requests.push(config.url || '')
      if (config.url === '/profiles') return Promise.resolve(response(config, [{ id: 'default', name: 'Default' }]))
      return new Promise((_resolve, reject) => {
        rejectRequest = () => reject({ config, response: { status: 404, data: { message: 'Profile not found: default' } } })
      })
    }
    const pending = customConfigApi.getMihomo()
    setActiveProfileId('second')
    rejectRequest()
    await expect(pending).rejects.toMatchObject({ response: { status: 404 } })
    await flushPromises()

    expect(activeProfileId.value).toBe('second')
    expect(requests).not.toContain('/profiles')
    expect(scopedRequests.value).toBe(0)
  })

  it('shares one profile-list load between shell and profile manager', async () => {
    let finish!: () => void
    let calls = 0
    api.defaults.adapter = config => {
      calls++
      return new Promise<AxiosResponse>(resolve => {
        finish = () => resolve(response(config, [{ id: 'default', name: 'Default' }]))
      })
    }
    const first = store.refreshProfiles()
    const second = store.refreshProfiles()
    expect(first).toBe(second)
    expect(calls).toBe(1)
    finish()
    await first
    expect(store.loading.value).toBe(false)
  })
})


describe('default profile for a new tab', () => {
  const list = [{ id: 'default', name: 'Default', is_default: false }, { id: 'setup', name: 'Setup', is_default: true }]
  it('opens the server default when this tab never picked a profile', async () => {
    clearActiveProfileId()
    let release!: () => void
    const listed = new Promise<void>(resolve => { release = resolve })
    api.defaults.adapter = async config => {
      if (config.url === '/profiles') await listed
      return response(config, config.url === '/profiles' ? list : {})
    }
    store.profileReady.value = false
    const refreshing = store.refreshProfiles()
    // Pages wait while the placeholder id is still active.
    expect(store.profileReady.value).toBe(false)
    release(); await refreshing
    expect(store.profileReady.value).toBe(true)
    expect(activeProfileId.value).toBe('setup')
  })

  it('keeps the profile this tab picked even when another one is the default', async () => {
    setActiveProfileId('default')
    api.defaults.adapter = async config => response(config, config.url === '/profiles' ? list : {})
    await store.refreshProfiles()
    expect(activeProfileId.value).toBe('default')
  })
})
