import { describe, expect, it } from 'vitest'
import { shallowMount } from '@vue/test-utils'
import MihomoIPv6VerificationResult from '@/components/generate/MihomoIPv6VerificationResult.vue'
import AgentStatusRow from '@/components/agents/AgentStatusRow.vue'
import type { IPv6Verification } from '@/types/mihomoIPv6'

const report = (extra: Partial<IPv6Verification> = {}): IPv6Verification => ({
  status: 'ok', config_revision: 7, checked_at: '2026-10-30T12:00:00Z', scope: 'client_path',
  checks: [{ id: 'proxy', label: '代理网站访问', status: 'ok', message: '已通过代理访问检测网站。' }], ...extra,
})

describe('IPv6 verification result', () => {
  it('keeps an untested client path visibly incomplete even if other checks succeeded', () => {
    const wrapper = shallowMount(MihomoIPv6VerificationResult, { props: { verification: report({ checks: [
      { id: 'proxy', label: '代理网站访问', status: 'ok', message: '访问成功。' },
      { id: 'client', label: '客户端全链路', status: 'unknown', message: '尚未配置独立检测客户端。' },
    ] }) } })
    const rows = wrapper.findAllComponents(AgentStatusRow)
    expect(rows[0].props()).toMatchObject({ status: '尚未全部验证', tone: 'warning' })
    expect(rows[2].props()).toMatchObject({ label: '客户端全链路', status: '未验证', tone: 'neutral' })
    wrapper.unmount()
  })

  it('does not mark an empty result as passed', () => {
    const wrapper = shallowMount(MihomoIPv6VerificationResult, { props: { verification: report({ checks: [] }) } })
    expect(wrapper.getComponent(AgentStatusRow).props('tone')).toBe('warning')
    wrapper.unmount()
  })

  it('only uses a successful summary when every reported check passed', () => {
    const wrapper = shallowMount(MihomoIPv6VerificationResult, { props: { verification: report() } })
    expect(wrapper.findAllComponents(AgentStatusRow)[0].props()).toMatchObject({ label: '客户端全链路验收', status: '已通过', tone: 'success' })
    wrapper.unmount()
  })

  it('labels a successful host-only check separately from its unconfigured client path', () => {
    const wrapper = shallowMount(MihomoIPv6VerificationResult, { props: { verification: report({ scope: 'mihomo_runtime', checks: [
      { id: 'proxy', label: '代理网站访问', status: 'ok', message: '访问成功。' },
      { id: 'client_not_configured', label: '客户端全链路', status: 'unknown', message: '尚未配置独立检测客户端。' },
    ] }) } })
    const rows = wrapper.findAllComponents(AgentStatusRow)
    expect(rows[0].props()).toMatchObject({ label: '主机自检', status: '已通过', tone: 'success' })
    expect(rows[2].props()).toMatchObject({ status: '未验证', tone: 'neutral' })
    wrapper.unmount()
  })
})
