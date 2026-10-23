import { describe, expect, it } from 'vitest'
import {
  gatewayForwardingView, persistConfirmText, STANDALONE_FORWARDING_AGENT_VERSION, type GatewayForwardingEntry
} from '@/lib/gatewayForwarding'

const entry = (changes: Partial<GatewayForwardingEntry> = {}): GatewayForwardingEntry => ({
  agent_id: 'a1', agent_name: 'pve-mihomo', agent_version: '1.8.41-go', routers: [{ id: 'r1', name: '主路由' }],
  state: 'needs_persist', can_persist: true, message: '', minimum_version: null,
  current: '0', persisted: false, config: 'missing', hook_script: 'missing', startup_hook: 'missing',
  layout: 'standalone', gateway_record: false, preserves_forwarding: false, ...changes
})

describe('gatewayForwardingView', () => {
  it('describes rows without an entry by loading, failure and absence', () => {
    expect(gatewayForwardingView(undefined, true)).toEqual({ label: '读取中…', tone: 'muted', action: null })
    expect(gatewayForwardingView(undefined, false)).toEqual({ label: '—', tone: 'muted', action: null })
    expect(gatewayForwardingView(undefined, false, true)).toMatchObject({ label: '无法读取', tone: 'warning', action: null })
    // An entry already read stays visible while the list reloads.
    expect(gatewayForwardingView(entry({ state: 'persisted', current: '1', persisted: true }), true).label).toBe('已开启并持久化')
  })

  it('offers the one-click persist only while forwarding still needs it', () => {
    expect(gatewayForwardingView(entry({ state: 'persisted', current: '1', persisted: true, can_persist: false })))
      .toEqual({ label: '已开启并持久化', tone: 'success', action: null })
    expect(gatewayForwardingView(entry({ current: '1' }), false)).toEqual({ label: '已开启，未持久化', tone: 'warning', action: 'persist' })
    expect(gatewayForwardingView(entry({ current: '0' }), false)).toEqual({ label: '未开启', tone: 'danger', action: 'persist' })
    expect(gatewayForwardingView(entry({ current: null }), false)).toEqual({ label: '状态未知', tone: 'warning', action: 'persist' })
    expect(gatewayForwardingView(entry({ current: '1', message: '读取 sysctl 失败' }), false))
      .toEqual({ label: '已开启，未持久化', tone: 'warning', action: 'persist', note: '读取 sysctl 失败' })
    expect(gatewayForwardingView(entry({ can_persist: false }), false).action).toBeNull()
  })

  it('points to the Agent page when the Agent needs an update', () => {
    expect(gatewayForwardingView(entry({ state: 'upgrade_required', can_persist: false, minimum_version: '1.8.41-go' }), false))
      .toEqual({ label: '请先更新 Agent', tone: 'warning', action: 'upgrade', note: '需要 1.8.41-go 或更新版本' })
    expect(gatewayForwardingView(entry({ state: 'upgrade_required', can_persist: false, minimum_version: null }), false).note)
      .toBe(`需要 ${STANDALONE_FORWARDING_AGENT_VERSION} 或更新版本`)
  })

  it.each([
    ['unsupported', '不支持自动持久化', 'muted', '仅支持 systemd 或 OpenRC'],
    ['blocked', '持久化文件已被修改', 'danger', 'IPv4 转发启动脚本已被修改，请先在设备上核对。'],
    ['docker', 'Docker 部署', 'muted', 'Docker 部署的 Agent 不管理宿主机网络'],
    ['missing', 'Agent 已删除', 'danger', '关联的 Agent 已删除'],
    ['not_mihomo', '非 Mihomo', 'warning', '代理网关不是 Mihomo Agent。'],
    ['error', '无法读取', 'warning', 'Agent 请求超时'],
  ] as const)('shows %s with its reason and no action', (state, label, tone, message) => {
    expect(gatewayForwardingView(entry({ state, can_persist: false, message }), false)).toEqual({ label, tone, note: message, action: null })
  })

  it('shows an offline Agent without a note', () => {
    expect(gatewayForwardingView(entry({ state: 'offline', can_persist: false, message: 'Agent 离线。' }), false))
      .toEqual({ label: 'Agent 离线', tone: 'muted', action: null })
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
