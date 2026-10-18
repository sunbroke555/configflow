import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import Nodes from '@/views/Nodes.vue'
import api, { nodeApi } from '@/api'
import { notify } from '@/lib/feedback'

vi.mock('@/api', () => ({
  default: { get: vi.fn(), post: vi.fn() },
  nodeApi: { getAll: vi.fn(), latency: vi.fn(), testLatency: vi.fn() },
  profileApi: { list: vi.fn() }
}))

const storedNodes = [
  { id: 'manual-hk', name: '香港 手动', enabled: true, proxy_string: 'trojan://password@192.0.2.1:443' },
  { id: 'manual-jp', name: '日本 手动', enabled: false, proxy_string: 'ss://password@192.0.2.2:443', subscription_id: '' },
  { id: 'imported', name: '香港 订阅导入', enabled: true, proxy_string: 'trojan://password@192.0.2.3:443', subscription_id: 'sub-one', subscription_name: '测试订阅' }
]
const previewNodes = [
  { name: '香港 订阅导入', type: 'trojan', source_type: 'subscription', source_id: 'sub-one', subscription_name: '测试订阅' },
  { name: '日本 订阅预览', type: 'ss', server: '192.0.2.4', port: 443, source_type: 'subscription', source_id: 'sub-one', subscription_name: '测试订阅' }
]
const matchMediaDescriptor = Object.getOwnPropertyDescriptor(window, 'matchMedia')
let wrapper: VueWrapper | undefined

beforeAll(() => {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
  })
})
afterAll(() => {
  if (matchMediaDescriptor) Object.defineProperty(window, 'matchMedia', matchMediaDescriptor)
  else delete (window as any).matchMedia
})
beforeEach(() => {
  vi.resetAllMocks()
  localStorage.clear()
  vi.mocked(nodeApi.getAll).mockResolvedValue({ data: storedNodes.map(node => ({ ...node })) } as any)
  vi.mocked(nodeApi.latency).mockResolvedValue({ data: { results: {} } } as any)
  vi.mocked(api.get).mockResolvedValue({ data: [{ id: 'sub-one', enabled: true }] } as any)
  vi.mocked(api.post).mockResolvedValue({ data: { nodes: previewNodes, imported_node_ids: ['imported'] } } as any)
})
afterEach(() => {
  wrapper?.unmount()
  wrapper = undefined
  vi.restoreAllMocks()
  localStorage.clear()
  document.body.innerHTML = ''
})

async function render(view: 'card' | 'list') {
  localStorage.setItem('configflow-nodes-view', view)
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: Nodes }] })
  await router.push('/')
  await router.isReady()
  wrapper = mount(Nodes, { attachTo: document.body, global: { plugins: [router] } })
  await flushPromises()
}

async function click(label: string, group?: string) {
  const root = group ? wrapper!.get(`[aria-label="${group}"]`) : wrapper!
  const button = root.findAll('button').find(button => button.text() === label)
  expect(button, label).toBeDefined()
  await button!.trigger('click')
  await flushPromises()
}

function names() {
  return wrapper!.findAll('[data-name]').map(node => node.attributes('data-name'))
}

