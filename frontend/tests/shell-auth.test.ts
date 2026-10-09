import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import App from '@/App.vue'
import Login from '@/views/Login.vue'
import Profiles from '@/views/Profiles.vue'
import api, { profileApi } from '@/api'
import { setActiveProfileId } from '@/profileContext'

vi.mock('@/api', () => ({
  default: { get: vi.fn(async (path: string) => ({ data: path === '/auth/status' ? { authEnabled: true } : { enabled: true } })) },
  profileApi: { list: vi.fn(async () => ({ data: [{ id: 'default', name: 'Default' }, { id: 'second', name: 'Second' }] })) },
  systemApi: { getVersion: vi.fn(async () => ({ data: { version: 'test' } })) }
}))

const wrappers: VueWrapper[] = []
const originalMatchMedia = Object.getOwnPropertyDescriptor(window, 'matchMedia')
const originalScroll = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollIntoView')
beforeAll(() => {
  // jsdom supplies neither media queries nor scrolling; application components stay real.
  Object.defineProperty(window, 'matchMedia', { configurable: true, value: vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn(), addListener: vi.fn(), removeListener: vi.fn() })) })
  Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() })
})
afterAll(() => {
  if (originalMatchMedia) Object.defineProperty(window, 'matchMedia', originalMatchMedia)
  else Reflect.deleteProperty(window, 'matchMedia')
  if (originalScroll) Object.defineProperty(Element.prototype, 'scrollIntoView', originalScroll)
  else Reflect.deleteProperty(Element.prototype, 'scrollIntoView')
})
beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  sessionStorage.clear()
  setActiveProfileId('default')
})
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  document.body.innerHTML = ''
})

async function renderLogin() {
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/login', component: Login }, { path: '/profiles', component: Profiles }
  ] })
  await router.push('/login')
  await router.isReady()
  const wrapper = mount(App, { attachTo: document.body, global: { plugins: [router] } })
  wrappers.push(wrapper)
  await flushPromises()
  return { router, wrapper }
}

describe('authenticated shell startup', () => {
  it('never loads protected profiles or aggregation settings on the login screen, and loads after login navigation', async () => {
    const { router } = await renderLogin()
    expect(profileApi.list).not.toHaveBeenCalled()
    expect(api.get).not.toHaveBeenCalledWith('/settings/subscription-aggregation')
    expect(document.querySelector('[aria-label="当前配置空间"]')).toBeNull()
    localStorage.setItem('token', 'synthetic-token')
    localStorage.setItem('username', 'Synthetic')
    await router.push('/profiles')
    await flushPromises()
    expect(profileApi.list).toHaveBeenCalled()
    expect(api.get).toHaveBeenCalledWith('/settings/subscription-aggregation')
    expect(document.querySelector('[aria-label="当前配置空间"]')).not.toBeNull()
  })

  it('does not remount a system editor when the tab-local profile changes', async () => {
    const { router } = await renderLogin()
    await router.push('/profiles')
    await flushPromises()
    const create = [...document.querySelectorAll('button')].find(button => button.textContent?.trim() === '新建配置空间')
    expect(create).toBeTruthy()
    create!.click()
    await flushPromises()
    const input = document.querySelector<HTMLInputElement>('#profile-name')!
    input.value = 'Unsaved global draft'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    setActiveProfileId('second')
    await flushPromises()
    expect(document.querySelector<HTMLInputElement>('#profile-name')?.value).toBe('Unsaved global draft')
  })
})
