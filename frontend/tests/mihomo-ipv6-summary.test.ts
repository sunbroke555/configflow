import { describe, expect, it } from 'vitest'
import { buildMihomoIPv6Summary } from '@/lib/mihomoIPv6Summary'
import type { IPv6ReadinessCheck, IPv6Verification, MihomoIPv6Readiness } from '@/types/mihomoIPv6'

const check = (id: string, status: IPv6ReadinessCheck['status'] = 'ok', message = `${id} 的检查结果`): IPv6ReadinessCheck => ({ id, label: id, status, message })
const readiness = (overrides: Partial<MihomoIPv6Readiness> = {}): MihomoIPv6Readiness => ({
  success: true, agent_id: 'gw', profile_id: 'default', config_revision: 7,
  configuration: { ipv6: true, dns_ipv6: false, fake_ipv6_range: 'fc00::/64', errors: [] }, host: {},
  checks: [check('host_address'), check('host_default_route'), check('host_persistence')],
  routers: [{ id: 'router_with_underscores', name: '家里的 ROS', status: 'ok', can_prepare: false, checks: [check('dns'), check('wan')] }],
  can_prepare: false, prepare_interface: 'eth0', check_token: 'token',
  connectivity: { status: 'not_checked', message: '尚未验证节点。' }, ...overrides,
})
const runtimeChecks = () => [check('runtime_proxy_http'), check('runtime_node_ipv6_transport')]
const clientChecks = () => ['client_http_ipv4', 'client_mihomo_path', 'client_ros_path', 'client_ipv6_transport'].map(id => check(id))
const verification = (overrides: Partial<IPv6Verification> = {}): IPv6Verification => ({
  status: 'ok', config_revision: 7, scope: 'mihomo_runtime', checks: runtimeChecks(), ...overrides,
})
const group = (id: string, input: Parameters<typeof buildMihomoIPv6Summary>[0]) => buildMihomoIPv6Summary(input).find(row => row.id === id)!

