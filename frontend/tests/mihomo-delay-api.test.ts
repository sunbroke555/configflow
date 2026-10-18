import { afterEach, describe, expect, it, vi } from 'vitest'
import api, { mihomoPanelApi } from '@/api'

vi.mock('@/router', () => ({ default: { push: vi.fn() } }))

afterEach(() => { vi.restoreAllMocks() })

describe('Mihomo single-node delay API', () => {
  it('uses a separate provider endpoint and safely round-trips both names as query parameters', async () => {
    const get = vi.spyOn(api, 'get').mockResolvedValue({ data: { success: true, data: { delay: 21 } } })
    const provider = '订阅 / 🇯🇵 100% ?#&'
    const name = '🇯🇵 JP-05 · AWS2 / %2F #?&'
    const url = 'https://example.test/generate_204?a=hello%20world&b=2'
    await mihomoPanelApi.providerProxyDelay('gateway / 1', provider, name, url, 7000)
    expect(get).toHaveBeenCalledWith('/agents/gateway%20%2F%201/mihomo/providers/proxy/delay', {
      params: { provider, name, url, timeout: 7000 }, timeout: 45000
    })
    const [path, config] = get.mock.calls[0]
    const request = new URL(api.getUri({ ...config, url: path }), 'https://configflow.test')
    expect(request.searchParams.get('provider')).toBe(provider)
    expect(request.searchParams.get('name')).toBe(name)
    expect(request.searchParams.get('url')).toBe(url)
    expect(request.searchParams.get('timeout')).toBe('7000')
  })

  it('keeps the normal endpoint, defaults and result shape unchanged', async () => {
    const result = { data: { success: true, data: { delay: 42 } } }
    const get = vi.spyOn(api, 'get').mockResolvedValue(result)
    expect(await mihomoPanelApi.proxyDelay('gw', 'DIRECT')).toBe(result)
    expect(get).toHaveBeenLastCalledWith('/agents/gw/mihomo/proxy/delay', {
      params: { name: 'DIRECT', url: undefined, timeout: 5000 }, timeout: 45000
    })
    expect(await mihomoPanelApi.providerProxyDelay('gw', 'default', 'node')).toBe(result)
    expect(get).toHaveBeenLastCalledWith('/agents/gw/mihomo/providers/proxy/delay', {
      params: { provider: 'default', name: 'node', url: undefined, timeout: 5000 }, timeout: 45000
    })
  })
})
