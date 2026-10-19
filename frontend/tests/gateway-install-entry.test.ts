import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import Agents from '@/views/Agents.vue'
import { agentApi, profileApi } from '@/api'
import { setActiveProfileId, activeProfileId } from '@/profileContext'
vi.mock('@/api', () => ({ default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() }, agentApi: { getAll: vi.fn(), getDeployment: vi.fn(), getUpgrade: vi.fn(), generateInstallCommand: vi.fn(), generateCoreInstallCommand: vi.fn() }, profileApi: { list: vi.fn() } }))
vi.mock('@/lib/feedback', () => ({ confirm: vi.fn(), confirmDanger: vi.fn(), notify: { warning: vi.fn(), loading: vi.fn(), success: vi.fn(), error: vi.fn(), dismiss: vi.fn() } }))
vi.mock('@/components/agents/GatewayInstallWizard.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return { default: defineComponent({ name: 'GatewayInstallWizard', props: ['profileId', 'profileName', 'open'], setup() { return () => h('div', '网关安装向导') } }) }
})
let wrapper: ReturnType<typeof mount>
const mediaDescriptor = Object.getOwnPropertyDescriptor(window, 'matchMedia')
const scrollDescriptor = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollIntoView')
beforeAll(() => {
  Object.defineProperty(window, 'matchMedia', { configurable: true, value: (query: string) => ({ matches: query.includes('prefers-reduced-motion'), media: query, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn() }) })
  Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() })
})
afterAll(() => {
  if (mediaDescriptor) Object.defineProperty(window, 'matchMedia', mediaDescriptor)
  else Reflect.deleteProperty(window, 'matchMedia')
  if (scrollDescriptor) Object.defineProperty(Element.prototype, 'scrollIntoView', scrollDescriptor)
  else Reflect.deleteProperty(Element.prototype, 'scrollIntoView')
})
beforeEach(async () => {
  vi.clearAllMocks(); setActiveProfileId('gateway-space')
  vi.mocked(agentApi.getAll).mockResolvedValue({ data: [] } as any)
  vi.mocked(profileApi.list).mockResolvedValue({ data: [{ id: 'gateway-space', name: '网关空间' }, { id: 'other', name: '其他空间' }] } as any)
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/agents', component: { render: () => null } }] })
  await router.push('/agents'); await router.isReady()
  wrapper = mount(Agents, { attachTo: document.body, global: { plugins: [router] } }); await flushPromises()
  const launch = [...document.querySelectorAll('button')].find(item => item.textContent?.replace(/\s/g, '') === '生成安装脚本')!
  launch.click(); await flushPromises()
})
afterEach(() => { wrapper.unmount(); document.body.innerHTML = ''; sessionStorage.clear() })
describe('Agent installation mode entry', () => {
  it('defaults to ordinary installation and preserves existing Shell/Docker fields', () => {
    expect(document.querySelector('[role="radio"][value="ordinary"]')?.getAttribute('aria-checked')).toBe('true')
    expect(document.getElementById('agent-name')).not.toBeNull()
    expect(document.querySelector('[role="radio"][value="shell"]')).not.toBeNull()
    expect(document.querySelector('[role="radio"][value="docker"]')).not.toBeNull()
    expect(wrapper.findComponent({ name: 'GatewayInstallWizard' }).exists()).toBe(false)
    expect(agentApi.generateInstallCommand).not.toHaveBeenCalled()
    expect(agentApi.generateCoreInstallCommand).not.toHaveBeenCalled()
  })
  it('returns to ordinary mode when the install dialog is reopened', async () => {
    ;(document.querySelector('[role="radio"][value="gateway"]') as HTMLElement).click(); await flushPromises()
    const close = [...document.querySelectorAll('[role="dialog"] button')].find(item => item.textContent?.trim() === '关闭')!
    close.click(); await flushPromises()
    const launch = [...document.querySelectorAll('button')].find(item => item.textContent?.replace(/\s/g, '') === '生成安装脚本')!
    launch.click(); await flushPromises()
    expect(document.querySelector('[role="radio"][value="ordinary"]')?.getAttribute('aria-checked')).toBe('true')
    expect(document.getElementById('agent-name')).not.toBeNull()
    expect(wrapper.findComponent({ name: 'GatewayInstallWizard' }).exists()).toBe(false)
    expect(agentApi.generateInstallCommand).not.toHaveBeenCalled()
    expect(agentApi.generateCoreInstallCommand).not.toHaveBeenCalled()
  })
  it('opens the gateway wizard only after mode selection and keeps entry profile fixed', async () => {
    ;(document.querySelector('[role="radio"][value="gateway"]') as HTMLElement).click(); await flushPromises()
    expect(document.getElementById('agent-name')).toBeNull()
    expect([...document.querySelectorAll('button')].some(item => item.textContent?.trim() === '生成安装命令')).toBe(false)
    const launch = [...document.querySelectorAll('button')].find(item => item.textContent?.trim() === '开始或继续网关安装')!
    launch.click(); await flushPromises()
    const editor = wrapper.getComponent({ name: 'GatewayInstallWizard' })
    expect(editor.props()).toMatchObject({ open: true, profileId: 'gateway-space', profileName: '网关空间' })
    setActiveProfileId('other'); await flushPromises()
    expect(activeProfileId.value).toBe('other')
    expect(editor.props('profileId')).toBe('gateway-space')
    expect(agentApi.generateInstallCommand).not.toHaveBeenCalled()
    expect(agentApi.generateCoreInstallCommand).not.toHaveBeenCalled()
  })
})
