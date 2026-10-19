import { describe, expect, it } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import OxidnsFields from '@/components/generate/OxidnsFields.vue'

import { values, render, click, input, lastChange } from './helpers/oxidns-common-settings'

describe('OxiDNS current common settings', () => {
  it('opens the requested recorder section without enabling or changing it', async () => {
    const wrapper = await render({ initialFeature: 'query_recorder' })
    const selected = wrapper.findAll('[role="tab"]').find(tab => tab.text() === '特色功能')!
    expect(selected.attributes('data-state')).toBe('active')
    expect(wrapper.get('[aria-label="查询记录设置"]').attributes('aria-expanded')).toBe('true')
    expect(wrapper.get('#oxi-enable-query_recorder').attributes('aria-checked')).toBe('false')
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

  it('edits shared download settings while scheduled rule updates remain disabled', async () => {
    const original = values()
    const wrapper = await render({ values: original, initialFeature: 'rule_update' })
    expect(wrapper.get('#oxi-enable-rule_update').attributes('aria-checked')).toBe('false')
    expect(wrapper.find('[id="oxi-rule_update.args.schedule"]').exists()).toBe(false)
    expect(wrapper.find('[id="oxi-rule_update.args.timezone"]').exists()).toBe(false)
    for (const [field, value] of [['timeout', '90s'], ['outbound', 'rules_proxy'], ['socks5', '127.0.0.1:1080']]) {
      const path = `rule_update.args.${field}`
      expect(wrapper.get(`[id="oxi-${path}"]`).isVisible()).toBe(true)
      await input(`oxi-${path}`, value)
      expect(lastChange(wrapper)).toEqual({ [path]: value })
      expect(wrapper.get('#oxi-enable-rule_update').attributes('aria-checked')).toBe('false')
    }
    expect(wrapper.emitted('change')).toHaveLength(3)
    expect(original.rule_update).toEqual({ enabled: false, args: {} })
  })

  it('keeps download errors active while ignoring only schedule errors when rule scheduling is disabled', async () => {
    const wrapper = await render({ values: { ...values(), rule_update: { enabled: true, args: { schedule: '30 3 * * *', timezone: 'Asia/Shanghai', timeout: '60s' } } }, initialFeature: 'rule_update' })
    const fields = wrapper.findAllComponents(OxidnsFields).find(field => field.props('fields').some((item: any) => item.path === 'rule_update.args.timeout'))!
    expect(wrapper.get('[id="oxi-rule_update.args.schedule"]').isVisible()).toBe(true)
    expect(wrapper.get('[id="oxi-rule_update.args.timezone"]').isVisible()).toBe(true)
    fields.vm.$emit('error', 'rule_update.args.schedule', 'Cron 无效')
    fields.vm.$emit('error', 'rule_update.args.timezone', '时区无效')
    await flushPromises()
    expect(wrapper.emitted('error')?.at(-1)?.[0]).toBe('Cron 无效')
    await wrapper.get('#oxi-enable-rule_update').trigger('click')
    expect(lastChange(wrapper)).toEqual({ 'rule_update.enabled': false })
    expect(wrapper.emitted('error')?.at(-1)?.[0]).toBe('')
    expect(wrapper.find('[id="oxi-rule_update.args.schedule"]').exists()).toBe(false)
    expect(wrapper.find('[id="oxi-rule_update.args.timezone"]').exists()).toBe(false)
    for (const field of ['timeout', 'outbound', 'socks5']) {
      fields.vm.$emit('error', `rule_update.args.${field}`, `${field} 无效`)
      await flushPromises()
      expect(wrapper.emitted('error')?.at(-1)?.[0]).toBe(`${field} 无效`)
      fields.vm.$emit('error', `rule_update.args.${field}`, '')
    }
    await wrapper.get('#oxi-enable-rule_update').trigger('click')
    expect(wrapper.emitted('error')?.at(-1)?.[0]).toBe('Cron 无效')
    expect((wrapper.get('[id="oxi-rule_update.args.schedule"]').element as HTMLInputElement).value).toBe('30 3 * * *')
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
