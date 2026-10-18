import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { clientToolsApi } from '@/api'
import DeviceNoteDialog from '@/components/mihomo/DeviceNoteDialog.vue'
import ConnectionRuleDialog from '@/components/mihomo/ConnectionRuleDialog.vue'
import { connectionDomains, connectionRuleValues, deviceNote } from '@/lib/device-notes'
vi.mock('@/api', () => ({ clientToolsApi: { notes: vi.fn(), saveNote: vi.fn(), ruleTargets: vi.fn(), addRule: vi.fn() } }))
vi.mock('@/lib/feedback', () => ({ notify: { success: vi.fn() } }))
const wrappers: VueWrapper[] = []
const wrap = (component: any, props: any) => {
  const wrapper = mount(component, { props, attachTo: document.body }); wrappers.push(wrapper); return wrapper
}
const catalog = { profile_id: 'office', profile_name: 'Office', notes: { '192.0.2.1': '工作电脑' } }
const targets = { profile_id: 'office', profile_name: 'Office', proxy: { id: 'proxy', name: '代理域名', problem: null, active: true }, direct: null }
const ok = (data: object) => ({ data: { success: true, ...data } } as any)
beforeEach(() => {
  vi.resetAllMocks()
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  vi.mocked(clientToolsApi.notes).mockResolvedValue(ok(catalog))
  vi.mocked(clientToolsApi.saveNote).mockResolvedValue(ok({ ...catalog, notes: { '192.0.2.1': 'NAS' } }))
  vi.mocked(clientToolsApi.ruleTargets).mockResolvedValue(ok(targets))
  vi.mocked(clientToolsApi.addRule).mockResolvedValue(ok({ added: [{}], skipped: [], warnings: ['规则集尚未启用'] }))
})
afterEach(() => { wrappers.splice(0).forEach(w => w.unmount()); document.body.innerHTML = ''; vi.unstubAllGlobals() })

it('normalizes source addresses and accepts only real domain metadata', () => {
  expect(deviceNote(catalog.notes, '::ffff:192.0.2.1')).toBe('工作电脑')
  expect(connectionDomains({ host: 'WWW.Example.COM.', sniffHost: 'www.example.com' })).toEqual(['www.example.com'])
  expect(connectionDomains({ host: '198.18.0.1', sniffHost: 'www.example.com' })).toEqual(['www.example.com'])
  for (const host of ['::1', '*.example.com', 'https://example.com', 'example.com:443', 'a\nb.com', 'a@b.com', 'localhost']) expect(connectionDomains({ host })).toEqual([])
})

it('falls back to a canonical destination IP without using source or synthetic addresses', () => {
  expect(connectionRuleValues({ destinationIP: '::ffff:192.0.2.8' })).toEqual(['192.0.2.8'])
  expect(connectionRuleValues({ destinationIP: '2001:0DB8::1' })).toEqual(['2001:db8::1'])
  expect(connectionRuleValues({ host: '192.0.2.8' })).toEqual(['192.0.2.8'])
  expect(connectionRuleValues({ host: 'domain.test', destinationIP: '192.0.2.8' })).toEqual(['domain.test'])
  expect(connectionRuleValues({ sniffHost: 'domain.test', destinationIP: '198.18.0.1', dnsMode: 'fake-ip' })).toEqual(['domain.test'])
  expect(connectionRuleValues({ destinationIP: '198.18.0.1', dnsMode: 'fake-ip' })).toEqual([])
  for (const destinationIP of ['', '0.0.0.0', '224.0.0.1', '192.0.2.0/24', 'bad', 'fe80::1%eth0']) {
    expect(connectionRuleValues({ destinationIP })).toEqual([])
  }
  expect(connectionRuleValues({ sourceIP: '192.0.2.8' } as any)).toEqual([])
})

