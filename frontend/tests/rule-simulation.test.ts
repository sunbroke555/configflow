import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DOMWrapper, flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { AxiosHeaders, type AxiosResponse, type InternalAxiosRequestConfig } from 'axios'
import Rules from '@/views/Rules.vue'
import api, { nodeApi } from '@/api'
import { scopedRequests, setActiveProfileId } from '@/profileContext'
import type { ProxyGroup } from '@/types'

vi.mock('@/router', () => ({ default: { push: vi.fn() } }))

// Keep the real page, dialogs, API wrappers and profile interceptors mounted.
// Only the HTTP transport is replaced with a local fixture.
let wrapper: VueWrapper | undefined
let group: ProxyGroup
let rules: Array<Record<string, any>>
const requests: InternalAxiosRequestConfig[] = []
const originalAdapter = api.defaults.adapter
const scrollDescriptor = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollIntoView')

function response(config: InternalAxiosRequestConfig, data: unknown): AxiosResponse {
  return { config, data, status: 200, statusText: 'OK', headers: new AxiosHeaders() }
}

beforeEach(() => {
  sessionStorage.clear()
  scopedRequests.value = 0
  setActiveProfileId('simulation-profile')
  requests.length = 0
  group = { id: 'group-one', name: 'Primary policy', type: 'select', enabled: true, subscriptions: ['sub-one'], regex: 'HK' }
  rules = [{ id: 'rule-one', itemType: 'rule', rule_type: 'DOMAIN-SUFFIX', value: 'example.test', policy: group.name, enabled: true }]
  vi.spyOn(nodeApi, 'latency')
  vi.spyOn(window, 'scrollTo').mockImplementation(() => undefined)
  api.defaults.adapter = async config => {
    requests.push(config)
    if (config.url === '/rules/match-test') return response(config, {
      success: true, matched: true, rule_id: 'rule-one', item_type: 'rule',
      policy: group.name, priority: 1, matched_line: `DOMAIN-SUFFIX,example.test,${group.name}`, elapsed_time: 3
    })
    if (config.url === '/proxy-groups/preview-regex') return response(config, {
      success: true, nodes: [{ name: 'HK first' }, { name: 'HK cached winner' }], count: 2
    })
    if (config.url === '/nodes/latency') return response(config, { results: {
      'HK first': { latency: null }, 'HK cached winner': { latency: 1 }
    } })
    if (config.url === '/proxy-groups') return response(config, [structuredClone(group)])
    const body = typeof config.data === 'string' ? JSON.parse(config.data) : config.data
    if (config.url === '/rules') {
      if (config.method === 'post') rules.push(body)
      return response(config, config.method === 'get' ? structuredClone(rules) : { success: true })
    }
    if (config.url?.startsWith('/rules/')) {
      const id = config.url.slice('/rules/'.length)
      if (config.method === 'put') rules = rules.map(rule => rule.id === id ? body : rule)
      if (config.method === 'delete') rules = rules.filter(rule => rule.id !== id)
      return response(config, { success: true })
    }
    return response(config, [])
  }
  vi.stubGlobal('matchMedia', vi.fn((query: string) => ({
    matches: query.includes('prefers-reduced-motion'), media: query, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn()
  })))
  Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() })
})

afterEach(async () => {
  wrapper?.unmount()
  wrapper = undefined
  await flushPromises()
  api.defaults.adapter = originalAdapter
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  if (scrollDescriptor) Object.defineProperty(Element.prototype, 'scrollIntoView', scrollDescriptor)
  else Reflect.deleteProperty(Element.prototype, 'scrollIntoView')
  document.body.innerHTML = ''
  sessionStorage.clear()
})

async function renderAndSimulate() {
  wrapper = mount(Rules, { attachTo: document.body })
  await flushPromises()
  await wrapper.get('[aria-label="要模拟的域名或 IP"]').setValue('www.example.test')
  await wrapper.get('form').trigger('submit')
  await vi.waitFor(() => expect(wrapper!.get('form').text()).toContain('DOMAIN-SUFFIX,example.test,Primary policy'))
  expect(wrapper.get('form').text()).toContain('www.example.test')
  expect(wrapper.get('form').text()).toContain('Primary policy')
  expect(nodeApi.latency).not.toHaveBeenCalled()
  expect(requests.some(request => request.url === '/nodes/latency')).toBe(false)
  const match = requests.find(request => request.url === '/rules/match-test')!
  expect(match.headers.get('X-ConfigFlow-Profile')).toBe('simulation-profile')
  return wrapper
}

