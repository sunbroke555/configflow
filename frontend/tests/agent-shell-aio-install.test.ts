import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import Agents from '@/views/Agents.vue'
import api, { agentApi, profileApi } from '@/api'
import { notify } from '@/lib/feedback'
import { setActiveProfileId } from '@/profileContext'

vi.mock('@/api', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
  agentApi: { getAll: vi.fn(), getDeployment: vi.fn(), getUpgrade: vi.fn(), generateScript: vi.fn() },
  profileApi: { list: vi.fn() }
}))
vi.mock('@/lib/feedback', () => ({
  notify: { warning: vi.fn(), loading: vi.fn(), success: vi.fn(), error: vi.fn(), dismiss: vi.fn() },
  confirm: vi.fn(), confirmDanger: vi.fn()
}))

let wrapper: ReturnType<typeof mount>
const mediaDescriptor = Object.getOwnPropertyDescriptor(window, 'matchMedia')
const scrollDescriptor = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollIntoView')

beforeAll(() => {
  Object.defineProperty(window, 'matchMedia', { configurable: true, value: (query: string) => ({
    matches: query.includes('prefers-reduced-motion'), media: query,
    addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn()
  }) })
  Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() })
})
afterAll(() => {
  if (mediaDescriptor) Object.defineProperty(window, 'matchMedia', mediaDescriptor)
  else Reflect.deleteProperty(window, 'matchMedia')
  if (scrollDescriptor) Object.defineProperty(Element.prototype, 'scrollIntoView', scrollDescriptor)
  else Reflect.deleteProperty(Element.prototype, 'scrollIntoView')
})

beforeEach(async () => {
  vi.clearAllMocks()
  localStorage.setItem('serverDomain', 'https://config.example.test')
  setActiveProfileId('default')
  vi.mocked(agentApi.getAll).mockResolvedValue({ data: [] } as any)
  vi.mocked(profileApi.list).mockResolvedValue({ data: [{ id: 'default', name: 'Default' }] } as any)
  vi.mocked(agentApi.generateScript).mockResolvedValue({ data: '#!/bin/sh\necho installed' } as any)
  vi.mocked(api.get).mockResolvedValue({ data: 'docker output' } as any)
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/agents', component: { render: () => null } }] })
  await router.push('/agents')
  await router.isReady()
  wrapper = mount(Agents, { attachTo: document.body, global: { plugins: [router] } })
  await flushPromises()
  await click('生成安装脚本')
  await input('agent-name', 'home-proxy')
})
afterEach(() => {
  wrapper.unmount()
  document.body.innerHTML = ''
  localStorage.clear()
  sessionStorage.clear()
})

