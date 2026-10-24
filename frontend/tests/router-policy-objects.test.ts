import { afterEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import PolicyRoutingObjects from '@/components/routers/PolicyRoutingObjects.vue'
import { routerApi } from '@/api'

vi.mock('@/api', () => ({ routerApi: { policy: vi.fn() } }))
const wrappers: VueWrapper[] = []
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); vi.clearAllMocks() })
const response = (objects: any[] = []) => ({ data: { success: true, generated_at: '2026-10-17T10:00:00',
  policy: { objects, source_agents: [{ id: 'dns', name: 'OxiDNS' }] } } })
const setup = async () => {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:pathMatch(.*)*', component: { template: '<div />' } }] })
  await router.push('/')
  const wrapper = mount(PolicyRoutingObjects, { props: { id: 'ros', profile: 'office' }, global: { plugins: [router] } })
  wrappers.push(wrapper)
  await flushPromises()
  return wrapper
}
const click = async (wrapper: VueWrapper, label: string) => {
  const button = wrapper.findAll('button').find(button => button.text().startsWith(label))!
  if (button.attributes('role') === 'tab') await button.trigger('mousedown', { button: 0 })
  await button.trigger('click')
  await flushPromises()
}

describe('RouterOS policy objects', () => {
  it('offers add actions even without published objects and emits edits with saved roles', async () => {
    vi.mocked(routerApi.policy).mockResolvedValue(response() as any)
    const wrapper = await setup()
    await click(wrapper, '管理来源设备'); expect(wrapper.emitted('add-source')).toHaveLength(1)
    await click(wrapper, '编辑目的规则集'); expect(wrapper.emitted('edit-destinations')).toHaveLength(1)
    const object = { id: '*1', kind: 'addresses', status: 'changed', editor: 'source', configured_address: '192.0.2.105/32', fields: { list: 'renamed', address: '192.0.2.99' } }
    vi.mocked(routerApi.policy).mockResolvedValue(response([object]) as any)
    await click(wrapper, '刷新对象'); await click(wrapper, '在客户端设备中管理')
    expect(wrapper.emitted('edit')?.[0]).toEqual([object])
    expect(wrapper.findAll('thead th').at(-1)?.text()).toBe('操作')
    expect(wrapper.findAll('tbody td').at(-1)?.text()).toBe('在客户端设备中管理')
    await wrapper.setProps({ disabled: true })
    await click(wrapper, '在客户端设备中管理'); expect(wrapper.emitted('edit')).toHaveLength(1)
  })
  it('pages large address lists, searches fields without loading source editors', async () => {
    vi.mocked(routerApi.policy).mockResolvedValue(response(Array.from({ length: 123 }, (_, i) => ({
      id: `*${i}`, kind: 'addresses', status: 'matched', fields: { list: 'CF_Destination_Proxy_IP_01', address: `10.0.0.${i}/32` }
    }))) as any)
    const wrapper = await setup()
    expect(routerApi.policy).toHaveBeenCalledWith('ros', 'office')
    expect(wrapper.findAll('tbody tr')).toHaveLength(50)
    await click(wrapper, '下一页')
    expect(wrapper.find('tbody').text()).toContain('10.0.0.50/32')
    await wrapper.find('input').setValue('10.0.0.122/32')
    expect(wrapper.findAll('tbody tr')).toHaveLength(1)
    expect(wrapper.text()).toContain('第 1 / 1 页')
  })

  it('distinguishes live edits from missing objects and shows runtime next hop', async () => {
    vi.mocked(routerApi.policy).mockResolvedValue(response([
      { id: '*1', kind: 'mangle', status: 'changed', fields: { chain: 'prerouting', action: 'jump', disabled: 'true', 'jump-target': 'CF_Proxy_Routing_01' } },
      { id: '*2', kind: 'mangle', status: 'missing', fields: { chain: 'CF_Proxy_Routing_01', action: 'mark-routing' } },
      { id: '*3', kind: 'routes', status: 'matched', fields: { 'routing-table': 'CF_Proxy_Routing_01', gateway: '192.168.0.3@main', 'immediate-gw': '192.168.0.3%lan4', active: 'true' } }
    ]) as any)
    const wrapper = await setup()
    await click(wrapper, 'Mangle')
    expect(wrapper.text()).toContain('已被外部修改')
    expect(wrapper.text()).toContain('设备上缺失')
    expect(wrapper.text()).toContain('查看上次发布记录')
    await click(wrapper, '下一跳')
    expect(wrapper.text()).toContain('192.168.0.3%lan4')
    expect(wrapper.text()).toContain('路由活跃')
  })

  it('discards an old device response and clears old data after failed refresh', async () => {
    let resolveOld!: (value: any) => void
    vi.mocked(routerApi.policy).mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve }) as any)
    const wrapper = await setup()
    vi.mocked(routerApi.policy).mockResolvedValueOnce(response() as any)
    await wrapper.setProps({ id: 'new', profile: 'home' })
    await flushPromises()
    resolveOld(response([{ id: '*old', kind: 'addresses', status: 'matched', fields: { address: 'old-data' } }]))
    await flushPromises()
    expect(wrapper.text()).not.toContain('old-data')
    expect(wrapper.text()).toContain('尚无已下发的策略对象')
    vi.mocked(routerApi.policy).mockRejectedValueOnce(new Error('设备无法连接'))
    await click(wrapper, '刷新')
    expect(wrapper.find('[role="alert"]').text()).toContain('设备无法连接')
    expect(wrapper.text()).not.toContain('尚无已下发的策略对象')
  })
})
