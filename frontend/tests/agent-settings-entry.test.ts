import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import Agents from '@/views/Agents.vue'
import { agentApi, profileApi } from '@/api'
import { activeProfileId, setActiveProfileId } from '@/profileContext'
import { notify } from '@/lib/feedback'

// Exercise the real cards and profile store, replacing HTTP and the editor body.
vi.mock('@/api', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
  agentApi: { getAll: vi.fn(), summaries: vi.fn(), getDeployment: vi.fn(), getUpgrade: vi.fn(), bindProfile: vi.fn() },
  profileApi: { list: vi.fn() }
}))
vi.mock('@/lib/feedback', () => ({
  confirm: vi.fn(), confirmDanger: vi.fn(),
  notify: { error: vi.fn(), success: vi.fn(), warning: vi.fn(), info: vi.fn(), loading: vi.fn(), dismiss: vi.fn() }
}))
vi.mock('@/views/Generate.vue', async () => {
  const { defineComponent, h, onMounted } = await import('vue')
  return { default: defineComponent({
    name: 'AgentSettingsEditorStub',
    props: { agentSettings: { type: Object, required: true } },
    emits: ['ready', 'close'],
    setup(_props, { emit }) {
      onMounted(() => emit('ready'))
      return () => h('button', { 'data-test': 'close-agent-settings', onClick: () => emit('close') }, '关闭设置')
    }
  }) }
})

const wrappers: ReturnType<typeof mount>[] = []
const mediaDescriptor = Object.getOwnPropertyDescriptor(window, 'matchMedia')
const scrollDescriptor = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollIntoView')
const canvasDescriptor = Object.getOwnPropertyDescriptor(HTMLCanvasElement.prototype, 'getContext')

beforeAll(() => {
  Object.defineProperty(window, 'matchMedia', { configurable: true, value: (query: string) => ({
    matches: query.includes('prefers-reduced-motion'), media: query,
    addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn()
  }) })
  Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() })
  Object.defineProperty(HTMLCanvasElement.prototype, 'getContext', { configurable: true, value: () => null })
})

afterAll(() => {
  if (mediaDescriptor) Object.defineProperty(window, 'matchMedia', mediaDescriptor)
  else Reflect.deleteProperty(window, 'matchMedia')
  if (scrollDescriptor) Object.defineProperty(Element.prototype, 'scrollIntoView', scrollDescriptor)
  else Reflect.deleteProperty(Element.prototype, 'scrollIntoView')
  if (canvasDescriptor) Object.defineProperty(HTMLCanvasElement.prototype, 'getContext', canvasDescriptor)
  else Reflect.deleteProperty(HTMLCanvasElement.prototype, 'getContext')
})

beforeEach(() => {
  vi.resetAllMocks()
  setActiveProfileId('workspace-current')
})

afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  document.body.innerHTML = ''
  sessionStorage.clear()
})

function agent(overrides: Record<string, unknown> = {}) {
  return {
    id: 'settings-agent', name: 'Settings test', host: '192.0.2.20', port: 8080,
    profile_id: 'workspace-agent', status: 'online', service_type: 'mihomo', deployment_method: 'shell',
    version: '1.3.0-go', last_heartbeat: '', enabled: true, ...overrides
  }
}

const profiles = [
  { id: 'default', name: 'Default', is_default: true },
  { id: 'workspace-current', name: '当前空间' },
  { id: 'workspace-agent', name: '设备空间' },
  { id: 'workspace-new', name: '新空间' },
]

async function render(item = agent()) {
  vi.mocked(agentApi.getAll).mockResolvedValue({ data: [item] } as any)
  vi.mocked(agentApi.summaries).mockResolvedValue({ data: [item] } as any)
  vi.mocked(profileApi.list).mockResolvedValue({ data: profiles } as any)
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/agents', component: { render: () => null } }] })
  await router.push('/agents')
  await router.isReady()
  const wrapper = mount(Agents, { attachTo: document.body, global: { plugins: [router] } })
  wrappers.push(wrapper)
  await flushPromises()
  return {
    wrapper,
    button: () => wrapper.get('button[aria-label="设置 Settings test 的配置"]'),
    editor: () => wrapper.findComponent({ name: 'AgentSettingsEditorStub' }),
    async open() {
      await wrapper.get('button[aria-label="设置 Settings test 的配置"]').trigger('click')
      await flushPromises()
    }
  }
}

