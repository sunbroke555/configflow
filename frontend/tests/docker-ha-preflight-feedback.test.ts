import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import DockerHAPreflightFeedback from '@/components/agents/DockerHAPreflightFeedback.vue'
import { agentApi } from '@/api'
import type { DockerHAPreflightReport } from '@/types/dockerHAGuide'

vi.mock('@/api', () => ({ agentApi: { generateDockerHAGuide: vi.fn() } }))
const wrappers: ReturnType<typeof mount>[] = []
function report(overrides: Partial<DockerHAPreflightReport> = {}): DockerHAPreflightReport {
  return { status: 'warning', summary: 'Docker 可用，需确认维护工具', checks: [
    { id: 'docker', label: 'Docker 引擎', status: 'pass', detail: 'rootful Linux daemon 可用' },
    { id: 'compose', label: 'Compose', status: 'warning', detail: '未安装 Compose', remedy: '可安装 Compose 2.30+ 供后续维护；自动安装使用 Docker Run。' },
  ], ...overrides }
}
function render() {
  const wrapper = mount(DockerHAPreflightFeedback, { props: { profileId: 'fixed-space', network: 'host' } })
  wrappers.push(wrapper)
  return wrapper
}
const response = (value = report()) => ({ data: { success: true, preflight_report: value } }) as any
const input = (wrapper: ReturnType<typeof render>, index: number) => wrapper.get(`#docker-ha-preflight-output-${index}`)
function button(wrapper: ReturnType<typeof render>, index: number) {
  return wrapper.findAll('button').find(item => item.text() === `检测主机 ${index}`)!
}
async function run(wrapper: ReturnType<typeof render>, index: number) {
  await button(wrapper, index).trigger('click')
  await flushPromises()
}
function deferred() {
  let resolve!: (value: any) => void
  let reject!: (error: any) => void
  const promise = new Promise<any>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}
beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(agentApi.generateDockerHAGuide).mockResolvedValue(response())
})
afterEach(() => wrappers.splice(0).forEach(wrapper => wrapper.unmount()))

