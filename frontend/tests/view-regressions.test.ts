import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { AxiosHeaders } from 'axios'
import type { Agent } from '@/types'
import ProxyGroups from '@/views/ProxyGroups.vue'
import Profiles from '@/views/Profiles.vue'
import Agents from '@/views/Agents.vue'
import ConfirmHost from '@/components/feedback/ConfirmHost.vue'
import MultiSelect from '@/components/common/MultiSelect.vue'
// Exact git blob from a5bb908, not a modified production component.
import LegacyConfirmHost from './fixtures/LegacyConfirmHost.vue'
import { agentApi, profileApi, proxyGroupApi, nodeApi } from '@/api'
import * as feedback from '@/lib/feedback'
import { setActiveProfileId } from '@/profileContext'

// All data and writes stay at the network seam. Views, stores and UI are real.
vi.mock('@/api', () => ({
 default: { get: vi.fn(async (path: string) => ({ data: path === '/aggregations' ? [
  { id: 'agg-on', name: 'Synthetic enabled aggregation', enabled: true },
  { id: 'agg-off', name: 'Synthetic disabled aggregation', enabled: false }
 ] : path === '/subscriptions' ? [{ id: 'sub-one', name: 'Synthetic subscription' }] : [] })), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
 agentApi: { getAll: vi.fn() },
 proxyGroupApi: { getAll: vi.fn(async () => ({ data: [
  { id: 'group-one', name: 'Synthetic strategy', type: 'select', enabled: true, manual_nodes: ['DIRECT'] }
 ] })), create: vi.fn(async () => ({})), update: vi.fn(async () => ({})) },
 nodeApi: { getAll: vi.fn(async () => ({ data: [{ id: 'node-one', name: 'Synthetic manual node' }] })) },
 profileApi: { list: vi.fn(), delete: vi.fn() }
}))
const wrappers: ReturnType<typeof mount>[] = []
// jsdom has no layout/scrolling; replace only that absent browser primitive.
const scrollDescriptor = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollIntoView')
beforeAll(() => Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() }))
afterAll(() => {
 if (scrollDescriptor) Object.defineProperty(Element.prototype, 'scrollIntoView', scrollDescriptor)
 else delete (Element.prototype as any).scrollIntoView
})
beforeEach(() => {
 vi.clearAllMocks()
 setActiveProfileId('default')
 vi.mocked(profileApi.list).mockResolvedValue({ data: [
  { id: 'default', name: 'Synthetic default' }, { id: 'target', name: 'Synthetic target' }
 ] } as any)
 vi.mocked(profileApi.delete).mockResolvedValue({} as any)
})
afterEach(() => {
 wrappers.splice(0).forEach(w => w.unmount())
 feedback.settleConfirm('cancel')
 vi.restoreAllMocks()
 document.body.innerHTML = ''
 sessionStorage.clear()
})
function render(component: any) {
 const w = mount(component, { attachTo: document.body })
 wrappers.push(w)
 return w
}
async function clickText(text: string) {
 await flushPromises()
 const button = Array.from(document.querySelectorAll('button')).find(b => b.textContent?.trim() === text)
 expect(button, `button ${text}`).toBeTruthy()
 button!.click()
 await flushPromises()
}
async function addSource(w: ReturnType<typeof mount>, label: string) {
 await clickText('添加策略组')
 const source = Array.from(document.querySelectorAll('label')).find(e => e.textContent?.trim() === label)
 expect(source, `source ${label}`).toBeTruthy()
 ;(source!.querySelector('[role="checkbox"]') as HTMLElement).click()
 await flushPromises()
 return w.findAllComponents(MultiSelect).find(s => s.props('placeholder') === {
  节点: '手动选择节点', 聚合: '选择订阅聚合', 策略: '选择已有策略组', 订阅: '选择订阅（自动包含订阅的所有节点）'
 }[label])!
}
describe('real view regressions with synthetic API data', () => {
 it('labels mixed Agent cards by service type, including Surge rather than MosDNS', async () => {
  const services = [
   { id: 'router', name: 'Primary router', service_type: 'mihomo' },
   { id: 'phone', name: 'Travel phone', service_type: 'surge' },
   { id: 'dns', name: 'DNS server', service_type: 'mosdns' }
  ] satisfies Pick<Agent, 'id' | 'name' | 'service_type'>[]
  vi.mocked(agentApi.getAll).mockResolvedValue({
   data: services.map(service => ({
    ...service, host: '127.0.0.1', port: 8080, profile_id: 'default',
    status: 'offline', last_heartbeat: '', version: '1.1.0-go', config_version: '0', enabled: true
   })),
   status: 200, statusText: 'OK', headers: {}, config: { headers: new AxiosHeaders() }
  })
  const w = render(Agents)
  await flushPromises()
  const cardLabel = (name: string) => {
   const header = w.findAll('header').find(header => header.find('p').exists() && header.get('p').text() === name)
   expect(header, `Agent card ${name}`).toBeDefined()
   return header!.get('[data-slot="badge"]').text()
  }
  expect(cardLabel('Primary router')).toBe('Mihomo')
  expect(cardLabel('Travel phone')).toBe('Surge')
  expect(cardLabel('DNS server')).toBe('MosDNS')
 })

 it('editing an existing group shows and saves a selected manual node', async () => {
  const w = render(ProxyGroups)
  await flushPromises()
  await clickText('编辑')
  await clickText('DIRECT')
  const option = Array.from(document.querySelectorAll('[role="option"]')).find(e => e.textContent?.trim() === 'Synthetic manual node')
  expect(option).toBeTruthy()
  ;(option as HTMLElement).click()
  await flushPromises()
  const select = w.findAllComponents(MultiSelect).find(s => s.props('placeholder') === '手动选择节点')!
  expect(select.props('modelValue')).toEqual(['DIRECT', 'node-one'])
  await select.find('button').trigger('click')
  await clickText('保存')
  expect(proxyGroupApi.update).toHaveBeenCalledTimes(1)
  expect(vi.mocked(proxyGroupApi.update).mock.calls[0][0]).toBe('group-one')
  expect(vi.mocked(proxyGroupApi.update).mock.calls[0][1].manual_nodes).toEqual(['DIRECT', 'node-one'])
 })

 it('subscription selector renders and selects API choices (positive DOM control)', async () => {
  const w = render(ProxyGroups)
  await flushPromises()
  const select = await addSource(w, '订阅')
  await clickText('选择订阅（自动包含订阅的所有节点）')
  const options = Array.from(document.querySelectorAll('[role="option"]'))
  expect(options.map(e => e.textContent?.trim())).toEqual(['Synthetic subscription'])
  ;(options[0] as HTMLElement).click()
  await flushPromises()
  expect(select.props('modelValue')).toEqual(['sub-one'])
 })
 it.each([
  ['节点', '手动选择节点', [
   { value: 'DIRECT', label: 'DIRECT' }, { value: 'REJECT', label: 'REJECT' },
   { value: 'node-one', label: 'Synthetic manual node' }
  ]],
  ['聚合', '选择订阅聚合', [{ value: 'agg-on', label: 'Synthetic enabled aggregation' }]],
  ['策略', '选择已有策略组', [{ value: 'group-one', label: 'Synthetic strategy' }]]
 ])('%s selector renders available API-backed choices', async (label, placeholder, expected) => {
  const warnings: string[] = []
  vi.spyOn(console, 'warn').mockImplementation((...args) => warnings.push(String(args[0])))
  const w = render(ProxyGroups)
  await flushPromises()
  expect(nodeApi.getAll).toHaveBeenCalledTimes(1)
  const select = await addSource(w, label)
  await clickText(placeholder)
  const options = Array.from(document.querySelectorAll('[role="option"]')).map(e => e.textContent?.trim())
  console.log('SELECTOR_TRACE', JSON.stringify({ label, options: select.props('options') ?? null, rendered: options, warnings }))
  expect(select.props('options')).toEqual(expected)
  expect(options).toEqual(expected.map(option => option.label))
  expect(proxyGroupApi.create).not.toHaveBeenCalled()
  expect(proxyGroupApi.update).not.toHaveBeenCalled()
  const selected = expected[expected.length - 1]
  const option = Array.from(document.querySelectorAll('[role="option"]')).find(e => e.textContent?.trim() === selected.label)
  ;(option as HTMLElement).click()
  await flushPromises()
  expect(select.props('modelValue')).toEqual([selected.value])
  const nameInput = document.querySelector('#group-name') as HTMLInputElement
  nameInput.value = `Synthetic ${label} selection`
  nameInput.dispatchEvent(new Event('input', { bubbles: true }))
  // Dismiss the option popover through its real trigger before saving the dialog.
  const trigger = select.find('button')
  await trigger.trigger('click')
  await clickText('保存')
  expect(proxyGroupApi.create).toHaveBeenCalledTimes(1)
  const field = { 节点: 'manual_nodes', 聚合: 'aggregations', 策略: 'include_groups' }[label]
  expect(vi.mocked(proxyGroupApi.create).mock.calls[0][0][field]).toEqual([selected.value])
  expect(warnings).toEqual([])
 })
})

