import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import MihomoConnections from '@/components/mihomo/MihomoConnections.vue'
import { mihomoPanelApi } from '@/api'
import { compareSources, connectionMatcher, INNER_SOURCE } from '@/types/mihomo'
import { confirmDanger } from '@/lib/feedback'

vi.mock('@/api', () => ({ mihomoPanelApi: { connections: vi.fn(), closeConnection: vi.fn(), closeAllConnections: vi.fn() } }))
vi.mock('@/lib/feedback', () => ({ confirmDanger: vi.fn(), notify: { success: vi.fn(), warning: vi.fn(), error: vi.fn() } }))
vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })

const connection = (id: string, host: string, sourceIP: string, extra: Record<string, unknown> = {}) => ({
  id, upload: 100, download: 200, start: new Date(Date.now() - 5000).toISOString(), chains: ['HK', 'Proxy'],
  rule: 'RuleSet', rulePayload: 'telegram',
  metadata: { network: 'tcp', type: 'Redir', sourceIP, sourcePort: '5000', destinationIP: '1.1.1.1', destinationPort: '443', host, ...extra }
})
const snapshot = (connections: unknown[], memory = 125 * 1024 * 1024) =>
  ({ data: { success: true, data: { uploadTotal: 1024, downloadTotal: 2048, memory, connections } } } as any)

const wrappers: VueWrapper[] = []
async function render() {
  const wrapper = mount(MihomoConnections, { props: { agentId: 'gw', active: true }, attachTo: document.body })
  wrappers.push(wrapper)
  await flushPromises()
  return { wrapper, state: (wrapper.vm as any).$.setupState }
}
beforeEach(() => { vi.clearAllMocks(); localStorage.clear() })
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); document.body.innerHTML = '' })

describe('connection helpers', () => {
  it('searches by regex and falls back to plain text for invalid patterns', () => {
    expect(connectionMatcher('^www\\.example')('www.example.com:443')).toBe(true)
    expect(connectionMatcher('^example')('www.example.com:443')).toBe(false)
    expect(connectionMatcher('a[b')('xa[by')).toBe(true)
    expect(connectionMatcher('')(undefined)).toBe(true)
  })

  it('orders sources with Inner first and IPv4 numerically', () => {
    expect(['10.0.0.50', '::1', '10.0.0.3', INNER_SOURCE, '10.0.0.161'].sort(compareSources))
      .toEqual([INNER_SOURCE, '10.0.0.3', '10.0.0.50', '10.0.0.161', '::1'])
  })
})