describe('Agent settings entry scope', () => {
  it.each(['mihomo', 'mosdns', 'oxidns', 'surge'])('opens %s settings in the bound profile without switching the global profile', async serviceType => {
    const item = agent({ service_type: serviceType })
    const { open, editor, button } = await render(item)
    await open()
    expect(editor().props('agentSettings')).toEqual({
      agentId: 'settings-agent', agentName: 'Settings test', serviceType,
      profileId: 'workspace-agent', profileName: '设备空间',
      targets: { agents: [item], profiles },
    })
    // 绑定只读轻量列表，不为打开设置再算一次完整内容同步
    expect(agentApi.summaries).toHaveBeenCalledTimes(1)
    expect(agentApi.getAll).toHaveBeenCalledTimes(1)
    expect(activeProfileId.value).toBe('workspace-current')
    expect(sessionStorage.getItem('configflow.activeProfile')).toBe('workspace-current')
    expect(agentApi.bindProfile).not.toHaveBeenCalled()
    expect(button().attributes('disabled')).toBeUndefined()
  })

  it('uses the latest actual default for an unbound Agent', async () => {
    const { open, editor } = await render(agent({ profile_id: '' }))
    vi.mocked(profileApi.list).mockResolvedValueOnce({ data: profiles.map(profile => ({
      ...profile, is_default: profile.id === 'workspace-new'
    })) } as any)
    await open()
    expect(editor().props('agentSettings')).toMatchObject({ profileId: 'workspace-new', profileName: '新空间' })
    expect(activeProfileId.value).toBe('workspace-current')
  })

  it('refreshes the Agent binding, software and profile name before opening', async () => {
    const { open, editor } = await render()
    vi.mocked(agentApi.summaries).mockResolvedValueOnce({ data: [agent({
      name: 'Renamed agent', profile_id: 'workspace-new', service_type: 'oxidns'
    })] } as any)
    vi.mocked(profileApi.list).mockResolvedValueOnce({ data: profiles.map(profile => (
      profile.id === 'workspace-new' ? { ...profile, name: '已重命名空间' } : profile
    )) } as any)
    await open()
    expect(editor().props('agentSettings')).toMatchObject({
      agentId: 'settings-agent', agentName: 'Renamed agent', serviceType: 'oxidns',
      profileId: 'workspace-new', profileName: '已重命名空间',
    })
    expect(activeProfileId.value).toBe('workspace-current')
  })

  it('allows an offline Agent to edit its configuration', async () => {
    const { open, editor, button } = await render(agent({ status: 'offline' }))
    expect(button().attributes('disabled')).toBeUndefined()
    await open()
    expect(editor().props('agentSettings')).toMatchObject({ agentId: 'settings-agent', profileId: 'workspace-agent' })
  })

  it('resolves the binding again after closing and reopening', async () => {
    const { wrapper, open, editor } = await render()
    await open()
    expect(editor().props('agentSettings')).toMatchObject({ profileId: 'workspace-agent' })
    await wrapper.get('[data-test="close-agent-settings"]').trigger('click')
    await flushPromises()
    expect(editor().exists()).toBe(false)

    vi.mocked(agentApi.summaries).mockResolvedValueOnce({ data: [agent({ profile_id: 'workspace-new' })] } as any)
    await open()
    expect(editor().props('agentSettings')).toMatchObject({ profileId: 'workspace-new', profileName: '新空间' })
    expect(activeProfileId.value).toBe('workspace-current')
  })
})

describe('Agent settings entry failures', () => {
  it.each([
    ['a deleted profile', [agent({ profile_id: 'deleted-profile' })], '此 Agent 绑定的配置空间已不存在，请重新绑定后再设置。'],
    ['a deleted Agent', [], '此 Agent 已不存在，请刷新列表。'],
    ['an unsupported software type', [agent({ service_type: 'custom' })], '此 Agent 的软件类型暂不支持配置设置。'],
  ])('rejects %s without falling back to the current profile', async (_label, rows, reason) => {
    const { open, editor, button } = await render()
    vi.mocked(agentApi.summaries).mockResolvedValueOnce({ data: rows } as any)
    await open()
    expect(editor().exists()).toBe(false)
    expect(notify.error).toHaveBeenCalledWith('打开配置设置失败', reason)
    expect(activeProfileId.value).toBe('workspace-current')
    expect(button().attributes('disabled')).toBeUndefined()
  })

  it.each(['agents', 'profiles'])('allows retry after the %s refresh fails', async source => {
    const { open, editor, button } = await render()
    const request = source === 'agents' ? vi.mocked(agentApi.summaries) : vi.mocked(profileApi.list)
    request.mockRejectedValueOnce(new Error('连接失败'))
    await open()
    expect(editor().exists()).toBe(false)
    expect(notify.error).toHaveBeenCalledWith('打开配置设置失败', '连接失败')
    expect(button().attributes('disabled')).toBeUndefined()

    await open()
    expect(editor().props('agentSettings')).toMatchObject({ profileId: 'workspace-agent' })
    expect(activeProfileId.value).toBe('workspace-current')
  })
})
