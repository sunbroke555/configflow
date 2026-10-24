import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount } from '@vue/test-utils'
import { defineComponent, h, nextTick } from 'vue'
import api, { customConfigApi, ruleApi, ruleSetApi } from '@/api'
import Generate from '@/views/Generate.vue'
import OxidnsSettings from '@/components/generate/OxidnsSettings.vue'
import { notify } from '@/lib/feedback'
import { scopedRequests, setActiveProfileId } from '@/profileContext'
import type { RuleDnsSnapshot } from '@/types'

const router = vi.hoisted(() => ({
  currentRoute: { value: { query: {} as Record<string, string> } },
  push: vi.fn(),
  replace: vi.fn(async () => undefined)
}))
vi.mock('@/router', () => ({ default: router }))

const originalAdapter = api.defaults.adapter
const wrappers: ReturnType<typeof shallowMount>[] = []
const WorkspaceStub = defineComponent({
  name: 'SettingsWorkspace',
  props: ['open', 'title', 'description', 'dirty', 'busy', 'size', 'saveLabel', 'backLabel'],
  emits: ['save', 'update:open'],
  setup(_props, { slots }) {
    return () => h('section', [slots.default?.()])
  }
})
const mosdnsLists = { direct_rules: ['rule-direct'], proxy_rules: [], direct_rulesets: [], proxy_rulesets: ['set-proxy'] }
const snapshot = (rules: RuleDnsSnapshot['rules'], inUse = true): RuleDnsSnapshot => ({
  engine: { mode: 'template', editable: true, reason: '', default_forward: 'forward_remote', prefer_ipv4: true,
    local_probe_active: false, oxidns_in_use: inUse, warnings: [], agents: [] },
  upstreams: [], rules, mosdns: {}
})

beforeEach(() => {
  setActiveProfileId('deeplink-profile')
  scopedRequests.value = 0
  router.currentRoute.value = { query: {} }
  api.defaults.adapter = async config => ({
    config, data: config.url === '/mosdns/rulesets' ? structuredClone(mosdnsLists) : {}, status: 200, statusText: 'OK', headers: {}
  })
  vi.spyOn(customConfigApi, 'getMosdns').mockResolvedValue({ data: { config: '' } } as any)
  vi.spyOn(ruleApi, 'getAll').mockResolvedValue({ data: ['rule-direct', 'rule-block', 'rule-pinned', 'rule-proxy', 'rule-geo']
    .map(id => ({ id, itemType: 'rule', rule_type: id === 'rule-geo' ? 'GEOIP' : 'DOMAIN', value: id })) } as any)
  vi.spyOn(ruleSetApi, 'getAll').mockResolvedValue({ data: [{ id: 'set-proxy', name: 'Proxy set' }, { id: 'set-off', name: 'Off set' }] } as any)
  vi.spyOn(notify, 'error').mockImplementation(() => 0)
  vi.spyOn(notify, 'success').mockImplementation(() => 0)
})

afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  api.defaults.adapter = originalAdapter
  scopedRequests.value = 0
  vi.restoreAllMocks()
  router.replace.mockClear()
})

describe('OxiDNS full-parameter deep link', () => {
  it('opens the OxiDNS settings on the full parameters and drops the one-time query', async () => {
    router.currentRoute.value = { query: { oxidns: 'config', keep: 'yes' } }
    const wrapper = shallowMount(Generate)
    wrappers.push(wrapper)
    await flushPromises()
    const editor = wrapper.getComponent(OxidnsSettings)
    expect(editor.props()).toMatchObject({ open: true, initialTab: 'config', profileId: 'deeplink-profile' })
    expect(router.replace).toHaveBeenCalledExactlyOnceWith({ query: { keep: 'yes' } })
    editor.vm.$emit('update:open', false)
    await nextTick()
    expect(editor.props()).toMatchObject({ open: false, initialTab: undefined })
  })

  it('leaves the settings closed without the link', async () => {
    const wrapper = shallowMount(Generate)
    wrappers.push(wrapper)
    await flushPromises()
    expect(wrapper.getComponent(OxidnsSettings).props()).toMatchObject({ open: false, initialTab: undefined })
    expect(router.replace).not.toHaveBeenCalled()
  })
})

describe('MosDNS settings note for rules handled differently by OxiDNS', () => {
  const renderMosdns = async () => {
    const wrapper = shallowMount(Generate, {
      props: { agentSettings: { agentId: 'dns', agentName: 'MosDNS', profileId: 'deeplink-profile', profileName: '主空间', serviceType: 'mosdns' } },
      global: { stubs: { SettingsWorkspace: WorkspaceStub, InfoNote: false }, renderStubDefaultSlot: true }
    })
    wrappers.push(wrapper)
    await flushPromises()
    return wrapper
  }
  const note = (wrapper: ReturnType<typeof shallowMount>) => wrapper.find('[data-oxidns-dns-difference]')

  it('counts blocks, pinned upstreams and different routes against the MosDNS draft', async () => {
    vi.spyOn(ruleApi, 'getDns').mockResolvedValue({ data: { success: true, ...snapshot({
      'rule-direct': { state: 'follow', route: 'direct', follow_route: 'direct' },
      'rule-block': { state: 'explicit', route: 'nxdomain' },
      'rule-pinned': { state: 'explicit', route: 'upstream:b2e55ef47454', upstream_label: '直连 · 10.0.0.53' },
      'rule-proxy': { state: 'explicit', route: 'proxy' },
      'rule-geo': { state: 'unsupported', reason: 'GEOIP 规则不参与 DNS 分流' },
      'set-off': { state: 'disabled', route: 'blackhole' }
    }) } } as any)
    const wrapper = await renderMosdns()
    expect(ruleApi.getDns).toHaveBeenCalledWith('deeplink-profile')
    // set-proxy is omitted (OxiDNS default) but MosDNS proxies it; rule-proxy is the reverse.
    expect(note(wrapper).text()).toBe('4 条规则在 OxiDNS 中的 DNS 处理与 MosDNS 不同（拦截 1 · 指定上游 1 · 直连/代理 2），MosDNS 按自身设置解析')
    ;(wrapper.vm as any).mosdnsProxyRules = ['rule-proxy']
    ;(wrapper.vm as any).mosdnsProxyRulesets = []
    await nextTick()
    expect(note(wrapper).text()).toBe('2 条规则在 OxiDNS 中的 DNS 处理与 MosDNS 不同（拦截 1 · 指定上游 1），MosDNS 按自身设置解析')
  })

  it('stays quiet when OxiDNS is not used, both sides agree, or the DNS state cannot be read', async () => {
    const getDns = vi.spyOn(ruleApi, 'getDns')
    getDns.mockResolvedValueOnce({ data: { success: true, ...snapshot({ 'rule-block': { state: 'explicit', route: 'refused' } }, false) } } as any)
    expect(note(await renderMosdns()).exists()).toBe(false)
    getDns.mockResolvedValueOnce({ data: { success: true, ...snapshot({
      'rule-direct': { state: 'explicit', route: 'direct' }, 'set-proxy': { state: 'follow', route: 'proxy', follow_route: 'proxy' }
    }) } } as any)
    expect(note(await renderMosdns()).exists()).toBe(false)
    vi.spyOn(console, 'error').mockImplementation(() => undefined)
    getDns.mockRejectedValueOnce(new Error('offline'))
    const wrapper = await renderMosdns()
    expect(note(wrapper).exists()).toBe(false)
    expect(notify.error).not.toHaveBeenCalled()
    expect(wrapper.emitted('ready')).toHaveLength(1)
  })
})
