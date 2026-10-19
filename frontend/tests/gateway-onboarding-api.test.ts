import { afterEach, describe, expect, it, vi } from 'vitest'
import api from '@/api'
import { gatewayOnboardingApi } from '@/api/gatewayOnboarding'
vi.mock('@/router', () => ({ default: { push: vi.fn() } }))
const originalAdapter = api.defaults.adapter
afterEach(() => { api.defaults.adapter = originalAdapter })
describe('gateway onboarding request contract', () => {
  it('pins all configuration and installation requests to the entry profile', async () => {
    const requests: any[] = []
    api.defaults.adapter = async config => {
      requests.push({ ...config, data: typeof config.data === 'string' ? JSON.parse(config.data) : config.data })
      return { data: {}, status: 200, statusText: 'OK', headers: {}, config }
    }
    const settings = { rule_set_ids: ['ip'], direct_rule_set_ids: ['cn'], proxy_rule_set_ids: ['proxy'], local_dns: 'https://223.5.5.5/dns-query', expected_config_revision: 7 }
    const input = { profile_id: 'fixed/space', name: 'home', server_url: 'http://192.168.1.3:8000', network: { interface: 'eth0', address: '192.168.1.2', lan_cidr: '192.168.1.0/24', upstream_gateway: '192.168.1.1' }, config_revision: 8 }
    await gatewayOnboardingApi.get(input.profile_id)
    await gatewayOnboardingApi.preview(input.profile_id, settings)
    await gatewayOnboardingApi.save(input.profile_id, settings)
    await gatewayOnboardingApi.create(input)
    await gatewayOnboardingApi.sessions(input.profile_id)
    await gatewayOnboardingApi.session('session/id', input.profile_id)
    await gatewayOnboardingApi.publish('session/id', input.profile_id)
    expect(requests.map(item => item.headers['X-ConfigFlow-Profile'])).toEqual(Array(7).fill(input.profile_id))
    expect(requests.map(item => item.url)).toEqual([
      '/profiles/fixed%2Fspace/gateway-onboarding', '/profiles/fixed%2Fspace/gateway-onboarding/preview', '/profiles/fixed%2Fspace/gateway-onboarding',
      '/agents/gateway-installations', '/agents/gateway-installations', '/agents/gateway-installations/session%2Fid', '/agents/gateway-installations/session%2Fid/publish',
    ])
    expect(requests[1].data).toEqual(settings)
    expect(requests[2].data).toEqual(settings)
    expect(requests[3].data).toEqual(input)
    expect(requests[4].params).toEqual({ profile_id: input.profile_id })
    expect(requests[6].data).toEqual({ retry_failed: false })
  })
})
