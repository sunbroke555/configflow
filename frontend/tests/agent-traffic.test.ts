import { afterEach, describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import AgentTraffic from '@/components/agents/AgentTraffic.vue'
import { recordTraffic, trafficSample, TRAFFIC_WINDOW_MS } from '@/lib/agentTraffic'
import type { Agent, AgentSystemMetrics } from '@/types'

const now = Date.parse('2026-10-19T12:00:00Z')
const metrics = (at: number, upload: unknown = 1024, download: unknown = 2048) => ({
  collected_at: new Date(at).toISOString(),
  network: { speed_sent: upload, speed_recv: download },
}) as AgentSystemMetrics
const agent = (sample = metrics(now), overrides: Partial<Agent> = {}) => ({
  id: 'a', name: 'Linux gateway', status: 'online', system_metrics: sample, ...overrides,
}) as Agent

const wrappers: ReturnType<typeof mount>[] = []
afterEach(() => wrappers.splice(0).forEach(wrapper => wrapper.unmount()))
function mountTraffic(device = agent()) {
  const wrapper = mount(AgentTraffic, {
    props: { agent: device, now },
    global: { stubs: {
      Sparkline: { name: 'Sparkline', props: ['data', 'timestamps', 'timeRange', 'min', 'max', 'maxGap', 'color'], template: '<canvas />' },
      FieldHelp: { props: ['label', 'text'], template: '<button :aria-label="label">{{ text }}</button>' },
    } },
  })
  wrappers.push(wrapper)
  return wrapper
}

describe('Agent traffic sampling', () => {
  it('starts with the actual sample and does not manufacture history from repeated polls', async () => {
    const wrapper = mountTraffic()
    expect(wrapper.get('[data-traffic-state]').text()).toBe('采样中')
    expect(wrapper.get('[data-traffic-rate="upload"]').text()).toBe('1.0 KB/s')
    await wrapper.setProps({ agent: agent(), now: now + 10_000 })
    await wrapper.setProps({ agent: agent(), now: now + 20_000 })
    expect(wrapper.findAllComponents({ name: 'Sparkline' })[0].props('data')).toEqual([1024])
    await wrapper.setProps({ agent: agent(metrics(now + 30_000, 512, 8192)), now: now + 30_000 })
    const charts = wrapper.findAllComponents({ name: 'Sparkline' })
    expect(charts[0].props('data')).toEqual([1024, 512])
    expect(charts[1].props('data')).toEqual([2048, 8192])
    expect(charts[0].props('max')).toBe(charts[1].props('max'))
    expect(charts[0].props('min')).toBe(0)
    expect(wrapper.get('[data-traffic-state]').text()).toBe('近 5 分钟')
  })

  it('keeps a measured zero distinct from missing or invalid rates', () => {
    const wrapper = mountTraffic(agent(metrics(now, 0, -1)))
    expect(wrapper.get('[data-traffic-rate="upload"]').text()).toBe('0 B/s')
    expect(wrapper.get('[data-traffic-rate="download"]').text()).toBe('—')
    expect(trafficSample(metrics(now, NaN, Infinity))).toMatchObject({ upload: null, download: null })
    expect(trafficSample(metrics(now, '2', undefined))?.upload).toBeNull()
  })

  it('stops calling old metrics current even while the Agent stays online', async () => {
    const wrapper = mountTraffic()
    await wrapper.setProps({ now: now + 120_000 })
    expect(wrapper.get('[data-traffic-state]').text()).toBe('数据未更新')
    expect(wrapper.get('[data-traffic-rate="upload"]').text()).toBe('—')
    expect(wrapper.findAllComponents({ name: 'Sparkline' })[0].props('data')).toEqual([1024])
    await wrapper.setProps({ now: now + TRAFFIC_WINDOW_MS + 1 })
    expect(wrapper.findAllComponents({ name: 'Sparkline' })[0].props('data')).toEqual([])
  })

  it('retains observed history during an outage without appending offline samples', async () => {
    const wrapper = mountTraffic()
    await wrapper.setProps({ agent: agent(metrics(now + 30_000), { status: 'offline' }), now: now + 30_000 })
    expect(wrapper.get('[data-traffic-state]').text()).toBe('离线')
    expect(wrapper.get('[data-traffic-rate="download"]').text()).toBe('—')
    expect(wrapper.findAllComponents({ name: 'Sparkline' })[0].props('timestamps')).toEqual([now])
  })

  it('clears the previous device history when a component is reused for another device', async () => {
    const wrapper = mountTraffic()
    await wrapper.setProps({ agent: agent(metrics(now + 30_000, 100), { id: 'b' }), now: now + 30_000 })
    expect(wrapper.findAllComponents({ name: 'Sparkline' })[0].props('data')).toEqual([100])
    expect(wrapper.get('[data-traffic-state]').text()).toBe('采样中')
  })

  it('does not invent a timestamp for missing, malformed or timezone-less metrics', () => {
    expect(trafficSample(undefined)).toBeNull()
    expect(trafficSample({ collected_at: 'not-a-date' } as AgentSystemMetrics)).toBeNull()
    expect(trafficSample({ collected_at: '2026-10-19T12:00:00' } as AgentSystemMetrics)).toBeNull()
    const wrapper = mountTraffic(agent(undefined, { system_metrics: undefined }))
    expect(wrapper.get('[data-traffic-state]').text()).toBe('暂无数据')
    expect(wrapper.get('[data-traffic-rate="upload"]').text()).toBe('—')
  })

  it('bounds and orders the time window, discarding future data and duplicate heartbeats', () => {
    const first = trafficSample(metrics(now - 30_000))!
    const latest = trafficSample(metrics(now))!
    expect(recordTraffic([latest], first, now)).toEqual([first, latest])
    expect(recordTraffic([first, latest], { ...latest, upload: 99 }, now)).toEqual([first, latest])
    expect(recordTraffic([first], trafficSample(metrics(now + 1)), now)).toEqual([first])
    expect(recordTraffic([first, latest], null, now + TRAFFIC_WINDOW_MS)).toEqual([latest])
  })
})
