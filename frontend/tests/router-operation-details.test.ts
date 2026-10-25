import { afterEach, describe, expect, it } from 'vitest'
import { mount, type VueWrapper } from '@vue/test-utils'
import RouterOperationPreview from '@/components/routers/RouterOperationPreview.vue'
import { operationDetails, type RouterPlanOperation } from '@/lib/routerOperationDetails'
const address = '/ip/firewall/address-list'
const fixture = (): RouterPlanOperation => ({ kind: 'ros_policy_replace', summary: '发布来源策略',
  previous: { spec: { sources: ['192.0.2.101/32'], destinations: ['203.0.113.0/24'], gateway: '192.0.2.3' }, resources: [{ id: '*missing', path: address, config: { address: '192.0.2.51' } }] },
  spec: { sources: ['192.0.2.105/32'], destinations: ['203.0.113.0/24', '198.51.100.0/24'], gateway: '192.0.2.4', egress_interface: 'lan' },
  bundle: [{ path: address, values: { list: 'CF_Source_Proxy_IP_02', address: '192.0.2.105/32', disabled: 'false' } },
    { path: '/ip/firewall/mangle', values: { chain: 'CF_Proxy_Routing_02', action: 'mark-routing', 'new-routing-mark': 'CF_Proxy_Routing_02', passthrough: 'false' } }],
  owned: [{ id: '*owned', path: address, config: { list: 'CF_Source_Proxy_IP_01', address: '192.0.2.101/32' } }],
  migrations: [{ id: '*static', path: '/ip/route', config: { 'dst-address': '198.51.100.0/24', gateway: '192.0.2.4', 'routing-table': 'main' } }],
  evidence: { unrelated_rule: 'do-not-render-evidence' }
})
const wrappers: VueWrapper[] = []
afterEach(() => wrappers.splice(0).forEach(w => w.unmount()))
describe('reviewable release operation details', () => {
  it('distinguishes logical address changes from exact object replacement and excludes unrelated evidence', () => {
    const groups = operationDetails(fixture())
    expect(groups.find(g => g.title === '来源范围')!.rows).toEqual([
      { label: '新增', value: '192.0.2.105/32', action: '新增' }, { label: '移除', value: '192.0.2.101/32', action: '移除' }])
    expect(groups.find(g => g.title === '目的网段')!.rows).toContainEqual({ label: '保留', value: '203.0.113.0/24', action: '保留' })
    expect(groups.find(g => g.title === '网关与出口')!.rows[0]).toEqual({ label: '下一跳', before: '192.0.2.3', value: '192.0.2.4' })
    expect(groups.find(g => g.title === '新增Mangle')!.rows[0].value).toContain('new-routing-mark: CF_Proxy_Routing_02')
    expect(groups.find(g => g.title === '删除旧受管对象')!.rows).toHaveLength(1)
    expect(groups.find(g => g.title === '迁移主表静态路由')!.rows[0].label).toContain('*static')
    expect(JSON.stringify(groups)).not.toContain('*missing'); expect(JSON.stringify(groups)).not.toContain('do-not-render-evidence')
  })
  it('shows withdrawal as removals without inventing new objects', () => {
    const op = fixture(); op.bundle = []; op.spec = null; op.migrations = []
    const groups = operationDetails(op)
    expect(groups.find(g => g.title === '来源范围')!.rows.every(r => r.action === '移除')).toBe(true)
    expect(groups.some(g => g.title.startsWith('新增'))).toBe(false)
    expect(groups.find(g => g.title === '删除旧受管对象')!.note).toContain('撤销策略')
  })
  it('retains NAT source restrictions and separates appended destinations from existing matchers', () => {
    const groups = operationDetails({ kind: 'nat_update', summary: '扩充 NAT', expected: { 'src-address': '192.0.2.0/24', protocol: 'tcp', 'out-interface': 'lan', password: 'not-public' }, add_dst: ['203.0.113.0/24'], comment: 'cf-test' })
    expect(groups.find(g => g.title === '现有匹配条件')!.rows.map(row => row.value)).toEqual(['192.0.2.0/24', 'tcp', 'lan'])
    expect(groups.find(g => g.title === '新增目的网段')!.rows[0].value).toBe('203.0.113.0/24')
    expect(JSON.stringify(groups)).not.toContain('not-public')
  })
  it('shows the NAT list rule, member changes and the old rules it replaces', () => {
    const legacy = Array.from({ length: 3 }, (_, i) => ({ id: `*L${i}`, path: '/ip/firewall/nat',
      config: { chain: 'srcnat', action: 'accept', 'src-address': '192.0.2.1/32', 'dst-address': `203.0.113.${i}/32`, comment: 'cf-mihomo' } }))
    const groups = operationDetails({ kind: 'nat_list_sync', summary: '新增 NAT 免转换规则', comment: 'cf-mihomo-nat',
      src_list: 'cf-mihomo-nat-src', dst_list: 'cf-mihomo-nat-dst', rule: null, out_interface: '', replace_rule: false, withdraw: false,
      src_add: ['192.0.2.1/32'], src_remove: [], dst_add: ['203.0.113.0/24'],
      dst_remove: [{ id: '*9', path: address, config: { list: 'cf-mihomo-nat-dst', address: '198.51.100.0/24' } }], legacy })
    expect(groups.map(g => g.title)).toEqual(['规则字段', '来源列表', '目的列表', '迁移删除的旧规则'])
    expect(groups[0].rows[0].value).toContain('目的地址列表 · dst-address-list: cf-mihomo-nat-dst')
    expect(groups[2].rows).toEqual([{ label: '新增', value: '203.0.113.0/24', action: '新增' }, { label: '移除', value: '198.51.100.0/24', action: '移除' }])
    expect(groups[3].rows).toHaveLength(3); expect(groups[3].note).toContain('共 3 条，合并为上面的 1 条规则')
    const stale = operationDetails({ kind: 'nat_list_sync', summary: '删除残留', comment: 'cf-mihomo-nat', src_list: 'cf-mihomo-nat-src',
      dst_list: 'cf-mihomo-nat-dst', rule: { id: '*R', path: '/ip/firewall/nat', config: {} }, out_interface: '', replace_rule: false,
      withdraw: false, src_add: [], src_remove: [], dst_add: [], dst_remove: [], legacy: [], stale: legacy.slice(0, 2) })
    expect(stale.map(g => g.title)).toEqual(['规则字段', '删除残留的旧规则'])
    expect(stale[1].rows).toHaveLength(2); expect(stale[1].note).toContain('目的网段已不再代理')
    const narrowed = operationDetails({ kind: 'nat_list_sync', summary: '限定出口', comment: 'cf-mihomo-nat', src_list: 'cf-mihomo-nat-src',
      dst_list: 'cf-mihomo-nat-dst', rule: { id: '*R', path: '/ip/firewall/nat', config: {} }, out_interface: 'bridge', replace_rule: true,
      withdraw: false, src_add: [], src_remove: [], dst_add: [], dst_remove: [], legacy: [] })
    expect(narrowed[0].rows[0].value).toContain('出口接口 · out-interface: bridge'); expect(narrowed[0].note).toContain('*R')
  })
  it('separates policy migrations by object type', () => {
    const op = fixture(); op.migrations = [...(op.migrations as unknown[]), { id: '*M', path: address, config: { list: 'cf-mihomo-nat-dst', address: '203.0.113.0/24' } }]
    const groups = operationDetails(op)
    expect(groups.find(g => g.title === '迁移主表静态路由')!.rows).toHaveLength(1)
    expect(groups.find(g => g.title === '移出 NAT 免转换目的列表')!.rows[0].label).toContain('*M')
  })
  it('mounts detail rows only when expanded, searches every address and paginates large plans', async () => {
    const op = fixture(); op.previous = null
    op.spec = { sources: Array.from({ length: 120 }, (_, i) => `192.0.2.${i + 1}/32`), destinations: [], gateway: '192.0.2.3' }
    const w = mount(RouterOperationPreview, { props: { op } }); wrappers.push(w)
    expect(w.findAll('dd')).toHaveLength(0)
    await w.get('button').trigger('click')
    expect(w.findAll('dd')).toHaveLength(50)
    await w.findAll('button').find(b => b.text() === '下一页')!.trigger('click')
    expect(w.find('dd').text()).toBe('192.0.2.51/32')
    await w.get('[aria-label="搜索变更详情"]').setValue('192.0.2.120/32')
    expect(w.findAll('dd')).toHaveLength(1); expect(w.find('dd').text()).toBe('192.0.2.120/32')
    await w.setProps({ op: { kind: 'route_add', summary: '新预览', cidr: '198.51.100.0/24', gateway: '192.0.2.4' } })
    expect(w.findAll('dd')).toHaveLength(0); expect(w.text()).toContain('新预览')
  })

  it('lists DNS guard objects in creation order and only previously owned objects for removal', () => {
    const create = [
      { role: 'hairpin-udp', path: '/ip/firewall/nat', values: { chain: 'srcnat', action: 'masquerade', 'dst-address': '192.0.2.53' } },
      { role: 'hijack-udp', path: '/ip/firewall/nat', top: true, values: { chain: 'dstnat', 'to-addresses': '192.0.2.53' } }
    ]
    const owned = [{ role: 'bypass:192.0.2.53/32', path: address, id: '*1', config: { list: 'cf-dnsguard-bypass' } },
      { role: 'netwatch', path: '/tool/netwatch', id: '*2', config: { host: '192.0.2.53', port: '53' } }]
    const groups = operationDetails({ kind: 'dns_guard_replace', summary: '更新', spec: {}, create, owned })
    expect(groups.find(g => g.title === '新建对象')!.rows.map(row => row.label)).toEqual(['NAT · hairpin-udp', 'NAT · hijack-udp'])
    expect(groups.find(g => g.title === '新建对象')!.rows[1].value).toContain('转发到地址 · to-addresses: 192.0.2.53')
    expect(groups.find(g => g.title === '删除旧受管对象')!.rows.map(row => row.label)).toEqual(['Netwatch · netwatch', '放行地址 · bypass:192.0.2.53/32'])
    const withdrawal = operationDetails({ kind: 'dns_guard_replace', summary: '撤销', spec: null, create: [], owned })
    expect(withdrawal.map(g => g.title)).toEqual(['删除旧受管对象'])
    expect(withdrawal[0].note).toContain('关闭强制 DNS')
  })
})

