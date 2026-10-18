import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import MihomoConnections from '@/components/mihomo/MihomoConnections.vue'
import { mihomoPanelApi } from '@/api'
import { compareSources, connectionMatcher, INNER_SOURCE } from '@/types/mihomo'

vi.mock('@/api', () => ({ mihomoPanelApi: { connections: vi.fn(), closeConnection: vi.fn(), closeAllConnections: vi.fn() } }))
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
beforeEach(() => { vi.clearAllMocks() })
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
    state.source = '10.0.0.3'
    state.search = 'example\\.com$'
    await flushPromises()
    expect(state.rows.map((row: { id: string }) => row.id)).toEqual(['a'])
    expect(wrapper.text()).toContain('断开筛选结果')
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
