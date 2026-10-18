import { beforeEach, afterEach, it, expect, vi } from 'vitest'
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import Subscriptions from '@/views/Subscriptions.vue'
import Aggregations from '@/views/SubscriptionAggregation.vue'
import api, { subscriptionApi } from '@/api'
vi.mock('@/api', () => ({ default: { get: vi.fn(), put: vi.fn() }, subscriptionApi: { getAll: vi.fn(), update: vi.fn() } }))
const defaults = {enable:true,url:'https://www.gstatic.com/generate_204',interval:300,timeout:5000,lazy:true,'expected-status':'204'}
let row: any, wrapper: VueWrapper
const copy = (v: any) => JSON.parse(JSON.stringify(v))
beforeEach(() => {
  vi.resetAllMocks()
  Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable:true, value:vi.fn() })
  Object.defineProperty(window, 'matchMedia', { configurable:true, value:vi.fn(() => ({matches:false,addEventListener:vi.fn(),removeEventListener:vi.fn()})) })
  row = {id:'source',name:'Source',url:'https://example.test/sub',enabled:true,interval:86400,subscriptions:[],nodes:['n'],health_check:{...defaults,lazy:false,enable:false}}
  vi.mocked(subscriptionApi.getAll).mockImplementation(async () => ({data:[copy(row)]}) as any)
  vi.mocked(subscriptionApi.update).mockImplementation(async (_id, payload) => {row=copy(payload);return {data:{success:true}} as any})
  vi.mocked(api.put).mockImplementation(async (_path, payload) => {row=copy(payload);return {data:{success:true}} as any})
  vi.mocked(api.get).mockImplementation(async path => ({data: path==='/aggregations' ? [copy(row)] : path==='/proxy-groups/health-defaults' ? {provider:defaults} : path.endsWith('/count') ? {success:true,total_count:0} : path==='/subscriptions/health' ? {items:[]} : path.endsWith('/health-impact') ? [{kind:'node',name:'Node'}, {profile_name:''}, {profile_name:'Default'}, {profile_name:'Default'}] : []}) as any)
})
afterEach(() => {wrapper?.unmount();document.body.innerHTML='';vi.restoreAllMocks()})
async function button(text:string) {
  const el=[...document.querySelectorAll('button')].find(e => e.textContent?.trim()===text)!
  expect(el).toBeTruthy();el.click();await flushPromises()
}
async function change(key:string,value:string) {
  const el=document.querySelector(`[data-health="${key}"]`) as HTMLInputElement
  el.value=value;el.dispatchEvent(new Event(el.tagName==='SELECT'?'change':'input',{bubbles:true}));await flushPromises()
}
for (const [name,component] of [['subscription',Subscriptions],['aggregation',Aggregations]] as const) {
  for (const partial of [{interval:321},{}]) {
    it(`${name} partial ${JSON.stringify(partial)} stays raw on name-only save and reopen`,async () => {
      row.health_check=copy(partial)
      const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component}]})
      await router.push('/');await router.isReady()
      wrapper=mount(component,{attachTo:document.body,global:{plugins:[router]}});await flushPromises()
      await button('编辑')
      expect(document.body.textContent).toContain('引用配置：Default')
      expect(document.body.textContent).not.toContain('引用配置：、')
      expect(vi.mocked(api.get).mock.calls.filter(([path]) => path === '/proxy-groups/health-defaults')).toHaveLength(0)
      expect((document.querySelector('[data-health="timeout"]') as HTMLInputElement).value).toBe('')
      expect((document.querySelector('[data-health="lazy"]') as HTMLSelectElement).value).toBe('unset')
      expect((document.querySelector('[data-health="enable"]') as HTMLSelectElement).value).toBe('unset')
      expect(document.body.textContent).toContain('未设置')
      const input=document.querySelector(name==='subscription'?'#sub-name':'#agg-name') as HTMLInputElement
      input.value='Renamed';input.dispatchEvent(new Event('input',{bubbles:true}));await flushPromises()
      await button('保存');expect(row.health_check).toEqual(partial)
      await button('编辑')
      expect((document.querySelector('[data-health="enable"]') as HTMLSelectElement).value).toBe('unset')
      expect(row.health_check).toEqual(partial)
    })
  }
  it(`${name} keeps unset provider fields absent through unrelated edit, save and reopen`,async () => {
    const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component}]})
    await router.push('/');await router.isReady()
    wrapper=mount(component,{attachTo:document.body,global:{plugins:[router]}});await flushPromises()
    await button('编辑')
    // Explicit false is distinct from unset.
    expect((document.querySelector('[data-health="lazy"]') as HTMLSelectElement).value).toBe('false')
    expect((document.querySelector('[data-health="enable"]') as HTMLSelectElement).value).toBe('false')
    await change('timeout','');await change('lazy','unset');await change('enable','unset')
    expect((document.querySelector('[data-health="timeout"]') as HTMLInputElement).value).toBe('')
    expect((document.querySelector('[data-health="lazy"]') as HTMLSelectElement).value).toBe('unset')
    expect((document.querySelector('[data-health="enable"]') as HTMLSelectElement).value).toBe('unset')
    await change('interval','321')
    const input=document.querySelector(name==='subscription'?'#sub-name':'#agg-name') as HTMLInputElement
    input.value='Renamed';input.dispatchEvent(new Event('input',{bubbles:true}));await flushPromises()
    await button('保存')
    expect(row.health_check).toEqual({url:defaults.url,interval:321,'expected-status':'204'})
    await button('编辑')
    expect((document.querySelector('[data-health="timeout"]') as HTMLInputElement).value).toBe('')
    expect((document.querySelector('[data-health="lazy"]') as HTMLSelectElement).value).toBe('unset')
    expect((document.querySelector('[data-health="enable"]') as HTMLSelectElement).value).toBe('unset')
  })
}

it('saves zero as manual-only source refresh and retains it on reopen', async () => {
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:Subscriptions}]})
  await router.push('/');await router.isReady()
  wrapper=mount(Subscriptions,{attachTo:document.body,global:{plugins:[router]}});await flushPromises()
  await button('编辑')
  const input=document.querySelector('#sub-interval') as HTMLInputElement
  input.value='0';input.dispatchEvent(new Event('input',{bubbles:true}));await flushPromises()
  await button('保存')
  expect(row.interval).toBe(0)
  expect(document.body.textContent).toContain('仅手动')
  await button('编辑')
  expect((document.querySelector('#sub-interval') as HTMLInputElement).value).toBe('0')
})

it.each(['', '-1', '1', '60.5'])('rejects invalid source interval %s without saving', async value => {
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:Subscriptions}]})
  await router.push('/');await router.isReady()
  wrapper=mount(Subscriptions,{attachTo:document.body,global:{plugins:[router]}});await flushPromises()
  await button('编辑')
  const input=document.querySelector('#sub-interval') as HTMLInputElement
  input.value=value;input.dispatchEvent(new Event('input',{bubbles:true}));await flushPromises()
  await button('保存')
  expect(subscriptionApi.update).not.toHaveBeenCalled()
})
