import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import Agents from '@/views/Agents.vue'
import api, { agentApi, profileApi } from '@/api'
import { notify } from '@/lib/feedback'
import { setActiveProfileId } from '@/profileContext'
import { AGENT_CORE_INSTALL_INTERFACE, buildAgentCoreInstallCommand, buildAgentInstallCommand, buildCoreInstallCommand } from '@/lib/coreInstallCommand'

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
  vi.mocked(agentApi.generateCoreInstallScript).mockResolvedValue({ data: '#!/bin/sh\n# ConfigFlow core installer interface: 1\necho cores-installed' } as any)
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

function findButton(text: string) {
  const buttons = [...document.querySelectorAll('button')]
  const label = (button: HTMLButtonElement) => button.textContent?.trim().replace(/\s+/g, ' ')
  return buttons.find(button => label(button) === text) ?? buttons.find(button => label(button)?.startsWith(`${text} `))
}
async function click(text: string) {
  const button = findButton(text)
  expect(button, text).toBeTruthy()
  button!.click()
  await flushPromises()
}
async function expand(text: string) {
  const button = findButton(text)
  expect(button, text).toBeTruthy()
  if (button!.getAttribute('aria-expanded') !== 'true') await click(text)
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
  const command = [...document.querySelectorAll('textarea')].map(item => item.value).find(value => value.includes('/api/agents/install-script?'))!
  return Object.fromEntries(new URL(commandUrl(command)).searchParams)
}
function commandUrl(command: string) {
  return command.match(/'(https?:\/\/[^']+)'/)![1]!
}