async function click(text: string) {
  const button = [...document.querySelectorAll('button')].find(item => item.textContent?.trim() === text)
  expect(button, text).toBeTruthy()
  button!.click()
  await flushPromises()
}
async function input(id: string, value: string) {
  const element = document.getElementById(id) as HTMLInputElement
  expect(element, id).toBeTruthy()
  element.value = value
  element.dispatchEvent(new Event('input', { bubbles: true }))
  await flushPromises()
}
async function radio(value: string) {
  const element = document.querySelector(`[role="radio"][value="${value}"]`) as HTMLElement
  expect(element, value).toBeTruthy()
  element.click()
  await flushPromises()
}
function generatedUrlParams() {
  const command = [...document.querySelectorAll('textarea')].map(item => item.value).find(value => value.includes('curl -sSL'))!
  return Object.fromEntries(new URL(command.match(/"([^"]+)"/)![1]!).searchParams)
}

describe('Shell Mihomo + OxiDNS installation', () => {
  it('generates matching preview and command parameters with independent Agent ports', async () => {
    await radio('aio')
    expect((document.getElementById('mihomo-agent-port') as HTMLInputElement).value).toBe('8080')
    expect((document.getElementById('oxidns-agent-port') as HTMLInputElement).value).toBe('8082')
    expect(document.getElementById('mosdns-agent-port')).toBeNull()
    expect(document.getElementById('agent-config-path')).toBeNull()
    expect(document.body.textContent).toContain('127.0.0.1:1053')
    expect(document.body.textContent).toContain('脚本仅安装和接管两个 Agent')

    await click('生成安装命令')
    const params = {
      name: 'home-proxy', type: 'aio', agent_ip: '', server_url: 'https://config.example.test',
      mihomo_agent_port: 8080, oxidns_agent_port: 8082,
      mihomo_config_path: '/etc/mihomo/config.yaml', oxidns_config_path: '/etc/oxidns/config.yaml',
      mihomo_service_unit: 'mihomo', oxidns_service_unit: 'oxidns',
      mihomo_service_binary: 'mihomo', oxidns_service_binary: 'oxidns'
    }
    expect(agentApi.generateScript).toHaveBeenCalledExactlyOnceWith(params)
    expect(generatedUrlParams()).toEqual(Object.fromEntries(Object.entries(params).filter(([, value]) => value !== '').map(([key, value]) => [key, String(value)])))
  })

  it('uses each core’s custom paths and excludes stale single-service lifecycle settings', async () => {
    await input('agent-restart', '/opt/single/control restart')
    await radio('aio')
    await click('核心服务高级配置')
    await input('aio-mihomo-config-path', '/opt/mihomo/config.yaml')
    await input('aio-oxidns-config-path', '/opt/oxidns/config.yaml')
    await input('aio-mihomo-service-unit', 'mihomo-native')
    await input('aio-oxidns-service-unit', 'oxidns-native')
    await input('aio-mihomo-service-binary', '/opt/mihomo/mihomo')
    await input('aio-oxidns-service-binary', '/opt/oxidns/oxidns')
    await input('oxidns-agent-port', '18082')
    await click('生成安装命令')
    const params = vi.mocked(agentApi.generateScript).mock.calls[0]![0]
    expect(params).toMatchObject({
      type: 'aio', oxidns_agent_port: 18082,
      mihomo_config_path: '/opt/mihomo/config.yaml', oxidns_config_path: '/opt/oxidns/config.yaml',
      mihomo_service_unit: 'mihomo-native', oxidns_service_unit: 'oxidns-native',
      mihomo_service_binary: '/opt/mihomo/mihomo', oxidns_service_binary: '/opt/oxidns/oxidns'
    })
    expect(params).not.toHaveProperty('restart_command')
    expect(params).not.toHaveProperty('service_manager')
    expect(params).not.toHaveProperty('config_path')
    expect(generatedUrlParams()).not.toHaveProperty('restart_command')
  })

  it('rejects conflicting ports before requesting or presenting an installation script', async () => {
    await radio('aio')
    await input('oxidns-agent-port', '8080')
    await click('生成安装命令')
    expect(notify.warning).toHaveBeenCalledWith('两个 Agent 必须使用不同的端口')
    expect(agentApi.generateScript).not.toHaveBeenCalled()
    expect(document.querySelector('textarea')).toBeNull()
  })

  it('keeps the existing single-service request and its explicit lifecycle auto-detection', async () => {
    await input('agent-port', '18080')
    await click('生成安装命令')
    expect(agentApi.generateScript).toHaveBeenCalledExactlyOnceWith({
      name: 'home-proxy', type: 'mihomo', port: 18080, agent_ip: '',
      config_path: '/etc/mihomo/config.yaml', restart_command: '', server_url: 'https://config.example.test'
    })
    expect(generatedUrlParams().restart_command).toBe('')
    expect(generatedUrlParams()).not.toHaveProperty('mihomo_agent_port')
  })

  it('keeps Docker AIO as Mihomo + OxiDNS when switching installation types', async () => {
    await radio('aio')
    await radio('docker')
    await radio('aio')
    expect(document.getElementById('mosdns-agent-port')).toBeNull()
    expect(document.getElementById('oxidns-agent-port')).not.toBeNull()
    await click('生成 Docker 部署命令')
    expect(api.get).toHaveBeenCalledWith('/agents/docker-agent-compose', { params: expect.objectContaining({
      enable_mihomo: true, enable_mosdns: false, enable_oxidns: true, mihomo_port: 8080, oxidns_port: 8082
    }) })
    expect(agentApi.generateScript).not.toHaveBeenCalled()
  })
})