describe('device notes', () => {
  it('saves to the bound profile with previous-name conflict guard', async () => {
    const w = wrap(DeviceNoteDialog, { agentId: 'gw', ip: '192.0.2.1' }); await flushPromises()
    const state = (w.vm as any).$.setupState
    expect(state.name).toBe('工作电脑'); state.name = 'NAS'; await state.save()
    expect(clientToolsApi.saveNote).toHaveBeenCalledExactlyOnceWith('gw', { profile_id: 'office', ip: '192.0.2.1', name: 'NAS', previous_name: '工作电脑' })
    expect(w.emitted('saved')?.[0]).toEqual([{ success: true, ...catalog, notes: { '192.0.2.1': 'NAS' } }])
    expect(w.emitted('close')).toHaveLength(1)
  })
  it('keeps edits and exposes a conflict instead of silently overwriting', async () => {
    vi.mocked(clientToolsApi.saveNote).mockRejectedValue({ response: { data: { message: '备注已改变，请重新打开' } } })
    const w = wrap(DeviceNoteDialog, { agentId: 'gw', ip: '192.0.2.1' }); await flushPromises()
    const state = (w.vm as any).$.setupState; state.name = 'NAS'; await state.save()
    expect(state.name).toBe('NAS'); expect(document.body.textContent).toContain('备注已改变'); expect(w.emitted('close')).toBeUndefined()
  })
})