describe('Shell Mihomo + OxiDNS installation', () => {
  it('generates matching preview and command parameters with independent Agent ports', async () => {
    await radio('aio')
    expect((document.getElementById('mihomo-agent-port') as HTMLInputElement).value).toBe('8080')
    expect((document.getElementById('oxidns-agent-port') as HTMLInputElement).value).toBe('8082')
    expect(document.getElementById('mosdns-agent-port')).toBeNull()
    expect(document.getElementById('agent-config-path')).toBeNull()
    expect(document.getElementById('aio-install-cores')?.getAttribute('aria-checked')).toBe('false')
    expect(findButton('安装说明')?.getAttribute('aria-expanded')).toBe('false')
    expect(document.body.textContent).not.toContain('IPv4 转发')
    await expand('安装说明')
    expect(document.body.textContent).toContain('127.0.0.1:1053')
    expect(document.body.textContent).toContain('IPv4 转发')

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
    await expand('服务管理高级配置（可选）')
    await input('agent-restart', '/opt/single/control restart')
    await radio('aio')
    await expand('核心服务高级配置')
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

  it.each([
    { mode: 'single', section: '服务管理高级配置（可选）', field: 'agent-config-path' },
    { mode: 'aio', section: '核心服务高级配置', field: 'aio-mihomo-config-path' }
  ])('reveals an invalid $mode configuration path when advanced settings are collapsed', async ({ mode, section, field }) => {
    if (mode === 'aio') await radio('aio')
    await expand(section)
    await input(field, '/opt/custom/')
    await click(section)
    expect(document.getElementById(field)).toBeNull()
    await click('生成安装命令')
    expect(agentApi.generateScript).not.toHaveBeenCalled()
    expect(notify.warning).toHaveBeenCalledWith('配置文件路径应指向一个文件（如 config.yaml），而不是文件夹')
    expect((document.getElementById(field) as HTMLInputElement)?.value).toBe('/opt/custom/')
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

describe('curl and wget installation commands', () => {
  it.each([
    { mode: 'single', type: 'mihomo', includeCore: false },
    { mode: 'single', type: 'mosdns', includeCore: false },
    { mode: 'single', type: 'oxidns', includeCore: true },
    { mode: 'aio', type: 'aio', includeCore: false },
    { mode: 'aio', type: 'aio', includeCore: true }
  ])('switches $mode $type includeCore=$includeCore without requesting another script', async ({ mode, type, includeCore }) => {
    if (mode === 'aio') await radio('aio')
    else if (type !== 'mihomo') await radio(type)
    if (includeCore) {
      document.getElementById(mode === 'aio' ? 'aio-install-cores' : 'agent-install-core')!.click()
      await flushPromises()
      vi.mocked(agentApi.generateScript).mockResolvedValueOnce({ data: `#!/bin/sh\n${AGENT_CORE_INSTALL_INTERFACE}\necho installed` } as any)
    }
    await click('生成安装命令')
    const output = () => document.querySelector('[aria-label="一键安装命令"] textarea') as HTMLTextAreaElement
    const curlCommand = output().value
    const url = commandUrl(curlCommand)
    const builder = includeCore ? buildAgentCoreInstallCommand : buildAgentInstallCommand
    expect(curlCommand).toBe(builder(url, 'curl'))
    expect(document.querySelector('[aria-label="安装命令下载方式"] [value="curl"]')?.getAttribute('aria-checked')).toBe('true')
    expect(document.body.textContent).toContain('以 root 执行')
    expect(document.querySelectorAll('[aria-label="一键安装命令"] textarea')).toHaveLength(1)
    vi.mocked(Element.prototype.scrollIntoView).mockClear()

    await radio('wget')
    expect(output().value).toBe(builder(url, 'wget'))
    expect(output().value).toContain(' && sh ')
    expect(output().value).not.toMatch(/\|\s*(?:sudo\s+)?(?:ba)?sh/)
    expect(agentApi.generateScript).toHaveBeenCalledTimes(1)
    await radio('curl')
    expect(output().value).toBe(curlCommand)
    expect(agentApi.generateScript).toHaveBeenCalledTimes(1)
    expect(Element.prototype.scrollIntoView).not.toHaveBeenCalled()
    await input('agent-name', 'another-agent')
    expect(document.querySelector('[aria-label="一键安装命令"]')).toBeNull()
  })

  it('switches the independent core downloader while retaining its selected core and preview', async () => {
    await radio('aio')
    await expand('仅安装核心')
    await radio('oxidns')
    await click('生成OxiDNS的安装命令')
    const output = () => document.getElementById('core-install-command') as HTMLTextAreaElement
    const curlCommand = output().value
    const url = commandUrl(curlCommand)
    expect(curlCommand).toBe(buildCoreInstallCommand(url, 'oxidns', 'curl'))
    await radio('wget')
    expect(output().value).toBe(buildCoreInstallCommand(url, 'oxidns', 'wget'))
    expect(agentApi.generateCoreInstallScript).toHaveBeenCalledTimes(1)
    await click('查看核心安装脚本')
    expect((document.querySelector('[aria-label="OxiDNS的独立安装脚本"]') as HTMLTextAreaElement).value).toContain('cores-installed')
    await click('关闭')
    await click('生成安装脚本')
    await radio('aio')
    await expand('仅安装核心')
    await click('生成OxiDNS的安装命令')
    expect(output().value).toBe(curlCommand)
    expect(document.querySelector('[aria-label="核心安装命令下载方式"] [value="curl"]')?.getAttribute('aria-checked')).toBe('true')
  })

  it.each(['javascript:alert(1)', 'https://config.example.test/?token=abc', 'https://user:pass@config.example.test'])('rejects an invalid Agent-only installation URL: %s', async url => {
    localStorage.setItem('serverDomain', url)
    await click('生成安装命令')
    expect(agentApi.generateScript).not.toHaveBeenCalled()
    expect(notify.error).toHaveBeenCalledWith(expect.stringContaining('HTTP / HTTPS'))
    expect(document.querySelector('textarea')).toBeNull()
  })
})

describe('independent Mihomo + OxiDNS core installation', () => {
  it.each([{ target: 'mihomo', name: 'Mihomo' }, { target: 'oxidns', name: 'OxiDNS' }])('generates an independent command for only $name', async ({ target, name }) => {
    await input('agent-name', '')
    await radio('aio')
    await expand('仅安装核心')
    await radio(target)
    await click(`生成${name}的安装命令`)
    const command = (document.getElementById('core-install-command') as HTMLTextAreaElement).value
    expect(command).toContain(`'${target}'`)
    expect(command).toContain('--core')
    expect(agentApi.generateCoreInstallScript).toHaveBeenCalledExactlyOnceWith()
    expect(agentApi.generateScript).not.toHaveBeenCalled()
  })

  it('clears commands and ignores pending results after changing the core selection', async () => {
    await radio('aio')
    await expand('仅安装核心')
    await click('生成两个核心的安装命令')
    await radio('mihomo')
    expect(document.getElementById('core-install-command')).toBeNull()
    let finish!: (value: any) => void
    vi.mocked(agentApi.generateCoreInstallScript).mockReturnValueOnce(new Promise(resolve => { finish = resolve }))
    await click('生成Mihomo的安装命令')
    await radio('oxidns')
    finish({ data: '#!/bin/sh\necho stale' })
    await flushPromises()
    expect(document.getElementById('core-install-command')).toBeNull()
    await click('生成OxiDNS的安装命令')
    expect((document.getElementById('core-install-command') as HTMLTextAreaElement).value).toContain("'oxidns'")
  })

  it('generates the core command with an empty Agent name and default layout, independently of Agent settings', async () => {
    await input('agent-name', '')
    await radio('aio')
    expect(document.querySelector('[aria-label="要安装的核心"]')).toBeNull()
    await expand('仅安装核心')
    await click('核心服务高级配置')
    await input('aio-mihomo-config-path', '/opt/custom/mihomo.yaml')
    await input('oxidns-agent-port', '8080')
    await click('生成两个核心的安装命令')
    expect(agentApi.generateCoreInstallScript).toHaveBeenCalledExactlyOnceWith()
    expect(agentApi.generateScript).not.toHaveBeenCalled()
    expect(notify.warning).not.toHaveBeenCalled()
    const command = (document.getElementById('core-install-command') as HTMLTextAreaElement).value
    expect(command).toContain("'https://config.example.test/api/agents/core-install-script'")
    expect(command).not.toMatch(/[\r\n]/)
    expect(command).toContain('curl -fSL ')
    expect(command).toContain(' && sh ')
    expect(command).not.toContain('sh -c ')
    expect(command).not.toContain('/opt/custom')
    expect(command).not.toContain('agent_name')
    expect(document.body.textContent).toContain('生成安装命令')
  })

  it('preserves Agent output while generating a separate core command and script preview', async () => {
    await radio('aio')
    await expand('仅安装核心')
    await click('生成安装命令')
    const agentParams = generatedUrlParams()
    await click('生成两个核心的安装命令')
    expect(generatedUrlParams()).toEqual(agentParams)
    expect(agentApi.generateScript).toHaveBeenCalledTimes(1)
    await click('查看核心安装脚本')
    expect((document.querySelector('[aria-label="两个核心的独立安装脚本"]') as HTMLTextAreaElement).value).toContain('cores-installed')
    expect(document.getElementById('core-install-command')).not.toBeNull()
  })

  it('clears core output when switching to single-service or Docker modes', async () => {
    await radio('aio')
    await expand('仅安装核心')
    await click('生成两个核心的安装命令')
    await radio('single')
    expect(document.getElementById('core-install-command')).toBeNull()
    await radio('aio')
    await expand('仅安装核心')
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
    await expand('仅安装核心')
    await click('生成两个核心的安装命令')
    expect(document.body.textContent).toContain('正在生成核心安装命令…')
    await radio('single')
    await radio('aio')
    await expand('仅安装核心')
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
    await expand('仅安装核心')
    await click('生成两个核心的安装命令')
    await click('关闭')
    fail(new Error('stale error'))
    await flushPromises()
    expect(notify.error).not.toHaveBeenCalled()
    await click('生成安装脚本')
    await radio('aio')
    await expand('仅安装核心')
    expect(document.getElementById('core-install-command')).toBeNull()
  })

  it('shows request failure and permits retry without affecting Agent generation', async () => {
    vi.mocked(agentApi.generateCoreInstallScript).mockRejectedValueOnce({ response: { data: { message: '核心安装文件尚未就绪' } } })
    await radio('aio')
    await expand('仅安装核心')
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
    await expand('仅安装核心')
    await click('生成两个核心的安装命令')
    expect(agentApi.generateCoreInstallScript).not.toHaveBeenCalled()
    expect(notify.error).toHaveBeenCalledWith(expect.stringContaining('HTTP / HTTPS'))
    expect(document.getElementById('core-install-command')).toBeNull()
  })

  it('does not present commands if an older server returns HTML instead of the script', async () => {
    vi.mocked(agentApi.generateCoreInstallScript).mockResolvedValueOnce({ data: '<!doctype html><title>ConfigFlow</title>' } as any)
    await radio('aio')
    await expand('仅安装核心')
    await click('生成两个核心的安装命令')
    expect(notify.error).toHaveBeenCalledWith('服务未返回有效的核心安装脚本，请确认 ConfigFlow 已更新')
    expect(document.getElementById('core-install-command')).toBeNull()
  })

  it.each(['#!/bin/sh\necho old-installer', '#!/bin/sh\n'])('rejects an older or incomplete installer before showing a selected-core command', async script => {
    vi.mocked(agentApi.generateCoreInstallScript).mockResolvedValueOnce({ data: script } as any)
    await radio('aio')
    await expand('仅安装核心')
    await radio('oxidns')
    await click('生成OxiDNS的安装命令')
    expect(notify.error).toHaveBeenCalledWith('安装脚本版本不支持单核心选择，请更新 ConfigFlow 后重试')
    expect(document.getElementById('core-install-command')).toBeNull()
  })

  it('quotes special URL characters in the generated one-line command', async () => {
    localStorage.setItem('serverDomain', "https://config.example.test/base'$(printf_injected)/")
    await radio('aio')
    await expand('仅安装核心')
    await click('生成两个核心的安装命令')
    const command = (document.getElementById('core-install-command') as HTMLTextAreaElement).value
    expect(command).not.toMatch(/[\r\n]/)
    expect(command).toContain("base'\"'\"'$(printf_injected)/api/agents/core-install-script")
  })
})


describe('single Agent with its corresponding core', () => {
  const combinedScript = `#!/bin/sh\n${AGENT_CORE_INSTALL_INTERFACE}\necho installed`
  async function selectCore() {
    const checkbox = document.getElementById('agent-install-core')!
    expect(checkbox).toBeTruthy()
    checkbox.click()
    await flushPromises()
  }
  function commands() { return [...document.querySelectorAll('textarea')].map(element => element.value) }

  it('defaults to Agent only and hides the option for MosDNS, Docker, and Shell AIO', async () => {
    expect(document.getElementById('agent-install-core')?.getAttribute('aria-checked')).toBe('false')
    await radio('mosdns')
    expect(document.getElementById('agent-install-core')).toBeNull()
    await radio('oxidns')
    expect(document.getElementById('agent-install-core')).not.toBeNull()
    await selectCore()
    await radio('aio')
    expect(document.getElementById('agent-install-core')).toBeNull()
    await radio('single')
    expect(document.getElementById('agent-install-core')?.getAttribute('aria-checked')).toBe('false')
    await radio('docker')
    expect(document.getElementById('agent-install-core')).toBeNull()
  })

  it.each(['mihomo', 'oxidns'])('generates one matching combined script and command for %s', async type => {
    if (type !== 'mihomo') await radio(type)
    await input('agent-port', '18080')
    await selectCore()
    vi.mocked(agentApi.generateScript).mockResolvedValueOnce({ data: combinedScript } as any)
    await click('生成安装命令')
    const expected = {
      name: 'home-proxy', type, port: 18080, agent_ip: '', config_path: `/etc/${type}/config.yaml`,
      restart_command: '', server_url: 'https://config.example.test',
      service_unit: type, service_binary: `/usr/local/bin/${type}`, install_core: true
    }
    expect(agentApi.generateScript).toHaveBeenCalledExactlyOnceWith(expected)
    const params = new URLSearchParams()
    Object.entries(expected).forEach(([key, value]) => { if (value !== '' || key === 'restart_command') params.set(key, String(value)) })
    // Object order is irrelevant to the endpoint, but the command must match the requested values.
    const command = commands()[0]!
    const actualUrl = commandUrl(command)
    expect(Object.fromEntries(new URL(actualUrl).searchParams)).toEqual(Object.fromEntries(params))
    expect(command).toBe(buildAgentCoreInstallCommand(actualUrl))
    expect(commands()).toHaveLength(1)
    expect(document.body.textContent).toContain('Linux（systemd / OpenRC）')
    expect(document.body.textContent).not.toContain('Ubuntu / Debian / CentOS')
    await click('查看完整安装脚本')
    expect(commands()).toContain(combinedScript)
    expect(agentApi.generateCoreInstallScript).not.toHaveBeenCalled()
  })

  it.each(['53', '1053', '18080.5', '65536'])('rejects an invalid or conflicting combined Agent port %s before requesting a script', async port => {
    await selectCore()
    await input('agent-port', port)
    await click('生成安装命令')
    expect(agentApi.generateScript).not.toHaveBeenCalled()
    expect(notify.warning).toHaveBeenCalledWith(port === '1053' ? 'Mihomo DNS 使用 1053，请为 Agent 选择其他端口' : 'Agent 端口应为 1024 到 65535 之间的整数')
    expect(commands()).toEqual([])
  })

  it('shows locked defaults while preserving and restoring custom service settings', async () => {
    await expand('服务管理高级配置（可选）')
    await input('agent-config-path', '/opt/custom/main.yaml')
    await input('agent-restart', '/opt/custom/control restart')
    await input('agent-service-unit', 'custom-core')
    await input('agent-service-binary', '/opt/custom/mihomo')
    await input('agent-service-stop', '/opt/custom/control stop')
    await input('agent-service-start', '/opt/custom/control start')
    await input('agent-service-status', '/opt/custom/control status')
    await selectCore()
    for (const [id, value] of Object.entries({ 'agent-config-path': '/etc/mihomo/config.yaml', 'agent-restart': '', 'agent-service-unit': 'mihomo', 'agent-service-binary': '/usr/local/bin/mihomo' })) {
      const element = document.getElementById(id) as HTMLInputElement
      expect(element.value).toBe(value)
      expect(element.disabled).toBe(true)
    }
    expect(document.getElementById('agent-service-stop')).toBeNull()
    vi.mocked(agentApi.generateScript).mockResolvedValueOnce({ data: combinedScript } as any)
    await click('生成安装命令')
    expect(vi.mocked(agentApi.generateScript).mock.calls[0]![0]).not.toHaveProperty('stop_command')
    await selectCore()
    expect(commands()).toEqual([])
    expect((document.getElementById('agent-config-path') as HTMLInputElement).value).toBe('/opt/custom/main.yaml')
    expect((document.getElementById('agent-config-path') as HTMLInputElement).disabled).toBe(false)
    await click('生成安装命令')
    expect(vi.mocked(agentApi.generateScript).mock.calls[1]![0]).toMatchObject({
      config_path: '/opt/custom/main.yaml', restart_command: '/opt/custom/control restart',
      service_unit: 'custom-core', service_binary: '/opt/custom/mihomo', stop_command: '/opt/custom/control stop',
      start_command: '/opt/custom/control start', status_command: '/opt/custom/control status'
    })
    expect(vi.mocked(agentApi.generateScript).mock.calls[1]![0]).not.toHaveProperty('install_core')
  })

  it.each(['#!/bin/sh\necho old-agent', '<html>old server</html>', '#!/bin/sh\n# ConfigFlow core installer interface: 1\n'])('rejects unsupported combined preview %s', async data => {
    await selectCore()
    vi.mocked(agentApi.generateScript).mockResolvedValueOnce({ data } as any)
    await click('生成安装命令')
    expect(commands()).toEqual([])
    expect(notify.error).toHaveBeenCalledWith('安装脚本版本不支持同时安装核心，请更新 ConfigFlow 后重试')
  })

  it.each(['toggle', 'type', 'mode', 'name', 'close'])('discards a pending combined preview after changing %s', async change => {
    await selectCore()
    let finish!: (response: any) => void
    vi.mocked(agentApi.generateScript).mockReturnValueOnce(new Promise(resolve => { finish = resolve }))
    await click('生成安装命令')
    if (change === 'toggle') await selectCore()
    if (change === 'type') await radio('oxidns')
    if (change === 'mode') await radio('aio')
    if (change === 'name') await input('agent-name', 'another-agent')
    if (change === 'close') await click('关闭')
    finish({ data: combinedScript })
    await flushPromises()
    expect(commands()).toEqual([])
    expect(notify.success).not.toHaveBeenCalled()
  })

  it('discards stale errors and clears successful output when the Agent port changes', async () => {
    await selectCore()
    let reject!: (reason: any) => void
    vi.mocked(agentApi.generateScript).mockReturnValueOnce(new Promise((_, fail) => { reject = fail }))
    await click('生成安装命令')
    await input('agent-port', '18081')
    reject(new Error('stale error'))
    await flushPromises()
    expect(notify.error).not.toHaveBeenCalled()
    vi.mocked(agentApi.generateScript).mockResolvedValueOnce({ data: combinedScript } as any)
    await click('生成安装命令')
    expect(commands()).toHaveLength(1)
    await input('agent-port', '18082')
    expect(commands()).toEqual([])
  })
})

describe('Shell AIO with both cores', () => {
  const combinedScript = `#!/bin/sh\n${AGENT_CORE_INSTALL_INTERFACE}\necho installed`
  const defaults = {
    'aio-mihomo-config-path': '/etc/mihomo/config.yaml',
    'aio-oxidns-config-path': '/etc/oxidns/config.yaml',
    'aio-mihomo-service-unit': 'mihomo',
    'aio-oxidns-service-unit': 'oxidns',
    'aio-mihomo-service-binary': '/usr/local/bin/mihomo',
    'aio-oxidns-service-binary': '/usr/local/bin/oxidns'
  }
  async function toggleCores() {
    const checkbox = document.getElementById('aio-install-cores')!
    expect(checkbox).toBeTruthy()
    checkbox.click()
    await flushPromises()
  }
  function commands() { return [...document.querySelectorAll('textarea')].map(element => element.value) }

  it('generates one combined command with both standard layouts and matching preview parameters', async () => {
    await radio('aio')
    await toggleCores()
    await input('mihomo-agent-port', '18080')
    await input('oxidns-agent-port', '18082')
    expect(document.getElementById('core-install-title')).toBeNull()
    vi.mocked(agentApi.generateScript).mockResolvedValueOnce({ data: combinedScript } as any)
    await click('生成安装命令')
    const expected = {
      name: 'home-proxy', type: 'aio', agent_ip: '', server_url: 'https://config.example.test',
      mihomo_agent_port: 18080, oxidns_agent_port: 18082,
      mihomo_config_path: '/etc/mihomo/config.yaml', oxidns_config_path: '/etc/oxidns/config.yaml',
      mihomo_service_unit: 'mihomo', oxidns_service_unit: 'oxidns',
      mihomo_service_binary: '/usr/local/bin/mihomo', oxidns_service_binary: '/usr/local/bin/oxidns',
      install_core: true
    }
    expect(agentApi.generateScript).toHaveBeenCalledExactlyOnceWith(expected)
    expect(commands()).toHaveLength(1)
    const command = commands()[0]!
    const url = commandUrl(command)
    expect(Object.fromEntries(new URL(url).searchParams)).toEqual(Object.fromEntries(
      Object.entries(expected).filter(([, value]) => value !== '').map(([key, value]) => [key, String(value)])
    ))
    expect(command).toBe(buildAgentCoreInstallCommand(url))
    expect(command).not.toMatch(/[\r\n]/)
    expect(document.body.textContent).toContain('Linux（systemd / OpenRC）')
    expect(document.body.textContent).not.toContain('Ubuntu / Debian / CentOS')
    expect(agentApi.generateCoreInstallScript).not.toHaveBeenCalled()
    await click('查看完整安装脚本')
    expect(commands()).toContain(combinedScript)
  })

  it('locks both standard layouts without overwriting custom takeover settings', async () => {
    await radio('aio')
    await click('核心服务高级配置')
    const custom = {
      'aio-mihomo-config-path': '/opt/mihomo/main.yaml', 'aio-oxidns-config-path': '/opt/oxidns/main.yaml',
      'aio-mihomo-service-unit': 'mihomo-native', 'aio-oxidns-service-unit': 'oxidns-native',
      'aio-mihomo-service-binary': '/opt/mihomo/core', 'aio-oxidns-service-binary': '/opt/oxidns/core'
    }
    for (const [id, value] of Object.entries(custom)) await input(id, value)
    await toggleCores()
    for (const [id, value] of Object.entries(defaults)) {
      const element = document.getElementById(id) as HTMLInputElement
      expect(element.value).toBe(value)
      expect(element.disabled).toBe(true)
    }
    vi.mocked(agentApi.generateScript).mockResolvedValueOnce({ data: combinedScript } as any)
    await click('生成安装命令')
    expect(vi.mocked(agentApi.generateScript).mock.calls[0]![0]).toMatchObject({
      install_core: true, mihomo_config_path: '/etc/mihomo/config.yaml', oxidns_config_path: '/etc/oxidns/config.yaml'
    })
    await toggleCores()
    expect(commands()).toEqual([])
    expect(findButton('仅安装核心')).toBeTruthy()
    for (const [id, value] of Object.entries(custom)) {
      const element = document.getElementById(id) as HTMLInputElement
      expect(element.value).toBe(value)
      expect(element.disabled).toBe(false)
    }
    await click('生成安装命令')
    expect(vi.mocked(agentApi.generateScript).mock.calls[1]![0]).toMatchObject({
      mihomo_config_path: '/opt/mihomo/main.yaml', oxidns_config_path: '/opt/oxidns/main.yaml',
      mihomo_service_unit: 'mihomo-native', oxidns_service_unit: 'oxidns-native',
      mihomo_service_binary: '/opt/mihomo/core', oxidns_service_binary: '/opt/oxidns/core'
    })
    expect(vi.mocked(agentApi.generateScript).mock.calls[1]![0]).not.toHaveProperty('install_core')
  })

  it.each([
    ['mihomo-agent-port', '1053'], ['oxidns-agent-port', '1053'],
    ['mihomo-agent-port', '53'], ['oxidns-agent-port', '53'],
    ['mihomo-agent-port', '18080.5'], ['oxidns-agent-port', '65536'],
    ['oxidns-agent-port', '8080']
  ])('rejects conflicting or invalid %s=%s before generation', async (id, value) => {
    await radio('aio')
    await toggleCores()
    await input(id!, value!)
    await click('生成安装命令')
    expect(agentApi.generateScript).not.toHaveBeenCalled()
    const message = value === '1053' ? 'Mihomo DNS 使用 1053，请为 Agent 选择其他端口'
      : value === '8080' ? '两个 Agent 必须使用不同的端口' : 'Agent 端口应为 1024 到 65535 之间的整数'
    expect(notify.warning).toHaveBeenCalledWith(message)
    expect(commands()).toEqual([])
  })

  it.each(['#!/bin/sh\necho old-aio', '#!/bin/sh\n# ConfigFlow core installer interface: 1\n'])('rejects an older non-combined AIO preview %s', async data => {
    await radio('aio')
    await toggleCores()
    vi.mocked(agentApi.generateScript).mockResolvedValueOnce({ data } as any)
    await click('生成安装命令')
    expect(commands()).toEqual([])
    expect(notify.error).toHaveBeenCalledWith('安装脚本版本不支持同时安装核心，请更新 ConfigFlow 后重试')
  })

  it.each(['toggle', 'mode', 'port', 'close'])('discards pending combined output after changing %s', async change => {
    await radio('aio')
    await toggleCores()
    let finish!: (response: any) => void
    vi.mocked(agentApi.generateScript).mockReturnValueOnce(new Promise(resolve => { finish = resolve }))
    await click('生成安装命令')
    if (change === 'toggle') await toggleCores()
    if (change === 'mode') { await radio('single'); await radio('aio') }
    if (change === 'port') await input('oxidns-agent-port', '18082')
    if (change === 'close') await click('关闭')
    finish({ data: combinedScript })
    await flushPromises()
    expect(commands()).toEqual([])
    expect(notify.success).not.toHaveBeenCalled()
  })

  it('clears an independent core command and discards in-flight core requests when combined installation is selected', async () => {
    await radio('aio')
    await expand('仅安装核心')
    await click('生成两个核心的安装命令')
    expect(document.getElementById('core-install-command')).not.toBeNull()
    await toggleCores()
    await toggleCores()
    expect(document.getElementById('core-install-command')).toBeNull()
    let finish!: (response: any) => void
    vi.mocked(agentApi.generateCoreInstallScript).mockReturnValueOnce(new Promise(resolve => { finish = resolve }))
    await expand('仅安装核心')
    await click('生成两个核心的安装命令')
    await toggleCores()
    vi.mocked(notify.success).mockClear()
    finish({ data: '#!/bin/sh\n# ConfigFlow core installer interface: 1\necho stale' })
    await flushPromises()
    expect(notify.success).not.toHaveBeenCalled()
    await toggleCores()
    expect(document.getElementById('core-install-command')).toBeNull()
    expect(document.body.textContent).not.toContain('正在生成核心安装命令…')
  })

  it('resets the opt-in when switching modes and leaves Docker deployment unchanged', async () => {
    await radio('aio')
    await toggleCores()
    await radio('single')
    expect(document.getElementById('agent-install-core')?.getAttribute('aria-checked')).toBe('false')
    await radio('aio')
    expect(document.getElementById('aio-install-cores')?.getAttribute('aria-checked')).toBe('false')
    await toggleCores()
    await radio('docker')
    expect(document.getElementById('aio-install-cores')).toBeNull()
    await radio('aio')
    await click('生成 Docker 部署命令')
    expect(api.get).toHaveBeenCalledWith('/agents/docker-agent-compose', { params: expect.objectContaining({
      enable_mihomo: true, enable_mosdns: false, enable_oxidns: true
    }) })
    expect(vi.mocked(api.get).mock.calls[0]![1]!.params).not.toHaveProperty('install_core')
    expect(agentApi.generateScript).not.toHaveBeenCalled()
    await radio('shell')
    expect(document.getElementById('aio-install-cores')?.getAttribute('aria-checked')).toBe('false')
  })
})
