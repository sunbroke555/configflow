import { afterEach, describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import AgentDNSChecks from '@/components/agents/AgentDNSChecks.vue'
import type { AgentDNSCheck } from '@/composables/useAgentDeployments'

const wrappers: ReturnType<typeof mount>[] = []
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  document.body.innerHTML = ''
})
function render(checks?: AgentDNSCheck[], compact = true) {
  const wrapper = mount(AgentDNSChecks, { props: { checks, compact, checkedAt: '2026-10-19T01:00:00Z' }, attachTo: document.body })
  wrappers.push(wrapper)
  return wrapper
}

describe('compact DNS checks', () => {
  it('keeps a passing report in one disclosure with descriptions and timestamp initially collapsed', async () => {
    const wrapper = render([{ name: 'DNS 解析', level: 'pass', message: '当前 DNS 返回有效响应' }])
    const toggle = wrapper.get('[aria-expanded]')
    expect(toggle.text()).toContain('DNS 检查')
    expect(toggle.text()).toContain('1 项通过')
    expect(toggle.attributes('aria-expanded')).toBe('false')
    expect(wrapper.get('li').isVisible()).toBe(false)
    expect(wrapper.get('time').isVisible()).toBe(false)
    expect(wrapper.get('p').isVisible()).toBe(false)
    expect(wrapper.get('p').text()).toContain('解析探测针对检查时运行的 DNS 服务')
    expect(wrapper.get('[data-agent-status-row]').attributes('data-tone')).toBe('success')
    expect(wrapper.find('details, summary').exists()).toBe(false)
    await toggle.trigger('click')
    expect(toggle.attributes('aria-expanded')).toBe('true')
    expect(wrapper.get('li').isVisible()).toBe(true)
    expect(wrapper.get('time').isVisible()).toBe(true)
    expect(wrapper.get('p').isVisible()).toBe(true)
  })

  it('always shows warning and error messages while collapsing repair advice and passing items', async () => {
    const wrapper = render([
      { name: 'DNS 监听', level: 'error', message: '0.0.0.0:53 已被占用', fix: '更改冲突程序的监听端口' },
      { name: 'DNS 劫持范围', level: 'warn', message: 'any:53 可能绕过 OxiDNS', fix: '核对 DNS 劫持规则' },
      { name: 'DNS 配置', level: 'pass', message: '已读取指定配置文件' },
    ])
    const toggle = wrapper.get('[aria-expanded]')
    const visibleIssues = wrapper.findAll('li').filter(item => item.isVisible())
    expect(visibleIssues.map(item => item.text())).toEqual([
      '错误 · DNS 监听0.0.0.0:53 已被占用',
      '警告 · DNS 劫持范围any:53 可能绕过 OxiDNS',
    ])
    expect(toggle.text()).toContain('DNS 检查')
    expect(toggle.text()).toContain('1 项错误')
    expect(toggle.attributes('aria-expanded')).toBe('false')
    expect(wrapper.get('[data-agent-status-row]').attributes('data-tone')).toBe('danger')
    const details = wrapper.get('[data-status-details]')
    expect(details.isVisible()).toBe(false)
    expect(details.text()).toContain('DNS 监听 · 修复建议：更改冲突程序的监听端口')
    expect(details.text()).toContain('DNS 配置：已读取指定配置文件')
    expect(wrapper.findAll('[aria-expanded]')).toHaveLength(1)
    await toggle.trigger('click')
    expect(details.isVisible()).toBe(true)
    expect(visibleIssues.every(item => item.isVisible())).toBe(true)
  })

  it('retains full long warning text and explicit wrapping without truncation', () => {
    const message = `无法连接 DNS 上游：udp://${'upstream'.repeat(60)}:1053\n请检查 DNS 服务是否运行。`
    const wrapper = render([{ name: 'DNS 上游', level: 'warn', message }])
    const warning = wrapper.get('li')
    expect(warning.get('p + p').text()).toBe(message)
    expect(warning.get('p + p').isVisible()).toBe(true)
    expect(warning.get('p + p').classes()).toContain('whitespace-pre-wrap')
    expect(wrapper.get('section').classes()).toContain('[overflow-wrap:anywhere]')
    expect(wrapper.find('.truncate, .line-clamp-1, .line-clamp-2').exists()).toBe(false)
    expect(wrapper.get('[aria-expanded]').text()).toContain('1 项提醒')
    expect(wrapper.get('[data-agent-status-row]').attributes('data-tone')).toBe('warning')
  })

  it('does not invent checks for an older Agent', () => {
    expect(render().find('section').exists()).toBe(false)
    expect(render([]).find('section').exists()).toBe(false)
  })

  it('preserves expanded repair advice and timestamp for non-compact consumers', () => {
    const wrapper = render([{ name: 'DNS 劫持', level: 'warn', message: 'any:53 可能绕过 OxiDNS', fix: '核对规则' }], false)
    expect(wrapper.get('li').text()).toContain('修复建议：核对规则')
    expect(wrapper.get('time').element.closest('details')).toBeNull()
    expect(wrapper.get('section').classes()).toContain('border')
  })
})
