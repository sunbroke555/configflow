import { describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { load as loadYaml } from 'js-yaml'
import { agentApi, profileApi, oxidnsApi } from '@/api'
import { confirmState, notify, settleConfirm } from '@/lib/feedback'
import SettingsWorkspace from '@/components/generate/SettingsWorkspace.vue'
import OxidnsSettings from '@/components/generate/OxidnsSettings.vue'

import { config, settings, publishGuard, etags, render, button, click, input, savedPayload, views, worker, yamlInput } from './helpers/oxidns-settings'

vi.mock('@/api', () => ({
  agentApi: { getAll: vi.fn(), summaries: vi.fn(), pushConfig: vi.fn(), getDeployment: vi.fn(), activateDeployment: vi.fn() },
  profileApi: { list: vi.fn() },
  oxidnsApi: { getSettings: vi.fn(), saveSettings: vi.fn(), prepareEditor: vi.fn(), readCommonSettings: vi.fn(), applyCommonSettings: vi.fn(), applyNativeSettings: vi.fn(), validateSettings: vi.fn() },
}))
describe('unified OxiDNS profile settings', () => {
  it('saves and publishes the returned version without closing the editor or marking unsaved changes', async () => {
    const wrapper = await render()
    await input(worker, '5')
    await click('保存并发布')
    expect(oxidnsApi.saveSettings).toHaveBeenCalledTimes(1)
    expect(agentApi.pushConfig).toHaveBeenCalledWith('dns', expect.any(String), publishGuard)
    expect(vi.mocked(oxidnsApi.saveSettings).mock.invocationCallOrder[0]).toBeLessThan(vi.mocked(agentApi.pushConfig).mock.invocationCallOrder[0])
    expect(wrapper.emitted('update:open')).toBeUndefined()
    expect(wrapper.findComponent(SettingsWorkspace).props('dirty')).toBe(false)
    expect(wrapper.findComponent(SettingsWorkspace).props('busy')).toBe(false)
    expect(document.body.textContent).toContain('草稿已保存')
    expect(document.body.textContent).not.toContain('发布成功')
  })

  it('guards each save with the stored version and continues from the version it wrote', async () => {
    await render()
    expect(document.querySelector('[data-rule-dns-note]')?.textContent).toContain('策略规则页的 DNS 修改会直接写入此草稿')
    await input(worker, '5')
    await click('保存并发布')
    expect(vi.mocked(oxidnsApi.saveSettings).mock.calls[0]![2]).toBe(etags.loaded)
    await input(worker, '6')
    await click('保存草稿')
    expect(vi.mocked(oxidnsApi.saveSettings).mock.calls[1]![2]).toBe(etags.saved)
  })

  it('reloads and replays common-settings edits when the rules page changed the stored settings', async () => {
    const wrapper = await render()
    await input(worker, '5')
    const changed = { response: { status: 409, data: { success: false, code: 'oxidns_changed_elsewhere', message: 'OxiDNS 设置已在其他页面修改（例如策略规则的 DNS），请重新加载后再保存' } } }
    vi.mocked(oxidnsApi.saveSettings).mockRejectedValueOnce(changed)
    const newer = { ...settings(), native_config: config.replace('extension: false', 'extension: true'), rule_actions: { rule_x: { action: 'blackhole' } } }
    vi.mocked(oxidnsApi.getSettings).mockResolvedValueOnce({ data: newer, headers: { etag: '"oxidns-v3"' } } as any)
    await click('保存草稿')
    expect(wrapper.emitted('update:open')).toBeUndefined()
    expect(notify.error).not.toHaveBeenCalled()
    expect(document.querySelector('[role="alert"]')?.textContent).toContain('OxiDNS 设置已在其他页面修改')
    expect(confirmState).toMatchObject({ open: true, title: 'OxiDNS 设置已在其他页面修改', confirmText: '重新加载并保留我的修改', altText: '重新加载（放弃本页未保存修改）' })
    vi.mocked(oxidnsApi.applyCommonSettings).mockClear()
    settleConfirm('confirm')
    await flushPromises()
    await vi.advanceTimersByTimeAsync(180); await flushPromises()
    expect(oxidnsApi.getSettings).toHaveBeenCalledTimes(2)
    expect(oxidnsApi.applyCommonSettings).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ rule_actions: newer.rule_actions }), { worker_threads: 5 }, 'oxi-profile')
    expect((document.querySelector(worker) as HTMLInputElement).value).toBe('5')
    await click('保存草稿')
    expect(vi.mocked(oxidnsApi.saveSettings).mock.calls.at(-1)![2]).toBe('"oxidns-v3"')
    expect(savedPayload().rule_actions).toEqual(newer.rule_actions)
    expect(loadYaml(savedPayload().native_config)).toMatchObject({ runtime: { worker_threads: 5, extension: true } })
  })

  it('offers only a discarding reload when YAML was edited before the stored settings changed', async () => {
    await render()
    await click('完整参数', views())
    await click('YAML', views())
    await input(yamlInput, config.replace('worker_threads: 8', 'worker_threads: 7'))
    vi.mocked(oxidnsApi.saveSettings).mockRejectedValueOnce({ response: { status: 409, data: { success: false, code: 'oxidns_changed_elsewhere', message: 'OxiDNS 设置已在其他页面修改' } } })
    await click('保存草稿')
    expect(confirmState).toMatchObject({ open: true, confirmText: '重新加载（放弃本页未保存修改）', altText: '' })
    expect(confirmState.description).toContain('无法在最新配置上自动重做')
    settleConfirm('cancel')
    await flushPromises()
    expect(oxidnsApi.getSettings).toHaveBeenCalledTimes(1)
    expect((document.querySelector(yamlInput) as HTMLTextAreaElement).value).toContain('worker_threads: 7')
    vi.mocked(oxidnsApi.saveSettings).mockRejectedValueOnce({ response: { status: 409, data: { success: false, code: 'oxidns_changed_elsewhere', message: 'OxiDNS 设置已在其他页面修改' } } })
    await click('保存草稿')
    vi.mocked(oxidnsApi.applyCommonSettings).mockClear()
    settleConfirm('confirm')
    await flushPromises()
    await vi.advanceTimersByTimeAsync(180); await flushPromises()
    expect(oxidnsApi.getSettings).toHaveBeenCalledTimes(2)
    expect(oxidnsApi.applyCommonSettings).not.toHaveBeenCalled()
    expect((document.querySelector(worker) as HTMLInputElement).value).toBe('8')
  })

  it('opens directly on the full parameters when linked from a locked rule DNS field', async () => {
    const wrapper = mount(OxidnsSettings, { attachTo: document.body, props: { open: true, profileId: 'oxi-profile', initialTab: 'config' } })
    await flushPromises()
    expect(button('完整参数', views()).getAttribute('aria-pressed')).toBe('true')
    wrapper.unmount()
  })

  it('retains the saved draft when publication validation rejects it', async () => {
    const wrapper = await render()
    await input(worker, '0')
    vi.mocked(agentApi.pushConfig).mockRejectedValueOnce({ response: { status: 400, data: { message: '工作线程数必须大于 0。' } } })
    await click('保存并发布')
    expect(agentApi.pushConfig).toHaveBeenCalledOnce()
    expect(document.body.textContent).toContain('草稿已保存，发布失败')
    expect(document.body.textContent).toContain('工作线程数必须大于 0。')
    expect(wrapper.emitted('update:open')).toBeUndefined()
    expect(wrapper.findComponent(SettingsWorkspace).props('dirty')).toBe(false)
    expect(savedPayload().native_config).toContain('worker_threads: 0')
  })

  it('never publishes if saving the draft fails', async () => {
    const wrapper = await render()
    await input(worker, '5')
    vi.mocked(oxidnsApi.saveSettings).mockRejectedValueOnce({ response: { data: { message: '保存暂时失败' } } })
    await click('保存并发布')
    expect(agentApi.pushConfig).not.toHaveBeenCalled()
    expect(wrapper.findComponent(SettingsWorkspace).props('dirty')).toBe(true)
    expect(document.body.textContent).toContain('保存暂时失败')
  })

  it('tracks real draft changes without treating editor navigation as unsaved edits', async () => {
    const wrapper = await render()
    const workspace = () => wrapper.findComponent(SettingsWorkspace)
    expect(workspace().props('dirty')).toBe(false)
    await click('完整参数', views())
    await click('YAML', views())
    expect(workspace().props('dirty')).toBe(false)
    await input(yamlInput, config + '\n# unsaved comment')
    expect(workspace().props('dirty')).toBe(true)
    await input(yamlInput, config)
    expect(workspace().props('dirty')).toBe(false)
    expect(oxidnsApi.saveSettings).not.toHaveBeenCalled()
  })

  it('keeps unfinished invalid form input protected when it cannot be synchronized', async () => {
    const wrapper = await render()
    await input(worker, '')
    expect(button('保存草稿').disabled).toBe(true)
    expect(wrapper.findComponent(SettingsWorkspace).props('dirty')).toBe(true)
    ;(document.querySelector('[aria-label="返回生成工作台"]') as HTMLButtonElement).click()
    await flushPromises()
    expect(confirmState.open).toBe(true)
    settleConfirm('cancel')
    await flushPromises()
    expect(wrapper.emitted('update:open')).toBeUndefined()
    expect((document.querySelector(worker) as HTMLInputElement).value).toBe('')
  })

  it('edits one document in all views and saves once to the chosen profile', async () => {
    const wrapper = await render()
    expect(oxidnsApi.prepareEditor).toHaveBeenCalledWith(settings(), 'oxi-profile')
    expect(button('常用设置', views()).getAttribute('aria-pressed')).toBe('true')
    await input(worker, '5')
    await click('完整参数', views())
    await click('YAML', views())
    const text = (document.querySelector(yamlInput) as HTMLTextAreaElement).value
    expect((loadYaml(text) as any).runtime.worker_threads).toBe(5)
    await input(yamlInput, text.replace('worker_threads: 5', 'worker_threads: 6'))
    await click('常用设置', views())
    expect((document.querySelector(worker) as HTMLInputElement).value).toBe('6')
    expect(oxidnsApi.saveSettings).not.toHaveBeenCalled()
    await click('保存草稿')
    expect(agentApi.pushConfig).not.toHaveBeenCalled()
    expect(oxidnsApi.saveSettings).toHaveBeenCalledTimes(1)
    expect(oxidnsApi.saveSettings).toHaveBeenLastCalledWith(expect.objectContaining({ config_mode: 'native' }), 'oxi-profile', etags.loaded)
    expect(loadYaml(savedPayload().native_config)).toEqual({ ...(loadYaml(config) as any), runtime: { worker_threads: 6, extension: false } })
    expect(savedPayload().template_settings).toEqual({ worker_threads: 2 })
    expect(wrapper.emitted('saved')).toHaveLength(1)
  })

  it('prepares the active legacy template and retains the inactive native backup', async () => {
    const dormant = 'plugins: [{tag: empty_http, type: http_server, args: {}}]'
    const legacy = { config_mode: 'template', native_config: dormant, worker_threads: 8 }
    vi.mocked(oxidnsApi.getSettings).mockResolvedValue({ data: legacy } as any)
    vi.mocked(oxidnsApi.prepareEditor).mockResolvedValue({ data: { settings: { ...settings(), legacy_native_config: dormant } } } as any)
    await render()
    expect(oxidnsApi.prepareEditor).toHaveBeenCalledWith(legacy, 'oxi-profile')
    expect(document.querySelector('[aria-label="旧草稿备份说明"]')).not.toBeNull()
    expect(button('保存草稿').disabled).toBe(false)
    expect(oxidnsApi.saveSettings).not.toHaveBeenCalled()
    await click('保存草稿')
    expect(savedPayload().legacy_native_config).toBe(dormant)
    expect(savedPayload().native_config).toBe(config)
  })

  it.each(['load', 'prepare'])('blocks saving on %s failure and allows retry', async stage => {
    const api = stage === 'load' ? oxidnsApi.getSettings : oxidnsApi.prepareEditor
    vi.mocked(api).mockRejectedValueOnce({ response: { data: { message: '配置加载失败' } } })
    await render()
    expect(document.querySelector('[role="alert"]')?.textContent).toContain('配置加载失败')
    expect(button('保存草稿').disabled).toBe(true)
    await click('重新加载')
    expect(button('保存草稿').disabled).toBe(false)
  })

  it('preserves attached files and unknown values through panel changes', async () => {
    const files = [{ path: 'certs/example.pem', content: 'TEST CERT', encoding: 'text' }, { path: 'rules/example.dat', content: 'AP8=', encoding: 'base64' }]
    vi.mocked(oxidnsApi.getSettings).mockResolvedValue({ data: { ...settings(), additional_files: files, extension_settings: { keep: false } } } as any)
    await render()
    await click('附加文件')
    expect((document.querySelector('[aria-label="文件 1 路径"]') as HTMLInputElement).value).toBe('certs/example.pem')
    await click('配置')
    await click('保存草稿')
    expect(savedPayload().additional_files).toEqual(files)
    expect(savedPayload().extension_settings).toEqual({ keep: false })
    expect(loadYaml(savedPayload().native_config)).toEqual(loadYaml(config))
  })

  it('keeps edits on save failure and discards them after cancel/reopen', async () => {
    const wrapper = await render()
    await input(worker, '5')
    vi.mocked(oxidnsApi.saveSettings).mockRejectedValueOnce({ response: { data: { message: '保存暂时失败' } } })
    await click('保存草稿')
    expect(document.querySelector('[role="alert"]')?.textContent).toContain('保存暂时失败')
    expect(button('保存草稿').disabled).toBe(false)
    ;(document.querySelector('[aria-label="返回生成工作台"]') as HTMLButtonElement).click()
    await flushPromises()
    expect(confirmState.open).toBe(true)
    settleConfirm('cancel')
    await flushPromises()
    expect(wrapper.emitted('update:open')).toBeUndefined()
    expect((document.querySelector(worker) as HTMLInputElement).value).toBe('5')
    ;(document.querySelector('[aria-label="返回生成工作台"]') as HTMLButtonElement).click()
    await flushPromises()
    settleConfirm('confirm')
    await flushPromises()
    expect(wrapper.emitted('update:open')?.at(-1)).toEqual([false])
    await wrapper.setProps({ open: false })
    await wrapper.setProps({ open: true })
    await flushPromises()
    expect((document.querySelector(worker) as HTMLInputElement).value).toBe('8')
    expect(oxidnsApi.getSettings).toHaveBeenCalledTimes(2)
  })


})
