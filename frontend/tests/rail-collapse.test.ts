import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h, ref } from 'vue'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createMemoryHistory, createRouter, useRoute } from 'vue-router'
import AppRail from '@/components/shell/AppRail.vue'

const STORAGE_KEY = 'configflow.sidebarCollapsed'
let wrapper: VueWrapper | undefined

beforeEach(() => {
  localStorage.clear()
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  vi.stubGlobal('matchMedia', (media: string) => ({
    media, matches: false,
    addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
  }))
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = undefined
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  localStorage.clear()
})

async function mountRail() {
  const aggregationEnabled = ref(true)
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/:pathMatch(.*)*', component: { render: () => null } }],
  })
  await router.push('/nodes')
  wrapper = mount(defineComponent({
    setup() {
      const route = useRoute()
      return () => h(AppRail, {
        activePath: route.path,
        profileName: 'Default',
        subscriptionAggregationEnabled: aggregationEnabled.value,
      })
    },
  }), { global: { plugins: [router] } })
  await flushPromises()
  return { rail: wrapper, router, aggregationEnabled }
}

describe('sidebar collapse', () => {
  it('collapses and expands with an accessible control and persists the preference', async () => {
    const { rail } = await mountRail()
    const navId = rail.get('nav[aria-label="主导航"]').attributes('id')
    expect(navId).toBeTruthy()
    const collapse = rail.get('button[aria-label="收起导航"]')
    expect(collapse.attributes('aria-controls')).toBe(navId)
    expect(collapse.attributes('aria-expanded')).toBe('true')
    expect(rail.get('aside').attributes('data-collapsed')).toBe('false')

    await collapse.trigger('click')
    expect(rail.get('aside').attributes('data-collapsed')).toBe('true')
    expect(localStorage.getItem(STORAGE_KEY)).toBe('true')
    const expand = rail.get('button[aria-label="展开导航"]')
    expect(expand.attributes('aria-expanded')).toBe('false')
    expect(expand.attributes('aria-controls')).toBe(navId)

    await expand.trigger('click')
    expect(rail.get('aside').attributes('data-collapsed')).toBe('false')
    expect(localStorage.getItem(STORAGE_KEY)).toBe('false')
  })

  it('restores the saved preference on a new mount', async () => {
    const { rail } = await mountRail()
    await rail.get('button[aria-label="收起导航"]').trigger('click')
    rail.unmount()
    wrapper = undefined

    const restored = await mountRail()
    expect(restored.rail.get('aside').attributes('data-collapsed')).toBe('true')
    expect(restored.rail.find('button[aria-label="展开导航"]').exists()).toBe(true)
  })

  it('continues to toggle when browser storage cannot be read or written', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('blocked') })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('full') })
    const { rail } = await mountRail()
    expect(rail.get('aside').attributes('data-collapsed')).toBe('false')

    await rail.get('button[aria-label="收起导航"]').trigger('click')
    expect(rail.get('aside').attributes('data-collapsed')).toBe('true')
    await rail.get('button[aria-label="展开导航"]').trigger('click')
    expect(rail.get('aside').attributes('data-collapsed')).toBe('false')
  })

  it('keeps collapsed navigation named, active and clickable while respecting feature visibility', async () => {
    localStorage.setItem(STORAGE_KEY, 'true')
    const { rail, router, aggregationEnabled } = await mountRail()
    const nav = rail.get('nav[aria-label="主导航"]')
    expect(nav.get('a[href="/nodes"]').attributes('aria-current')).toBe('page')
    expect(nav.get('a[href="/nodes"]').attributes('aria-label')).toBe('节点库')

    await nav.get('a[href="/rule-library"]').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/rule-library')
    expect(nav.get('a[href="/rule-library"]').attributes('aria-current')).toBe('page')
    expect(nav.get('a[href="/nodes"]').attributes('aria-current')).toBeUndefined()
    expect(nav.find('a[href="/subscription-aggregation"]').exists()).toBe(true)
    aggregationEnabled.value = false
    await flushPromises()
    expect(nav.find('a[href="/subscription-aggregation"]').exists()).toBe(false)
  })
})
