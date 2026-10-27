import { afterEach, beforeEach, expect, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { load as loadYaml, dump as dumpYaml } from 'js-yaml'
import OxidnsSettings from '@/components/generate/OxidnsSettings.vue'
import OxidnsNativeEditor from '@/components/generate/OxidnsNativeEditor.vue'
import OxidnsConfigNavigation from '@/components/generate/OxidnsConfigNavigation.vue'
import { agentApi, profileApi, oxidnsApi } from '@/api'
import { notify, settleConfirm } from '@/lib/feedback'

// These suites mount the whole OxiDNS settings form per test; leave headroom for
// parallel workers on a busy machine without raising the global timeout.
vi.setConfig({ testTimeout: 15_000 })

const wrappers: ReturnType<typeof mount>[] = []
const config = 'runtime: {worker_threads: 8, extension: false}\nplugins: [{tag: main, type: sequence, args: [{exec: reject 0}]}]\nunknown: {values: [0, false, null]}'
const projection = (settings: any) => ({ values: { worker_threads: (loadYaml(settings.native_config) as any)?.runtime?.worker_threads }, capabilities: {} })
const settings = () => ({ config_mode: 'native', native_config: config, additional_files: [], template_settings: { worker_threads: 2 } })
const publishGuard = { expected_profile_id: 'oxi-profile', expected_service_type: 'oxidns', expected_config_revision: 7 }
// Stored-settings versions: as loaded, and after a successful save.
const etags = { loaded: '"oxidns-v1"', saved: '"oxidns-v2"' }
beforeEach(() => {
  const realSetTimeout = globalThis.setTimeout
  const realClearTimeout = globalThis.clearTimeout
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  const fakeSetTimeout = globalThis.setTimeout
  const fakeClearTimeout = globalThis.clearTimeout
  const debounceTimers = new Set<ReturnType<typeof setTimeout>>()
  // Only OxidnsSettings' 150 ms change and 300 ms validation debounces use
  // the clock. Reka UI focus/animation timers retain their normal scheduling.
  vi.spyOn(globalThis, 'setTimeout').mockImplementation(((callback: (...args: any[]) => void, delay?: number, ...args: any[]) => {
    if (delay !== 150 && delay !== 300) return realSetTimeout(callback, delay, ...args)
    const timer = fakeSetTimeout(callback, delay, ...args)
    debounceTimers.add(timer)
    return timer
  }) as typeof setTimeout)
  vi.spyOn(globalThis, 'clearTimeout').mockImplementation((timer) => {
    if (debounceTimers.delete(timer as ReturnType<typeof setTimeout>)) fakeClearTimeout(timer)
    else realClearTimeout(timer)
  })
  settleConfirm('cancel')
  vi.clearAllMocks()
  vi.mocked(oxidnsApi.getSettings).mockImplementation(async () => ({ data: settings(), headers: { etag: etags.loaded } }) as any)
  vi.mocked(oxidnsApi.validateSettings).mockImplementation(async (value: any) => {
    const doc = loadYaml(value.native_config) as any
    const issues: any[] = []
    if (doc?.runtime?.worker_threads === 0) issues.push({path: ['runtime', 'worker_threads'], message: '工作线程数必须大于 0。', code: 'minimum', severity: 'error'})
    doc?.plugins?.forEach((plugin: any, index: number) => {
      if (['http_server', 'quic_server'].includes(plugin.type) && !plugin.args?.listen) issues.push({path: ['plugins', index, 'args', 'listen'], message: '请填写监听地址。', code: 'required', severity: 'error'})
    })
    return {data: {valid: !issues.length, issues}} as any
  })
  vi.mocked(oxidnsApi.saveSettings).mockResolvedValue({ data: { success: true, publish_guard: publishGuard }, headers: { etag: etags.saved } } as any)
  vi.mocked(profileApi.list).mockResolvedValue({ data: [{ id: 'oxi-profile', name: 'DNS配置', is_default: true }] } as any)
  vi.mocked(agentApi.summaries).mockResolvedValue({ data: [{ id: 'dns', name: 'OxiDNS', profile_id: 'oxi-profile', service_type: 'oxidns', status: 'online' }] } as any)
  vi.mocked(agentApi.pushConfig).mockImplementation(async (_id, deploymentId) => ({ data: { success: true, deployment_id: deploymentId, status: 'verifying' } }) as any)
  vi.mocked(agentApi.getDeployment).mockImplementation(async (_id, deploymentId) => ({ data: { deployment_id: deploymentId, status: 'verifying' } }) as any)
  vi.mocked(oxidnsApi.prepareEditor).mockImplementation(async value => ({ data: { settings: structuredClone(value) } }) as any)
  vi.mocked(oxidnsApi.readCommonSettings).mockImplementation(async value => ({ data: projection(value) }) as any)
  vi.mocked(oxidnsApi.applyCommonSettings).mockImplementation(async (value: any, changes) => {
    const settings = JSON.parse(JSON.stringify(value))
    const document = loadYaml(settings.native_config) as any
    document.runtime ??= {}
    if ('worker_threads' in changes) document.runtime.worker_threads = changes.worker_threads
    settings.native_config = dumpYaml(document)
    return { data: { settings, ...projection(settings) } } as any
  })
  vi.mocked(oxidnsApi.applyNativeSettings).mockImplementation(async (value: any, native_config, _profileId?: string, _renames?: Record<string, string>, _standardAdd?: string[]) => {
    const settings = {...JSON.parse(JSON.stringify(value)), native_config}
    return {data: {settings, ...projection(settings), notices: []}} as any
  })
  vi.spyOn(notify, 'success').mockImplementation(() => 0)
  vi.spyOn(notify, 'error').mockImplementation(() => 0)
})
afterEach(() => {
  settleConfirm('cancel')
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  document.body.innerHTML = ''
  vi.restoreAllMocks()
  vi.useRealTimers()
})
async function render() {
  const wrapper = mount(OxidnsSettings, { attachTo: document.body, props: { open: true, profileId: 'oxi-profile' } })
  wrappers.push(wrapper)
  await flushPromises()
  return wrapper
}
function button(text: string, scope: ParentNode = document) {
  const button = Array.from(scope.querySelectorAll('button')).find(button => button.textContent?.trim() === text)
  expect(button, text).toBeDefined()
  return button!
}
async function click(text: string, scope: ParentNode = document) {
  const button = Array.from(scope.querySelectorAll('button')).find(button => button.textContent?.trim() === text)
  expect(button, text).toBeDefined()
  button!.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, button: 0 }))
  button!.click()
  await flushPromises()
}
async function settleValidation() {
  if (document.querySelector('[aria-label="配置检查结果"]')?.textContent?.includes('正在检查')) {
    await vi.advanceTimersByTimeAsync(330); await flushPromises()
  }
}
async function input(selector: string, value: string) {
  const element = document.querySelector(selector) as HTMLInputElement | HTMLTextAreaElement
  expect(element, selector).not.toBeNull()
  element.value = value
  element.dispatchEvent(new Event('input', { bubbles: true }))
  await flushPromises()
  if (selector === worker) { await vi.advanceTimersByTimeAsync(180); await flushPromises() }
  await settleValidation()
}
const savedPayload = () => vi.mocked(oxidnsApi.saveSettings).mock.calls.at(-1)?.[0] as Record<string, any>

const views = () => document.querySelector('[aria-label="配置编辑方式"]')!
const worker = '#oxi-worker_threads'
const yamlInput = '[aria-label="OxiDNS 完整配置 YAML"]'

async function removeNativePlugin(wrapper: ReturnType<typeof mount>, tag: string) {
  await click('完整参数', views())
  const navigation = wrapper.getComponent(OxidnsNativeEditor).getComponent(OxidnsConfigNavigation)
  const flatten = (items: any[]): any[] => items.flatMap(item => [item, ...flatten(item.children || [])])
  const item = flatten(navigation.props('items')).find(item => item.label === tag && !item.available)
  expect(item, `native plugin ${tag}`).toBeDefined()
  navigation.vm.$emit('select', item.id)
  await flushPromises()
  const remove = document.querySelector<HTMLButtonElement>('[aria-label^="删除原生插件 "]')
  expect(remove).not.toBeNull()
  remove!.click()
  await flushPromises()
}


export { config, projection, settings, publishGuard, etags, render, button, click, settleValidation, input, savedPayload, views, worker, yamlInput, removeNativePlugin }