describe.each(['card', 'list'] as const)('%s 来源筛选', view => {
  it('手动节点仅显示无订阅来源的节点，订阅节点包含导入和预览节点', async () => {
    await render(view)
    expect(names()).toEqual(['香港 手动', '日本 手动', '香港 订阅导入', '日本 订阅预览'])
    expect(wrapper!.get('[aria-label="按来源筛选"]').text()).not.toContain('节点库')

    await click('手动节点')
    expect(names()).toEqual(['香港 手动', '日本 手动'])
    expect(wrapper!.get('[aria-label="按来源筛选"] button[aria-pressed="true"]').text()).toBe('手动节点')
    await click('全选')
    expect(wrapper!.text()).toContain('删除 2 项')

    await click('订阅节点')
    expect(names()).toEqual(['香港 订阅导入', '日本 订阅预览'])
    await click('全部来源')
    expect(names()).toHaveLength(4)
  })

  it('手动来源可叠加地区、协议和关键词筛选', async () => {
    await render(view)
    await click('手动节点')
    await click('香港', '按地区筛选')
    expect(names()).toEqual(['香港 手动'])
    await click('SS', '按协议筛选')
    expect(names()).toEqual([])
    expect(wrapper!.text()).toContain('没有匹配的节点')
    await click('SS', '按协议筛选')
    await click('全部', '按地区筛选')
    await wrapper!.get('input[placeholder="搜索名称、地址或备注…"]').setValue('日本')
    expect(names()).toEqual(['日本 手动'])
  })

  it('没有额外订阅预览时仍能排除库内的订阅导入节点', async () => {
    vi.mocked(api.post).mockResolvedValue({ data: { nodes: [], imported_node_ids: ['imported'] } } as any)
    await render(view)
    await click('手动节点')
    expect(names()).toEqual(['香港 手动', '日本 手动'])
    await click('订阅节点')
    expect(names()).toEqual(['香港 订阅导入'])
  })

  it('手动节点同名不会遮蔽订阅节点，不同订阅同名也均保留', async () => {
    vi.mocked(api.get).mockResolvedValue({ data: [{ id: 'sub-one' }, { id: 'sub-two' }] } as any)
    vi.mocked(api.post).mockResolvedValue({ data: {
      imported_node_ids: ['imported'],
      nodes: [
        ...previewNodes,
        { name: '香港 手动', type: 'trojan', source_type: 'subscription', source_id: 'sub-one', source_name: '测试订阅' },
        { name: '香港 订阅导入', type: 'trojan', source_type: 'subscription', source_id: 'sub-two', source_name: '另一订阅' }
      ]
    } } as any)
    await render(view)
    expect(names()).toEqual(['香港 手动', '日本 手动', '香港 订阅导入', '日本 订阅预览', '香港 手动', '香港 订阅导入'])
    await click('订阅节点')
    expect(names()).toEqual(['香港 订阅导入', '日本 订阅预览', '香港 手动', '香港 订阅导入'])
    expect(wrapper!.text()).toContain('另一订阅')
    await click('手动节点')
    expect(names()).toEqual(['香港 手动', '日本 手动'])
    expect(api.post).toHaveBeenCalledWith('/proxy-groups/preview-regex', expect.objectContaining({ deduplicate_by_source: true }))
  })

  it('订阅筛选排除后端正则未通过的导入节点，全部来源仍可管理它们', async () => {
    // 后端使用 Python 正则，此处只模拟最终筛选结果，不在浏览器重复解释正则。
    vi.mocked(api.get).mockResolvedValue({ data: [{ id: 'sub-one', regex_filter: '(?i)日本' }] } as any)
    vi.mocked(api.post).mockResolvedValue({ data: { imported_node_ids: [], nodes: [previewNodes[1]] } } as any)
    await render(view)
    await click('订阅节点')
    expect(names()).toEqual(['日本 订阅预览'])
    await click('全部来源')
    expect(names()).toContain('香港 订阅导入')
  })

  it('禁用或已删除订阅的导入节点不出现在订阅筛选中', async () => {
    vi.mocked(api.get).mockResolvedValue({ data: [{ id: 'sub-one', enabled: false }] } as any)
    await render(view)
    expect(api.post).not.toHaveBeenCalled()
    await click('订阅节点')
    expect(names()).toEqual([])
    await click('全部来源')
    expect(names()).toEqual(storedNodes.map(node => node.name))
  })

  it('订阅加载失败时提示错误，不将未验证的导入节点当作合格结果', async () => {
    const error = vi.spyOn(notify, 'error').mockImplementation(() => 0)
    vi.mocked(api.post).mockRejectedValue(new Error('network error'))
    await render(view)
    expect(error).toHaveBeenCalledWith('加载订阅节点失败')
    await click('订阅节点')
    expect(names()).toEqual([])
    await click('全部来源')
    expect(names()).toEqual(storedNodes.map(node => node.name))
  })
})

