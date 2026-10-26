import { describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import { load as loadYaml, dump as dumpYaml } from 'js-yaml'
import OxidnsNativeEditor from '@/components/generate/OxidnsNativeEditor.vue'
import OxidnsConfigNavigation from '@/components/generate/OxidnsConfigNavigation.vue'
import OxidnsCommonSettings from '@/components/generate/OxidnsCommonSettings.vue'
import { agentApi, profileApi, oxidnsApi, ruleApi, ruleSetApi } from '@/api'
import { confirmState, settleConfirm } from '@/lib/feedback'
import SettingsWorkspace from '@/components/generate/SettingsWorkspace.vue'

import { config, projection, settings, render, button, click, settleValidation, input, savedPayload, views, worker, removeNativePlugin } from './helpers/oxidns-settings'

vi.mock('@/api', () => ({
  agentApi: { getAll: vi.fn(), summaries: vi.fn(), pushConfig: vi.fn(), getDeployment: vi.fn(), activateDeployment: vi.fn() },
  profileApi: { list: vi.fn() },
  oxidnsApi: { getSettings: vi.fn(), saveSettings: vi.fn(), prepareEditor: vi.fn(), readCommonSettings: vi.fn(), applyCommonSettings: vi.fn(), applyNativeSettings: vi.fn(), validateSettings: vi.fn() },
  ruleApi: { getAll: vi.fn() }, ruleSetApi: { getAll: vi.fn() }
}))
describe('unified OxiDNS profile settings', () => {
  it('saves all settings metadata returned by native deletion and dependency reconciliation', async () => {
    const original = {...settings(), extension_settings: {keep: false}, common_editor_state: {existing: {value: 0}}}
    const doc = loadYaml(original.native_config) as any
    const cache = {tag: 'lazy_cache', type: 'cache', args: {size: 10240, lazy_cache_ttl: 300}}
    doc.plugins = [cache, {tag: 'main', type: 'sequence', args: [{exec: '$lazy_cache'}, {exec: 'reject 0'}]}]
    original.native_config = dumpYaml(doc)
    const result = {...original, native_config: dumpYaml({...doc, plugins: [{tag: 'main', type: 'sequence', args: [{exec: 'reject 0'}]}]}),
      common_editor_state: {...original.common_editor_state, parked_plugins: {lazy_cache: cache}}}
    const common = {...projection(result), values: {...projection(result).values, cache_enabled: false}}
    vi.mocked(oxidnsApi.getSettings).mockResolvedValue({data: original} as any)
    vi.mocked(oxidnsApi.applyNativeSettings).mockResolvedValueOnce({data: {settings: result, ...common, notices: ['已停用主缓存并移除执行引用。']}} as any)
    const wrapper = await render()
    await removeNativePlugin(wrapper, 'lazy_cache'); await settleValidation()
    expect(oxidnsApi.applyNativeSettings).toHaveBeenCalledOnce()
    expect(vi.mocked(oxidnsApi.applyNativeSettings).mock.calls[0].slice(0, 3)).toEqual([expect.objectContaining({native_config: original.native_config}), expect.any(String), 'oxi-profile'])
    expect(vi.mocked(oxidnsApi.applyNativeSettings).mock.calls[0][3] ?? {}).toEqual({})
    const candidate = loadYaml(vi.mocked(oxidnsApi.applyNativeSettings).mock.calls[0][1]) as any
    expect(candidate.plugins.map((plugin: any) => plugin.tag)).toEqual(['main'])
    expect(candidate.plugins[0].args[0]).toEqual({exec: '$lazy_cache'})
    expect(wrapper.getComponent(OxidnsNativeEditor).props('modelValue')).toBe(result.native_config)
    expect(document.body.textContent).toContain('已停用主缓存并移除执行引用')
    await click('常用设置', views())
    expect(wrapper.getComponent(OxidnsCommonSettings).props('values')).toEqual(common.values)
    expect(oxidnsApi.readCommonSettings).toHaveBeenCalledOnce()
    await click('保存草稿')
    expect(savedPayload()).toEqual(result)
    expect(wrapper.getComponent(SettingsWorkspace).props('dirty')).toBe(false)
  })

  it('passes explicit standard restoration intent and saves restored parameters with returned metadata', async () => {
    const cache = {tag: 'lazy_cache', type: 'cache', args: {size: 4096, lazy_cache_ttl: 900, extension: false}}
    const original = {...settings(), common_editor_state: {parked_plugins: {lazy_cache: cache}, unrelated: {keep: false}}}
    const doc = loadYaml(original.native_config) as any
    doc.plugins = [cache, {tag: 'main', type: 'sequence', args: [{exec: '$lazy_cache'}, {exec: 'reject 0'}]}]
    const restored = {...original, native_config: dumpYaml(doc), common_editor_state: {unrelated: {keep: false}}}
    vi.mocked(oxidnsApi.getSettings).mockResolvedValue({data: original} as any)
    vi.mocked(oxidnsApi.applyNativeSettings).mockResolvedValueOnce({data: {settings: restored, ...projection(restored), notices: ['已恢复主缓存及原参数']}} as any)
    const wrapper = await render()
    await click('完整参数', views())
    wrapper.getComponent(OxidnsNativeEditor).getComponent(OxidnsConfigNavigation).vm.$emit('select', 'add:standard:lazy_cache')
    await flushPromises(); await settleValidation()
    expect(oxidnsApi.applyNativeSettings).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({native_config: original.native_config, common_editor_state: original.common_editor_state}), expect.any(String), 'oxi-profile', undefined, ['lazy_cache'])
    const candidate = loadYaml(vi.mocked(oxidnsApi.applyNativeSettings).mock.calls[0][1]) as any
    expect(candidate.plugins.at(-1)).toEqual({tag: 'lazy_cache', type: 'cache', args: {}})
    expect(document.body.textContent).toContain('已恢复主缓存及原参数')
    expect((document.querySelector('[aria-label="插件 1.args.size 值"]') as HTMLInputElement).value).toBe('4096')
    await click('保存草稿')
    expect(savedPayload()).toEqual(restored)
  })

  it('blocks saving and publishing while native synchronization is pending and protects its dirty draft', async () => {
    let resolveApply!: (value: any) => void
    vi.mocked(oxidnsApi.applyNativeSettings).mockImplementationOnce(() => new Promise(resolve => { resolveApply = resolve }))
    const wrapper = await render()
    await removeNativePlugin(wrapper, 'main')
    expect(oxidnsApi.applyNativeSettings).toHaveBeenCalledOnce()
    expect(wrapper.getComponent(OxidnsNativeEditor).props('modelValue')).toBe(config)
    expect(wrapper.getComponent(SettingsWorkspace).props('dirty')).toBe(true)
    expect(button('保存草稿').disabled).toBe(true)
    expect(button('保存并发布').disabled).toBe(true)
    wrapper.getComponent(SettingsWorkspace).vm.$emit('save')
    await click('保存并发布')
    expect(oxidnsApi.saveSettings).not.toHaveBeenCalled()
    expect(agentApi.pushConfig).not.toHaveBeenCalled()
    ;(document.querySelector('[aria-label="返回生成工作台"]') as HTMLButtonElement).click(); await flushPromises()
    expect(confirmState.open).toBe(true)
    settleConfirm('cancel'); await flushPromises()
    expect(wrapper.emitted('update:open')).toBeUndefined()

    const applied = {...settings(), native_config: vi.mocked(oxidnsApi.applyNativeSettings).mock.calls[0][1]}
    resolveApply({data: {settings: applied, ...projection(applied), notices: []}})
    await flushPromises(); await settleValidation()
    expect(button('保存草稿').disabled).toBe(false)
    expect(button('保存并发布').disabled).toBe(false)
    expect(wrapper.getComponent(SettingsWorkspace).props('dirty')).toBe(true)
    await click('保存草稿')
    expect((loadYaml(savedPayload().native_config) as any).plugins).toEqual([])
  })

  it('keeps the latest native metadata when concurrent responses resolve in reverse order in one tick', async () => {
    const resolvers: Array<(value: any) => void> = []
    vi.mocked(oxidnsApi.applyNativeSettings).mockImplementation(() => new Promise(resolve => { resolvers.push(resolve) }))
    const wrapper = await render()
    const native = wrapper.getComponent(OxidnsNativeEditor)
    const reconcile = native.props('reconcile') as (before: string, candidate: string, renames?: Record<string, string>) => Promise<{native_config: string; notices?: string[]}>
    const older = {...settings(), native_config: config.replace('worker_threads: 8', 'worker_threads: 3').replace('tag: main', 'tag: old_main'), common_editor_state: {revision: 'older'}}
    const latest = {...settings(), native_config: config.replace('worker_threads: 8', 'worker_threads: 4').replace('tag: main', 'tag: new_main'),
      common_editor_state: {revision: 'latest', preserved: {empty: '', off: false}}, extension_settings: {value: 0}}
    const first = reconcile(config, older.native_config, {main: 'old_main'})
    const second = reconcile(config, latest.native_config, {main: 'new_main'})
    const outcomes = Promise.allSettled([first, second])
    expect(oxidnsApi.applyNativeSettings).toHaveBeenCalledTimes(2)
    expect(vi.mocked(oxidnsApi.applyNativeSettings).mock.calls[1]).toEqual([expect.objectContaining({native_config: config}), latest.native_config, 'oxi-profile', {main: 'new_main'}, undefined])
    // Both callbacks run before the editor accepts the winning text. A shared
    // response slot must not let the older response replace its full metadata.
    resolvers[1]({data: {settings: latest, ...projection(latest), notices: ['最新联动']}})
    resolvers[0]({data: {settings: older, ...projection(older), notices: ['过期联动']}})
    const [stale, current] = await outcomes
    expect(stale.status).toBe('rejected')
    expect(current.status).toBe('fulfilled')
    if (current.status !== 'fulfilled') throw new Error('Expected the latest native response')
    native.vm.$emit('update:modelValue', current.value.native_config)
    await flushPromises(); await settleValidation()
    expect(wrapper.getComponent(OxidnsCommonSettings).props('values').worker_threads).toBe(4)
    await click('保存草稿')
    expect(savedPayload()).toEqual(latest)
    expect(wrapper.getComponent(SettingsWorkspace).props('dirty')).toBe(false)
  })

  it('blocks validation navigation and attached-file edits during native synchronization', async () => {
    const file = {path: 'plugins/cache.yaml', content: 'plugins: []', encoding: 'text'}
    const original = {...settings(), additional_files: [file]}
    vi.mocked(oxidnsApi.getSettings).mockResolvedValue({data: original} as any)
    vi.mocked(oxidnsApi.validateSettings).mockResolvedValue({data: {valid: false, issues: [{path: ['plugins'], source: file.path, code: 'invalid_structure', message: '附加文件待调整', severity: 'error'}]}} as any)
    let resolveApply!: (value: any) => void
    vi.mocked(oxidnsApi.applyNativeSettings).mockImplementationOnce(() => new Promise(resolve => { resolveApply = resolve }))
    const wrapper = await render()
    const locate = () => document.querySelector<HTMLButtonElement>('[aria-label="定位错误 1"]')!
    const pathInput = () => document.querySelector<HTMLInputElement>('[aria-label="文件 1 路径"]')!
    expect(locate().disabled).toBe(false)
    await removeNativePlugin(wrapper, 'main')
    expect(locate().disabled).toBe(true)
    expect(pathInput().disabled).toBe(true)
    locate().click(); await flushPromises()
    expect(button('附加文件').getAttribute('aria-pressed')).toBe('false')
    expect(pathInput().value).toBe(file.path)

    const applied = {...original, native_config: vi.mocked(oxidnsApi.applyNativeSettings).mock.calls[0][1]}
    resolveApply({data: {settings: applied, ...projection(applied), notices: []}})
    await flushPromises(); await settleValidation()
    expect(locate().disabled).toBe(false)
    expect(pathInput().disabled).toBe(false)
    locate().click(); await flushPromises()
    expect(button('附加文件').getAttribute('aria-pressed')).toBe('true')
    expect(document.activeElement).toBe(pathInput())
    await click('保存草稿')
    expect(savedPayload().additional_files).toEqual([file])
  })

  it('retries a failed native synchronization without saving the previous document', async () => {
    vi.mocked(oxidnsApi.applyNativeSettings).mockRejectedValueOnce({response: {data: {message: '配置依赖同步暂时失败'}}})
    const wrapper = await render()
    await removeNativePlugin(wrapper, 'main')
    expect(document.body.textContent).toContain('配置依赖同步暂时失败')
    expect(wrapper.getComponent(OxidnsNativeEditor).props('modelValue')).toBe(config)
    expect(wrapper.getComponent(SettingsWorkspace).props('dirty')).toBe(true)
    expect(button('保存草稿').disabled).toBe(true)
    expect(button('保存并发布').disabled).toBe(true)
    expect(oxidnsApi.saveSettings).not.toHaveBeenCalled()
    const candidate = vi.mocked(oxidnsApi.applyNativeSettings).mock.calls[0][1]
    await click('重试同步'); await settleValidation()
    expect(oxidnsApi.applyNativeSettings).toHaveBeenCalledTimes(2)
    expect(vi.mocked(oxidnsApi.applyNativeSettings).mock.calls[1][1]).toBe(candidate)
    expect(button('保存草稿').disabled).toBe(false)
    expect(document.body.textContent).not.toContain('配置依赖同步暂时失败')
    await click('保存草稿')
    expect(savedPayload().native_config).toBe(candidate)
  })

  it('discards a failed native edit while retaining earlier synchronized changes', async () => {
    const wrapper = await render()
    await input(worker, '4')
    const synchronized = wrapper.getComponent(OxidnsNativeEditor).props('modelValue')
    vi.mocked(oxidnsApi.applyNativeSettings).mockRejectedValueOnce({response: {data: {message: '存在无法自动调整的引用'}}})
    await removeNativePlugin(wrapper, 'main')
    expect(button('保存草稿').disabled).toBe(true)
    await click('撤销未同步修改')
    expect(wrapper.getComponent(OxidnsNativeEditor).props('modelValue')).toBe(synchronized)
    expect(document.body.textContent).not.toContain('存在无法自动调整的引用')
    expect(button('保存草稿').disabled).toBe(false)
    expect(wrapper.getComponent(SettingsWorkspace).props('dirty')).toBe(true)
    await click('保存草稿')
    expect((loadYaml(savedPayload().native_config) as any)).toMatchObject({runtime: {worker_threads: 4}, plugins: [{tag: 'main'}]})
    expect(oxidnsApi.applyNativeSettings).toHaveBeenCalledOnce()
  })

  it('ignores native synchronization responses from a previously selected profile', async () => {
    let resolveApply!: (value: any) => void
    vi.mocked(oxidnsApi.applyNativeSettings).mockImplementationOnce(() => new Promise(resolve => { resolveApply = resolve }))
    const wrapper = await render()
    await removeNativePlugin(wrapper, 'main')
    const candidate = vi.mocked(oxidnsApi.applyNativeSettings).mock.calls[0][1]
    const next = {...settings(), native_config: config.replace('worker_threads: 8', 'worker_threads: 6'), common_editor_state: {profile: 'next'}}
    vi.mocked(oxidnsApi.getSettings).mockResolvedValue({data: next} as any)
    await wrapper.setProps({profileId: 'next-profile'}); await flushPromises()
    const stale = {...settings(), native_config: candidate, common_editor_state: {profile: 'old'}}
    resolveApply({data: {settings: stale, ...projection(stale), notices: ['过期联动提示']}})
    await flushPromises(); await settleValidation()
    expect((document.querySelector(worker) as HTMLInputElement).value).toBe('6')
    expect(wrapper.getComponent(SettingsWorkspace).props('dirty')).toBe(false)
    expect(button('保存草稿').disabled).toBe(false)
    expect(document.body.textContent).not.toContain('过期联动提示')
    expect(document.body.textContent).not.toContain('当前配置已切换')
    await click('保存草稿')
    expect(oxidnsApi.saveSettings).toHaveBeenCalledExactlyOnceWith(next, 'next-profile', undefined)
  })


})
