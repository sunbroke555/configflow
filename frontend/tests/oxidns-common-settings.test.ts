import { afterEach, describe, expect, it } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import OxidnsCommonSettings from '@/components/generate/OxidnsCommonSettings.vue'
import OxidnsFields from '@/components/generate/OxidnsFields.vue'
import OxidnsMatchEditor from '@/components/generate/OxidnsMatchEditor.vue'
import MultiSelect from '@/components/common/MultiSelect.vue'
import { featureGroups } from '@/lib/oxidns'

const wrappers: ReturnType<typeof mount>[] = []
const values = () => ({ worker_threads: 8, listen: '127.0.0.1:5353', local_dns: '- addr: 192.0.2.53\n  extension: false\n', remote_dns: '192.0.2.54', fallback_dns: '', block_qtypes: [65], local_concurrent: 2,
  direct_rules: ['direct'], proxy_rules: [], direct_rulesets: [], proxy_rulesets: [], custom_matches: [], custom_hosts: '', custom_match_position: 'head',
  api: {enabled: true, listen: '127.0.0.1:9199'}, log: {level: 'info'},
  ...Object.fromEntries(featureGroups.map(feature => [feature.key, {enabled: false, args: {}}])) })
async function render(extra: Record<string, any> = {}) {
  const wrapper = mount(OxidnsCommonSettings, {attachTo: document.body, props: {values: values(), capabilities: {}, rules: [{id: 'direct', name: 'Direct'}, {id: 'proxy', name: 'Proxy'}], ruleSets: [], ...extra}})
  wrappers.push(wrapper)
  await flushPromises()
  return wrapper
}
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); document.body.innerHTML = '' })
async function click(text: string) {
  const target = Array.from(document.querySelectorAll('button')).find(button => button.textContent?.trim() === text)
  expect(target, text).toBeDefined()
  target!.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, button: 0})); target!.click()
  await flushPromises()
}
async function input(id: string, value: string) {
  const target = document.getElementById(id) as HTMLInputElement
  expect(target, id).not.toBeNull()
  target.value = value; target.dispatchEvent(new Event('input', {bubbles: true}))
  await flushPromises()
}
const lastChange = (wrapper: ReturnType<typeof mount>) => wrapper.emitted('change')?.at(-1)?.[0]

