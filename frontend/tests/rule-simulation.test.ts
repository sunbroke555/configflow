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
let dnsState: Record<string, any>
let matchRuleId: string
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
  matchRuleId = 'rule-one'
  rules = [{ id: 'rule-one', itemType: 'rule', rule_type: 'DOMAIN-SUFFIX', value: 'example.test', policy: group.name, enabled: true }]
  dnsState = {
    success: true, upstreams: [], mosdns: {}, rules: { 'rule-one': { state: 'follow', route: 'proxy', follow_route: 'proxy' } },
    engine: { mode: 'template', editable: true, reason: '', default_forward: 'forward_remote', prefer_ipv4: true,
      local_probe_active: false, oxidns_in_use: true, warnings: [], agents: [] }
  }
  vi.spyOn(nodeApi, 'latency')
  vi.spyOn(window, 'scrollTo').mockImplementation(() => undefined)
  api.defaults.adapter = async config => {
    requests.push(config)
    if (config.url === '/rules/match-test') {
      const rule = rules.find(item => item.id === matchRuleId)
      return response(config, rule ? {
        success: true, matched: true, rule_id: rule.id, item_type: 'rule',
        policy: rule.policy, priority: rules.indexOf(rule) + 1, matched_line: `${rule.rule_type},${rule.value},${rule.policy}`, elapsed_time: 3
      } : { success: true, matched: false, elapsed_time: 3 })
    }
    if (config.url === '/proxy-groups/preview-regex') return response(config, {
      success: true, nodes: [{ name: 'HK first' }, { name: 'HK cached winner' }], count: 2
    })
    if (config.url === '/nodes/latency') return response(config, { results: {
      'HK first': { latency: null }, 'HK cached winner': { latency: 1 }
    } })
    if (config.url === '/proxy-groups') return response(config, [structuredClone(group)])
    const body = typeof config.data === 'string' ? JSON.parse(config.data) : config.data
    if (config.url === '/rules/dns') return response(config, structuredClone(dnsState))
    if (config.url === '/rules/compose') {
      const created = { id: `rule-${rules.length + 1}`, itemType: 'rule', rule_type: body.source.rule_type, value: body.source.text.trim(),
        policy: body.rule.policy, enabled: body.rule.enabled }
      rules.unshift(created)
      return response(config, { success: true, data: { rules: [created], library: null }, dns: {}, skipped: [], warnings: [], draft: true })
    }
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
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
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
    await new DOMWrapper(document.querySelector<HTMLTextAreaElement>('#rule-values')!).setValue('created.test')
    await clickText('保存 1 条规则', document.querySelector('[role="dialog"]')!)
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
    expect(mutations.map(request => `${request.method} ${request.url}`)).toEqual(['post /rules/compose', `put /rules/${created.id}`, `delete /rules/${created.id}`])
    expect(JSON.parse(mutations[0]!.data)).toMatchObject({ source: { kind: 'values', rule_type: 'DOMAIN-SUFFIX', text: 'created.test' }, rule: { policy: 'DIRECT' }, dns: 'follow' })
    expect(JSON.parse(mutations[1]!.data)).not.toHaveProperty('dns')
    mutations.forEach(request => expect(request.headers.get('X-ConfigFlow-Profile')).toBe('simulation-profile'))
    expect(nodeApi.latency).not.toHaveBeenCalled()
  })

  it('shows the DNS handling of the matched rule next to its policy', async () => {
    const page = await renderAndSimulate()
    await vi.waitFor(() => expect(page.get('[data-sim-dns]').text()).toBe('DNS：跟随 · 代理 DNS'))
    expect(requests.find(request => request.url === '/rules/dns')!.headers.get('X-ConfigFlow-Profile')).toBe('simulation-profile')
  })

  it('leaves the DNS chip out when the profile does not use OxiDNS', async () => {
    dnsState.engine.oxidns_in_use = false
    const page = await renderAndSimulate()
    expect(page.find('[data-sim-dns]').exists()).toBe(false)
    expect(page.text()).not.toContain('DNS：')
  })
})

