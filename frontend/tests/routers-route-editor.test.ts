import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DOMWrapper, flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import Routers from '@/views/Routers.vue'
import { defineComponent, h } from 'vue'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import { canReloadPage } from '@/lib/pageReloadGuard'
import { agentApi, routerApi, ruleApi } from '@/api'
import { confirm, confirmDanger } from '@/lib/feedback'
import { activeProfileId } from '@/profileContext'

vi.mock('@/api', () => ({
  agentApi: { getAll: vi.fn() }, ruleApi: { getAll: vi.fn() },
  routerApi: { list: vi.fn(), create: vi.fn(), update: vi.fn(), remove: vi.fn(), probe: vi.fn(), report: vi.fn(),
    plan: vi.fn(), apply: vi.fn(), routes: vi.fn(), routePlan: vi.fn(), routeApply: vi.fn() }
}))
vi.mock('@/lib/feedback', () => ({ notify: { success: vi.fn(), error: vi.fn() },
  confirm: vi.fn(), confirmDanger: vi.fn(), confirmState: { resolve: null }, settleConfirm: vi.fn() }))

const ROUTER = { id: 'r1', name: '主路由', type: 'routeros', endpoint: 'https://192.0.2.1', proxy_gateway_agent_id: '', dns_agent_id: '' }
const ROUTE = { id: '*1', cidr: '192.168.20.0/24', gateway: '192.0.2.2', enabled: true, comment: '原备注',
  table: 'main', editable: true, reason: '', managed: false, revision: 'revision1' }
const VALUES = { cidr: '192.168.30.0/24', gateway: '192.0.2.3', enabled: false, comment: '测试\n第二行' }
const preview = (before: any = null, after: any = VALUES) => ({ data: { success: true, plan: {
  action: before ? after ? 'update' : 'delete' : 'add', before, after, summary: '路由变更预览',
  warnings: ['带 cf 标记的路由可能被后续自动发布调整'], fingerprint: 'fingerprint1', empty: false
} } })
const deferred = () => {
  let resolve!: (value: any) => void
  let reject!: (error: any) => void
  const promise = new Promise<any>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}
const button = (label: string) => Array.from(document.body.querySelectorAll('button')).find(item => item.textContent?.trim() === label)!
const field = (id: string) => new DOMWrapper(document.getElementById(id) as HTMLInputElement)
let wrapper: VueWrapper | null = null
const openManager = async () => {
  wrapper = mount(Routers, { attachTo: document.body })
  await flushPromises()
  button('静态路由').click()
  await flushPromises()
  return wrapper
}
const fillAdd = async () => {
  button('新增路由').click()
  await flushPromises()
  await field('static-route-cidr').setValue(VALUES.cidr)
  await field('static-route-gateway').setValue(VALUES.gateway)
  await field('static-route-comment').setValue(VALUES.comment)
  document.getElementById('static-route-enabled')!.click()
  await flushPromises()
}
beforeEach(() => {
  vi.resetAllMocks()
  activeProfileId.value = 'profile-a'
  vi.mocked(routerApi.list).mockResolvedValue({ data: { routers: [ROUTER] } } as any)
  vi.mocked(agentApi.getAll).mockResolvedValue({ data: [] } as any)
  vi.mocked(ruleApi.getAll).mockResolvedValue({ data: [] } as any)
  vi.mocked(routerApi.routes).mockResolvedValue({ data: { success: true, routes: [ROUTE] } } as any)
  vi.mocked(routerApi.routePlan).mockResolvedValue(preview() as any)
  vi.mocked(routerApi.routeApply).mockResolvedValue({ data: { success: true, result: { changed: true } } } as any)
  vi.mocked(confirm).mockResolvedValue(true)
  vi.mocked(confirmDanger).mockResolvedValue(true)
})
afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  activeProfileId.value = 'default'
})

