import { afterEach, describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import MihomoIPv6VerificationResult from '@/components/generate/MihomoIPv6VerificationResult.vue'
import type { IPv6Verification } from '@/types/mihomoIPv6'

const report = (extra: Partial<IPv6Verification> = {}): IPv6Verification => ({
  status: 'ok', config_revision: 7, checked_at: '2026-10-30T12:00:00Z', scope: 'client_path',
  checks: [
    { id: 'host_address', label: '主机地址', status: 'ok', message: '地址已就绪。' },
    { id: 'host_persistence', label: '开机恢复', status: 'ok', message: '恢复配置已保存。' },
    { id: 'runtime_dns_a', label: 'DNS 应答', status: 'ok', message: '已取得 IPv4 答案。' },
    ...['client_http_ipv4', 'client_mihomo_path', 'client_ros_path', 'client_ipv6_transport'].map(id => ({ id, label: id, status: 'ok' as const, message: '本次客户端证据已取得。' })),
    { id: 'runtime_proxy_http', label: '代理网站访问', status: 'ok', message: '已通过代理访问检测网站。' },
    { id: 'runtime_node_ipv6_transport', label: '节点 IPv6 连接', status: 'ok', message: '已确认本次 IPv6 连接。' }], ...extra,
})

afterEach(() => { document.body.innerHTML = '' })

describe('IPv6 verification result', () => {
  it('keeps an untested client path visibly incomplete alongside successful host evidence', () => {
    const wrapper = mount(MihomoIPv6VerificationResult, { attachTo: document.body, props: { verification: report({ checks: [
      { id: 'runtime_proxy_http', label: '代理网站访问', status: 'ok', message: '访问成功。' },
      { id: 'client_ros_path', label: '客户端 ROS 路径', status: 'unknown', message: '尚未取得本次 ROS 连接记录。' },
    ] }) } })
    expect(wrapper.get('[data-agent-status-row="客户端全链路验收"]').attributes('data-tone')).toBe('warning')
    expect(wrapper.findAll('[data-ipv6-check-group]')).toHaveLength(5)
    expect(wrapper.get('[data-ipv6-check-group="client"] [data-status-notice]').text()).toContain('尚未取得本次 ROS 连接记录')
    wrapper.unmount()
  })

  it('does not mark an empty result as passed', () => {
    const wrapper = mount(MihomoIPv6VerificationResult, { attachTo: document.body, props: { verification: report({ checks: [] }) } })
    expect(wrapper.get('[data-agent-status-row="客户端全链路验收"]').attributes('data-tone')).toBe('warning')
    wrapper.unmount()
  })

  it('keeps successful details collapsed until the group is opened', async () => {
    const wrapper = mount(MihomoIPv6VerificationResult, { attachTo: document.body, props: { verification: report() } })
    const node = wrapper.get('[data-ipv6-check-group="node"]')
    expect(node.find('[data-status-notice]').exists()).toBe(false)
    expect(node.get('[data-status-details]').isVisible()).toBe(false)
    await node.get('button').trigger('click')
    expect(node.get('[data-status-details]').isVisible()).toBe(true)
    expect(node.text()).toContain('已通过代理访问检测网站。')
    wrapper.unmount()
  })

  it('labels a host-only check separately from an unconfigured client', () => {
    const wrapper = mount(MihomoIPv6VerificationResult, { attachTo: document.body, props: { verification: report({ scope: 'mihomo_runtime', checks: [
      ...report().checks.filter(check => !check.id.startsWith('client_')),
      { id: 'client_not_configured', label: '客户端全链路', status: 'unknown', message: '尚未配置独立检测客户端。' },
    ] }) } })
    expect(wrapper.get('[data-agent-status-row="主机自检"]').attributes('data-tone')).toBe('success')
    const client = wrapper.get('[data-ipv6-check-group="client"]')
    expect(client.get('[data-agent-status-row]').attributes('data-tone')).toBe('neutral')
    expect(client.get('button').text()).toContain('未配置')
    wrapper.unmount()
  })

  it('keeps automatic publication results compact and reveals the same five groups', async () => {
    const wrapper = mount(MihomoIPv6VerificationResult, { attachTo: document.body, props: { verification: report(), compact: true } })
    const summary = wrapper.get('[data-agent-status-row="客户端全链路验收"]')
    expect(summary.get('[data-status-details]').isVisible()).toBe(false)
    await summary.get('button[aria-label="客户端全链路验收：已通过"]').trigger('click')
    expect(summary.get('button[aria-label="客户端全链路验收：已通过"]').attributes('aria-expanded')).toBe('true')
    expect(summary.get('[data-status-details]').isVisible()).toBe(true)
    expect(wrapper.findAll('[data-ipv6-check-group]')).toHaveLength(5)
    wrapper.unmount()
  })
})
