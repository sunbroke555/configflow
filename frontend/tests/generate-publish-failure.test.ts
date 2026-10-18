import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { computed, defineComponent, markRaw } from 'vue'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import GenerateStudio from '@/components/generate/GenerateStudio.vue'
import { agentApi, generateApi, statsApi, ruleSetApi } from '@/api'
import { notify } from '@/lib/feedback'
import { ruleLocationDetails } from './fixtures/agent-failure'

const live = vi.hoisted(() => ({ agents: [] as any[], start: vi.fn(), stop: vi.fn(), refresh: vi.fn() }))
vi.mock('@/api', () => ({
  agentApi: { pushConfig: vi.fn(), getDeployment: vi.fn() },
  generateApi: { previewMihomo: vi.fn(), previewMosdns: vi.fn(), previewOxidns: vi.fn(), previewSurge: vi.fn(), previewLoon: vi.fn() },
  statsApi: { getOverview: vi.fn() }, ruleSetApi: { getAll: vi.fn() },
}))
vi.mock('@/stores/live', () => ({ useLive: () => ({ ...live, boundAgents: computed(() => live.agents), isOnline: () => true }) }))
vi.mock('@/lib/feedback', () => ({ notify: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn(), loading: vi.fn(), dismiss: vi.fn() } }))
const wrappers: VueWrapper[] = []
const labels = { mihomo: 'Mihomo', mosdns: 'MosDNS', oxidns: 'OxiDNS', surge: 'Surge' } as const
const icon = markRaw(defineComponent({ template: '<i />' }))
const button = (w: VueWrapper, label: string) => w.findAll('button').find(item => item.text() === label)!
beforeEach(() => {
  vi.resetAllMocks()
  vi.useFakeTimers()
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => setTimeout(() => cb(1), 1))
  for (const api of Object.values(generateApi)) vi.mocked(api).mockResolvedValue({ data: { content: 'rules: []' } } as any)
  vi.mocked(statsApi.getOverview).mockResolvedValue({ data: { success: true, data: {} } } as any)
  vi.mocked(ruleSetApi.getAll).mockResolvedValue({ data: [] } as any)
})
afterEach(() => { wrappers.splice(0).forEach(w => w.unmount()); vi.useRealTimers(); vi.unstubAllGlobals() })
async function render(service: keyof typeof labels) {
  live.agents = [{ id: service, name: `${labels[service]} test`, service_type: service }]
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: icon }, { path: '/agents', component: icon }] })
  await router.push('/')
  const targets = Object.entries(labels).map(([key, title]) => ({ key: key as keyof typeof labels, title, icon, desc: '', url: '', urlDisplay: '', actions: [] }))
  const wrapper = mount(GenerateStudio, { props: { targets }, global: { plugins: [router] } })
  wrappers.push(wrapper)
  if (service !== 'mihomo') await button(wrapper, labels[service]).trigger('click')
  await button(wrapper, '生成').trigger('click')
  await vi.advanceTimersByTimeAsync(3000)
  await flushPromises()
  expect(button(wrapper, '推送').attributes('disabled')).toBeUndefined()
  await button(wrapper, '推送').trigger('click')
  await flushPromises()
  return wrapper
}