describe('Mihomo IPv6 check summaries', () => {
  it('shows exactly five groups without inventing node or client connectivity from a passing inspection', () => {
    const groups = buildMihomoIPv6Summary({ readiness: readiness() })
    expect(groups.map(row => row.label)).toEqual(['IPv6 网络', '重启恢复', 'DNS 与 ROS 配套', '节点实际连接', '客户端分流'])
    expect(groups[0]).toMatchObject({ tone: 'success', status: '已通过', issues: [] })
    expect(groups[1]).toMatchObject({ status: '恢复配置已就绪', tone: 'success' })
    expect(groups[1]!.note).toContain('未实际重启设备')
    expect(groups.slice(3).map(row => [row.status, row.tone])).toEqual([['待验证', 'neutral'], ['待验证', 'neutral']])
  })

  it('uses the new full router snapshot instead of old success or a removed router', () => {
    const current = readiness()
    current.routers[0]!.checks = [check('dns', 'warning', '需要核对 DNS')]
    const result = group('dns-router', { readiness: current, verification: verification({ checks: [
      ...runtimeChecks(), check('router_router_with_underscores_dns'), check('router_removed_wan'),
    ] }) })
    expect(result.tone).toBe('warning')
    expect(result.checks.map(row => row.id)).toEqual(['router_router_with_underscores_dns'])
    expect(result.checks[0]!.displayLabel).toBe('家里的 ROS · dns')
  })

  it('uses a newly completed verification snapshot instead of an older passing inspection', () => {
    const current = readiness()
    const report = verification({ checks: [check('host_default_route', 'error'), check('router_router_with_underscores_dns', 'error'), ...runtimeChecks()] })
    const input = { readiness: current, verification: report, preferVerificationSnapshot: true }
    expect(group('ipv6-network', input).tone).toBe('danger')
    expect(group('dns-router', input).tone).toBe('danger')
    expect(group('dns-router', input).checks.map(row => row.id)).toEqual(['router_router_with_underscores_dns'])
    expect(group('restart', input).status).toBe('待检查')
  })

  it('does not overwrite partial failures of a newly failed run with an older inspection', () => {
    const report = verification({ status: 'error', checks: [check('host_address', 'error'), check('verification_failed', 'error')] })
    const input = { readiness: readiness(), verification: report, preferVerificationSnapshot: true }
    expect(group('ipv6-network', input).tone).toBe('danger')
    expect(group('node', input).tone).toBe('danger')
  })

  it('retains the current inspection while a new run is pending or its snapshot has become stale', () => {
    const current = readiness()
    const input = { readiness: current, preferVerificationSnapshot: true, verification: verification({ status: 'running', checks: [] }) }
    expect(group('ipv6-network', input).tone).toBe('success')
    input.verification = verification({ config_revision: 6, checks: [check('host_address', 'error'), ...runtimeChecks()] })
    expect(group('ipv6-network', input).tone).toBe('success')
    expect(group('node', input).tone).not.toBe('success')
  })

  it('keeps static node and client rule analysis in ROS instead of claiming actual requests passed', () => {
    const result = buildMihomoIPv6Summary({ verification: verification({ checks: [
      check('router_ros_node_0_0'), check('router_ros_fake4_0'), check('router_ros_dhcpv6_input', 'unknown'),
    ] }) })
    expect(result[2]!.checks).toHaveLength(3)
    expect(result[2]!.status).toBe('尚未全部验证')
    expect(result[3]!.status).toBe('待验证')
    expect(result[4]!.status).toBe('待验证')
  })

  it('keeps DNS IPv6 off separate from using an IPv6 node and IPv4 client forwarding', () => {
    const input = { readiness: readiness(), verification: verification({ scope: 'client_path', checks: [...runtimeChecks(), ...clientChecks()] }) }
    expect(group('node', input)).toMatchObject({ status: '已通过', tone: 'success' })
    expect(group('client', input)).toMatchObject({ status: '已通过', tone: 'success' })
  })

  it('requires both an actual proxy request and IPv6 transport evidence', () => {
    expect(group('node', { verification: verification({ checks: [check('runtime_proxy_http')] }) }).tone).toBe('neutral')
    expect(group('node', { verification: verification({ checks: [check('runtime_node_ipv6_transport')] }) }).tone).toBe('neutral')
    expect(group('node', { verification: verification() }).tone).toBe('success')
  })

  it('does not substitute host socket evidence for the client request', () => {
    const report = verification({ scope: 'client_path', checks: [...runtimeChecks(), ...clientChecks().filter(row => row.id !== 'client_ipv6_transport')] })
    expect(group('node', { verification: report }).tone).toBe('success')
    expect(group('client', { verification: report })).toMatchObject({ status: '尚未全部验证', tone: 'neutral' })
  })

  it('shows an unconfigured test client as neutral without saying client forwarding is unnecessary', () => {
    const result = group('client', { verification: verification({ checks: [...runtimeChecks(), check('client_not_configured', 'unknown')] }) })
    expect(result).toMatchObject({ status: '未配置检测客户端', tone: 'neutral', issues: [] })
    expect(result.note).toBe('未配置检测客户端，本次只检查 Mihomo 主机。')
    expect(result.status).not.toBe('无需检查')
  })

  it('never hides a failed or conflicting client finding behind the unconfigured marker', () => {
    const result = group('client', { verification: verification({ checks: [check('client_not_configured', 'unknown'), check('client_http_ipv4', 'error', '网站访问失败')] }) })
    expect(result).toMatchObject({ tone: 'danger', issues: ['网站访问失败'] })
    const malformed = group('client', { verification: verification({ checks: [check('client_not_configured', 'error', '客户端有错误')] }) })
    expect(malformed.tone).toBe('danger')
  })

  it('marks only explicitly unused IPv6 Fake-IP as not needed and retains contradictory failures', () => {
    const unused = '当前保存配置未启用 IPv6 Fake-IP，本次不新增相关路由'
    const report = verification({ checks: [check('router_ros_fake_ipv6', 'ok', unused), ...runtimeChecks()] })
    expect(group('dns-router', { verification: report }).checks[0]).toMatchObject({ displayStatus: '无需检查', displayTone: 'neutral' })
    report.checks[0]!.status = 'error'
    expect(group('dns-router', { verification: report })).toMatchObject({ tone: 'danger' })
    expect(group('dns-router', { verification: report }).checks[0]!.displayStatus).toBeUndefined()
  })

  it.each(['result_stale', 'target_changed', 'interrupted', 'verification_failed', 'runtime_config_stability'])('discards successful runtime evidence when %s invalidates the run', id => {
    const result = buildMihomoIPv6Summary({ readiness: readiness(), verification: verification({ checks: [
      ...runtimeChecks(), ...clientChecks(), check(id, id === 'verification_failed' ? 'error' : 'unknown'),
    ] }) })
    expect(result[0]!.tone).toBe('success')
    expect(result[3]!.tone).not.toBe('success')
    expect(result[4]!.tone).not.toBe('success')
    expect(result.flatMap(row => row.checks).some(row => row.id === 'runtime_proxy_http')).toBe(false)
  })

  it('keeps a successful stability check from invalidating the run', () => {
    const result = group('node', { verification: verification({ checks: [...runtimeChecks(), check('runtime_config_stability')] }) })
    expect(result.tone).toBe('success')
  })

  it('does not merge a different saved revision or a stale flag into current success', () => {
    expect(group('node', { readiness: readiness(), verification: verification({ config_revision: 6 }) }).tone).not.toBe('success')
    const stale = { ...verification(), stale: true }
    expect(group('node', { verification: stale }).tone).not.toBe('success')
  })

  it('explains missing deployment evidence plainly while retaining its technical detail', () => {
    const report = verification({ checks: [check('runtime_deployed_config', 'unknown', '未提供本次发布内容的指纹')] })
    const result = group('ipv6-network', { verification: report })
    expect(result.issues).toEqual(['尚未确认 Mihomo 使用的是当前保存的配置，请发布后再验证。'])
    expect(result.checks[0]!.message).toBe('未提供本次发布内容的指纹')
  })

  it('keeps unfamiliar errors and deduplicates repeated technical issues without dropping their evidence', () => {
    const report = verification({ checks: [check('new_backend_check', 'error', '新检查失败'), check('router_ros_fake4_0', 'unknown'), check('router_ros_fake4_1', 'unknown')] })
    expect(group('node', { verification: report })).toMatchObject({ tone: 'danger', issues: ['新检查失败'] })
    const ros = group('dns-router', { verification: report })
    expect(ros.issues).toHaveLength(1)
    expect(ros.checks).toHaveLength(2)
  })

  it('does not turn a failed empty report or a successful port probe into a successful node test', () => {
    expect(group('node', { verification: verification({ status: 'error', checks: [] }) }).tone).toBe('danger')
    expect(group('node', { readiness: readiness({ connectivity: { status: 'ok', message: '节点端口可达' } }) }).tone).not.toBe('success')
  })

  it('does not show an unexplained top-level warning as all passing groups', () => {
    const input = { readiness: readiness(), verification: verification({ status: 'warning' }) }
    expect(group('node', input)).toMatchObject({ status: '尚未全部验证', tone: 'neutral' })
    expect(group('node', input).issues[0]).toContain('未返回具体问题')
  })

  it('shows running state only for the scopes actually being verified', () => {
    const input = { readiness: readiness(), verification: verification({ status: 'running', checks: [] }) }
    expect(group('node', input)).toMatchObject({ status: '验证中', tone: 'progress' })
    expect(group('client', input).status).toBe('待验证')
    input.verification.scope = 'client_path'
    expect(group('client', input).status).toBe('验证中')
  })

  it('retains the timestamp only for accepted runtime evidence', () => {
    const report = verification({ checked_at: '2026-10-30T12:00:00Z' })
    expect(group('node', { verification: report }).note).toContain('上次验证：')
    report.config_revision = 6
    expect(group('node', { readiness: readiness(), verification: report }).note).not.toContain('上次验证：')
  })
})
