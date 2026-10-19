import { afterEach, describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import AgentOperationSteps from '@/components/agents/AgentOperationSteps.vue'
import AgentFailureDetails from '@/components/agents/AgentFailureDetails.vue'

const wrappers: ReturnType<typeof mount>[] = []
afterEach(() => wrappers.splice(0).forEach(wrapper => wrapper.unmount()))
const render = (props: Record<string, unknown>) => {
  const wrapper = mount(AgentOperationSteps, { props: props as any })
  wrappers.push(wrapper)
  return wrapper
}

describe('observed operation steps', () => {
  it('can show the actual operation object without duplicating its timeline in the visible notice', () => {
    const message = '正在下载规则集「海外服务」（2/3）'
    const wrapper = render({ task: { status: 'preparing', message, events: [{ at: '2026-10-19T01:00:00Z', status: 'preparing', message }] }, busy: true, showCurrent: false, showSteps: false })
    expect(wrapper.get('[role="status"]').text()).toBe(message)
    expect(wrapper.find('details, ol, [aria-label="步骤记录"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('准备文件')
  })
  it('embeds recorded steps without a second disclosure or duplicated current message', () => {
    const wrapper = render({ task: { status: 'succeeded', message: '已完成服务检查', events: [{ at: '2026-10-19T01:00:00Z', status: 'checking', message: '检查配置发布后的 DNS 响应' }] }, embedded: true, showCurrent: false, showMessage: false })
    expect(wrapper.find('details, summary, [role="status"]').exists()).toBe(false)
    expect(wrapper.get('[aria-label="步骤记录"]').attributes('tabindex')).toBe('0')
    expect(wrapper.get('ol').text()).toContain('检查配置发布后的 DNS 响应')
    expect(wrapper.text()).toContain('步骤记录 · 1')
    expect(wrapper.text()).not.toContain('已完成服务检查')
  })
  it('shows actual download objects and keeps only recorded stages in collapsed details', () => {
    const events = [
      { at: '2026-10-18T12:00:00Z', status: 'preparing', message: '正在下载订阅「家庭节点」' },
      { at: '2026-10-18T12:00:03Z', status: 'preparing', message: '正在下载规则集「海外服务」（2/3）' },
    ]
    const wrapper = render({ task: { status: 'preparing', message: events[1].message, events }, busy: true })
    expect(wrapper.get('[role="status"]').text()).toContain('正在下载规则集「海外服务」（2/3）')
    expect(wrapper.get('details').attributes('open')).toBeUndefined()
    expect(wrapper.findAll('li')).toHaveLength(2)
    expect(wrapper.get('ol').text()).toContain('家庭节点')
    expect(wrapper.text()).not.toContain('启动检查')
    expect(wrapper.text()).not.toContain('%')
  })
  it('describes the actual Agent artifact and preserves last-known progress during restart', async () => {
    const wrapper = render({ kind: 'upgrade', task: { status: 'verifying', target_version: '1.8.21-go' }, busy: true })
    expect(wrapper.text()).toContain('校验 Agent 1.8.21-go 的程序文件')
    await wrapper.setProps({ task: { status: 'restarting', query_error: true, target_version: '1.8.21-go' } })
    expect(wrapper.text()).toContain('上次报告：重启 Agent')
    expect(wrapper.text()).toContain('尚未确认更新成功')
    expect(wrapper.text()).not.toContain('正在下载')
  })
  it('does not invent a timeline when older Agents or pending requests have no events', () => {
    const wrapper = render({ task: { status: 'unknown' } })
    expect(wrapper.findAll('li')).toHaveLength(0)
    expect(wrapper.text()).toContain('未提供步骤记录')
    expect(wrapper.text()).not.toContain('下载更新')
  })
  it('can hide duplicate normal messages without hiding DNS warnings or failures', async () => {
    const wrapper = mount(AgentFailureDetails, { props: { showProgressMessage: false, task: {
      status: 'preparing', message: '正在下载规则集', dns_checks: [{ name: 'DNS', level: 'warn', message: '请检查上游' }],
    } } })
    wrappers.push(wrapper)
    expect(wrapper.text()).not.toContain('正在下载规则集')
    expect(wrapper.text()).toContain('请检查上游')
    await wrapper.setProps({ task: { status: 'failed', message: '下载规则集失败' } })
    expect(wrapper.text()).toContain('下载规则集失败')
  })
})