describe('connection rules', () => {
  const ipTarget = { id: 'ip-proxy', name: '代理 IP', behavior: 'classical', problem: null, active: true, targets: ['proxy'] }
  it('allows choosing an IP set when the domain default cannot store IPs, and submits a single address', async () => {
    vi.mocked(clientToolsApi.ruleTargets).mockResolvedValue(ok({ ...targets, ip_rulesets: [ipTarget, { ...ipTarget, id: 'direct-ip', targets: ['direct'] }] }))
    const w = wrap(ConnectionRuleDialog, { agentId: 'gw', values: ['192.0.2.8'], target: 'proxy' }); await flushPromises()
    const state = (w.vm as any).$.setupState
    expect(state.ipTargets).toHaveLength(1)
    await state.save(); expect(clientToolsApi.addRule).not.toHaveBeenCalled()
    state.ipRulesetId = 'ip-proxy'; await flushPromises()
    expect(document.body.textContent).toContain('IP-CIDR,192.0.2.8/32,no-resolve')
    expect(document.body.textContent).not.toContain('此域名及其子域名')
    await state.save()
    expect(clientToolsApi.addRule).toHaveBeenCalledExactlyOnceWith('gw', { profile_id: 'office', ruleset_id: 'ip-proxy', target: 'proxy', value: '192.0.2.8', rule_type: 'IP-CIDR' })
  })
  it('uses a compatible default and previews bare CIDR for an IP-only set', async () => {
    vi.mocked(clientToolsApi.ruleTargets).mockResolvedValue(ok({ ...targets, ip_rulesets: [{ ...ipTarget, id: 'proxy', behavior: 'ipcidr' }] }))
    vi.mocked(clientToolsApi.addRule).mockResolvedValue(ok({ added: [], skipped: [{}], warnings: [] }))
    const w = wrap(ConnectionRuleDialog, { agentId: 'gw', values: ['2001:db8::8'], target: 'proxy' }); await flushPromises()
    const state = (w.vm as any).$.setupState
    expect(state.ipRulesetId).toBe('proxy')
    expect(state.rulePreview).toBe('2001:db8::8/128')
    await state.save()
    expect(clientToolsApi.addRule).toHaveBeenCalledExactlyOnceWith('gw', { profile_id: 'office', ruleset_id: 'proxy', target: 'proxy', value: '2001:db8::8', rule_type: 'IP-CIDR6' })
    expect(document.body.textContent).toContain('已覆盖此 IP')
  })
  it('explains missing IP targets and does not write an IP into a domain-only set', async () => {
    const w = wrap(ConnectionRuleDialog, { agentId: 'gw', values: ['192.0.2.8'], target: 'proxy' }); await flushPromises()
    await (w.vm as any).$.setupState.save()
    expect(clientToolsApi.addRule).not.toHaveBeenCalled()
    expect(document.body.textContent).toContain('暂无可用的 IP 规则集')
  })
  it.each([
    ['192.0.2.123', '24', '192.0.2.0/24', '23', '192.0.2.0/23', 'IP-CIDR', 33],
    ['2001:db8:1234:5678::abcd', '64', '2001:db8:1234:5678::/64', '56', '2001:db8:1234:5600::/56', 'IP-CIDR6', 129]
  ])('offers host and subnet matching for %s with a canonical subnet preview', async (address, defaultPrefix, defaultNetwork, otherPrefix, otherNetwork, ruleType, optionCount) => {
    vi.mocked(clientToolsApi.ruleTargets).mockResolvedValue(ok({ ...targets, ip_rulesets: [{ ...ipTarget, id: 'proxy' }] }))
    const w = wrap(ConnectionRuleDialog, { agentId: 'gw', values: [address], target: 'proxy' }); await flushPromises()
    const state = (w.vm as any).$.setupState
    expect(state.ipScope).toBe('host'); expect(state.ipPrefix).toBe(defaultPrefix)
    expect(state.prefixOptions).toHaveLength(optionCount as number)
    state.ipScope = 'network'; await flushPromises()
    expect(state.rulePreview).toBe(`${ruleType},${defaultNetwork},no-resolve`)
    state.ipPrefix = otherPrefix; await flushPromises()
    expect(state.rulePreview).toBe(`${ruleType},${otherNetwork},no-resolve`)
    state.ipScope = 'host'; await flushPromises()
    expect(state.ipCidr).toBe(`${address}/${ruleType === 'IP-CIDR' ? 32 : 128}`)
    state.ipScope = 'network'; await state.save()
    expect(clientToolsApi.addRule).toHaveBeenCalledExactlyOnceWith('gw', { profile_id: 'office', ruleset_id: 'proxy', target: 'proxy', value: otherNetwork, rule_type: ruleType })
  })
  it('previews the exact domain and submits only after confirmation', async () => {
    const w = wrap(ConnectionRuleDialog, { agentId: 'gw', values: ['www.example.com'], target: 'proxy' }); await flushPromises()
    expect(clientToolsApi.addRule).not.toHaveBeenCalled()
    expect(document.body.textContent).toContain('Office · 代理域名'); expect(document.body.textContent).toContain('DOMAIN,www.example.com')
    const state = (w.vm as any).$.setupState; await state.save()
    expect(clientToolsApi.addRule).toHaveBeenCalledExactlyOnceWith('gw', { profile_id: 'office', ruleset_id: 'proxy', target: 'proxy', value: 'www.example.com', rule_type: 'DOMAIN' })
    expect(document.body.textContent).toContain('已加入规则集'); expect(document.body.textContent).toContain('规则集尚未启用')
    await state.save(); expect(clientToolsApi.addRule).toHaveBeenCalledTimes(1)
  })
  it('blocks missing and remote defaults', async () => {
    const w = wrap(ConnectionRuleDialog, { agentId: 'gw', values: ['www.example.com'], target: 'direct' }); await flushPromises()
    const state = (w.vm as any).$.setupState; await state.save()
    expect(clientToolsApi.addRule).not.toHaveBeenCalled()
    state.state.direct = { id: 'remote', name: '远程', problem: 'not_content' }; await state.save()
    expect(clientToolsApi.addRule).not.toHaveBeenCalled(); expect(document.body.textContent).toContain('设置可编辑的默认直连规则集')
  })
  it('shows duplicate coverage and errors without claiming success on failure', async () => {
    vi.mocked(clientToolsApi.addRule).mockRejectedValueOnce({ response: { data: { message: '默认规则集已改变' } } })
      .mockResolvedValueOnce(ok({ added: [], skipped: [{}], warnings: [] }))
    const w = wrap(ConnectionRuleDialog, { agentId: 'gw', values: ['www.example.com'], target: 'proxy' }); await flushPromises()
    const state = (w.vm as any).$.setupState; await state.save()
    expect(document.body.textContent).toContain('默认规则集已改变'); expect(state.result).toBe(null)
    await state.save(); expect(document.body.textContent).toContain('无需重复添加')
  })
})