// Run separately by test-name filter to compare old and fixed confirmation hosts.
describe.each([
 ['legacy a5bb908', LegacyConfirmHost], ['current b781cb2', ConfirmHost]
])('Profiles deletion using %s', (version, host) => {
 it('uninstrumented native confirmation click reproduces version-specific API behavior', async () => {
  const w = render(Profiles)
  render(host)
  await flushPromises()
  await w.get('button[aria-label="删除"]').trigger('click')
  await clickText('删除')
  expect(profileApi.delete).toHaveBeenCalledTimes(version.startsWith('legacy') ? 0 : 1)
  expect(feedback.confirmState.open).toBe(false)
 })
 it('confirmed deletion reaches API and refreshes synthetic profile list', async () => {
  const w = render(Profiles)
  render(host)
  await flushPromises()
  vi.mocked(profileApi.delete).mockImplementation(async () => {
   vi.mocked(profileApi.list).mockResolvedValue({ data: [{ id: 'default', name: 'Synthetic default' }] } as any)
   return {} as any
  })
  const events: string[] = []
  const original = feedback.settleConfirm
  vi.spyOn(feedback, 'settleConfirm').mockImplementation(choice => {
   events.push(`${choice}:resolver=${Boolean(feedback.confirmState.resolve)}`)
   original(choice)
  })
  await w.get('button[aria-label="删除"]').trigger('click')
  await flushPromises()
  expect(feedback.confirmState.open).toBe(true)
  expect(document.querySelector('[role="alertdialog"]')?.textContent).toContain('Synthetic target')
  await clickText('删除')
  console.log('DELETE_TRACE', JSON.stringify({ version, events, calls: vi.mocked(profileApi.delete).mock.calls, remaining: w.text().includes('Synthetic target') }))
  if (version.startsWith('legacy')) {
   // Immutable legacy fixture is a negative control, not the implementation under test.
   expect(profileApi.delete).not.toHaveBeenCalled()
   expect(profileApi.list).toHaveBeenCalledTimes(1)
   expect(w.text()).toContain('Synthetic target')
  } else {
   expect(profileApi.delete).toHaveBeenCalledExactlyOnceWith('target')
   expect(profileApi.list).toHaveBeenCalledTimes(2)
   expect(w.text()).not.toContain('Synthetic target')
  }
 })
 it('cancel does not delete or refresh', async () => {
  const w = render(Profiles)
  render(host)
  await flushPromises()
  await w.get('button[aria-label="删除"]').trigger('click')
  await clickText('取消')
  expect(profileApi.delete).not.toHaveBeenCalled()
  expect(profileApi.list).toHaveBeenCalledTimes(1)
  expect(w.text()).toContain('Synthetic target')
 })
 it('default profile has no deletion action', async () => {
  vi.mocked(profileApi.list).mockResolvedValue({ data: [{ id: 'default', name: 'Synthetic default' }] } as any)
  const w = render(Profiles)
  await flushPromises()
  expect(w.find('button[aria-label="删除"]').exists()).toBe(false)
  expect(profileApi.delete).not.toHaveBeenCalled()
 })
})

describe('current Profiles API rejection feedback', () => {
 it('reports server denial rather than silently ignoring confirmed deletion', async () => {
  const w = render(Profiles)
  render(ConfirmHost)
  const error = vi.spyOn(feedback.notify, 'error').mockImplementation(() => 0)
  vi.mocked(profileApi.delete).mockRejectedValue({ response: { data: { message: 'Synthetic bound-profile denial' } } })
  await flushPromises()
  await w.get('button[aria-label="删除"]').trigger('click')
  await clickText('删除')
  expect(profileApi.delete).toHaveBeenCalledExactlyOnceWith('target')
  expect(error).toHaveBeenCalledWith('Synthetic bound-profile denial')
  expect(w.text()).toContain('Synthetic target')
 })
})
