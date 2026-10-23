import { describe, expect, it } from 'vitest'
import type { LinuxGatewayStatus } from '@/api/linuxGateway'
import { FORWARDING_NOT_PERSISTED, GATEWAY_OFF_NOT_PERSISTED, ROUTER_SPLIT_MESSAGES, forwardingLabel, forwardingTone, fullTunReady, gatewayLeftover, routerSplitErrorMessage, tunLabel, tunTone } from '@/lib/routerSplit'

const ready = (changes: Partial<LinuxGatewayStatus> = {}): LinuxGatewayStatus => ({
  success: true, supported: true, enabled_in_config: false, managed: { active: false, preserves_forwarding: true },
  ipv4_forwarding: { current: '1', config: 'managed', hook_script: 'managed', startup_hook: 'registered', persisted: true, supported: true },
  tun: { configured: true, device: 'Mihomo', present: true, gateway_marker: false }, ...changes,
})
const forwarding = (changes: Partial<NonNullable<LinuxGatewayStatus['ipv4_forwarding']>>) => ready({ ipv4_forwarding: { ...ready().ipv4_forwarding!, ...changes } })
const tun = (changes: Partial<NonNullable<LinuxGatewayStatus['tun']>>) => ready({ tun: { ...ready().tun!, ...changes } })

describe('router split status labels', () => {
  it.each([
    [ready(), '已开启并持久化', 'success'],
    [forwarding({ persisted: false }), '已开启，未持久化', 'warning'],
    [forwarding({ current: '0', persisted: false }), '未开启', 'danger'],
    [forwarding({ current: '' }), '未开启', 'danger'],
    [ready({ ipv4_forwarding: undefined }), '需要 Agent 1.8.40-go', 'warning'],
    [null, '需要 Agent 1.8.40-go', 'warning'],
  ] as const)('labels IPv4 forwarding %#', (status, label, tone) => {
    expect(forwardingLabel(status)).toBe(label)
    expect(forwardingTone(status)).toBe(tone)
  })

  it.each([
    [ready(), '运行中 · Mihomo', 'success'],
    [tun({ device: '' }), '运行中 · Meta', 'success'],
    [tun({ present: false }), '未运行', 'danger'],
    [tun({ configured: false }), '未启用完整 TUN', 'warning'],
    [ready({ tun: undefined }), '需要 Agent 1.8.40-go', 'warning'],
  ] as const)('labels the deployed TUN with its own device name %#', (status, label, tone) => {
    expect(tunLabel(status)).toBe(label)
    expect(tunTone(status)).toBe(tone)
  })

  it('is ready only with persisted forwarding, a running TUN and no gateway record', () => {
    expect(fullTunReady(ready())).toBe(true)
    for (const status of [
      null, ready({ success: false }), forwarding({ persisted: false }), forwarding({ current: '0' }),
      tun({ present: false }), tun({ configured: false }), tun({ gateway_marker: true }),
      ready({ managed: { active: true } }), ready({ managed: { active: false, pending: true } }), ready({ enabled_in_config: true }),
      // The setup check refuses an unreadable gateway record as well.
      ready({ managed: { active: false, error: '网关状态文件无法读取' } }),
      ready({ ipv4_forwarding: undefined, tun: undefined }),
    ]) expect(fullTunReady(status)).toBe(false)
  })

  it('reports a gateway record that is active, pending or unreadable', () => {
    expect(gatewayLeftover(ready())).toBeNull()
    expect(gatewayLeftover(null)).toBeNull()
    expect(gatewayLeftover(ready({ managed: { active: true } }))).toBe('present')
    expect(gatewayLeftover(ready({ managed: { active: false, pending: true } }))).toBe('present')
    expect(gatewayLeftover(ready({ managed: { active: false, error: 'bad journal' } }))).toBe('unknown')
  })

  it('prefers the server text and falls back to the shared copy', () => {
    expect(routerSplitErrorMessage('ROUTER_SPLIT_RESTORE_BLOCKED', '恢复后 Linux 网关为关闭状态')).toBe('恢复后 Linux 网关为关闭状态')
    expect(routerSplitErrorMessage('ROUTER_SPLIT_LEGACY')).toBe(ROUTER_SPLIT_MESSAGES.ROUTER_SPLIT_LEGACY)
    expect(routerSplitErrorMessage('UPGRADE_REQUIRED', '')).toBe('请先将 Mihomo Agent 更新到 1.8.40-go 或更新版本。')
    // Refused gateway-record teardown from the bridge, and a refused gateway-off
    // edit of an old router split, while forwarding is not persisted.
    for (const [code, text] of [['FORWARDING_NOT_PERSISTED', FORWARDING_NOT_PERSISTED], ['ROUTER_SPLIT_FORWARDING_NOT_PERSISTED', GATEWAY_OFF_NOT_PERSISTED]]) {
      expect(routerSplitErrorMessage(code)).toBe(text)
      expect(routerSplitErrorMessage(code, '服务端原文')).toBe('服务端原文')
    }
    expect(GATEWAY_OFF_NOT_PERSISTED).toContain('请保持 Linux 网关开启')
    expect(routerSplitErrorMessage('SOMETHING_ELSE')).toBe('请求未完成，请稍后重试。')
    // Nothing in the shared copy points to a migration that no longer exists.
    for (const message of Object.values(ROUTER_SPLIT_MESSAGES)) expect(message).not.toContain('迁移')
  })
})