describe('OxiDNS current common settings', () => {
  it('opens the requested recorder section without enabling or changing it', async () => {
    const wrapper = await render({ initialFeature: 'query_recorder' })
    const selected = wrapper.findAll('[role="tab"]').find(tab => tab.text() === '特色功能')!
    expect(selected.attributes('data-state')).toBe('active')
    expect(wrapper.get('[aria-label="查询记录设置"]').attributes('aria-expanded')).toBe('true')
    expect(wrapper.get('#oxi-enable-query_recorder').attributes('aria-checked')).toBe('false')
    expect(wrapper.emitted('change')).toBeUndefined()
  })
  it('shows all seven former control groups without tool, preview or save actions', async () => {
    await render()
    for (const label of ['核心设置', '分流规则', 'DNS 上游', '缓存', '特色功能', 'WebUI / 日志', '高级']) {
      expect(document.querySelector(`[role="tab"][data-state]`) && [...document.querySelectorAll('[role="tab"]')].some(tab => tab.textContent?.trim() === label)).toBe(true)
    }
    expect(document.querySelector('[role="dialog"]')).toBeNull()
    expect(document.body.textContent).not.toContain('预览变更')
    expect(document.body.textContent).not.toContain('模板工具')
    expect(document.body.textContent).not.toContain('保存设置')
    await click('特色功能')
    for (const group of featureGroups) expect(document.querySelector(`[aria-label="${group.title}说明"]`)).not.toBeNull()
  })

  it('edits one field at a time without mutating the supplied current projection', async () => {
    const original = values()
    const wrapper = await render({values: original})
    await input('oxi-worker_threads', '6')
    expect(lastChange(wrapper)).toEqual({worker_threads: 6})
    expect(original.worker_threads).toBe(8)
    await wrapper.setProps({values: {...original, worker_threads: 6, listen: '127.0.0.1:5354'}})
    expect((document.getElementById('oxi-worker_threads') as HTMLInputElement).value).toBe('6')
    expect((document.getElementById('oxi-listen') as HTMLInputElement).value).toBe('127.0.0.1:5354')
  })

  it('keeps incomplete numbers across sections and external updates without sending zero', async () => {
    const wrapper = await render()
    await input('oxi-worker_threads', '')
    expect(wrapper.emitted('change')).toBeUndefined()
    expect(wrapper.emitted('error')?.at(-1)?.[0]).toContain('请填写整数')
    await click('DNS 上游')
    await input('oxi-local_concurrent', '3')
    await wrapper.setProps({values: {...values(), local_concurrent: 3, worker_threads: 4}})
    await click('核心设置')
    expect((document.getElementById('oxi-worker_threads') as HTMLInputElement).value).toBe('')
    expect(wrapper.emitted('error')?.at(-1)?.[0]).toContain('请填写整数')
    await input('oxi-worker_threads', '5')
    expect(lastChange(wrapper)).toEqual({worker_threads: 5})
    expect(wrapper.emitted('error')?.at(-1)?.[0]).toBe('')
  })

  it('shows custom capability help and navigates without showing a stale input', async () => {
    const wrapper = await render({capabilities: {worker_threads: {editable: false, reason: '运行时字段使用扩展结构', target: 'runtime'}, query_recorder: {editable: false, reason: '多个手工实例', target: 'plugins'}}})
    expect(document.getElementById('oxi-worker_threads')).toBeNull()
    expect(document.querySelector('[aria-label="工作线程数配置状态说明"]')).not.toBeNull()
    const disabledPreset = [...document.querySelectorAll('button')].find(button => button.textContent?.trim() === '填入已验证性能参数')!
    expect(disabledPreset.disabled).toBe(false)
    await click('完整参数')
    expect(wrapper.emitted('navigate')?.at(-1)).toEqual(['runtime'])
    await click('特色功能')
    expect(document.querySelector('[aria-label="查询记录设置"]')?.parentElement?.textContent).toContain('自定义配置')
    expect(document.getElementById('oxi-enable-query_recorder')).toBeNull()
    expect(wrapper.emitted('change')).toBeUndefined()
  })

  it('emits a single performance batch preserving upstream and rule selections', async () => {
    const wrapper = await render()
    await click('填入已验证性能参数')
    const changes = lastChange(wrapper) as Record<string, any>
    expect(changes).toMatchObject({worker_threads: 2, cache_lazy_ttl: 300, probe_cache_size: 2048})
    expect(changes).not.toHaveProperty('local_dns')
    expect(changes).not.toHaveProperty('direct_rules')
    expect(wrapper.emitted('change')).toHaveLength(1)
  })

  it('sends rule selections and feature toggles as current field changes', async () => {
    const wrapper = await render()
    await click('分流规则')
    wrapper.findAllComponents(MultiSelect)[2].vm.$emit('update:modelValue', ['direct', 'proxy'])
    await flushPromises()
    expect(lastChange(wrapper)).toEqual({direct_rules: ['direct', 'proxy']})
    await click('特色功能')
    const trigger = document.querySelector('[aria-label="查询记录设置"]') as HTMLButtonElement
    trigger.click(); await flushPromises()
    const enabled = document.getElementById('oxi-enable-query_recorder') as HTMLButtonElement
    enabled.click(); await flushPromises()
    expect(lastChange(wrapper)).toEqual({'query_recorder.enabled': true})
    expect(document.getElementById('oxi-query_recorder.args.path')).not.toBeNull()
  })

  it('retains upstream extension values and invalid YAML while changing another group', async () => {
    const wrapper = await render()
    await click('DNS 上游')
    const local = document.querySelector('section[aria-label="直连 DNS"]')!
    const toggle = [...local.querySelectorAll('button')].find(button => button.textContent?.trim() === 'YAML')!
    toggle.click(); await flushPromises()
    const yaml = local.querySelector('textarea')!
    expect(yaml.value).toContain('extension: false')
    yaml.value = '- addr: ['; yaml.dispatchEvent(new Event('input', {bubbles: true})); await flushPromises()
    expect(wrapper.emitted('change')).toBeUndefined()
    await click('核心设置')
    await input('oxi-worker_threads', '3')
    await wrapper.setProps({values: {...values(), worker_threads: 3}})
    await click('DNS 上游')
    expect(yaml.value).toBe('- addr: [')
    expect(wrapper.emitted('error')?.at(-1)?.[0]).not.toBe('')
  })

  it('keeps remaining fallback addresses and sends an empty list only after removing the last', async () => {
    const original = {...values(), fallback_dns: '- addr: tcp://192.0.2.54:1053\n- addr: tcp://192.0.2.55:1053\n'}
    const wrapper = await render({values: original})
    await click('DNS 上游')
    await wrapper.get('[aria-label="删除备用 DNS第 1 项"]').trigger('click')
    await flushPromises()
    const remaining = (lastChange(wrapper) as {fallback_dns: string}).fallback_dns
    expect(remaining).not.toContain('192.0.2.54')
    expect(remaining).toContain('192.0.2.55')
    await wrapper.setProps({values: {...original, fallback_dns: remaining}})
    expect(wrapper.find('[aria-label="备用 DNS第 1 项地址"]').exists()).toBe(true)

    await wrapper.get('[aria-label="删除备用 DNS第 1 项"]').trigger('click')
    await flushPromises()
    expect(lastChange(wrapper)).toEqual({fallback_dns: ''})
    await wrapper.setProps({values: {...original, fallback_dns: ''}})
    expect(wrapper.get('section[aria-label="备用 DNS"]').text()).toContain('未配置备用地址，不使用代理 DNS 回退')
    expect(wrapper.emitted('change')).toHaveLength(2)
    expect(original.fallback_dns).toContain('192.0.2.54')
    expect((wrapper.get('#oxi-local_dns').element as HTMLInputElement).value).toBe('192.0.2.53')
    expect(wrapper.find('#oxi-fallback_enabled').exists()).toBe(false)
  })

  it('adds a valid fallback address through the list without an extra switch or changing direct DNS', async () => {
    const wrapper = await render()
    await click('DNS 上游')
    const section = wrapper.get('section[aria-label="备用 DNS"]')
    await section.findAll('button').find(button => button.text().trim() === '添加上游')!.trigger('click')
    expect(wrapper.emitted('change')).toBeUndefined()
    await input('oxi-fallback_dns', 'tcp://192.0.2.54:1053')
    expect(lastChange(wrapper)).toEqual({fallback_dns: expect.stringContaining('tcp://192.0.2.54:1053')})
    expect(wrapper.find('#oxi-fallback_enabled').exists()).toBe(false)
    expect(wrapper.emitted('change')).toHaveLength(1)
    expect((wrapper.get('#oxi-local_dns').element as HTMLInputElement).value).toBe('192.0.2.53')
  })

  it('leaves custom fallback chains read-only and links to their native plugin', async () => {
    const wrapper = await render({capabilities: {fallback_dns: {editable: false, reason: '代理执行链包含自定义步骤', target: 'proxy_dns_seq'}}})
    await click('DNS 上游')
    expect(wrapper.find('section[aria-label="备用 DNS"]').exists()).toBe(false)
    expect(document.querySelector('[aria-label="备用 DNS配置状态说明"]')).not.toBeNull()
    await click('完整参数')
    expect(wrapper.emitted('navigate')?.at(-1)).toEqual(['proxy_dns_seq'])
    expect(wrapper.emitted('change')).toBeUndefined()
  })

  it('edits only actual custom matches and rejects paused metadata in YAML', async () => {
    const wrapper = await render({values: {...values(), custom_matches: [{matches: 'qtype 65', exec: 'reject 0'}]}})
    await click('高级')
    expect(wrapper.getComponent(OxidnsMatchEditor).props('allowDisable')).toBe(false)
    expect(document.querySelector('[aria-label="启用匹配项 1"]')).toBeNull()
    const editor = document.querySelector('[data-testid="oxidns-match-editor"]')!
    const toggle = [...editor.querySelectorAll('button')].find(button => button.textContent?.trim() === 'YAML')!
    toggle.click(); await flushPromises()
    const yaml = editor.querySelector('textarea')!
    yaml.value = '- matches: qtype 65\n  exec: reject 0\n  enabled: false'; yaml.dispatchEvent(new Event('input', {bubbles: true})); await flushPromises()
    expect(wrapper.emitted('error')?.at(-1)?.[0]).toContain('不支持 enabled')
    expect(wrapper.emitted('change')).toBeUndefined()
  })

  it('distinguishes absent native defaults from an explicit false value', async () => {
    await render({values: {...values(), worker_threads: null, always_standby: null, log: {level: null}}, capabilities: {worker_threads: {editable: true, absent: true}, always_standby: {editable: true, absent: true}}})
    expect((document.getElementById('oxi-worker_threads') as HTMLInputElement).placeholder).toBe('未配置（使用核心默认）')
    await click('DNS 上游')
    expect(document.getElementById('oxi-always_standby')?.parentElement?.textContent).toContain('未配置 · 核心默认')
    await click('WebUI / 日志')
    expect(document.getElementById('oxi-log.level')?.textContent).toContain('未配置（使用核心默认）')
  })

  it('keeps inactive feature drafts without preventing the feature from being disabled', async () => {
    const wrapper = await render({values: {...values(), query_recorder: {enabled: true, args: {path: 'state/query.db', retention_days: 7}}}})
    await click('特色功能')
    ;(document.querySelector('[aria-label="查询记录设置"]') as HTMLButtonElement).click(); await flushPromises()
    await input('oxi-query_recorder.args.retention_days', '')
    expect(wrapper.emitted('error')?.at(-1)?.[0]).toContain('请填写整数')
    ;(document.getElementById('oxi-enable-query_recorder') as HTMLButtonElement).click(); await flushPromises()
    expect(lastChange(wrapper)).toEqual({'query_recorder.enabled': false})
    expect(wrapper.emitted('error')?.at(-1)?.[0]).toBe('')
    ;(document.getElementById('oxi-enable-query_recorder') as HTMLButtonElement).click(); await flushPromises()
    expect((document.getElementById('oxi-query_recorder.args.retention_days') as HTMLInputElement).value).toBe('')
    expect(wrapper.emitted('error')?.at(-1)?.[0]).toContain('请填写整数')
  })

  it.each([
    ['cache_size', 'cache_enabled'],
    ['cache_lazy_ttl', 'cache_enabled'],
    ['probe_cache_size', 'probe_cache_enabled'],
    ['cache_dump_interval', 'cache_dump_enabled'],
    ['cache_dump_interval', 'cache_enabled'],
  ])('retains invalid %s without blocking %s from being disabled', async (field, toggle) => {
    const original = {...values(), cache_enabled: true, cache_size: 10240, cache_lazy_ttl: 300, probe_cache_enabled: true, probe_cache_size: 2048, cache_dump_enabled: true, cache_dump_file: 'state/cache.dump', cache_dump_interval: 600}
    const wrapper = await render({values: original})
    await click('缓存')
    await input(`oxi-${field}`, '')
    expect(wrapper.emitted('error')?.at(-1)?.[0]).toContain('请填写整数')
    expect(wrapper.emitted('change')).toBeUndefined()

    await wrapper.get(`#oxi-${toggle}`).trigger('click')
    expect(lastChange(wrapper)).toEqual({[toggle]: false})
    expect(wrapper.emitted('error')?.at(-1)?.[0]).toBe('')
    await wrapper.setProps({values: {...original, [toggle]: false}})
    expect((wrapper.get(`#oxi-${field}`).element as HTMLInputElement).value).toBe('')

    await wrapper.get(`#oxi-${toggle}`).trigger('click')
    expect(lastChange(wrapper)).toEqual({[toggle]: true})
    expect((wrapper.get(`#oxi-${field}`).element as HTMLInputElement).value).toBe('')
    expect(wrapper.emitted('error')?.at(-1)?.[0]).toContain('请填写整数')
    expect(wrapper.emitted('change')).toHaveLength(2)
  })

  it('keeps independent probe errors active when disabling the main cache', async () => {
    const wrapper = await render({values: {...values(), cache_enabled: true, cache_size: 10240, probe_cache_enabled: true, probe_cache_size: 2048}})
    await click('缓存')
    await input('oxi-cache_size', '')
    await input('oxi-probe_cache_size', '')
    await wrapper.get('#oxi-cache_enabled').trigger('click')
    expect(wrapper.emitted('error')?.at(-1)?.[0]).toContain('探测缓存条目数')
    await wrapper.get('#oxi-probe_cache_enabled').trigger('click')
    expect(wrapper.emitted('error')?.at(-1)?.[0]).toBe('')
  })

  it('updates cache error dependencies when the current projection changes externally', async () => {
    const original = {...values(), cache_enabled: true, cache_size: 10240}
    const wrapper = await render({values: original})
    await click('缓存')
    await input('oxi-cache_size', '')
    await wrapper.setProps({values: {...original, cache_enabled: false}})
    expect(wrapper.emitted('error')?.at(-1)?.[0]).toBe('')
    await wrapper.setProps({values: original})
    expect(wrapper.emitted('error')?.at(-1)?.[0]).toContain('请填写整数')
    expect((wrapper.get('#oxi-cache_size').element as HTMLInputElement).value).toBe('')
    expect(wrapper.emitted('change')).toBeUndefined()
  })

  it('applies only editable performance fields and separately disables navigation', async () => {
    const wrapper = await render({navigationDisabled: true, capabilities: {aggregate_rules: {editable: false, reason: '当前无需合并'}, worker_threads: {editable: false, reason: '手工结构'}}})
    await click('填入已验证性能参数')
    const changes = lastChange(wrapper) as Record<string, any>
    expect(changes.cache_lazy_ttl).toBe(300)
    expect(changes).not.toHaveProperty('aggregate_rules')
    expect(changes).not.toHaveProperty('worker_threads')
    const native = [...document.querySelectorAll('button')].filter(button => ['全部功能', '完整参数'].includes(button.textContent?.trim() || ''))
    expect(native.length).toBeGreaterThan(0)
    expect(native.every(button => button.disabled)).toBe(true)
    await input('oxi-listen', '127.0.0.1:5454')
    expect(lastChange(wrapper)).toEqual({listen: '127.0.0.1:5454'})
  })

  it.each(['ros_route', 'ros_address_list'])('restores %s TLS fields when rapid off/on returns the unchanged server projection', async feature => {
    const tls = { server_name: 'router.example.test', ca: 'certs/router-ca.pem', insecure: false }
    const original = {...values(), [feature]: {enabled: true, args: {address: '192.0.2.1:8729', tls}}}
    const wrapper = await render({values: original})
    const toggle = () => document.getElementById(`oxi-${feature}.args.tls`) as HTMLButtonElement
    toggle().click(); await flushPromises()
    expect(lastChange(wrapper)).toEqual({[`${feature}.args.tls`]: null})
    toggle().click(); await flushPromises()
    expect(lastChange(wrapper)).toEqual({[`${feature}.args.tls`]: {}})
    // Both clicks can share one patch. Its response still contains the original TLS values.
    await wrapper.setProps({values: structuredClone(original)})
    expect((document.getElementById(`oxi-${feature}.args.tls.server_name`) as HTMLInputElement).value).toBe(tls.server_name)
    expect((document.getElementById(`oxi-${feature}.args.tls.ca`) as HTMLInputElement).value).toBe(tls.ca)
    expect(document.getElementById(`oxi-${feature}.args.tls.insecure`)?.getAttribute('aria-checked')).toBe('false')
    expect(original).toMatchObject({[feature]: {args: {tls}}})
  })

  it('reconciles normalized values even when the projection is unchanged and preserves unfinished input', async () => {
    const original = values()
    const wrapper = await render({values: original})
    await input('oxi-worker_threads', '6')
    await input('oxi-listen', '127.0.0.1:5454')
    await wrapper.setProps({values: structuredClone(original)})
    expect((document.getElementById('oxi-worker_threads') as HTMLInputElement).value).toBe('8')
    expect((document.getElementById('oxi-listen') as HTMLInputElement).value).toBe(original.listen)
    await input('oxi-worker_threads', '')
    await input('oxi-listen', '127.0.0.1:5555')
    await wrapper.setProps({values: structuredClone(original)})
    expect((document.getElementById('oxi-worker_threads') as HTMLInputElement).value).toBe('')
    expect((document.getElementById('oxi-listen') as HTMLInputElement).value).toBe(original.listen)
    expect(wrapper.emitted('error')?.at(-1)?.[0]).toContain('请填写整数')
  })

  it('sends TLS removal as JSON-safe null and provides full native access', async () => {
    const wrapper = await render()
    const fields = wrapper.findAllComponents(OxidnsFields).find(field => field.props('fields').some((item: any) => item.path === 'ros_route.args.tls'))!
    fields.vm.$emit('change', 'ros_route.args.tls', undefined)
    await flushPromises()
    expect(lastChange(wrapper)).toEqual({'ros_route.args.tls': null})
    await click('全部功能')
    expect(wrapper.emitted('full')).toHaveLength(1)
  })
})