describe('independent static route management', () => {
  it('opens without a report or working Agent service and explains read-only routes', async () => {
    vi.mocked(agentApi.getAll).mockRejectedValue(new Error('offline'))
    vi.mocked(routerApi.routes).mockResolvedValue({ data: { success: true, routes: [ROUTE,
      { ...ROUTE, id: '*2', cidr: '0.0.0.0/0', editable: false, reason: '动态路由由 DHCP 客户端维护' }] } } as any)
    await openManager()
    expect(routerApi.routes).toHaveBeenCalledWith('r1', 'profile-a')
    expect(routerApi.report).not.toHaveBeenCalled()
    expect(document.body.textContent).toContain('只读：动态路由由 DHCP 客户端维护')
    expect(document.querySelector<HTMLButtonElement>('[aria-label="编辑路由 0.0.0.0/0"]')!.disabled).toBe(true)
    expect(document.querySelector<HTMLButtonElement>('[aria-label="删除路由 0.0.0.0/0"]')!.disabled).toBe(true)
  })

  it('shows read failures, retries and allows creating the first route', async () => {
    vi.mocked(routerApi.routes).mockRejectedValueOnce({ response: { data: { message: '路由器连接失败' } } })
      .mockResolvedValue({ data: { success: true, routes: [] } } as any)
    await openManager()
    expect(document.body.textContent).toContain('路由器连接失败')
    expect(button('新增路由').disabled).toBe(true)
    button('刷新路由').click()
    await flushPromises()
    expect(document.body.textContent).toContain('没有静态路由')
    expect(button('新增路由').disabled).toBe(false)
  })

  it('previews full values and warnings before add, rejects double submits and refreshes after applying', async () => {
    const pending = deferred()
    vi.mocked(routerApi.routePlan).mockReturnValue(pending.promise)
    await openManager()
    await fillAdd()
    button('预览路由变更').click()
    button('预览路由变更').click()
    await flushPromises()
    expect(routerApi.routePlan).toHaveBeenCalledTimes(1)
    expect(routerApi.routeApply).not.toHaveBeenCalled()
    pending.resolve(preview())
    await flushPromises()
    expect(routerApi.routePlan).toHaveBeenCalledWith('r1', { action: 'add', values: VALUES }, 'profile-a')
    const text = vi.mocked(confirm).mock.calls[0][0]
    for (const value of [VALUES.cidr, VALUES.gateway, '停用', VALUES.comment, '修改前\n无', 'cf 标记']) expect(text).toContain(value)
    expect(routerApi.routeApply).toHaveBeenCalledWith('r1', { action: 'add', values: VALUES, fingerprint: 'fingerprint1' }, 'profile-a')
    expect(routerApi.routes).toHaveBeenCalledTimes(2)
    expect(document.getElementById('static-route-cidr')).toBeNull()
    expect(routerApi.apply).not.toHaveBeenCalled()
  })

  it('uses revision and exact original values for edit confirmation; cancellation writes nothing', async () => {
    vi.mocked(routerApi.routePlan).mockResolvedValue(preview(ROUTE, VALUES) as any)
    vi.mocked(confirm).mockResolvedValue(false)
    await openManager()
    document.querySelector<HTMLButtonElement>('[aria-label="编辑路由 192.168.20.0/24"]')!.click()
    await flushPromises()
    expect((document.getElementById('static-route-comment') as HTMLTextAreaElement).value).toBe('原备注')
    await field('static-route-cidr').setValue(VALUES.cidr)
    await field('static-route-gateway').setValue(VALUES.gateway)
    await field('static-route-comment').setValue(VALUES.comment)
    document.getElementById('static-route-enabled')!.click()
    button('预览路由变更').click()
    await flushPromises()
    expect(routerApi.routePlan).toHaveBeenCalledWith('r1', { action: 'update', id: '*1', revision: 'revision1', values: VALUES }, 'profile-a')
    expect(vi.mocked(confirm).mock.calls[0][0]).toContain('备注：原备注')
    expect(routerApi.routeApply).not.toHaveBeenCalled()
  })

  it('previews deletion and requires the destructive confirmation', async () => {
    vi.mocked(routerApi.routePlan).mockResolvedValue(preview(ROUTE, null) as any)
    await openManager()
    document.querySelector<HTMLButtonElement>('[aria-label="删除路由 192.168.20.0/24"]')!.click()
    await flushPromises()
    expect(confirmDanger).toHaveBeenCalledWith(expect.stringContaining('修改后\n无'), expect.objectContaining({ confirmText: '确认删除路由' }))
    expect(routerApi.routeApply).toHaveBeenCalledWith('r1', { action: 'delete', id: '*1', revision: 'revision1', fingerprint: 'fingerprint1' }, 'profile-a')
  })

  it('refreshes a conflicting revision and requires another preview without losing the edit', async () => {
    vi.mocked(routerApi.routePlan).mockResolvedValue(preview(ROUTE, { ...ROUTE, comment: '我的修改' }) as any)
    vi.mocked(routerApi.routeApply).mockRejectedValueOnce({ response: { status: 409, data: { message: '路由已被修改' } } })
    vi.mocked(routerApi.routes).mockResolvedValueOnce({ data: { success: true, routes: [ROUTE] } } as any)
      .mockResolvedValue({ data: { success: true, routes: [{ ...ROUTE, comment: '外部修改', revision: 'revision2' }] } } as any)
    await openManager()
    document.querySelector<HTMLButtonElement>('[aria-label="编辑路由 192.168.20.0/24"]')!.click()
    await flushPromises()
    await field('static-route-comment').setValue('我的修改')
    button('预览路由变更').click()
    await flushPromises()
    expect(routerApi.routePlan).toHaveBeenCalledTimes(1)
    expect(document.body.textContent).toContain('请核对后重新预览')
    expect((document.getElementById('static-route-comment') as HTMLTextAreaElement).value).toBe('我的修改')
    button('预览路由变更').click()
    await flushPromises()
    expect(routerApi.routePlan).toHaveBeenLastCalledWith('r1', expect.objectContaining({ revision: 'revision2', values: expect.objectContaining({ comment: '我的修改' }) }), 'profile-a')
    expect(routerApi.routeApply).toHaveBeenCalledTimes(2)
  })

  it('ignores an old device list after switching routers', async () => {
    const pending = deferred()
    vi.mocked(routerApi.list).mockResolvedValue({ data: { routers: [ROUTER, { ...ROUTER, id: 'r2', name: '备用主路由' }] } } as any)
    vi.mocked(routerApi.routes).mockReturnValueOnce(pending.promise)
      .mockResolvedValue({ data: { success: true, routes: [{ ...ROUTE, cidr: '10.0.0.0/8' }] } } as any)
    await openManager()
    wrapper!.findAll('button').filter(item => item.text() === '静态路由')[1].element.click()
    await flushPromises()
    pending.resolve({ data: { success: true, routes: [ROUTE] } })
    await flushPromises()
    expect(document.body.textContent).toContain('静态路由 · 备用主路由')
    expect(document.body.textContent).toContain('10.0.0.0/8')
    expect(document.body.textContent).not.toContain('192.168.20.0/24')
  })

  it('discards a pending preview after changing profiles', async () => {
    const pending = deferred()
    vi.mocked(routerApi.routePlan).mockReturnValue(pending.promise)
    await openManager()
    await fillAdd()
    button('预览路由变更').click()
    await flushPromises()
    activeProfileId.value = 'profile-b'
    pending.resolve(preview())
    await flushPromises()
    expect(document.body.textContent).not.toContain('静态路由 ·')
    expect(confirm).not.toHaveBeenCalled()
    expect(routerApi.routeApply).not.toHaveBeenCalled()
  })

  it('cannot apply an old confirmation after the page is unmounted', async () => {
    const pending = deferred()
    vi.mocked(confirm).mockReturnValue(pending.promise)
    await openManager()
    await fillAdd()
    button('预览路由变更').click()
    await flushPromises()
    expect(confirm).toHaveBeenCalledOnce()
    // The edit modal closes before the global confirmation opens, so it cannot cover it.
    expect(document.getElementById('static-route-cidr')).toBeNull()
    wrapper!.unmount()
    wrapper = null
    pending.resolve(true)
    await flushPromises()
    expect(routerApi.routeApply).not.toHaveBeenCalled()
  })

  it('validates IPv4 and preserves an overlong existing comment for correction', async () => {
    vi.mocked(routerApi.routes).mockResolvedValue({ data: { success: true, routes: [{ ...ROUTE, comment: '字'.repeat(513) }] } } as any)
    await openManager()
    document.querySelector<HTMLButtonElement>('[aria-label="编辑路由 192.168.20.0/24"]')!.click()
    await flushPromises()
    expect((document.getElementById('static-route-comment') as HTMLTextAreaElement).value).toHaveLength(513)
    button('预览路由变更').click()
    await flushPromises()
    expect(document.body.textContent).toContain('备注不能超过 512 个字符')
    await field('static-route-comment').setValue('已修正')
    await field('static-route-gateway').setValue('192.0.2.999')
    button('预览路由变更').click()
    await flushPromises()
    expect(document.body.textContent).toContain('下一跳必须是 IPv4 地址')
    expect(routerApi.routePlan).not.toHaveBeenCalled()
  })
})