describe('Docker HA pasted preflight feedback', () => {
  it('is optional, does not submit while typing, and disables empty inputs independently', async () => {
    const wrapper = render()
    expect(wrapper.text()).toContain('可选')
    expect(wrapper.text()).toContain('不能替代安装后的 Agent、容器权限、VIP 与 DNS 配置检查')
    expect(button(wrapper, 1).attributes('disabled')).toBeDefined()
    expect(button(wrapper, 2).attributes('disabled')).toBeDefined()
    await input(wrapper, 1).setValue('   ')
    expect(button(wrapper, 1).attributes('disabled')).toBeDefined()
    await input(wrapper, 1).setValue('host-a-output')
    expect(button(wrapper, 1).attributes('disabled')).toBeUndefined()
    expect(button(wrapper, 2).attributes('disabled')).toBeDefined()
    expect(agentApi.generateDockerHAGuide).not.toHaveBeenCalled()
  })

  it('checks each host separately with the fixed profile and shows detailed remedies', async () => {
    const wrapper = render()
    await input(wrapper, 1).setValue('host-a-output')
    await input(wrapper, 2).setValue('host-b-output')
    await run(wrapper, 1)
    expect(agentApi.generateDockerHAGuide).toHaveBeenLastCalledWith({ action: 'check', output: 'host-a-output', network_mode: 'host' }, 'fixed-space')
    expect(wrapper.find('[data-preflight-report="1"]').exists()).toBe(true)
    expect(wrapper.find('[data-preflight-report="2"]').exists()).toBe(false)
    expect(wrapper.get('[data-preflight-check="docker"]').text()).toContain('通过')
    expect(wrapper.get('[data-preflight-check="compose"]').text()).toContain('需注意')
    expect(wrapper.get('[data-preflight-check="compose"]').text()).toContain('处理办法：可安装 Compose 2.30+')
    await run(wrapper, 2)
    expect(agentApi.generateDockerHAGuide).toHaveBeenLastCalledWith({ action: 'check', output: 'host-b-output', network_mode: 'host' }, 'fixed-space')
    expect(wrapper.find('[data-preflight-report="2"]').exists()).toBe(true)
  })

  it('preserves both inputs on a server error and allows retry without reporting success', async () => {
    vi.mocked(agentApi.generateDockerHAGuide).mockRejectedValueOnce({ response: { data: { message: '请求输出超过限制，请重新采集' } } })
    const wrapper = render()
    await input(wrapper, 1).setValue('unchanged-output')
    await input(wrapper, 2).setValue('independent-output')
    await run(wrapper, 1)
    expect(wrapper.get('[role="alert"]').text()).toContain('请求输出超过限制')
    expect(wrapper.find('[data-preflight-report="1"]').exists()).toBe(false)
    expect((input(wrapper, 1).element as HTMLTextAreaElement).value).toBe('unchanged-output')
    expect((input(wrapper, 2).element as HTMLTextAreaElement).value).toBe('independent-output')
    await run(wrapper, 1)
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    expect(wrapper.find('[data-preflight-report="1"]').exists()).toBe(true)
  })

  it('invalidates only the edited host report while keeping the other host result', async () => {
    const wrapper = render()
    await input(wrapper, 1).setValue('first')
    await input(wrapper, 2).setValue('second')
    await run(wrapper, 1)
    await run(wrapper, 2)
    await input(wrapper, 1).setValue('edited')
    expect(wrapper.find('[data-preflight-report="1"]').exists()).toBe(false)
    expect(wrapper.find('[data-preflight-report="2"]').exists()).toBe(true)
  })

  it('clears both results on network changes and checks the retained output against the new mode', async () => {
    const wrapper = render()
    await input(wrapper, 1).setValue('first')
    await input(wrapper, 2).setValue('second')
    await run(wrapper, 1)
    await run(wrapper, 2)
    await wrapper.setProps({ network: 'macvlan' })
    expect(wrapper.findAll('[data-preflight-report]')).toHaveLength(0)
    expect((input(wrapper, 1).element as HTMLTextAreaElement).value).toBe('first')
    await run(wrapper, 1)
    expect(agentApi.generateDockerHAGuide).toHaveBeenLastCalledWith({ action: 'check', output: 'first', network_mode: 'macvlan' }, 'fixed-space')
  })

  it('ignores a stale success after a network change during a request', async () => {
    const pending = deferred()
    vi.mocked(agentApi.generateDockerHAGuide).mockReturnValueOnce(pending.promise)
    const wrapper = render()
    await input(wrapper, 1).setValue('output')
    await run(wrapper, 1)
    expect(button(wrapper, 1).attributes('disabled')).toBeDefined()
    await wrapper.setProps({ network: 'macvlan' })
    pending.resolve(response(report({ summary: 'obsolete host result' })))
    await flushPromises()
    expect(wrapper.text()).not.toContain('obsolete host result')
    expect(wrapper.find('[data-preflight-report="1"]').exists()).toBe(false)
    expect(button(wrapper, 1).attributes('disabled')).toBeUndefined()
  })

  it('ignores a stale failure after editing and completing a newer request', async () => {
    const pending = deferred()
    vi.mocked(agentApi.generateDockerHAGuide).mockReturnValueOnce(pending.promise)
    const wrapper = render()
    await input(wrapper, 1).setValue('old')
    await run(wrapper, 1)
    await input(wrapper, 1).setValue('new')
    await run(wrapper, 1)
    pending.reject(new Error('obsolete request failed'))
    await flushPromises()
    expect(wrapper.text()).not.toContain('obsolete request failed')
    expect(wrapper.find('[data-preflight-report="1"]').exists()).toBe(true)
  })

  it('warns when identical text is pasted twice without claiming two-host verification', async () => {
    const wrapper = render()
    await input(wrapper, 1).setValue('same output')
    await input(wrapper, 2).setValue('same output')
    expect(wrapper.get('[role="alert"]').text()).toContain('两份检查输出相同')
    expect(wrapper.text()).toContain('不能据此确认两台独立主机')
    await input(wrapper, 2).setValue('different output')
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
  })

  it('warns for matching host fingerprints even when both reports pass', async () => {
    vi.mocked(agentApi.generateDockerHAGuide).mockResolvedValue(response(report({ status: 'pass', host_fingerprint: 'same-host' })))
    const wrapper = render()
    await input(wrapper, 1).setValue('first sample')
    await input(wrapper, 2).setValue('second sample')
    await run(wrapper, 1)
    await run(wrapper, 2)
    expect(wrapper.get('[role="alert"]').text()).toContain('宿主指纹相同')
    await input(wrapper, 2).setValue('fresh sample')
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
  })

  it('renders legacy or truncated output as unknown with the backend remedy', async () => {
    vi.mocked(agentApi.generateDockerHAGuide).mockResolvedValue(response(report({ status: 'unknown', summary: '未识别到完整新版检查块', checks: [
      { id: 'format', label: '输出完整性', status: 'unknown', detail: '旧版或截断输出', remedy: '重新复制本页新版命令并粘贴完整输出。' },
    ] })))
    const wrapper = render()
    await input(wrapper, 1).setValue('Docker version 28')
    await run(wrapper, 1)
    expect(wrapper.get('[data-preflight-report="1"]').text()).toContain('无法判断')
    expect(wrapper.text()).toContain('重新复制本页新版命令并粘贴完整输出')
  })

  it.each(['toString', 'pass'])('rejects an empty or unrecognized %s report instead of showing a pass', async status => {
    vi.mocked(agentApi.generateDockerHAGuide).mockResolvedValue({ data: { success: true, preflight_report: { status, summary: 'bad report', checks: [] } } } as any)
    const wrapper = render()
    await input(wrapper, 1).setValue('output')
    await run(wrapper, 1)
    expect(wrapper.get('[role="alert"]').text()).toContain('未收到完整检测结果')
    expect(wrapper.find('[data-preflight-report="1"]').exists()).toBe(false)
  })

  it('renders diagnostic content as text, including long paths and HTML-like input', async () => {
    const hostile = '<img src=x onerror="alert(1)">' + '/very-long-path'.repeat(30)
    vi.mocked(agentApi.generateDockerHAGuide).mockResolvedValue(response(report({ status: 'fail', summary: hostile, checks: [
      { id: 'port', label: 'UDP 53', status: 'fail', detail: hostile, remedy: '<script>unsafe()</script>' },
    ] })))
    const wrapper = render()
    await input(wrapper, 1).setValue(hostile)
    await run(wrapper, 1)
    expect(wrapper.get('[data-preflight-report="1"]').text()).toContain(hostile)
    expect(wrapper.find('img').exists()).toBe(false)
    expect(wrapper.find('script').exists()).toBe(false)
    expect(wrapper.get('[data-preflight-check="port"]').text()).toContain('未通过')
  })

  it('rejects more than 96 KiB of UTF-8 without submitting and retains the draft', async () => {
    const wrapper = render()
    const large = '检'.repeat(33000)
    await input(wrapper, 1).setValue(large)
    await run(wrapper, 1)
    expect(agentApi.generateDockerHAGuide).not.toHaveBeenCalled()
    expect(wrapper.get('[role="alert"]').text()).toContain('超过 96 KiB')
    expect((input(wrapper, 1).element as HTMLTextAreaElement).value).toBe(large)
  })
})
