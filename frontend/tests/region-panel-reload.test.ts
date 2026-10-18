import { afterEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import RegionPanel from '@/components/domain-discovery/RegionPanel.vue'
import { canReloadPage } from '@/lib/pageReloadGuard'
import api from '@/api'

vi.mock('@/api', () => ({ default: { put: vi.fn() } }))
vi.mock('@/lib/feedback', () => ({ notify: { success: vi.fn(), error: vi.fn(), warning: vi.fn() } }))
const wrappers: VueWrapper[] = []
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); vi.clearAllMocks() })

const region = {
  enabled: false, ready: true, port: 17891, min_agent_version: '1.0.0', services: [],
  result: null, agent: null, domains: {}, policies: [], regional_rulesets: {},
}

describe('region port automatic reload protection', () => {
  it('keeps unblurred and pending port changes until the saved value is confirmed', async () => {
    let resolve!: (value: any) => void
    vi.mocked(api.put).mockImplementationOnce(() => new Promise(done => { resolve = done }))
    const wrapper = mount(RegionPanel, { props: { region, busy: false } })
    wrappers.push(wrapper)
    expect(canReloadPage()).toBe(true)
    const input = wrapper.get<HTMLInputElement>('[aria-label="检测入口端口"]')
    await input.setValue('17892')
    expect(canReloadPage()).toBe(false)
    await input.trigger('blur')
    expect(canReloadPage()).toBe(false)
    resolve({ data: { success: true } })
    await flushPromises()
    await wrapper.setProps({ region: { ...region, port: 17892 } })
    expect(input.element.value).toBe('17892')
    expect(canReloadPage()).toBe(true)
  })
})