async function clickText(text: string, scope: ParentNode = document) {
  const button = Array.from(scope.querySelectorAll('button')).find(item => item.textContent?.trim() === text)
  expect(button, `button ${text}`).toBeTruthy()
  button!.click()
  await flushPromises()
}

describe('rule match simulation', () => {
  it.each(['url-test', 'fallback', 'load-balance'])('does not predict a %s device exit from cached node latency', async type => {
    group.type = type
    const page = await renderAndSimulate()
    expect(page.get('form').text()).not.toContain('HK first')
    expect(page.get('form').text()).not.toContain('HK cached winner')
    expect(page.get('form').text()).not.toContain('按最近测速推断')
    expect(requests.some(request => request.url === '/proxy-groups/preview-regex')).toBe(false)
  })

  it.each(['subscription', 'aggregation'])('keeps a select group default from its %s preview without implying device health', async source => {
    if (source === 'aggregation') Object.assign(group, { aggregations: ['agg-one'], aggregation_regex: 'HK.*' })
    const page = await renderAndSimulate()
    const preview = requests.find(request => request.url === '/proxy-groups/preview-regex')!
    expect(JSON.parse(preview.data).source).toBe(source)
    expect(preview.headers.get('X-ConfigFlow-Profile')).toBe('simulation-profile')
    expect(page.get('form').text()).toContain('默认：HK first')
    expect(page.get('form').text()).not.toContain('HK cached winner')
    expect(page.get('form').text()).not.toContain('1ms')
    expect(page.get('[title="策略组默认选中，实际出口以目标设备 Mihomo 面板为准"]').text()).toBe('默认：HK first')
  })

  it('does not resolve a followed group as an independent default', async () => {
    group.follow_group = 'another-group'
    const page = await renderAndSimulate()
    expect(page.get('form').text()).not.toContain('HK first')
    expect(requests.some(request => request.url === '/proxy-groups/preview-regex')).toBe(false)
  })

  it('preserves rule create, edit and delete after a simulation', async () => {
    const page = await renderAndSimulate()
    await clickText('添加规则')
    await new DOMWrapper(document.querySelector<HTMLInputElement>('#rule-value')!).setValue('created.test')
    await clickText('保存', document.querySelector('[role="dialog"]')!)
    const created = rules.find(rule => rule.value === 'created.test')!
    expect(created).toBeTruthy()
    expect(page.text()).toContain('created.test')
    const row = page.findAll('.rule-row').find(item => item.text().includes('created.test'))!
    await row.get('[aria-label="编辑"]').trigger('click')
    await flushPromises()
    await new DOMWrapper(document.querySelector<HTMLInputElement>('#rule-value')!).setValue('edited.test')
    await clickText('保存', document.querySelector('[role="dialog"]')!)
    expect(rules.find(rule => rule.id === created.id)?.value).toBe('edited.test')
    const edited = page.findAll('.rule-row').find(item => item.text().includes('edited.test'))!
    await edited.get('[aria-label="从当前配置删除，删除后无法恢复"]').trigger('click')
    await flushPromises()
    expect(rules.some(rule => rule.id === created.id)).toBe(false)
    expect(page.text()).not.toContain('edited.test')
    const mutations = requests.filter(request => ['post', 'put', 'delete'].includes(request.method || '') && request.url !== '/rules/match-test' && request.url !== '/proxy-groups/preview-regex')
    expect(mutations.map(request => request.method)).toEqual(['post', 'put', 'delete'])
    mutations.forEach(request => expect(request.headers.get('X-ConfigFlow-Profile')).toBe('simulation-profile'))
    expect(nodeApi.latency).not.toHaveBeenCalled()
  })
})
