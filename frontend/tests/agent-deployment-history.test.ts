import { afterEach, describe, expect, it } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import AgentDeploymentHistory from '@/components/agents/AgentDeploymentHistory.vue'
import type { Agent } from '@/types'

const wrappers: ReturnType<typeof mount>[] = []
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); document.body.innerHTML = '' })
const old = { deployment_id: 'old-success', status: 'succeeded', updated_at: '2026-10-15T08:00:00Z' }
const latest = { deployment_id: 'current-success', status: 'succeeded', updated_at: '2026-10-16T08:00:00Z' }
const agent = { id: 'oxi', name: 'OxiDNS test', status: 'online', service_type: 'oxidns', latest_deployment: latest, deployments: { old, latest } } as unknown as Agent
async function render(props: Record<string, unknown> = {}) {
  const wrapper = mount(AgentDeploymentHistory, { attachTo: document.body, props: { agent, busy: false, blocked: false, ...props } })
  wrappers.push(wrapper)
  await flushPromises()
  return wrapper
}

describe('Agent deployment history rollback availability', () => {
  it('offers only the latest successful record and identifies the rollback scope', async () => {
    const wrapper = await render()
    const buttons = [...document.querySelectorAll('button')].filter(button => button.textContent === '回滚到发布前')
    expect(buttons).toHaveLength(1)
    expect(buttons[0].closest('tr')?.textContent).toContain('current-success')
    expect(document.body.textContent).toContain('state/ 运行数据保留')
    buttons[0].click()
    expect(wrapper.emitted('rollback')?.[0]).toEqual([latest])
  })
  it.each([{ busy: true }, { blocked: true }, { agent: { ...agent, status: 'offline' } }])('disables rollback when unavailable: %j', async props => {
    await render(props)
    const button = [...document.querySelectorAll('button')].find(button => button.textContent === '回滚到发布前')!
    expect(button.disabled).toBe(true)
  })
  it('shows completed manual rollback as success without offering it again', async () => {
    await render({ progress: { ...latest, status: 'rolled_back', manual_rollback: true } })
    expect(document.body.textContent).toContain('手动回滚成功')
    expect(document.body.textContent).not.toContain('回滚到发布前')
    expect(document.body.textContent).not.toContain('发布失败，已回滚')
  })
})

describe('Agent deployment history diagnostics', () => {
  it('keeps DNS warnings and their fixes visible in successful release history', async () => {
    await render({ progress: { ...latest, dns_checks: [{ name: 'DNS 劫持', level: 'warn', message: 'any:53 可能绕过 OxiDNS', fix: '检查劫持规则' }] } })
    const report = document.querySelector('[aria-label="DNS 检查结果"]')!
    expect(report.textContent).toContain('警告 · DNS 劫持')
    expect(report.textContent).toContain('修复建议：检查劫持规则')
  })
  it('renders persisted publication and rollback diagnostics independently', async () => {
    await render({ progress: { ...latest, status: 'rollback_failed', error: '配置校验失败', failed_stage: 'verifying', error_details: 'line 17: missing group', rollback_error: '服务恢复失败', rollback_error_details: 'failed to bind :53' } })
    expect(document.body.textContent).toContain('失败步骤：校验中')
    expect([...document.querySelectorAll('pre')].map(element => element.textContent)).toEqual(['line 17: missing group', 'failed to bind :53'])
    expect([...document.querySelectorAll('details')].every(element => !element.open)).toBe(true)
  })
})
