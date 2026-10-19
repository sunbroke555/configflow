import { afterEach, describe, expect, it, vi } from 'vitest'
import api from '@/api'
import { linuxGatewayApi } from '@/api/linuxGateway'
import { activeProfileId } from '@/profileContext'

vi.mock('@/router', () => ({ default: { push: vi.fn() } }))
const originalAdapter = api.defaults.adapter
afterEach(() => { api.defaults.adapter = originalAdapter })

describe('Linux gateway HTTP boundary', () => {
  it('keeps profile scope in headers/query and sends only the strict network fields to the bridge', async () => {
    const requests: any[] = []
    api.defaults.adapter = async request => {
      requests.push({ ...request, data: typeof request.data === 'string' ? JSON.parse(request.data) : request.data })
      return { data: { success: true }, status: 200, statusText: 'OK', headers: {}, config: request }
    }
    const profileId = activeProfileId.value === 'fixed-space' ? 'other-fixed-space' : 'fixed-space'
    const spec = { interface: 'eth0', address: '192.168.1.2', lan_cidr: '192.168.1.0/24', upstream_gateway: '192.168.1.1' }
    await linuxGatewayApi.get(profileId)
    await linuxGatewayApi.save(profileId, { enabled: true, rule_set_ids: ['selected-id'] })
    await linuxGatewayApi.status('host/name', profileId)
    await linuxGatewayApi.preview('host/name', profileId, spec)
    await linuxGatewayApi.apply('host/name', profileId, spec)
    await linuxGatewayApi.remove('host/name', profileId)
    expect(requests.map(item => item.headers['X-ConfigFlow-Profile'])).toEqual(Array(6).fill(profileId))
    expect(requests[1].data).toEqual({ enabled: true, rule_set_ids: ['selected-id'] })
    expect(requests.slice(2).map(item => item.params)).toEqual(Array(4).fill({ profile_id: profileId }))
    expect(requests[3].url).toBe('/agents/host%2Fname/linux-gateway/preview')
    expect(requests[3].data).toEqual(spec)
    expect(requests[4].data).toEqual(spec)
    expect(requests[5].data).toEqual({})
  })
})
