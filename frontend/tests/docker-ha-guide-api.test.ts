import { afterEach, expect, it, vi } from 'vitest'
import api, { agentApi } from '@/api'
import { getActiveProfileId, setActiveProfileId } from '@/profileContext'
vi.mock('@/router', () => ({ default: { push: vi.fn() } }))
const originalAdapter = api.defaults.adapter
const originalProfile = getActiveProfileId()
afterEach(() => { api.defaults.adapter = originalAdapter; setActiveProfileId(originalProfile) })

it('pins guide enrollment to the installation profile even after the global profile changes', async () => {
  const requests: any[] = []
  api.defaults.adapter = async config => {
    requests.push({ ...config, data: typeof config.data === 'string' ? JSON.parse(config.data) : config.data })
    return { data: { success: true }, status: 200, statusText: 'OK', headers: {}, config }
  }
  setActiveProfileId('unrelated-global-space')
  await agentApi.generateDockerHAGuide({ action: 'generate', mode: 'new', agent_name: 'dns-a', image: 'registry.example/agent:ha' }, 'installation-space')
  expect(requests[0].url).toBe('/agents/docker-oxidns-ha-guide')
  expect(requests[0].headers['X-ConfigFlow-Profile']).toBe('installation-space')
  expect(requests[0].data.image).toBe('registry.example/agent:ha')
  await agentApi.getDockerHAEnvironment('agent/id')
  expect(requests[1].url).toBe('/agents/agent%2Fid/oxidns-ha-environment')
  expect(requests[1].timeout).toBe(60000)
})


it('posts pasted preflight output and selected network under the fixed installation profile', async () => {
  const requests: any[] = []
  api.defaults.adapter = async config => {
    requests.push({ ...config, data: typeof config.data === 'string' ? JSON.parse(config.data) : config.data })
    return { data: { success: true, preflight_report: { status: 'unknown', summary: 'sample', checks: [] } }, status: 200, statusText: 'OK', headers: {}, config }
  }
  setActiveProfileId('different-global-space')
  const payload = { action: 'check' as const, output: 'full-check-output', network_mode: 'macvlan' as const }
  await agentApi.generateDockerHAGuide(payload, 'fixed-installation-space')
  expect(requests[0].url).toBe('/agents/docker-oxidns-ha-guide')
  expect(requests[0].data).toEqual(payload)
  expect(requests[0].headers['X-ConfigFlow-Profile']).toBe('fixed-installation-space')
})
