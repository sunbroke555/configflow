import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import ProfileSwitcher from '@/components/ProfileSwitcher.vue'
import ConfirmHost from '@/components/feedback/ConfirmHost.vue'
import { useProfileStore } from '@/stores/profile'
import { activeProfileId, scopedRequests, setActiveProfileId } from '@/profileContext'
import { confirmState, notify, settleConfirm } from '@/lib/feedback'

vi.mock('@/api', () => ({ profileApi: { list: vi.fn() } }))

const wrappers: ReturnType<typeof mount>[] = []
const store = useProfileStore()
const originalScroll = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollIntoView')
const longName = '家庭网络与远程办公共用的配置空间'.repeat(4)

beforeAll(() => {
  Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() })
})

afterAll(() => {
  if (originalScroll) Object.defineProperty(Element.prototype, 'scrollIntoView', originalScroll)
  else Reflect.deleteProperty(Element.prototype, 'scrollIntoView')
})

beforeEach(() => {
  vi.stubGlobal('ResizeObserver', class {
    observe() {}
    unobserve() {}
    disconnect() {}
  })
  sessionStorage.clear()
  setActiveProfileId('default')
  store.profiles.value = [
    { id: 'default', name: 'Default' },
    { id: 'home', name: '家庭网络' },
    { id: 'long-profile', name: longName },
    { id: 'unnamed', name: '' }
  ]
  store.loading.value = false
  scopedRequests.value = 0
  vi.spyOn(notify, 'warning').mockImplementation(() => 0)
})

afterEach(() => {
  settleConfirm('cancel')
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  scopedRequests.value = 0
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
  sessionStorage.clear()
})

async function render(props: { variant?: 'rail' | 'bar'; collapsed?: boolean } = { variant: 'rail' }) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: ['/', '/profiles'].map(path => ({ path, component: { template: '<div />' } }))
  })
  await router.push('/')
  await router.isReady()
  const wrapper = mount(ProfileSwitcher, { props, attachTo: document.body, global: { plugins: [router] } })
  wrappers.push(wrapper, mount(ConfirmHost, { attachTo: document.body }))
  await flushPromises()
  return { wrapper, router }
}

async function openMenu(wrapper: ReturnType<typeof mount>) {
  await wrapper.get('[role="combobox"]').trigger('keydown', { key: 'ArrowDown' })
  await flushPromises()
  const menu = document.querySelector<HTMLElement>('[role="listbox"]')
  expect(menu).not.toBeNull()
  return menu!
}

async function chooseProfile(wrapper: ReturnType<typeof mount>, name: string) {
  const menu = await openMenu(wrapper)
  const option = [...menu.querySelectorAll<HTMLElement>('[role="option"]')]
    .find(item => item.querySelector('[title]')?.getAttribute('title') === name)
  expect(option).toBeTruthy()
  option!.focus()
  option!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
  await flushPromises()
  expect(confirmState.open).toBe(true)
}

async function answerConfirmation(label: string) {
  const button = [...document.querySelectorAll<HTMLButtonElement>('[role="alertdialog"] button')]
    .find(item => item.textContent?.trim() === label)
  expect(button).toBeTruthy()
  button!.click()
  await flushPromises()
}

describe('compact profile switcher', () => {
  it('keeps the expanded rail single-line and retains profile management in the top bar', async () => {
    const { wrapper, router } = await render()
    expect(wrapper.get('[role="combobox"]').classes()).toContain('h-11!')
    expect(wrapper.get('[data-slot="select-value"]').text()).toBe('Default')
    expect(wrapper.get('[role="combobox"]').text()).not.toContain('配置空间')
    expect(wrapper.find('[aria-label="管理配置空间"]').exists()).toBe(false)

    await wrapper.setProps({ variant: 'bar' })
    const button = wrapper.get('[aria-label="管理配置空间"]')
    expect(button.exists()).toBe(true)
    await button.trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/profiles')
    expect(wrapper.get('[role="combobox"]').attributes('title')).toBe('Default')
  })

  it('opens the real select from a collapsed rail and commits a confirmed choice', async () => {
    const { wrapper } = await render({ variant: 'rail', collapsed: true })
    const trigger = wrapper.get('[role="combobox"]')
    expect(trigger.classes()).toContain('size-11!')
    expect(trigger.attributes('aria-description')).toBe('Default')
    await chooseProfile(wrapper, '家庭网络')
    expect(activeProfileId.value).toBe('default')
    await answerConfirmation('切换')
    expect(activeProfileId.value).toBe('home')
    expect(sessionStorage.getItem('configflow.activeProfile')).toBe('home')
    expect(trigger.attributes('title')).toBe('家庭网络')
    expect(trigger.attributes('aria-description')).toBe('家庭网络')
  })

  it('restores the original name and selection when a collapsed switch is cancelled', async () => {
    const { wrapper } = await render({ variant: 'rail', collapsed: true })
    await chooseProfile(wrapper, '家庭网络')
    await answerConfirmation('取消')
    expect(activeProfileId.value).toBe('default')
    expect(wrapper.get('[role="combobox"]').attributes('title')).toBe('Default')
    const menu = await openMenu(wrapper)
    expect(menu.querySelector('[role="option"][aria-selected="true"]')?.textContent).toContain('Default')
  })

  it.each(['loading', 'request'] as const)('disables the collapsed trigger during %s', async state => {
    const { wrapper } = await render({ variant: 'rail', collapsed: true })
    if (state === 'loading') store.loading.value = true
    else scopedRequests.value = 1
    await flushPromises()
    const trigger = wrapper.get('[role="combobox"]')
    expect((trigger.element as HTMLButtonElement).disabled).toBe(true)
    await trigger.trigger('click')
    expect(document.querySelector('[role="listbox"]')).toBeNull()
    expect(confirmState.open).toBe(false)
    expect(activeProfileId.value).toBe('default')
  })

  it('rejects a confirmed switch if a scoped request started while confirmation was open', async () => {
    const { wrapper } = await render({ variant: 'rail', collapsed: true })
    await chooseProfile(wrapper, '家庭网络')
    scopedRequests.value = 1
    await answerConfirmation('切换')
    expect(activeProfileId.value).toBe('default')
    expect(wrapper.get('[role="combobox"]').attributes('title')).toBe('Default')
    expect(notify.warning).toHaveBeenCalledWith('当前配置正在加载或保存，请完成后再切换')
  })

  it('keeps complete long names accessible and bounds the menu while showing name and id', async () => {
    setActiveProfileId('long-profile')
    const { wrapper } = await render({ variant: 'rail', collapsed: true })
    expect(wrapper.get('[role="combobox"]').attributes('title')).toBe(longName)
    expect(wrapper.get('[role="combobox"]').attributes('aria-description')).toBe(longName)
    const menu = await openMenu(wrapper)
    expect(menu.className).toContain('max-w-[calc(100vw-2rem)]')
    expect(menu.textContent).toContain(longName)
    expect(menu.textContent).toContain('long-profile')
    expect(menu.querySelector(`[title="${longName}"]`)?.classList.contains('truncate')).toBe(true)
    expect(menu.querySelector('[title="unnamed"]')?.textContent).toBe('unnamed')
  })

  it('falls back to the active id before the profile list loads', async () => {
    setActiveProfileId('pending-profile')
    store.profiles.value = []
    const { wrapper } = await render({ variant: 'rail', collapsed: true })
    const trigger = wrapper.get('[role="combobox"]')
    expect(trigger.attributes('title')).toBe('pending-profile')
    expect(trigger.attributes('aria-description')).toBe('pending-profile')
    expect(trigger.text()).toContain('P')
  })
})
