import { afterEach, describe, expect, it, vi } from 'vitest'
import api from '@/api'
import { linuxGatewayApi } from '@/api/linuxGateway'
import { activeProfileId } from '@/profileContext'

vi.mock('@/router', () => ({ default: { push: vi.fn() } }))
const originalAdapter = api.defaults.adapter
afterEach(() => { api.defaults.adapter = originalAdapter })

describe('Linux gateway HTTP boundary', () => {
  it('scopes draft previews and guarded saves/restores separately from host operations', async () => {
    const requests: any[] = []
    api.defaults.adapter = async config => {
      requests.push({ ...config, data: JSON.parse(config.data) })
      return { data: { success: true }, status: 200, statusText: 'OK', headers: {}, config }
    }
    const settings = { enabled: true, rule_set_ids: ['r'] }
    const guard = { expected_config_revision: 10, preview_token: 'preview' }
    await linuxGatewayApi.previewDraft('space', settings, 10)
    await linuxGatewayApi.save('space', settings, guard)
    await linuxGatewayApi.previewRestore('space', 10)
    await linuxGatewayApi.restoreDraft('space', guard)
    expect(requests.map(item => item.headers['X-ConfigFlow-Profile'])).toEqual(Array(4).fill('space'))
    expect(requests.map(item => item.url)).toEqual([
      '/custom-config/linux-gateway/preview', '/custom-config/linux-gateway',
      '/custom-config/linux-gateway/restore/preview', '/custom-config/linux-gateway/restore',
    ])
    expect(requests.map(item => item.data)).toEqual([
      { settings, expected_config_revision: 10 }, { settings, ...guard }, { expected_config_revision: 10 }, guard,
    ])
  })
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
  it('persists forwarding and confirms a legacy teardown only through explicit bodies', async () => {
    const requests: any[] = []
    api.defaults.adapter = async request => {
      requests.push({ ...request, data: typeof request.data === 'string' ? JSON.parse(request.data) : request.data })
      return { data: { success: true }, status: 200, statusText: 'OK', headers: {}, config: request }
    }
    await linuxGatewayApi.persistForwarding('host/name', 'space')
    await linuxGatewayApi.remove('host/name', 'space', true)
    await linuxGatewayApi.remove('host/name', 'space')
    expect(requests.map(item => [item.method, item.url])).toEqual([
      ['post', '/agents/host%2Fname/linux-gateway/forwarding'], ['post', '/agents/host%2Fname/linux-gateway/remove'], ['post', '/agents/host%2Fname/linux-gateway/remove'],
    ])
    expect(requests.map(item => item.data)).toEqual([{}, { confirm_router_split_teardown: true }, {}])
    expect(requests.map(item => item.headers['X-ConfigFlow-Profile'])).toEqual(Array(3).fill('space'))
    expect(requests.map(item => item.params)).toEqual(Array(3).fill({ profile_id: 'space' }))
  })
})
