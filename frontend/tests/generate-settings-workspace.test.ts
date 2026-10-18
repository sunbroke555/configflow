import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount } from '@vue/test-utils'
import { defineComponent, h, nextTick } from 'vue'
import api, { customConfigApi, proxyGroupApi, ruleApi, ruleSetApi } from '@/api'
import Generate from '@/views/Generate.vue'
import GenerateStudio from '@/components/generate/GenerateStudio.vue'
import SettingsWorkspace from '@/components/generate/SettingsWorkspace.vue'
import SettingsPublishBar from '@/components/generate/SettingsPublishBar.vue'
import { notify } from '@/lib/feedback'
import { scopedRequests, setActiveProfileId } from '@/profileContext'

vi.mock('@/router', () => ({ default: { push: vi.fn() } }))

const originalAdapter = api.defaults.adapter
const wrappers: ReturnType<typeof shallowMount>[] = []
const initialConfig = '# Keep this comment\nmixed-port: 7890\n'
const guard = (service: string, revision = 12) => ({
  expected_profile_id: 'workspace-profile', expected_service_type: service, expected_config_revision: revision
})
const WorkspaceStub = defineComponent({
  name: 'SettingsWorkspace',
  props: ['open', 'title', 'description', 'dirty', 'busy', 'size', 'saveLabel'],
  emits: ['save', 'update:open'],
  setup(props, { slots }) {
    return () => h('section', [slots.default?.(), slots.footer?.({ disabled: props.busy })])
  }
})
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}

beforeEach(() => {
  setActiveProfileId('workspace-profile')
  scopedRequests.value = 0
  api.defaults.adapter = async config => ({ config, data: { publish_guard: guard('mosdns') }, status: 200, statusText: 'OK', headers: {} })
  for (const method of ['getMihomo', 'getSurge', 'getLoon', 'getMosdns'] as const) {
    vi.spyOn(customConfigApi, method).mockResolvedValue({ data: { config: initialConfig, smart_groups: [{ group_id: 'group-1', policy_priority: 'HK:0' }] } } as any)
  }
  for (const method of ['saveMihomo', 'saveSurge', 'saveLoon', 'saveMosdns'] as const) {
    vi.spyOn(customConfigApi, method).mockResolvedValue({ data: { success: true, publish_guard: guard(method.slice(4).toLowerCase()) } } as any)
  }
  vi.spyOn(ruleApi, 'getAll').mockResolvedValue({ data: [] } as any)
  vi.spyOn(ruleSetApi, 'getAll').mockResolvedValue({ data: [] } as any)
  vi.spyOn(proxyGroupApi, 'getAll').mockResolvedValue({ data: [{ id: 'group-1', name: 'Group 1', type: 'url-test' }] } as any)
  vi.spyOn(notify, 'error').mockImplementation(() => 0)
  vi.spyOn(notify, 'success').mockImplementation(() => 0)
})

afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  api.defaults.adapter = originalAdapter
  scopedRequests.value = 0
  sessionStorage.clear()
  localStorage.clear()
  vi.restoreAllMocks()
})

async function openSettings(target: string, label: string) {
  const wrapper = shallowMount(Generate, { global: { stubs: { SettingsWorkspace: WorkspaceStub } } })
  wrappers.push(wrapper)
  await flushPromises()
  const action = wrapper.getComponent(GenerateStudio).props('targets')
    .find((item: any) => item.key === target)!.actions.find((item: any) => item.label === label)!
  await action.run()
  await flushPromises()
  const workspace = wrapper.findAllComponents(SettingsWorkspace).find(item => item.props('open'))!
  expect(workspace).toBeDefined()
  return { wrapper, workspace, draft: wrapper.vm as any }
}

