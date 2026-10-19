import { describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import { load as loadYaml } from 'js-yaml'
import OxidnsNativeEditor from '@/components/generate/OxidnsNativeEditor.vue'
import OxidnsConfigNavigation from '@/components/generate/OxidnsConfigNavigation.vue'
import { agentApi, profileApi, oxidnsApi, ruleApi, ruleSetApi } from '@/api'

import { config, settings, render, button, click, settleValidation, input, savedPayload, views, worker, yamlInput } from './helpers/oxidns-settings'

vi.mock('@/api', () => ({
  agentApi: { getAll: vi.fn(), pushConfig: vi.fn(), getDeployment: vi.fn(), activateDeployment: vi.fn() },
  profileApi: { list: vi.fn() },
  oxidnsApi: { getSettings: vi.fn(), saveSettings: vi.fn(), prepareEditor: vi.fn(), readCommonSettings: vi.fn(), applyCommonSettings: vi.fn(), applyNativeSettings: vi.fn(), validateSettings: vi.fn() },
  ruleApi: { getAll: vi.fn() }, ruleSetApi: { getAll: vi.fn() }
}))
describe('unified OxiDNS profile settings', () => {
  it.each(['http_server', 'quic_server'])('reports incomplete %s while allowing draft storage', async type => {
    const wrapper = await render()
    await click('完整参数', views())
    wrapper.getComponent(OxidnsNativeEditor).getComponent(OxidnsConfigNavigation).vm.$emit('select', `add:plugin:${type}`)
    await flushPromises(); await settleValidation()
    expect(button('保存草稿').disabled).toBe(false)
    expect(document.body.textContent).toContain('必填 · 待填写')
    expect(document.querySelector('[aria-label="配置检查问题"]')?.textContent).toContain('请填写监听地址')
    expect(document.body.textContent).toContain('发布前需要修正')
    expect(oxidnsApi.saveSettings).not.toHaveBeenCalled()
    await click('YAML', views())
    const args = type === 'http_server'
      ? '{listen: "127.0.0.1:8053", entries: [{path: /dns-query, exec: main}]}'
      : '{listen: "127.0.0.1:8853", entry: main, cert: cert.pem, key: key.pem}'
    await input(yamlInput, `plugins: [{tag: main, type: sequence, args: [{exec: reject 0}]}, {tag: ${type}, type: ${type}, args: ${args}}]`)
    expect(button('保存草稿').disabled).toBe(false)
    await click('保存草稿')
    expect((loadYaml(savedPayload().native_config) as any).plugins[1].args.listen).toMatch(/^127/)
  })

  it('retains valid empty reverse_lookup args with default help', async () => {
    const wrapper = await render()
    await click('完整参数', views())
    wrapper.getComponent(OxidnsNativeEditor).getComponent(OxidnsConfigNavigation).vm.$emit('select', 'add:plugin:reverse_lookup')
    await flushPromises(); await settleValidation()
    expect(document.body.textContent).toContain('使用默认参数')
    expect(document.querySelector('[aria-label="反向查询缓存默认参数说明"]')).not.toBeNull()
    expect(button('保存草稿').disabled).toBe(false)
    await click('保存草稿')
    expect((loadYaml(savedPayload().native_config) as any).plugins.at(-1)).toEqual({ tag: 'reverse_lookup', type: 'reverse_lookup', args: {} })
  })

  it.each(['plugins: [broken', '[1, 2]'])('blocks malformed or non-object draft %s and clears it across profiles', async malformed => {
    const wrapper = await render()
    await click('YAML', views())
    await input(yamlInput, malformed)
    expect(button('保存草稿').disabled).toBe(true)
    vi.mocked(oxidnsApi.getSettings).mockResolvedValue({ data: { ...settings(), native_config: config.replace('worker_threads: 8', 'worker_threads: 4') } } as any)
    await wrapper.setProps({ profileId: 'other-profile' })
    await flushPromises()
    expect(button('保存草稿').disabled).toBe(false)
    await click('保存草稿')
    expect(oxidnsApi.saveSettings).toHaveBeenLastCalledWith(expect.objectContaining({ native_config: expect.stringContaining('worker_threads: 4') }), 'other-profile')
  })


  it('shows the same server parameter errors in all views without preventing draft storage', async () => {
    const wrapper = await render()
    await input(worker, '0')
    expect(oxidnsApi.applyCommonSettings).toHaveBeenLastCalledWith(expect.anything(), {worker_threads: 0}, 'oxi-profile')
    expect((document.querySelector(worker) as HTMLInputElement).value).toBe('0')
    expect(document.body.textContent).toContain('工作线程数必须大于 0')
    expect(button('保存草稿').disabled).toBe(false)
    document.querySelector<HTMLButtonElement>('[aria-label="定位错误 1"]')!.click(); await flushPromises()
    expect(button('完整参数', views()).getAttribute('aria-pressed')).toBe('true')
    await input('[aria-label="全局参数.runtime.worker_threads 值"]', '4')
    expect(document.body.textContent).not.toContain('工作线程数必须大于 0')
    await input('[aria-label="全局参数.runtime.worker_threads 值"]', '0')
    expect(document.body.textContent).toContain('工作线程数必须大于 0')
    await click('YAML', views())
    const text = (document.querySelector(yamlInput) as HTMLTextAreaElement).value
    await input(yamlInput, text.replace('worker_threads: 0', 'worker_threads: 3'))
    expect(document.body.textContent).not.toContain('工作线程数必须大于 0')
    await input(yamlInput, text)
    expect(document.body.textContent).toContain('工作线程数必须大于 0')
    expect(button('保存草稿').disabled).toBe(false)
    await click('保存草稿')
    expect(savedPayload().native_config).toContain('worker_threads: 0')
    expect(wrapper.emitted('saved')).toHaveLength(1)
  })

  it('allows offline draft storage when validation is unavailable or malformed and supports retry', async () => {
    vi.mocked(oxidnsApi.validateSettings).mockRejectedValueOnce(new Error('校验服务不可用'))
    await render()
    expect(document.body.textContent).toContain('校验服务不可用')
    expect(document.body.textContent).toContain('可先保存草稿')
    expect(button('保存草稿').disabled).toBe(false)
    vi.mocked(oxidnsApi.validateSettings).mockResolvedValueOnce({data: {valid: true}} as any)
    await click('重新检查')
    expect(button('保存草稿').disabled).toBe(false)
    expect(document.body.textContent).toContain('无效结果')
    await click('重新检查')
    expect(document.body.textContent).toContain('规则检查通过')
    await click('保存草稿')
    expect(oxidnsApi.saveSettings).toHaveBeenCalledTimes(1)
  })

  it('saves without waiting for a slow validator or triggering an extra check', async () => {
    let resolveValidation!: (value: any) => void
    vi.mocked(oxidnsApi.validateSettings).mockImplementationOnce(() => new Promise(resolve => {resolveValidation = resolve}))
    const wrapper = await render()
    expect(document.body.textContent).toContain('正在检查草稿参数')
    expect(button('保存草稿').disabled).toBe(false)
    await click('保存草稿')
    expect(oxidnsApi.validateSettings).toHaveBeenCalledTimes(1)
    expect(oxidnsApi.saveSettings).toHaveBeenCalledTimes(1)
    expect(wrapper.emitted('saved')).toHaveLength(1)
    await wrapper.setProps({open: false})
    resolveValidation({data: {valid: false, issues: [{path: [], code: 'stale', message: '关闭后的错误', severity: 'error'}]}}); await flushPromises()
    expect(document.body.textContent).not.toContain('关闭后的错误')
  })

  it('shows warning scope and keeps valid empty/default values unchanged', async () => {
    const native = 'plugins: [{tag: reverse, type: reverse_lookup, args: {}}, {tag: cache, type: cache, args: null}]\nunknown: {empty: "", nil: null, zero: 0, off: false}\n'
    vi.mocked(oxidnsApi.getSettings).mockResolvedValueOnce({data: {...settings(), native_config: native}} as any)
    vi.mocked(oxidnsApi.validateSettings).mockResolvedValueOnce({data: {valid: true, issues: [{path: ['unknown'], code: 'unknown_field', message: '扩展字段由目标核心验证。', severity: 'warning'}]}} as any)
    await render()
    expect(document.body.textContent).toContain('规则检查通过，存在待目标环境验证项')
    expect(button('保存草稿').disabled).toBe(false)
    await click('保存草稿')
    expect(savedPayload().native_config).toBe(native)
  })

  it('ignores an older validation result after a newer edit was checked', async () => {
    await render()
    await click('YAML', views())
    let resolveFirst!: (value: any) => void
    vi.mocked(oxidnsApi.validateSettings).mockImplementationOnce(() => new Promise(resolve => {resolveFirst = resolve}))
    await input(yamlInput, config.replace('worker_threads: 8', 'worker_threads: 3'))
    expect(button('保存草稿').disabled).toBe(false)
    await input(yamlInput, config.replace('worker_threads: 8', 'worker_threads: 4'))
    expect(document.body.textContent).toContain('规则检查通过')
    resolveFirst({data: {valid: false, issues: [{path: ['runtime'], code: 'stale', message: '过期检查', severity: 'error'}]}}); await flushPromises()
    expect(document.body.textContent).not.toContain('过期检查')
    await click('保存草稿')
    expect(savedPayload().native_config).toContain('worker_threads: 4')
  })

  it('ignores validation results from a previous profile or cancelled dialog', async () => {
    let resolveFirst!: (value: any) => void
    vi.mocked(oxidnsApi.validateSettings).mockImplementationOnce(() => new Promise(resolve => {resolveFirst = resolve}))
    const wrapper = await render()
    await wrapper.setProps({profileId: 'next-profile'}); await flushPromises()
    resolveFirst({data: {valid: false, issues: [{path: [], code: 'stale', message: '其他空间检查', severity: 'error'}]}}); await flushPromises()
    expect(document.body.textContent).not.toContain('其他空间检查')
    let resolveSecond!: (value: any) => void
    vi.mocked(oxidnsApi.validateSettings).mockImplementationOnce(() => new Promise(resolve => {resolveSecond = resolve}))
    await click('YAML', views())
    await input(yamlInput, config.replace('worker_threads: 8', 'worker_threads: 3'))
    await wrapper.setProps({open: false})
    resolveSecond({data: {valid: false, issues: [{path: [], code: 'stale', message: '已取消检查', severity: 'error'}]}}); await flushPromises()
    await wrapper.setProps({open: true}); await flushPromises()
    expect(document.body.textContent).not.toContain('已取消检查')
    expect(button('保存草稿').disabled).toBe(false)
    expect(oxidnsApi.saveSettings).not.toHaveBeenCalled()
  })

  it('checks attached-file changes and locates include diagnostics in the files panel', async () => {
    const file = {path: 'plugins/cache.yaml', content: 'plugins: []', encoding: 'text'}
    vi.mocked(oxidnsApi.getSettings).mockResolvedValueOnce({data: {...settings(), additional_files: [file]}} as any)
    vi.mocked(oxidnsApi.validateSettings).mockResolvedValueOnce({data: {valid: false, issues: [{path: ['plugins', 0, 'args', 'size'], source: file.path, code: 'minimum', message: '缓存容量不能小于零。', severity: 'error'}]}} as any)
    await render()
    document.querySelector<HTMLButtonElement>('[aria-label="定位错误 1"]')!.click(); await flushPromises()
    expect(button('附加文件').getAttribute('aria-pressed')).toBe('true')
    expect(document.activeElement?.getAttribute('aria-label')).toBe('文件 1 路径')
    await input('[aria-label="文件 1 路径"]', 'plugins/updated.yaml')
    expect(vi.mocked(oxidnsApi.validateSettings).mock.calls.at(-1)?.[0]).toMatchObject({additional_files: [{...file, path: 'plugins/updated.yaml'}]})
    expect(button('保存草稿').disabled).toBe(false)
  })

  it('shows structural save-endpoint issues while retaining the editable draft', async () => {
    await render()
    vi.mocked(oxidnsApi.saveSettings).mockRejectedValueOnce({response: {data: {message: '配置结构检查未通过。', issues: [{path: ['include'], code: 'invalid_structure', message: '附加配置结构无效。', severity: 'error'}]}}})
    await click('保存草稿')
    expect(document.body.textContent).toContain('附加配置结构无效')
    expect(button('保存草稿').disabled).toBe(false)
    expect(document.querySelector('[aria-label="定位错误 1"]')).not.toBeNull()
  })
})
