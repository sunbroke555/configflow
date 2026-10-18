import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import Agents from '@/views/Agents.vue'
import api, { agentApi, profileApi } from '@/api'
import { notify } from '@/lib/feedback'
import { setActiveProfileId } from '@/profileContext'
import { execFileSync } from 'node:child_process'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

vi.mock('@/api', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
  agentApi: { getAll: vi.fn(), getDeployment: vi.fn(), getUpgrade: vi.fn(), generateScript: vi.fn(), generateCoreInstallScript: vi.fn() },
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
  vi.mocked(agentApi.generateCoreInstallScript).mockResolvedValue({ data: '#!/bin/sh\necho cores-installed' } as any)
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

describe('independent Mihomo + OxiDNS core installation', () => {
  it('generates the core command with an empty Agent name and default layout, independently of Agent settings', async () => {
    await input('agent-name', '')
    await radio('aio')
    await click('核心服务高级配置')
    await input('aio-mihomo-config-path', '/opt/custom/mihomo.yaml')
    await input('oxidns-agent-port', '8080')
    await click('生成两个核心的安装命令')
    expect(agentApi.generateCoreInstallScript).toHaveBeenCalledExactlyOnceWith()
    expect(agentApi.generateScript).not.toHaveBeenCalled()
    expect(notify.warning).not.toHaveBeenCalled()
    const command = (document.getElementById('core-install-command') as HTMLTextAreaElement).value
    expect(command).toContain("'https://config.example.test/api/agents/core-install-script'")
    expect(command).not.toContain('/opt/custom')
    expect(command).not.toContain('agent_name')
    expect(document.body.textContent).toContain('TUN 初始关闭')
    expect(document.body.textContent).toContain('高级配置只用于接管已有服务')
    expect(document.body.textContent).toContain('生成安装命令')
  })

  it('preserves Agent output while generating a separate core command and script preview', async () => {
    await radio('aio')
    await click('生成安装命令')
    const agentParams = generatedUrlParams()
    await click('生成两个核心的安装命令')
    expect(generatedUrlParams()).toEqual(agentParams)
    expect(agentApi.generateScript).toHaveBeenCalledTimes(1)
    await click('查看核心安装脚本')
    expect((document.querySelector('[aria-label="两个核心的独立安装脚本"]') as HTMLTextAreaElement).value).toContain('cores-installed')
    expect(document.body.textContent).toContain('复制核心安装命令')
  })

  it('clears core output when switching to single-service or Docker modes', async () => {
    await radio('aio')
    await click('生成两个核心的安装命令')
    await radio('single')
    expect(document.getElementById('core-install-command')).toBeNull()
    await radio('aio')
    expect(document.getElementById('core-install-command')).toBeNull()
    await click('生成两个核心的安装命令')
    await radio('docker')
    await radio('shell')
    expect(document.getElementById('core-install-command')).toBeNull()
  })

  it('ignores a completed request after the user switches modes and returns', async () => {
    let finish!: (value: any) => void
    vi.mocked(agentApi.generateCoreInstallScript).mockReturnValueOnce(new Promise(resolve => { finish = resolve }))
    await radio('aio')
    await click('生成两个核心的安装命令')
    expect(document.body.textContent).toContain('正在生成核心安装命令…')
    await radio('single')
    await radio('aio')
    finish({ data: '#!/bin/sh\necho stale' })
    await flushPromises()
    expect(document.getElementById('core-install-command')).toBeNull()
    expect(notify.success).not.toHaveBeenCalled()
    await click('生成两个核心的安装命令')
    expect(document.getElementById('core-install-command')).not.toBeNull()
  })

  it('discards a pending error after closing the dialog', async () => {
    let fail!: (reason: any) => void
    vi.mocked(agentApi.generateCoreInstallScript).mockReturnValueOnce(new Promise((_, reject) => { fail = reject }))
    await radio('aio')
    await click('生成两个核心的安装命令')
    await click('关闭')
    fail(new Error('stale error'))
    await flushPromises()
    expect(notify.error).not.toHaveBeenCalled()
    await click('生成安装脚本')
    await radio('aio')
    expect(document.getElementById('core-install-command')).toBeNull()
  })

  it('shows request failure and permits retry without affecting Agent generation', async () => {
    vi.mocked(agentApi.generateCoreInstallScript).mockRejectedValueOnce({ response: { data: { message: '核心安装文件尚未就绪' } } })
    await radio('aio')
    await click('生成两个核心的安装命令')
    expect(notify.error).toHaveBeenCalledWith('核心安装文件尚未就绪')
    expect(document.getElementById('core-install-command')).toBeNull()
    expect(agentApi.generateScript).not.toHaveBeenCalled()
    await click('生成两个核心的安装命令')
    expect(document.getElementById('core-install-command')).not.toBeNull()
  })

  it.each(['javascript:alert(1)', 'https://config.example.test/?token=abc', 'https://user:pass@config.example.test'])('rejects an invalid installation service URL: %s', async baseUrl => {
    localStorage.setItem('serverDomain', baseUrl)
    await radio('aio')
    await click('生成两个核心的安装命令')
    expect(agentApi.generateCoreInstallScript).not.toHaveBeenCalled()
    expect(notify.error).toHaveBeenCalledWith(expect.stringContaining('HTTP / HTTPS'))
    expect(document.getElementById('core-install-command')).toBeNull()
  })

  it('does not present commands if an older server returns HTML instead of the script', async () => {
    vi.mocked(agentApi.generateCoreInstallScript).mockResolvedValueOnce({ data: '<!doctype html><title>ConfigFlow</title>' } as any)
    await radio('aio')
    await click('生成两个核心的安装命令')
    expect(notify.error).toHaveBeenCalledWith('服务未返回有效的核心安装脚本，请确认 ConfigFlow 已更新')
    expect(document.getElementById('core-install-command')).toBeNull()
  })

  it('quotes special URL characters and executes only a successfully downloaded script', async () => {
    const directory = mkdtempSync(join(tmpdir(), 'configflow-core-command-'))
    const url = "https://config.example.test/base'$(printf_injected)/api/agents/core-install-script"
    try {
      localStorage.setItem('serverDomain', "https://config.example.test/base'$(printf_injected)/")
      await radio('aio')
      await click('生成两个核心的安装命令')
      const command = (document.getElementById('core-install-command') as HTMLTextAreaElement).value
      const env = { ...process.env, PATH: `${directory}:${process.env.PATH}`, CORE_TEST_DIR: directory }
      writeFileSync(join(directory, 'curl'), `#!/bin/sh\nprintf '%s' "$4" > "$CORE_TEST_DIR/url"\nprintf 'echo executed > "$CORE_TEST_DIR/result"\\n' > "$3"\nexit 22\n`, { mode: 0o755 })
      expect(() => execFileSync('sh', ['-c', command], { env, stdio: 'pipe' })).toThrow()
      expect(() => readFileSync(join(directory, 'result'), 'utf8')).toThrow()
      expect(readFileSync(join(directory, 'url'), 'utf8')).toBe(url)
      writeFileSync(join(directory, 'curl'), `#!/bin/sh\nprintf '%s' "$4" > "$CORE_TEST_DIR/url"\nprintf 'echo executed > "$CORE_TEST_DIR/result"\\n' > "$3"\n`, { mode: 0o755 })
      execFileSync('sh', ['-c', command], { env, stdio: 'pipe' })
      expect(readFileSync(join(directory, 'result'), 'utf8').trim()).toBe('executed')
      expect(readFileSync(join(directory, 'url'), 'utf8')).toBe(url)
    } finally {
      rmSync(directory, { recursive: true, force: true })
    }
  })
})