describe('generation settings workspace integration', () => {
  it('submits the whole MosDNS draft in one request and returns that commit receipt', async () => {
    const { workspace, draft } = await openSettings('mosdns', '设置')
    draft.mosdnsCustomHosts = 'example.test 192.0.2.1'
    draft.mosdnsLogFile = 'new.log'
    const post = vi.spyOn(api, 'post').mockResolvedValue({ data: { success: true, publish_guard: guard('mosdns', 27) } } as any)
    expect(await workspace.getComponent(SettingsPublishBar).props('save')()).toEqual(guard('mosdns', 27))
    expect(post).toHaveBeenCalledExactlyOnceWith('/mosdns/settings', expect.objectContaining({
      custom_config: initialConfig, custom_hosts: 'example.test 192.0.2.1', log_file: 'new.log'
    }), { headers: { 'X-ConfigFlow-Profile': 'workspace-profile' } })
    expect(customConfigApi.saveMosdns).not.toHaveBeenCalled()
  })

  it.each([
    ['mihomo', 'saveMihomo'], ['surge', 'saveSurge'], ['loon', 'saveLoon']
  ] as const)('%s keeps configuration text intact and restores clean state after reverting', async (target, saveMethod) => {
    const { workspace, draft } = await openSettings(target, '基础配置')
    expect(workspace.props()).toMatchObject({ size: 'wide', dirty: false, busy: false })
    draft.customConfigContent = `${initialConfig}\n# Edited comment\n`
    await nextTick()
    expect(workspace.props('dirty')).toBe(true)
    draft.customConfigContent = initialConfig
    await nextTick()
    expect(workspace.props('dirty')).toBe(false)
    const edited = '  # Keep whitespace\n\ncustom: value\n'
    draft.customConfigContent = edited
    await nextTick()
    workspace.vm.$emit('save')
    await flushPromises()
    expect(customConfigApi[saveMethod]).toHaveBeenCalledExactlyOnceWith({ config: edited }, 'workspace-profile')
    expect(workspace.props('open')).toBe(false)
  })

  it('keeps the basic draft open and dirty after a save failure, then permits retry', async () => {
    const { workspace, draft } = await openSettings('mihomo', '基础配置')
    vi.mocked(customConfigApi.saveMihomo).mockRejectedValueOnce(new Error('network unavailable'))
    draft.customConfigContent = ''
    await nextTick()
    workspace.vm.$emit('save')
    await flushPromises()
    expect(workspace.props()).toMatchObject({ open: true, dirty: true, busy: false })
    expect(draft.customConfigContent).toBe('')
    expect(notify.error).toHaveBeenCalledWith('保存基础配置失败')
    workspace.vm.$emit('save')
    await flushPromises()
    expect(customConfigApi.saveMihomo).toHaveBeenLastCalledWith({ config: '' }, 'workspace-profile')
    expect(workspace.props('open')).toBe(false)
  })

  it('tracks every MosDNS draft section without treating tab navigation as an edit', async () => {
    const { workspace, draft } = await openSettings('mosdns', '设置')
    expect(workspace.props()).toMatchObject({ size: 'form', dirty: false })
    draft.mosdnsActiveTab = 'cache'
    await nextTick()
    expect(workspace.props('dirty')).toBe(false)
    const changes: Record<string, unknown> = {
      mosdnsDirectRulesets: ['direct-set'], mosdnsProxyRulesets: ['proxy-set'],
      mosdnsDirectRules: ['direct-rule'], mosdnsProxyRules: ['proxy-rule'],
      mosdnsCustomMatches: [{ id: 'match', enabled: true, matchesText: 'qname example.org', exec: 'reject 0' }],
      mosdnsCustomMatchPosition: 'head',
      mosdnsLocalDnsEntries: [{ id: 'local', mode: 'simple', addr: '223.5.5.5', bootstrap: '', enable_pipeline: false }],
      mosdnsRemoteDnsEntries: [{ id: 'remote', mode: 'yaml', yaml_config: '- addr: [' }],
      mosdnsFallbackDnsEntries: [{ id: 'fallback', mode: 'simple', addr: '1.1.1.1', bootstrap: '', enable_pipeline: true }],
      mosdnsDefaultForward: 'forward_local', mosdnsCustomHosts: 'example.org 127.0.0.1',
      mosdnsLogEnabled: false, mosdnsLogLevel: 'error', mosdnsLogFile: './edited.log',
      mosdnsApiEnabled: false, mosdnsApiAddress: '127.0.0.1:8339',
      mosdnsCacheEnabled: false, mosdnsCacheSize: 20480, mosdnsCacheLazyTtl: 1800,
      mosdnsCacheDumpEnabled: false, mosdnsCacheDumpFile: './edited.dump', mosdnsCacheDumpInterval: 600
    }
    for (const [field, value] of Object.entries(changes)) {
      const original = draft[field]
      draft[field] = value
      await nextTick()
      expect(workspace.props('dirty'), field).toBe(true)
      draft[field] = original
      await nextTick()
      expect(workspace.props('dirty'), `${field} reverted`).toBe(false)
    }
    expect(customConfigApi.saveMosdns).not.toHaveBeenCalled()
  })

  it('retains the whole MosDNS draft on save failure and closes only after a successful retry', async () => {
    const { workspace, draft } = await openSettings('mosdns', '设置')
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const post = vi.spyOn(api, 'post').mockImplementation(async (url: string) => {
      if (url === '/mosdns/settings') throw new Error('save failed')
      return { data: { success: true } } as any
    })
    draft.mosdnsCustomHosts = 'example.org 127.0.0.1'
    await nextTick()
    workspace.vm.$emit('save')
    await flushPromises()
    expect(workspace.props()).toMatchObject({ open: true, dirty: true, busy: false })
    expect(draft.mosdnsCustomHosts).toBe('example.org 127.0.0.1')
    post.mockResolvedValue({ data: { success: true } } as any)
    workspace.vm.$emit('save')
    await flushPromises()
    expect(post).toHaveBeenCalledWith('/mosdns/settings', expect.objectContaining({ custom_hosts: 'example.org 127.0.0.1' }), { headers: { 'X-ConfigFlow-Profile': 'workspace-profile' } })
    expect(workspace.props('open')).toBe(false)
  })

  it('keeps Smart edits and the existing filtered save payload', async () => {
    const { workspace, draft } = await openSettings('surge', 'Smart')
    expect(workspace.props()).toMatchObject({ size: 'compact', dirty: false })
    draft.surgeSmartGroups[0].policy_priority = 'US:1'
    await nextTick()
    expect(workspace.props('dirty')).toBe(true)
    draft.surgeSmartGroups[0].policy_priority = 'HK:0'
    await nextTick()
    expect(workspace.props('dirty')).toBe(false)
    draft.surgeSmartGroups.push({ group_id: '', policy_priority: '' })
    await nextTick()
    expect(workspace.props('dirty')).toBe(true)
    vi.mocked(customConfigApi.saveSurge).mockRejectedValueOnce(new Error('save failed'))
    workspace.vm.$emit('save')
    await flushPromises()
    expect(workspace.props()).toMatchObject({ open: true, dirty: true, busy: false })
    workspace.vm.$emit('save')
    await flushPromises()
    expect(customConfigApi.saveSurge).toHaveBeenLastCalledWith({ smart_groups: [{ group_id: 'group-1', policy_priority: 'HK:0' }] }, 'workspace-profile')
    expect(workspace.props('open')).toBe(false)
  })

  it.each([
    ['mihomo', '基础配置', 'saveMihomo'],
    ['surge', '基础配置', 'saveSurge'],
    ['surge', 'Smart', 'saveSurge']
  ] as const)('%s %s saves the draft before handing its receipt to publishing', async (target, label, saveMethod) => {
    const { workspace, draft } = await openSettings(target, label)
    const publish = workspace.getComponent(SettingsPublishBar)
    expect(publish.props()).toMatchObject({ open: true, profileId: 'workspace-profile', serviceType: target, disabled: false })
    if (label === 'Smart') draft.surgeSmartGroups[0].policy_priority = 'US:1'
    else draft.customConfigContent = 'edited: true\n'
    await nextTick()
    expect(workspace.props('dirty')).toBe(true)
    const receipt = await publish.props('save')()
    await nextTick()
    expect(receipt).toEqual(guard(target))
    expect(workspace.props()).toMatchObject({ open: true, dirty: false, busy: false })
    expect(customConfigApi[saveMethod]).toHaveBeenCalledTimes(1)
    publish.vm.$emit('save-draft')
    await flushPromises()
    expect(workspace.props('open')).toBe(false)
  })

  it('keeps Loon as draft-only because it has no publish target', async () => {
    const { workspace } = await openSettings('loon', '基础配置')
    expect(workspace.props('saveLabel')).toBe('保存草稿')
    expect(workspace.findComponent(SettingsPublishBar).exists()).toBe(false)
  })

  it('waits for the complete MosDNS save before returning a publish receipt', async () => {
    const { workspace } = await openSettings('mosdns', '设置')
    const pending = deferred<any>()
    const post = vi.spyOn(api, 'post').mockReturnValueOnce(pending.promise)
    const saving = workspace.getComponent(SettingsPublishBar).props('save')()
    await nextTick()
    expect(workspace.props('busy')).toBe(true)
    pending.resolve({ data: { success: true, publish_guard: guard('mosdns', 27) } })
    const receipt = await saving
    await nextTick()
    expect(receipt).toEqual(guard('mosdns', 27))
    expect(customConfigApi.saveMosdns).not.toHaveBeenCalled()
    expect(post.mock.calls.map(([url]) => url)).toEqual(['/mosdns/settings'])
    expect(workspace.props()).toMatchObject({ open: true, dirty: false, busy: false })
  })

  it('does not permit publishing after the complete MosDNS save fails', async () => {
    const { workspace, draft } = await openSettings('mosdns', '设置')
    vi.spyOn(console, 'error').mockImplementation(() => {})
    draft.mosdnsCustomHosts = 'example.org 127.0.0.1'
    const post = vi.spyOn(api, 'post').mockRejectedValueOnce(new Error('save failed'))
    expect(await workspace.getComponent(SettingsPublishBar).props('save')()).toBeNull()
    await nextTick()
    expect(workspace.props()).toMatchObject({ open: true, dirty: true, busy: false })
    expect(post).toHaveBeenCalledTimes(1)
    expect(customConfigApi.saveMosdns).not.toHaveBeenCalled()
  })

  it('rejects duplicate saves and tracks only the submitted draft as saved', async () => {
    const { workspace, draft } = await openSettings('mihomo', '基础配置')
    const pending = deferred<any>()
    vi.mocked(customConfigApi.saveMihomo).mockReturnValueOnce(pending.promise)
    const save = workspace.getComponent(SettingsPublishBar).props('save')
    draft.customConfigContent = 'submitted: true\n'
    const first = save()
    await nextTick()
    expect(workspace.props('busy')).toBe(true)
    expect(workspace.getComponent(SettingsPublishBar).props('disabled')).toBe(true)
    expect(await save()).toBeNull()
    expect(customConfigApi.saveMihomo).toHaveBeenCalledTimes(1)
    draft.customConfigContent = 'later: true\n'
    pending.resolve({ data: { publish_guard: guard('mihomo') } })
    expect(await first).toEqual(guard('mihomo'))
    await nextTick()
    expect(workspace.props()).toMatchObject({ open: true, dirty: true, busy: false })
    expect(customConfigApi.saveMihomo).toHaveBeenCalledWith({ config: 'submitted: true\n' }, 'workspace-profile')
  })

  it('does not publish a late save response into a reopened editor session', async () => {
    const { workspace, draft } = await openSettings('mihomo', '基础配置')
    const pending = deferred<any>()
    vi.mocked(customConfigApi.saveMihomo).mockReturnValueOnce(pending.promise)
    draft.customConfigContent = 'first: true\n'
    const savedContent = draft.customConfigSavedContent
    const saving = workspace.getComponent(SettingsPublishBar).props('save')()
    draft.customConfigDialogVisible = false
    draft.customConfigDialogVisible = true
    draft.customConfigContent = 'second: true\n'
    pending.resolve({ data: { publish_guard: guard('mihomo') } })
    expect(await saving).toBeNull()
    await nextTick()
    expect(draft.customConfigSavedContent).toBe(savedContent)
    expect(draft.customConfigContent).toBe('second: true\n')
    expect(workspace.props()).toMatchObject({ open: true, dirty: true, busy: false })
    expect(notify.success).not.toHaveBeenCalled()
  })

  it('does not publish a completed MosDNS save after the page unmounts', async () => {
    const { wrapper, workspace } = await openSettings('mosdns', '设置')
    const pending = deferred<any>()
    const post = vi.spyOn(api, 'post').mockReturnValueOnce(pending.promise)
    const saving = workspace.getComponent(SettingsPublishBar).props('save')()
    wrapper.unmount()
    wrappers.splice(wrappers.indexOf(wrapper), 1)
    pending.resolve({ data: { publish_guard: guard('mosdns') } })
    expect(await saving).toBeNull()
    expect(post).toHaveBeenCalledTimes(1)
    expect(notify.success).not.toHaveBeenCalled()
  })

  it('keeps draft saving available with an older server but never manufactures a publish receipt', async () => {
    const { workspace } = await openSettings('mihomo', '基础配置')
    vi.mocked(customConfigApi.saveMihomo).mockResolvedValue({ data: { success: true } } as any)
    expect(await workspace.getComponent(SettingsPublishBar).props('save')()).toBeNull()
    await nextTick()
    expect(workspace.props()).toMatchObject({ open: true, dirty: false, busy: false })
    workspace.vm.$emit('save')
    await flushPromises()
    expect(workspace.props('open')).toBe(false)
  })

})
