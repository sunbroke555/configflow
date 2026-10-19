import { afterEach, describe, expect, it, vi } from 'vitest'
import { h } from 'vue'
import { mount } from '@vue/test-utils'
import AgentStatusRow from '@/components/agents/AgentStatusRow.vue'
import AgentUpgradeStatus from '@/components/agents/AgentUpgradeStatus.vue'
import AgentDeploymentStatus from '@/components/agents/AgentDeploymentStatus.vue'
import type { AgentDeployment } from '@/composables/useAgentDeployments'
import type { AgentUpgrade } from '@/composables/useAgentUpgrades'

const wrappers: ReturnType<typeof mount>[] = []
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  document.body.innerHTML = ''
})
function keep<T extends ReturnType<typeof mount>>(wrapper: T): T {
  wrappers.push(wrapper)
  return wrapper
}
const deployment = (overrides: Partial<AgentDeployment> = {}): AgentDeployment => ({ deployment_id: 'deployment-one', status: 'succeeded', ...overrides })
const upgrade = (overrides: Partial<AgentUpgrade> = {}): AgentUpgrade => ({ update_id: 'update-one', target_version: '1.8.24-go', previous_version: '1.8.23-go', status: 'succeeded', ...overrides })

describe('Agent status disclosure', () => {
  it('starts collapsed and exposes a native keyboard-accessible button with the matching controlled region', async () => {
    const wrapper = keep(mount(AgentStatusRow, { attachTo: document.body, props: { label: '配置推送', status: '成功', tone: 'success' }, slots: { default: '<p>配置校验、重载和服务检查均通过。</p>' } }))
    const button = wrapper.get('button')
    const details = wrapper.get('[data-status-details]')
    expect(button.element.tagName).toBe('BUTTON')
    expect(button.attributes('type')).toBe('button')
    expect(button.element.tabIndex).toBe(0)
    expect(button.attributes('aria-controls')).toBe(details.attributes('id'))
    expect(button.attributes('aria-expanded')).toBe('false')
    expect(button.attributes('aria-label')).toBe('配置推送：成功')
    expect(details.isVisible()).toBe(false)
    await button.trigger('click')
    expect(button.attributes('aria-expanded')).toBe('true')
    expect(details.isVisible()).toBe(true)
    await button.trigger('click')
    expect(button.attributes('aria-expanded')).toBe('false')
    expect(details.isVisible()).toBe(false)
  })

  it('keeps warnings and actionable controls visible while successful history stays collapsed', async () => {
    const retry = vi.fn()
    const wrapper = keep(mount(AgentStatusRow, { attachTo: document.body,
      props: { label: 'DNS 联动', status: '待确认', tone: 'warning' },
      slots: {
        notice: () => h('div', [h('p', '无法取得最新状态，请重试查询。'), h('button', { type: 'button', onClick: retry }, '重新查询')]),
        default: '<p>上一次同步已完成。</p>',
      },
    }))
    expect(wrapper.get('[data-status-notice]').isVisible()).toBe(true)
    expect(wrapper.get('[data-status-notice]').text()).toContain('无法取得最新状态')
    expect(wrapper.get('[data-status-details]').isVisible()).toBe(false)
    await wrapper.get('[data-status-notice] button').trigger('click')
    expect(retry).toHaveBeenCalledOnce()
    expect(wrapper.get('button[aria-expanded]').attributes('aria-expanded')).toBe('false')
  })

  it('does not expose an empty disclosure when there are no details', () => {
    const wrapper = keep(mount(AgentStatusRow, { attachTo: document.body, props: { label: 'DNS 联动', status: '未绑定', tone: 'neutral' } }))
    expect(wrapper.find('button').exists()).toBe(false)
    expect(wrapper.find('[aria-expanded], [data-status-details]').exists()).toBe(false)
    expect(wrapper.text()).toBe('DNS 联动未绑定')
  })

  it('announces completion even when the busy notice disappears and details remain collapsed', async () => {
    const wrapper = keep(mount(AgentStatusRow, { props: { label: 'Agent 更新', status: '下载更新', tone: 'progress' }, slots: { default: '<p>更新步骤</p>' } }))
    const result = wrapper.get('[aria-live="polite"]')
    expect(result.attributes('aria-atomic')).toBe('true')
    await wrapper.setProps({ status: '成功', tone: 'success' })
    expect(result.attributes('aria-label')).toBe('Agent 更新：成功')
    expect(result.text()).toBe('成功')
    expect(result.element.closest('[data-status-details]')).toBeNull()
  })
})

