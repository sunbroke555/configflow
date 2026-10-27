import { describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import { load as loadYaml, dump as dumpYaml } from 'js-yaml'
import OxidnsNativeEditor from '@/components/generate/OxidnsNativeEditor.vue'
import OxidnsCommonSettings from '@/components/generate/OxidnsCommonSettings.vue'
import OxidnsFilesEditor from '@/components/generate/OxidnsFilesEditor.vue'
import { agentApi, profileApi, oxidnsApi } from '@/api'
import { confirmState, settleConfirm } from '@/lib/feedback'
import SettingsWorkspace from '@/components/generate/SettingsWorkspace.vue'

import { config, projection, settings, render, button, click, settleValidation, input, savedPayload, views, worker, yamlInput } from './helpers/oxidns-settings'

vi.mock('@/api', () => ({
  agentApi: { getAll: vi.fn(), summaries: vi.fn(), pushConfig: vi.fn(), getDeployment: vi.fn(), activateDeployment: vi.fn() },
  profileApi: { list: vi.fn() },
  oxidnsApi: { getSettings: vi.fn(), saveSettings: vi.fn(), prepareEditor: vi.fn(), readCommonSettings: vi.fn(), applyCommonSettings: vi.fn(), applyNativeSettings: vi.fn(), validateSettings: vi.fn() },
}))
describe('unified OxiDNS profile settings', () => {
  it.each([
    ['cache_size', 'cache_enabled'],
    ['cache_dump_interval', 'cache_dump_enabled'],
  ])('synchronizes and saves disabling %s despite its unfinished draft', async (field, toggle) => {
    const initial = settings()
    const doc = loadYaml(initial.native_config) as any
    doc.plugins = [
      {tag: 'lazy_cache', type: 'cache', args: {size: 10240, lazy_cache_ttl: 300, dump_file: 'state/cache.dump', dump_interval: 600}},
      {tag: 'probe_cache', type: 'cache', args: {size: 2048}},
      {tag: 'main', type: 'sequence', args: [{exec: '$lazy_cache'}, {exec: '$probe_cache'}, {exec: 'reject 0'}]},
    ]
    initial.native_config = dumpYaml(doc)
    const cacheProjection = (settings: any) => {
      const plugins = (loadYaml(settings.native_config) as any).plugins
      const main = plugins.find((plugin: any) => plugin.tag === 'lazy_cache')
      const probe = plugins.find((plugin: any) => plugin.tag === 'probe_cache')
      return {values: {...projection(settings).values, cache_enabled: Boolean(main), cache_size: main?.args.size, cache_lazy_ttl: main?.args.lazy_cache_ttl,
        probe_cache_enabled: Boolean(probe), probe_cache_size: probe?.args.size, cache_dump_enabled: Boolean(main?.args.dump_file), cache_dump_file: main?.args.dump_file, cache_dump_interval: main?.args.dump_interval}, capabilities: {}}
    }
    vi.mocked(oxidnsApi.getSettings).mockResolvedValue({data: initial} as any)
    vi.mocked(oxidnsApi.readCommonSettings).mockImplementation(async value => ({data: cacheProjection(value)}) as any)
    vi.mocked(oxidnsApi.applyCommonSettings).mockImplementation(async (value: any, changes) => {
      const updated = JSON.parse(JSON.stringify(value))
      const document = loadYaml(updated.native_config) as any
      for (const [key, tag] of [['cache_enabled', 'lazy_cache'], ['probe_cache_enabled', 'probe_cache']]) {
        if (changes[key] === false) {
          document.plugins = document.plugins.filter((plugin: any) => plugin.tag !== tag)
          document.plugins.find((plugin: any) => plugin.tag === 'main').args = document.plugins.find((plugin: any) => plugin.tag === 'main').args.filter((step: any) => step.exec !== `$${tag}`)
        }
      }
      if (changes.cache_dump_enabled === false) {
        const args = document.plugins.find((plugin: any) => plugin.tag === 'lazy_cache').args
        delete args.dump_file
        delete args.dump_interval
      }
      updated.native_config = dumpYaml(document)
      return {data: {settings: updated, ...cacheProjection(updated)}} as any
    })
    const wrapper = await render()
    await click('缓存')
    await input(`#oxi-${field}`, '')
    expect(button('保存草稿').disabled).toBe(true)
    expect(oxidnsApi.applyCommonSettings).not.toHaveBeenCalled()

    ;(document.getElementById(`oxi-${toggle}`) as HTMLButtonElement).click(); await flushPromises()
    await vi.advanceTimersByTimeAsync(180); await flushPromises(); await settleValidation()
    expect(oxidnsApi.applyCommonSettings).toHaveBeenCalledExactlyOnceWith(expect.any(Object), {[toggle]: false}, 'oxi-profile')
    expect((document.getElementById(`oxi-${field}`) as HTMLInputElement).value).toBe('')
    expect(button('保存草稿').disabled).toBe(false)
    await click('保存草稿')
    expect(oxidnsApi.saveSettings).toHaveBeenCalledOnce()
    expect(cacheProjection(savedPayload()).values[toggle as 'cache_enabled' | 'probe_cache_enabled' | 'cache_dump_enabled']).toBe(false)
    expect((loadYaml(savedPayload().native_config) as any).unknown).toEqual(doc.unknown)
    expect(savedPayload().native_config).not.toMatch(/(?:size|dump_interval): 0/)
    expect(wrapper.findComponent(SettingsWorkspace).props('dirty')).toBe(false)
  })

  it('removes the template tool and applies common changes to the shared draft without saving', async () => {
    const wrapper = await render()
    expect(document.querySelector('[aria-label="模板工具"]')).toBeNull()
    wrapper.getComponent(OxidnsCommonSettings).vm.$emit('change', { worker_threads: 3 })
    await vi.advanceTimersByTimeAsync(180); await flushPromises(); await settleValidation()
    expect(oxidnsApi.applyCommonSettings).toHaveBeenCalledWith(expect.objectContaining({ native_config: config }), { worker_threads: 3 }, 'oxi-profile')
    expect(oxidnsApi.saveSettings).not.toHaveBeenCalled()
    expect((document.querySelector(worker) as HTMLInputElement).value).toBe('3')
    await click('保存草稿')
    expect((loadYaml(savedPayload().native_config) as any).runtime.worker_threads).toBe(3)
    expect(savedPayload().template_settings).toEqual({ worker_threads: 2 })
  })

  it('orders rapid edits and blocks saving until the queue is drained', async () => {
    await render()
    let resolveFirst!: (value: any) => void
    vi.mocked(oxidnsApi.applyCommonSettings).mockImplementationOnce(() => new Promise(resolve => { resolveFirst = resolve }))
    await input(worker, '3')
    expect(button('保存草稿').disabled).toBe(true)
    expect(button('YAML', views()).disabled).toBe(true)
    await input(worker, '4')
    expect((document.querySelector(worker) as HTMLInputElement).value).toBe('4')
    const first = { ...settings(), native_config: config.replace('worker_threads: 8', 'worker_threads: 3') }
    resolveFirst({ data: { settings: first, ...projection(first) } }); await flushPromises(); await settleValidation()
    expect(oxidnsApi.applyCommonSettings).toHaveBeenCalledTimes(2)
    expect(vi.mocked(oxidnsApi.applyCommonSettings).mock.calls[1][0]).toEqual(first)
    expect((document.querySelector(worker) as HTMLInputElement).value).toBe('4')
    expect(button('保存草稿').disabled).toBe(false)
    await click('保存草稿')
    expect((loadYaml(savedPayload().native_config) as any).runtime.worker_threads).toBe(4)
  })

  it('protects an unsaved common edit while its draft synchronization is still in flight', async () => {
    const wrapper = await render()
    let resolveApply!: (value: any) => void
    vi.mocked(oxidnsApi.applyCommonSettings).mockImplementationOnce(() => new Promise(resolve => { resolveApply = resolve }))
    await input(worker, '3')
    expect(oxidnsApi.applyCommonSettings).toHaveBeenCalledTimes(1)
    expect(wrapper.findComponent(OxidnsNativeEditor).props('modelValue')).toBe(config)
    expect(wrapper.findComponent(SettingsWorkspace).props('dirty')).toBe(true)
    ;(document.querySelector('[aria-label="返回生成工作台"]') as HTMLButtonElement).click()
    await flushPromises()
    expect(confirmState.open).toBe(true)
    settleConfirm('cancel')
    await flushPromises()
    expect(wrapper.emitted('update:open')).toBeUndefined()
    expect((document.querySelector(worker) as HTMLInputElement).value).toBe('3')

    const applied = { ...settings(), native_config: config.replace('worker_threads: 8', 'worker_threads: 3') }
    resolveApply({ data: { settings: applied, ...projection(applied) } })
    await flushPromises()
    await settleValidation()
    expect(wrapper.findComponent(SettingsWorkspace).props('dirty')).toBe(true)
    expect((document.querySelector(worker) as HTMLInputElement).value).toBe('3')
    expect(oxidnsApi.saveSettings).not.toHaveBeenCalled()
    await click('保存草稿')
    expect((loadYaml(savedPayload().native_config) as any).runtime.worker_threads).toBe(3)
  })

  it('keeps TLS controls and the saved draft consistent after off/on clicks share one patch', async () => {
    const tls = {server_name: 'router.example.test', ca: 'certs/router-ca.pem', insecure: false}
    const args = {address: '192.0.2.1:8729', tls}
    const doc = loadYaml(config) as any
    doc.plugins.push({tag: 'ros_route', type: 'ros_route', args})
    const original = {...settings(), native_config: dumpYaml(doc)}
    const common = () => ({values: {...projection(original).values, ros_route: {enabled: true, args: structuredClone(args)}}, capabilities: {}})
    vi.mocked(oxidnsApi.getSettings).mockResolvedValue({data: original} as any)
    vi.mocked(oxidnsApi.readCommonSettings).mockImplementation(async () => ({data: common()}) as any)
    vi.mocked(oxidnsApi.applyCommonSettings).mockImplementationOnce(async value => ({data: {settings: JSON.parse(JSON.stringify(value)), ...common()}}) as any)
    await render()
    await click('特色功能')
    ;(document.querySelector('[aria-label="RouterOS 主机路由设置"]') as HTMLButtonElement).click()
    await flushPromises()
    const toggle = () => document.getElementById('oxi-ros_route.args.tls') as HTMLButtonElement
    toggle().click(); await flushPromises()
    toggle().click(); await flushPromises()
    await vi.advanceTimersByTimeAsync(180); await flushPromises(); await settleValidation()
    expect(oxidnsApi.applyCommonSettings).toHaveBeenCalledTimes(1)
    expect(oxidnsApi.applyCommonSettings).toHaveBeenCalledWith(expect.anything(), {'ros_route.args.tls': {}}, 'oxi-profile')
    expect((document.getElementById('oxi-ros_route.args.tls.server_name') as HTMLInputElement).value).toBe(tls.server_name)
    expect((document.getElementById('oxi-ros_route.args.tls.ca') as HTMLInputElement).value).toBe(tls.ca)
    await click('保存草稿')
    expect((loadYaml(savedPayload().native_config) as any).plugins.find((plugin: any) => plugin.tag === 'ros_route').args.tls).toEqual(tls)
  })

  it('retains invalid drafts without turning an empty number into zero', async () => {
    await render()
    await input(worker, '')
    expect((document.querySelector(worker) as HTMLInputElement).value).toBe('')
    expect(button('保存草稿').disabled).toBe(true)
    expect(oxidnsApi.applyCommonSettings).not.toHaveBeenCalled()
    await input(worker, '3')
    expect(button('保存草稿').disabled).toBe(false)
    expect(vi.mocked(oxidnsApi.applyCommonSettings).mock.calls[0][1]).toEqual({ worker_threads: 3 })
  })

  it('retains failed changes until retry without saving stale values', async () => {
    await render()
    vi.mocked(oxidnsApi.applyCommonSettings).mockRejectedValueOnce({ response: { data: { message: '当前结构无法自动修改' } } })
    await input(worker, '3')
    expect(document.body.textContent).toContain('当前结构无法自动修改')
    expect(button('保存草稿').disabled).toBe(true)
    expect((document.querySelector(worker) as HTMLInputElement).value).toBe('3')
    await click('重试同步'); await settleValidation()
    expect(button('保存草稿').disabled).toBe(false)
    await click('YAML', views())
    expect((loadYaml((document.querySelector(yamlInput) as HTMLTextAreaElement).value) as any).runtime.worker_threads).toBe(3)
  })

  it('discards only unsynchronized edits and restores the actual values', async () => {
    await render()
    await input(worker, '4')
    vi.mocked(oxidnsApi.applyCommonSettings).mockRejectedValueOnce(new Error('Unavailable'))
    await input(worker, '5')
    await click('撤销未同步修改')
    expect((document.querySelector(worker) as HTMLInputElement).value).toBe('4')
    expect(button('保存草稿').disabled).toBe(false)
    await click('保存草稿')
    expect((loadYaml(savedPayload().native_config) as any).runtime.worker_threads).toBe(4)
  })

  it('ignores a stale apply response after changing profile', async () => {
    const wrapper = await render()
    let resolveFirst!: (value: any) => void
    vi.mocked(oxidnsApi.applyCommonSettings).mockImplementationOnce(() => new Promise(resolve => { resolveFirst = resolve }))
    await input(worker, '3')
    vi.mocked(oxidnsApi.getSettings).mockResolvedValue({ data: { ...settings(), native_config: config.replace('worker_threads: 8', 'worker_threads: 6') } } as any)
    await wrapper.setProps({ profileId: 'next-profile' }); await flushPromises()
    const first = { ...settings(), native_config: config.replace('worker_threads: 8', 'worker_threads: 3') }
    resolveFirst({ data: { settings: first, ...projection(first) } }); await flushPromises(); await settleValidation()
    expect((document.querySelector(worker) as HTMLInputElement).value).toBe('6')
    await click('保存草稿')
    expect(oxidnsApi.saveSettings).toHaveBeenLastCalledWith(expect.objectContaining({ native_config: expect.stringContaining('worker_threads: 6') }), 'next-profile', undefined)
  })

  it('keeps a completed file upload when an older common-settings response arrives and retries with that file', async () => {
    const wrapper = await render()
    await click('附加文件')
    const files = wrapper.getComponent(OxidnsFilesEditor)
    const upload = files.get('[aria-label="上传附加文件"]')
    const content = 'full:tracker.hhanclub.net\n'
    const bytes = new TextEncoder().encode(content)
    let finishUpload!: (value: ArrayBuffer) => void
    const pendingUpload = new Promise<ArrayBuffer>(resolve => { finishUpload = resolve })
    Object.defineProperty(upload.element, 'files', { configurable: true, value: [
      { name: 'custom.txt', size: bytes.length, arrayBuffer: () => pendingUpload },
    ] })
    await upload.trigger('change')
    expect(files.text()).toContain('正在读取…')
    await click('配置')

    let resolveApply!: (value: any) => void
    vi.mocked(oxidnsApi.applyCommonSettings).mockImplementationOnce(() => new Promise(resolve => { resolveApply = resolve }))
    await input(worker, '3')
    expect(oxidnsApi.applyCommonSettings).toHaveBeenCalledOnce()
    expect(files.props('disabled')).toBe(true)
    finishUpload(bytes.buffer as ArrayBuffer)
    await flushPromises()
    const uploaded = { path: 'custom.txt', content, encoding: 'text' }
    expect(files.props('modelValue')).toEqual([uploaded])

    const stale = { ...settings(), native_config: config.replace('worker_threads: 8', 'worker_threads: 3') }
    resolveApply({ data: { settings: stale, ...projection(stale) } })
    await flushPromises()
    await settleValidation()
    expect(document.body.textContent).toContain('配置已变化，请撤销未同步修改后重新编辑。')
    expect(files.props('modelValue')).toEqual([uploaded])
    expect(wrapper.getComponent(OxidnsNativeEditor).props('modelValue')).toBe(config)
    expect(button('保存草稿').disabled).toBe(true)
    expect(oxidnsApi.saveSettings).not.toHaveBeenCalled()

    await click('重试同步')
    await settleValidation()
    expect(oxidnsApi.applyCommonSettings).toHaveBeenCalledTimes(2)
    expect(vi.mocked(oxidnsApi.applyCommonSettings).mock.calls[1]).toEqual([
      expect.objectContaining({ native_config: config, additional_files: [uploaded] }), { worker_threads: 3 }, 'oxi-profile',
    ])
    expect(button('保存草稿').disabled).toBe(false)
    await click('保存草稿')
    expect(savedPayload().additional_files).toEqual([uploaded])
    expect((loadYaml(savedPayload().native_config) as any).runtime.worker_threads).toBe(3)
  })

  it('allows complete editing when projection fails and retries common settings', async () => {
    vi.mocked(oxidnsApi.readCommonSettings).mockRejectedValueOnce(new Error('Unavailable'))
    await render()
    expect(document.body.textContent).toContain('读取常用设置失败')
    await click('完整参数', views())
    expect(button('完整参数', views()).getAttribute('aria-pressed')).toBe('true')
    await click('常用设置', views())
    expect((document.querySelector(worker) as HTMLInputElement).value).toBe('8')
  })


})