it('表格中的订阅预览节点保持只读，不能编辑、删除或选中', async () => {
  await render('list')
  const row = wrapper!.get('[data-name="日本 订阅预览"]')
  expect(row.find('[role="checkbox"]').exists()).toBe(false)
  expect(row.find('button[aria-label="编辑 日本 订阅预览"]').exists()).toBe(false)
  expect(row.find('button[aria-label="删除 日本 订阅预览"]').exists()).toBe(false)
  expect(row.find('button[aria-label="单独测速 日本 订阅预览"]').exists()).toBe(true)
  expect(row.text()).toContain('测试订阅')
})

it('代理测速分批展示真实进度，失败保留原因，已停用节点不测试', async () => {
  const records = Array.from({ length: 7 }, (_, i) => ({
    id: `node-${i}`, name: `node-${i}`, enabled: i !== 6, proxy_string: 'trojan://secret@example.invalid:443'
  }))
  vi.mocked(nodeApi.getAll).mockResolvedValue({ data: records } as any)
  vi.mocked(api.get).mockResolvedValue({ data: [] } as any)
  let finishFirst!: (value: any) => void
  let finishSecond!: (value: any) => void
  vi.mocked(nodeApi.testLatency)
    .mockReturnValueOnce(new Promise(resolve => { finishFirst = resolve }))
    .mockReturnValueOnce(new Promise(resolve => { finishSecond = resolve }))
  await render('card')
  await click('全部测速')
  expect(nodeApi.testLatency).toHaveBeenNthCalledWith(1, ['node-0', 'node-1', 'node-2', 'node-3', 'node-4'])
  expect(wrapper!.text()).toContain('正在测速… 0/6')
  const results = Object.fromEntries(records.slice(0, 5).map(node => [node.name, { latency: 150 }]))
  finishFirst({ data: { results, missing: [] } })
  await flushPromises()
  expect(wrapper!.text()).toContain('正在测速… 5/6')
  expect(wrapper!.get('[data-name="node-0"]').text()).toContain('150')
  expect(nodeApi.testLatency).toHaveBeenNthCalledWith(2, ['node-5'])
  finishSecond({ data: { results: { 'node-5': { latency: null, error_code: 'auth', error: '代理认证失败，请检查节点凭据' } } } })
  await flushPromises()
  const failed = wrapper!.get('[data-name="node-5"]').text()
  expect(failed).toContain('代理认证失败')
  expect(failed).not.toContain('超时')
  expect(wrapper!.text()).toContain('全部测速')
})

it('组件不可用时提示原因，不将现有结果覆盖为超时', async () => {
  const error = vi.spyOn(notify, 'error').mockImplementation(() => 0)
  vi.mocked(nodeApi.latency).mockResolvedValue({ data: { results: { '香港 手动': { latency: 123 } } } } as any)
  vi.mocked(nodeApi.testLatency).mockRejectedValue({ response: { data: { message: '独立测速组件未安装' } } })
  await render('list')
  await wrapper!.get('button[aria-label="单独测速 香港 手动"]').trigger('click')
  await flushPromises()
  expect(error).toHaveBeenCalledWith('独立测速组件未安装', undefined)
  expect(wrapper!.get('[data-name="香港 手动"]').text()).toContain('123')
})

it('离开页面后不再提交后续测速批次', async () => {
  vi.mocked(nodeApi.getAll).mockResolvedValue({ data: Array.from({ length: 6 }, (_, i) => ({
    id: `n${i}`, name: `n${i}`, enabled: true
  })) } as any)
  vi.mocked(api.get).mockResolvedValue({ data: [] } as any)
  let finish!: (value: any) => void
  vi.mocked(nodeApi.testLatency).mockReturnValue(new Promise(resolve => { finish = resolve }))
  await render('list')
  await click('全部测速')
  wrapper!.unmount()
  wrapper = undefined
  finish({ data: { results: {} } })
  await flushPromises()
  expect(nodeApi.testLatency).toHaveBeenCalledTimes(1)
})