describe('compact deployment status', () => {
  it.each([
    [{ status: 'succeeded' }, '配置推送', '成功', 'success'],
    [{ status: 'succeeded', query_error: true }, '配置推送', '待确认', 'warning'],
    [{ status: 'rolled_back', manual_rollback: true }, '配置回滚', '成功', 'success'],
    [{ status: 'rolled_back' }, '配置推送', '失败，已回滚', 'danger'],
    [{ status: 'rollback_failed' }, '配置推送', '回滚失败', 'danger'],
    [{ status: 'succeeded', rollback_rejected: true }, '配置回滚', '未执行', 'warning'],
  ] as const)('distinguishes confirmed results from manual rollback and uncertain reports: %o', (task, label, status, tone) => {
    const wrapper = keep(mount(AgentDeploymentStatus, { attachTo: document.body, props: { task: deployment(task), compact: true }, slots: { details: '<p>完整发布步骤</p>' } }))
    expect(wrapper.get('section').attributes('aria-label')).toBe(label)
    expect(wrapper.get('section').attributes('data-tone')).toBe(tone)
    expect(wrapper.get('button').text()).toBe(`${label}${status}`)
    expect(wrapper.get('[data-status-details]').isVisible()).toBe(false)
  })

  it('retains visible failure reasons and recovery actions outside the collapsed deployment log', async () => {
    const recover = vi.fn()
    const wrapper = keep(mount(AgentDeploymentStatus, { attachTo: document.body,
      props: { task: deployment({ status: 'rollback_failed' }), compact: true },
      slots: {
        notice: () => h('div', [h('p', '核心未启动，恢复旧配置也失败。'), h('button', { type: 'button', onClick: recover }, '重新检查恢复状态')]),
        details: '<p>已上传配置并尝试启动服务。</p>',
      },
    }))
    expect(wrapper.get('[data-status-notice]').text()).toContain('恢复旧配置也失败')
    expect(wrapper.get('[data-status-notice]').isVisible()).toBe(true)
    expect(wrapper.get('[data-status-details]').isVisible()).toBe(false)
    await wrapper.get('[data-status-notice] button').trigger('click')
    expect(recover).toHaveBeenCalledOnce()
  })

  it('keeps the original status presentation for non-compact callers', () => {
    const wrapper = keep(mount(AgentDeploymentStatus, { attachTo: document.body, props: { task: deployment() } }))
    expect(wrapper.get('p').text()).toBe('上次配置推送：成功')
    expect(wrapper.find('[data-agent-status-row]').exists()).toBe(false)
  })
})

describe('compact Agent update status', () => {
  it('collapses completed update details while retaining the version and recorded steps on expansion', async () => {
    const wrapper = keep(mount(AgentUpgradeStatus, { attachTo: document.body, props: { task: upgrade({ message: '设备身份与配置迁移检查通过', events: [
      { at: '2026-10-19T01:00:00Z', status: 'downloading', message: '下载 Agent 1.8.24-go 程序文件' },
      { at: '2026-10-19T01:00:03Z', status: 'succeeded', message: '服务已启动' },
    ] }) } }))
    expect(wrapper.get('section').attributes('data-tone')).toBe('success')
    expect(wrapper.get('button').text()).toBe('Agent 更新成功')
    expect(wrapper.find('[data-status-notice]').exists()).toBe(false)
    expect(wrapper.get('[data-status-details]').isVisible()).toBe(false)
    await wrapper.get('button').trigger('click')
    expect(wrapper.get('[data-status-details]').isVisible()).toBe(true)
    expect(wrapper.get('[data-status-details]').text()).toContain('目标版本：1.8.24-go')
    expect(wrapper.get('[data-status-details]').text()).toContain('设备身份与配置迁移检查通过')
    expect(wrapper.get('ol').text()).toContain('下载 Agent 1.8.24-go 程序文件')
    expect(wrapper.find('details').exists()).toBe(false)
  })

  it('shows an unconfirmed prior success as a warning and keeps the connection reason and retry visible', async () => {
    const retry = vi.fn()
    const wrapper = keep(mount(AgentUpgradeStatus, { attachTo: document.body,
      props: { task: upgrade({ query_error: true }) },
      slots: { actions: () => h('button', { type: 'button', onClick: retry }, '查询状态') },
    }))
    expect(wrapper.get('section').attributes('data-tone')).toBe('warning')
    expect(wrapper.get('button[aria-expanded]').text()).toBe('Agent 更新待确认')
    expect(wrapper.get('[data-status-notice]').text()).toContain('暂时无法取得最新状态')
    expect(wrapper.get('[data-status-notice]').isVisible()).toBe(true)
    expect(wrapper.get('[data-status-details]').isVisible()).toBe(false)
    await wrapper.get('[data-status-notice] button').trigger('click')
    expect(retry).toHaveBeenCalledOnce()
  })

  it.each([
    ['failed', '失败'], ['rolled_back', '失败，已回滚'], ['rollback_failed', '回滚失败'],
  ])('keeps a %s update reason and recovery action immediately accessible', async (status, label) => {
    const query = vi.fn()
    const wrapper = keep(mount(AgentUpgradeStatus, { attachTo: document.body,
      props: { task: upgrade({ status, error: '新版程序未能启动，请检查核心日志。' }) },
      slots: { actions: () => h('button', { type: 'button', onClick: query }, '查询状态') },
    }))
    expect(wrapper.get('section').attributes('data-tone')).toBe('danger')
    expect(wrapper.get('button[aria-expanded]').text()).toBe(`Agent 更新${label}`)
    expect(wrapper.get('[data-status-notice]').text()).toContain('新版程序未能启动')
    expect(wrapper.get('[data-status-notice]').isVisible()).toBe(true)
    expect(wrapper.get('[data-status-details]').isVisible()).toBe(false)
    await wrapper.get('[data-status-notice] button').trigger('click')
    expect(query).toHaveBeenCalledOnce()
  })

  it('keeps the real in-progress download object visible while its timeline is collapsed', () => {
    const message = '正在下载 Agent 1.8.24-go 的 linux-arm64 程序文件。'
    const wrapper = keep(mount(AgentUpgradeStatus, { attachTo: document.body, props: { busy: true, task: upgrade({ status: 'downloading', message, events: [{ at: '2026-10-19T01:00:00Z', status: 'downloading', message }] }) } }))
    expect(wrapper.get('[data-status-notice]').text()).toContain(message)
    expect(wrapper.get('[data-status-notice]').isVisible()).toBe(true)
    expect(wrapper.get('[data-status-details]').isVisible()).toBe(false)
    expect(wrapper.get('ol').isVisible()).toBe(false)
    expect(wrapper.text()).not.toContain('%')
  })
})