describe('mihomo connections', () => {
  it('shows memory and records connections that disappear as closed', async () => {
    vi.mocked(mihomoPanelApi.connections)
      .mockResolvedValueOnce(snapshot([connection('a', 'www.example.com', '10.0.0.3'), connection('b', '223.5.5.5', '')]))
      .mockResolvedValueOnce(snapshot([connection('a', 'www.example.com', '10.0.0.3')]))
    const { wrapper, state } = await render()
    expect(wrapper.text()).toContain('125.0 MB')
    expect(wrapper.text()).toContain('活跃 (2)')
    await state.load()
    await flushPromises()
    expect(wrapper.text()).toContain('活跃 (1)')
    expect(wrapper.text()).toContain('已关闭 (1)')
    state.view = 'closed'
    await flushPromises()
    expect(wrapper.text()).toContain('223.5.5.5:443')
    expect(wrapper.text()).toContain(INNER_SOURCE)
    expect(wrapper.text()).not.toContain('www.example.com:443')
  })

  it('filters by source and regex, and limits bulk close to the matches', async () => {
    vi.mocked(mihomoPanelApi.connections).mockResolvedValue(snapshot([
      connection('a', 'www.example.com', '10.0.0.3'), connection('b', 'api.example.com', '10.0.0.50'), connection('c', 'cdn.test', '10.0.0.3')
    ]))
    const { wrapper, state } = await render()
    expect(state.sources).toEqual(['10.0.0.3', '10.0.0.50'])
    state.filters.sources = ['10.0.0.3']
    state.search = 'example\\.com$'
    await flushPromises()
    expect(state.rows.map((row: { id: string }) => row.id)).toEqual(['a'])
    expect(wrapper.text()).toContain('断开筛选结果')
    vi.mocked(confirmDanger).mockResolvedValue(true)
    await state.closeAll()
    expect(mihomoPanelApi.closeConnection).toHaveBeenCalledExactlyOnceWith('gw', 'a')
    expect(mihomoPanelApi.closeAllConnections).not.toHaveBeenCalled()
  })

  it('sorts numeric headers in both directions and remembers only table preferences', async () => {
    vi.mocked(mihomoPanelApi.connections).mockResolvedValue(snapshot([
      { ...connection('small', 'a', '10.0.0.3'), download: 900, upload: 50_000 },
      { ...connection('large', 'z', '10.0.0.50'), download: 12_000, upload: 100 }
    ]))
    const { wrapper, state } = await render()
    const ids = () => wrapper.findAll('[data-connection-id]').map(row => row.attributes('data-connection-id'))
    await wrapper.get('button[aria-label="按下载量排序"]').trigger('click')
    expect(ids()).toEqual(['large', 'small'])
    expect(wrapper.find('th[aria-sort="descending"]').text()).toBe('下载量')
    await wrapper.get('button[aria-label="按下载量排序"]').trigger('click')
    expect(ids()).toEqual(['small', 'large'])
    await wrapper.get('button[aria-label="按上传量排序"]').trigger('click')
    expect(ids()).toEqual(['small', 'large'])
    state.toggleColumn('upload', false)
    state.filters.sources = ['10.0.0.3']
    state.search = 'a'
    await flushPromises()
    const next = await render()
    expect(next.state.settings.sort).toBe('upload')
    expect(next.wrapper.find('button[aria-label="按上传量排序"]').exists()).toBe(false)
    expect(next.state.filtering).toBe(false)
    expect(next.state.rows).toHaveLength(2)
  })

  it('keeps selected filters when a source disappears, and allows clearing an empty result', async () => {
    vi.mocked(mihomoPanelApi.connections).mockResolvedValue(snapshot([connection('a', 'a', '10.0.0.3')]))
    const { wrapper, state } = await render()
    state.filters.sources = ['10.0.0.99']
    await flushPromises()
    expect(wrapper.text()).toContain('没有匹配的连接')
    expect(state.filterGroups[0].options.map((option: { value: string }) => option.value)).toContain('10.0.0.99')
    await state.closeAll()
    expect(confirmDanger).not.toHaveBeenCalled()
    expect(mihomoPanelApi.closeAllConnections).not.toHaveBeenCalled()
    state.clearFilters()
    await flushPromises()
    expect(state.rows).toHaveLength(1)
  })

  it('paginates all results and clamps the page when connections disappear', async () => {
    const connections = Array.from({ length: 305 }, (_, i) => connection(String(i).padStart(3, '0'), 'a', '10.0.0.3'))
    vi.mocked(mihomoPanelApi.connections).mockResolvedValue(snapshot(connections))
    const { wrapper, state } = await render()
    expect(wrapper.findAll('[data-connection-id]')).toHaveLength(100)
    state.page = 4
    await flushPromises()
    expect(wrapper.findAll('[data-connection-id]')).toHaveLength(5)
    expect(wrapper.text()).toContain('301–305')
    vi.mocked(mihomoPanelApi.connections).mockResolvedValue(snapshot(connections.slice(0, 2)))
    await state.load()
    await flushPromises()
    expect(state.page).toBe(1)
    expect(wrapper.findAll('[data-connection-id]')).toHaveLength(2)
  })

  it('freezes all matching active IDs across pages before confirmation and limits close concurrency', async () => {
    const matches = Array.from({ length: 105 }, (_, i) => connection(`match-${i}`, 'match.example', '10.0.0.3'))
    vi.mocked(mihomoPanelApi.connections).mockResolvedValue(snapshot([...matches, connection('other', 'other', '10.0.0.50')]))
    const { state } = await render()
    state.closed = [state.toRow(connection('closed', 'match.example', '10.0.0.3'), Date.now())]
    state.view = 'all'
    state.filters.sources = ['10.0.0.3']
    await flushPromises()
    let approve!: (value: boolean) => void
    vi.mocked(confirmDanger).mockImplementationOnce(() => new Promise(resolve => { approve = resolve }))
    let concurrent = 0, peak = 0
    vi.mocked(mihomoPanelApi.closeConnection).mockImplementation(async () => {
      peak = Math.max(peak, ++concurrent)
      await Promise.resolve()
      concurrent--
      return {} as any
    })
    const pending = state.closeAll()
    expect(confirmDanger).toHaveBeenCalledWith(expect.stringContaining('105 条活动连接'), expect.anything())
    state.clearFilters()
    approve(true)
    await pending
    expect(mihomoPanelApi.closeConnection).toHaveBeenCalledTimes(105)
    expect(vi.mocked(mihomoPanelApi.closeConnection).mock.calls.map(call => call[1]).sort()).toEqual(matches.map(row => row.id).sort())
    expect(peak).toBeLessThanOrEqual(8)
    expect(mihomoPanelApi.closeAllConnections).not.toHaveBeenCalled()
  })

  it('does not close after cancellation or switching agents during confirmation', async () => {
    vi.mocked(mihomoPanelApi.connections).mockResolvedValue(snapshot([connection('a', 'a', '')]))
    const { state, wrapper } = await render()
    vi.mocked(confirmDanger).mockResolvedValueOnce(false)
    await state.closeAll()
    expect(state.closingAll).toBe(false)
    let approve!: (value: boolean) => void
    vi.mocked(confirmDanger).mockImplementationOnce(() => new Promise(resolve => { approve = resolve }))
    const pending = state.closeAll()
    await wrapper.setProps({ agentId: 'other' })
    approve(true)
    await pending
    expect(mihomoPanelApi.closeConnection).not.toHaveBeenCalled()
    expect(mihomoPanelApi.closeAllConnections).not.toHaveBeenCalled()
  })

  it('opens the detail dialog with overview, raw data and proxy chain', async () => {
    vi.mocked(mihomoPanelApi.connections).mockResolvedValue(snapshot([connection('a', 'www.example.com', '10.0.0.3', { remoteDestination: '1.1.1.1' })]))
    const { wrapper } = await render()
    await wrapper.find('button[aria-label="查看 www.example.com:443 的连接详情"]').trigger('click')
    await flushPromises()
    const dialog = document.body.querySelector('[role="dialog"]')
    expect(dialog?.textContent).toContain('连接详情')
    expect(dialog?.textContent).toContain('Redir | tcp')
    expect(dialog?.textContent).toContain('RuleSet: telegram')
    expect(dialog?.textContent).toContain('Proxy → HK')
    expect(dialog?.textContent).toContain('IPv4')
  })
})

describe('mihomo connection summary', () => {
  it('puts counters on the first row and trend metrics on the second', async () => {
    vi.mocked(mihomoPanelApi.connections).mockResolvedValue(snapshot([]))
    const { state } = await render()
    expect(state.summary.map((item: { label: string }) => item.label))
      .toEqual(['连接', '累计上传', '累计下载', '内存使用', '上传速度', '下载速度'])
  })
})