describe('rule match simulation scan', () => {
  const stubMotion = (reduce: boolean) => vi.stubGlobal('matchMedia', vi.fn((query: string) => ({
    matches: reduce && query.includes('prefers-reduced-motion'), media: query, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn()
  })))

  const useRules = (count: number) => {
    rules = Array.from({ length: count }, (_, index) => ({
      id: `rule-${index + 1}`, itemType: 'rule', rule_type: 'DOMAIN-SUFFIX', value: `site${index + 1}.test`,
      // 策略交替，避免相邻规则合成分组
      policy: index % 2 ? 'DIRECT' : group.name, enabled: true
    }))
  }

  /** 记录依次加上 is-scan 的行，以及高亮期间被触发的组件重渲染次数 */
  const recordScan = (root: Node) => {
    const record = { scanned: [] as string[], maxAtOnce: 0, rendersWhileScanning: 0 }
    const observer = new MutationObserver(list => {
      for (const mutation of list) {
        const row = mutation.target as HTMLElement
        if (row.classList.contains('is-scan') && !(mutation.oldValue || '').split(/\s+/).includes('is-scan')) record.scanned.push(row.dataset.ruleKey!)
      }
      record.maxAtOnce = Math.max(record.maxAtOnce, (root as Element).querySelectorAll('.is-scan').length)
    })
    observer.observe(root, { subtree: true, attributes: true, attributeFilter: ['class'], attributeOldValue: true })
    const mixin = { renderTriggered() { if ((root as Element).querySelector('.is-scan')) record.rendersWhileScanning++ } }
    return { record, mixin, stop: () => observer.disconnect() }
  }

  async function mountAndSubmit(mixin: object, query = 'www.site.test') {
    wrapper = mount(Rules, { attachTo: document.body, global: { mixins: [mixin] } })
    await flushPromises()
    await wrapper.get('[aria-label="要模拟的域名或 IP"]').setValue(query)
    await wrapper.get('form').trigger('submit')
    return wrapper
  }

  it('moves the scan highlight row by row without re-rendering the list', async () => {
    stubMotion(false)
    useRules(4)
    matchRuleId = 'rule-3'
    const scan = recordScan(document.body)
    const page = await mountAndSubmit(scan.mixin)
    await vi.waitFor(() => expect(page.get('form').text()).toContain('DOMAIN-SUFFIX,site3.test'), { timeout: 3000 })
    scan.stop()
    expect(scan.record.scanned).toEqual(['rule-rule-1', 'rule-rule-2', 'rule-rule-3'])
    expect(scan.record.maxAtOnce).toBe(1)
    expect(scan.record.rendersWhileScanning).toBe(0)
    expect(document.querySelector('.is-scan')).toBeNull()
    expect(page.get('[data-rule-key="rule-rule-3"]').classes()).toContain('is-match')
    expect(page.findAll('.rule-row.is-match')).toHaveLength(1)
  })

  it('scans every row again for each new simulation', async () => {
    stubMotion(false)
    useRules(3)
    matchRuleId = 'rule-2'
    const scan = recordScan(document.body)
    const page = await mountAndSubmit(scan.mixin)
    await vi.waitFor(() => expect(page.get('form').text()).toContain('DOMAIN-SUFFIX,site2.test'), { timeout: 3000 })
    matchRuleId = 'missing'
    await page.get('form').trigger('submit')
    await vi.waitFor(() => expect(page.get('form').text()).toContain('未命中任何规则'), { timeout: 3000 })
    scan.stop()
    expect(scan.record.scanned).toEqual(['rule-rule-1', 'rule-rule-2', 'rule-rule-1', 'rule-rule-2', 'rule-rule-3'])
    expect(document.querySelector('.is-scan')).toBeNull()
    expect(page.findAll('.rule-row.is-match')).toHaveLength(0)
  })

  it('stops the scan and does not scroll when the page is left mid-scan', async () => {
    stubMotion(false)
    useRules(4)
    matchRuleId = 'rule-4'
    const scan = recordScan(document.body)
    const page = await mountAndSubmit(scan.mixin)
    await vi.waitFor(() => expect(document.querySelector('.is-scan')).not.toBeNull(), { timeout: 3000 })
    scan.stop()
    const rows = Array.from(page.element.querySelectorAll<HTMLElement>('[data-rule-key]'))
    const afterLeave = recordScan(page.element)
    page.unmount()
    wrapper = undefined
    await new Promise(resolve => window.setTimeout(resolve, 500))
    afterLeave.stop()
    expect(afterLeave.record.scanned).toEqual([])
    expect(rows.filter(row => row.classList.contains('is-scan'))).toEqual([])
    expect(window.scrollTo).not.toHaveBeenCalled()
  })

  it('shows the result without scanning when the system reduces motion', async () => {
    stubMotion(true)
    useRules(4)
    matchRuleId = 'rule-4'
    const scan = recordScan(document.body)
    const page = await mountAndSubmit(scan.mixin)
    await vi.waitFor(() => expect(page.get('form').text()).toContain('DOMAIN-SUFFIX,site4.test'))
    scan.stop()
    expect(scan.record.scanned).toEqual([])
    expect(page.get('[data-rule-key="rule-rule-4"]').classes()).toContain('is-match')
  })
})