describe('Generated config publication diagnostics', () => {
  it('shows real preparation objects while POST is pending and does not regress to its older response', async () => {
    let resolvePost!: (value: any) => void
    let requestId = ''
    vi.mocked(agentApi.pushConfig).mockImplementationOnce((_agent, deploymentId) => {
      requestId = deploymentId!
      return new Promise(resolve => { resolvePost = resolve })
    })
    const events = [{ at: '2026-10-18T12:00:00Z', status: 'preparing', message: '正在下载规则集「海外服务」' }]
    vi.mocked(agentApi.getDeployment).mockImplementation(async (_id, deploymentId) => ({ data: {
      deployment_id: deploymentId, status: 'preparing', message: events[0].message, events,
    } }) as any)
    const wrapper = await render('mihomo')
    expect(wrapper.text()).toContain('正在提交发布请求')
    await vi.advanceTimersByTimeAsync(0)
    await flushPromises()
    const progress = wrapper.get('[aria-label="配置发布结果"]')
    expect(progress.get('[role="status"]').text()).toContain('正在下载规则集「海外服务」')
    expect(progress.findAll('li')).toHaveLength(1)
    expect(notify.loading).not.toHaveBeenCalled()
    resolvePost({ data: { deployment_id: requestId, status: 'preparing', message: '请求已创建' } })
    await flushPromises()
    expect(progress.get('[role="status"]').text()).toContain('正在下载规则集「海外服务」')
    expect(agentApi.pushConfig).toHaveBeenCalledOnce()
  })
  it('retains DNS warnings beside the push controls after a successful publication', async () => {
    vi.mocked(agentApi.pushConfig).mockImplementationOnce(async (_agent, deploymentId) => ({ data: { deployment_id: deploymentId, status: 'succeeded', dns_checks: [{ name: 'DNS 解析', level: 'warn', message: '当前 DNS 查询超时', fix: '检查 DNS 上游' }] } }) as any)
    const wrapper = await render('mihomo')
    const result = wrapper.get('[aria-label="配置发布结果"]')
    expect(result.text()).toContain('成功')
    expect(result.text()).toContain('警告 · DNS 解析')
    expect(result.text()).toContain('修复建议：检查 DNS 上游')
    expect(notify.warning).toHaveBeenCalledExactlyOnceWith('配置已发布，DNS 检查有提醒', '当前 DNS 查询超时')
    expect(notify.success).not.toHaveBeenCalledWith('配置发布成功，服务检查已通过')
    expect(notify.success).not.toHaveBeenCalledWith('已推送到 1 台 Agent')
  })
  it.each(['mihomo', 'mosdns', 'oxidns'] as const)('shows %s terminal task details by the push controls', async service => {
    vi.mocked(agentApi.pushConfig).mockImplementationOnce(async (_agent, deploymentId) => ({ data: { deployment_id: deploymentId, status: 'failed', failed_stage: 'verifying', error: '配置校验失败', error_details: ruleLocationDetails } }) as any)
    const wrapper = await render(service)
    expect(wrapper.get('[aria-label="配置发布结果"]').text()).toContain('失败步骤：校验中')
    expect(wrapper.get('pre[tabindex="0"]').element.textContent).toBe(ruleLocationDetails)
    expect(wrapper.get('a[href="/agents"]').text()).toContain('查看发布记录')
    expect(notify.error).toHaveBeenCalledWith('发布失败', '配置校验失败')
  })
  it('does not treat Surge config-write success as restart success', async () => {
    vi.mocked(agentApi.pushConfig).mockResolvedValueOnce({ data: { success: true, restart: { success: false, message: '重启失败', error_details: 'line 17: invalid policy' } } } as any)
    const wrapper = await render('surge')
    expect(wrapper.get('[aria-label="配置发布结果"]').text()).toContain('配置已写入，但重启失败')
    expect(wrapper.get('pre[tabindex="0"]').text()).toBe('line 17: invalid policy')
    expect(notify.success).not.toHaveBeenCalledWith('已推送到 1 台 Agent')
  })
  it.each(['mihomo', 'mosdns', 'oxidns', 'surge'] as const)('preserves a %s HTTP rejection even when no deployment id was assigned', async service => {
    vi.mocked(agentApi.pushConfig).mockRejectedValueOnce({ response: { status: 400, data: { message: '生成失败', error_details: ruleLocationDetails } } })
    const wrapper = await render(service)
    expect(wrapper.get('pre[tabindex="0"]').element.textContent).toBe(ruleLocationDetails)
    expect(wrapper.get('[aria-label="配置发布结果"]').text()).toContain('生成失败')
  })
})
