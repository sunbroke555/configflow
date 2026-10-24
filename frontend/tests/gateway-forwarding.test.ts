import { describe, expect, it } from 'vitest'
import {
  gatewayForwardingReason, gatewayForwardingView, gatewayRoutersText, persistConfirmText, routerForwardingWarning, systemPersistenceText,
  STANDALONE_FORWARDING_AGENT_VERSION, type GatewayForwardingEntry
} from '@/lib/gatewayForwarding'
import type { ForwardFilter } from '@/lib/forwardFilter'

const entry = (changes: Partial<GatewayForwardingEntry> = {}): GatewayForwardingEntry => ({
  agent_id: 'a1', agent_name: 'pve-mihomo', agent_version: '1.8.41-go', routers: [{ id: 'r1', name: '主路由' }],
  state: 'needs_persist', can_persist: true, message: '', minimum_version: null,
  current: '0', persisted: false, config: 'missing', hook_script: 'missing', startup_hook: 'missing',
  layout: 'standalone', gateway_record: false, preserves_forwarding: false, ...changes
})

const mayDrop = (changes: Partial<ForwardFilter> = {}): ForwardFilter => ({
  protocol: 1, state: 'may_drop', reason: '', device: 'Meta', auto_redirect: true, policy: 'drop',
  flows: ['forward'], forward_protocols: ['udp'], sources: [], remedies: [],
  hint: '主路由转来的 UDP 可能被丢弃。', verify: '', checked_at: '', cached: false, ...changes
})

describe('gatewayForwardingView', () => {
  it('offers the one-click persist only while forwarding still needs it', () => {
    expect(gatewayForwardingView(entry({ state: 'persisted', current: '1', persisted: true, can_persist: false })))
      .toEqual({ label: '已开启并持久化', tone: 'success', action: null })
    expect(gatewayForwardingView(entry({ current: '1' }))).toEqual({ label: '已开启，未持久化', tone: 'warning', action: 'persist' })
    expect(gatewayForwardingView(entry({ current: '0' }))).toEqual({ label: '未开启', tone: 'danger', action: 'persist' })
    expect(gatewayForwardingView(entry({ current: null }))).toEqual({ label: '状态未知', tone: 'warning', action: 'persist' })
    expect(gatewayForwardingView(entry({ current: '1', message: '读取 sysctl 失败' })))
      .toEqual({ label: '已开启，未持久化', tone: 'warning', action: 'persist', note: '读取 sysctl 失败' })
    expect(gatewayForwardingView(entry({ can_persist: false })).action).toBeNull()
  })

  it('accepts forwarding the host persisted itself, offering ConfigFlow persistence only for a gateway record', () => {
    const system = { state: 'system_persisted', current: '1', system_source: '/etc/sysctl.d/99-ip-forward.conf:1' } as const
    expect(gatewayForwardingView(entry(system))).toEqual({ label: '已开启，系统已持久化', tone: 'success', action: null })
    expect(gatewayForwardingView(entry({ ...system, gateway_record: true }))).toEqual({
      label: '已开启，系统已持久化', tone: 'warning', action: 'persist',
      note: '这台设备有 Linux 网关记录：撤销记录时 Agent 会把转发改回关闭，由 ConfigFlow 持久化后才会保留。' })
    expect(systemPersistenceText(entry(system))).toBe(
      '这台设备自己的 sysctl 配置（/etc/sysctl.d/99-ip-forward.conf:1）已持久化 IPv4 转发，重启后保持开启；ConfigFlow 没有接管，也不会改动它。')
    expect(systemPersistenceText(entry({ ...system, system_source: null }))).toBe(
      '这台设备自己的 sysctl 配置已持久化 IPv4 转发，重启后保持开启；ConfigFlow 没有接管，也不会改动它。')
    expect(systemPersistenceText(entry())).toBeNull()
    // Forwarding is on, so the router page has nothing to warn about.
    expect(routerForwardingWarning(entry(system))).toBeNull()
  })

  it('asks for an Agent update without its own link (the card has the update banner)', () => {
    expect(gatewayForwardingView(entry({ state: 'upgrade_required', can_persist: false, minimum_version: '1.8.41-go' })))
      .toEqual({ label: '请先更新 Agent', tone: 'warning', action: null, note: '需要 1.8.41-go 或更新版本才能在面板开启并持久化。' })
    expect(gatewayForwardingView(entry({ state: 'upgrade_required', can_persist: false, minimum_version: null })).note)
      .toBe(`需要 ${STANDALONE_FORWARDING_AGENT_VERSION} 或更新版本才能在面板开启并持久化。`)
  })

  it.each([
    ['unsupported', '不支持自动持久化', 'neutral', '仅支持 systemd 或 OpenRC'],
    ['blocked', '持久化文件已被修改', 'danger', 'IPv4 转发启动脚本已被修改，请先在设备上核对。'],
    ['docker', 'Docker 部署', 'neutral', 'Docker 部署的 Agent 不管理宿主机网络'],
    ['missing', 'Agent 已删除', 'danger', '关联的 Agent 已删除'],
    ['not_mihomo', '非 Mihomo', 'warning', '代理网关不是 Mihomo Agent。'],
    ['error', '无法读取', 'warning', 'Agent 请求超时'],
  ] as const)('shows %s with its reason and no action', (state, label, tone, message) => {
    expect(gatewayForwardingView(entry({ state, can_persist: false, message }))).toEqual({ label, tone, note: message, action: null })
  })

  it('shows an offline Agent without a note', () => {
    expect(gatewayForwardingView(entry({ state: 'offline', can_persist: false, message: 'Agent 离线。' })))
      .toEqual({ label: 'Agent 离线', tone: 'neutral', action: null })
  })
})