describe('static route automatic reload protection', () => {
  it('keeps the request and subsequent route refresh protected after confirmation closes', async () => {
    const pending = deferred(), refresh = deferred()
    vi.mocked(routerApi.routeApply).mockReturnValueOnce(pending.promise)
    await openManager()
    await fillAdd()
    vi.mocked(routerApi.routes).mockReturnValueOnce(refresh.promise)
    button('预览路由变更').click()
    await flushPromises()
    try {
      expect(routerApi.routeApply).toHaveBeenCalledOnce()
      expect(document.querySelector('[role="dialog"][data-state="open"]')).toBeNull()
      expect(canReloadPage()).toBe(false)
      pending.resolve({ data: { success: true, result: { changed: true } } })
      await flushPromises()
      expect(routerApi.routes).toHaveBeenCalledTimes(2)
      expect(canReloadPage()).toBe(false)
      refresh.resolve({ data: { success: true, routes: [ROUTE] } })
      await flushPromises()
      expect(canReloadPage()).toBe(true)
      expect(routerApi.routeApply).toHaveBeenCalledOnce()
    } finally {
      pending.resolve({ data: { success: true, result: { changed: true } } })
      refresh.resolve({ data: { success: true, routes: [ROUTE] } })
      await flushPromises()
    }
  })

  it.each([500, 409])('retains edits after HTTP %i and releases protection after the editor is closed', async status => {
    const pending = deferred(), refresh = deferred()
    vi.mocked(routerApi.routeApply).mockReturnValueOnce(pending.promise)
    await openManager()
    await fillAdd()
    if (status === 409) vi.mocked(routerApi.routes).mockReturnValueOnce(refresh.promise)
    button('预览路由变更').click()
    await flushPromises()
    try {
      expect(canReloadPage()).toBe(false)
      pending.reject({ response: { status, data: { message: status === 409 ? '路由已被修改' : '写入失败' } } })
      await flushPromises()
      if (status === 409) {
        expect(canReloadPage()).toBe(false)
        refresh.resolve({ data: { success: true, routes: [ROUTE] } })
        await flushPromises()
      }
      expect((document.getElementById('static-route-comment') as HTMLTextAreaElement).value).toBe(VALUES.comment)
      expect(canReloadPage()).toBe(false)
      button('取消').click()
      await flushPromises()
      expect(canReloadPage()).toBe(true)
      expect(routerApi.routeApply).toHaveBeenCalledOnce()
    } finally {
      pending.resolve({ data: { success: true, result: { changed: true } } })
      refresh.resolve({ data: { success: true, routes: [ROUTE] } })
      await flushPromises()
    }
  })

  it.each(['success', 'failure', 'conflict'])('allows normal navigation but keeps an in-flight write protected across unmount (%s)', async outcome => {
    const pending = deferred()
    vi.mocked(routerApi.routeApply).mockReturnValueOnce(pending.promise)
    const router = createRouter({ history: createMemoryHistory(), routes: [
      { path: '/routers', component: Routers },
      { path: '/other', component: { render: () => h('p', 'Other page') } },
    ] })
    await router.push('/routers')
    wrapper = mount(defineComponent({ render: () => h(RouterView) }), { attachTo: document.body, global: { plugins: [router] } })
    await flushPromises()
    button('静态路由').click()
    await flushPromises()
    await fillAdd()
    button('预览路由变更').click()
    await flushPromises()
    try {
      expect(routerApi.routeApply).toHaveBeenCalledOnce()
      await router.push('/other')
      await flushPromises()
      expect(router.currentRoute.value.path).toBe('/other')
      expect(wrapper.text()).toContain('Other page')
      expect(document.querySelector('[role="dialog"][data-state="open"]')).toBeNull()
      expect(canReloadPage()).toBe(false)
      if (outcome === 'success') pending.resolve({ data: { success: true, result: { changed: true } } })
      else pending.reject({ response: { status: outcome === 'conflict' ? 409 : 500, data: { message: '写入未完成' } } })
      await flushPromises()
      expect(canReloadPage()).toBe(true)
      expect(routerApi.routeApply).toHaveBeenCalledOnce()
      expect(routerApi.routes).toHaveBeenCalledOnce()
    } finally {
      pending.resolve({ data: { success: true, result: { changed: true } } })
      await flushPromises()
    }
  })
})