describe('gatewayRoutersText and gatewayForwardingReason', () => {
  it('names the routers and adds profiles only when they differ', () => {
    expect(gatewayRoutersText(entry())).toBe('路由器「主路由」')
    expect(gatewayRoutersText(entry({ routers: [
      { id: 'r1', name: 'ROS', profile_id: 'home', profile_name: 'Home' },
      { id: 'r2', name: '', profile_id: 'home', profile_name: 'Home' },
    ] }))).toBe('路由器「ROS」、「r2」')
    expect(gatewayRoutersText(entry({ routers: [
      { id: 'r1', name: 'ROS', profile_id: 'home', profile_name: 'Home' },
      { id: 'r9', name: 'iKuai', profile_id: 'lab', profile_name: '实验' },
    ] }))).toBe('路由器「ROS」（Home）、「iKuai」（实验）')
    expect(gatewayRoutersText(entry({ routers: [] }))).toBe('路由器')
  })

  it('explains why the device needs forwarding', () => {
    expect(gatewayForwardingReason(entry())).toBe(
      '路由器「主路由」把要分流的流量转给这台设备，需要开启 IPv4 转发；未开启时转来的 UDP（如 QUIC、游戏、语音）和 ping 会被丢弃。持久化后重启设备也保持开启。')
  })
})

describe('routerForwardingWarning', () => {
  it('warns only when forwarding is certainly off, the gateway is unusable, or a firewall may drop traffic', () => {
    expect(routerForwardingWarning(undefined)).toBeNull()
    expect(routerForwardingWarning(entry({ current: '0' }))).toEqual({ agent: 'pve-mihomo', problem: '未开启 IPv4 转发' })
    expect(routerForwardingWarning(entry({ state: 'missing', agent_name: 'gone' }))).toEqual({ agent: null, problem: '代理网关 Agent 已删除' })
    expect(routerForwardingWarning(entry({ state: 'not_mihomo' }))).toEqual({ agent: 'pve-mihomo', problem: '不是 Mihomo' })
    expect(routerForwardingWarning(entry({ state: 'persisted', current: '1', persisted: true, forward_filter: mayDrop() })))
      .toEqual({ agent: 'pve-mihomo', problem: '可能拦截 UDP' })
  })

  it.each([
    ['not persisted', { current: '1' }],
    ['unknown value', { current: null }],
    ['persisted', { state: 'persisted', current: '1', persisted: true }],
    ['offline', { state: 'offline' }],
    ['update needed', { state: 'upgrade_required' }],
    ['read error', { state: 'error' }],
    ['docker', { state: 'docker' }],
    ['firewall ok', { state: 'persisted', current: '1', forward_filter: mayDrop({ state: 'ok' }) }],
  ] as const)('stays quiet when %s', (_name, changes) => {
    expect(routerForwardingWarning(entry(changes as Partial<GatewayForwardingEntry>))).toBeNull()
  })

  it('falls back to the Agent id without a name', () => {
    expect(routerForwardingWarning(entry({ agent_name: '' }))).toEqual({ agent: 'a1', problem: '未开启 IPv4 转发' })
  })
})

describe('persistConfirmText', () => {
  it('names the host, the persisted file and what is left untouched', () => {
    const text = persistConfirmText(entry())
    expect(text.title).toBe('开启「pve-mihomo」的 IPv4 转发？')
    expect(text.description).toBe('将在「pve-mihomo」写入 /etc/sysctl.d/99-configflow-aio-forward.conf，并在 Mihomo Agent 启动前注册转发检查，随后立即开启 IPv4 转发；不修改核心配置，也不重启 Mihomo 或 Agent。')
  })

  it('adds the Linux gateway note when the host has a gateway record', () => {
    expect(persistConfirmText(entry({ gateway_record: true })).description)
      .toMatch(/不重启 Mihomo 或 Agent。这台设备有 Linux 网关记录：之后撤销网关时会保留 IPv4 转发开启。$/)
  })
})
