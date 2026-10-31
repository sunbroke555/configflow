import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import AgentPushDiff from '@/components/agents/AgentPushDiff.vue'
import api from '@/api'
import type { PushDiffResponse } from '@/api/agentPushDiff'
import { diffLineTone, fieldText, summaryText, unavailableCopy, valueText } from '@/lib/agentPushDiff'
import { getAgentConfigSync } from '@/lib/agentConfigSync'
import type { Agent } from '@/types'

vi.mock('@/api', () => ({ default: { get: vi.fn() } }))

const wrappers: ReturnType<typeof mount>[] = []
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  document.body.innerHTML = ''
  vi.useRealTimers()
})
beforeEach(() => vi.mocked(api.get).mockReset())

const agent = (overrides: Partial<Agent> = {}) => ({
  id: 'lab-mihomo', name: '实验 Mihomo', host: '192.0.2.2', port: 27647, token: '', service_type: 'mihomo', status: 'online',
  last_heartbeat: '', version: '1.8.50-go', config_version: '0a1b2c3d', enabled: true,
  content_sync: { status: 'changed' }, ...overrides,
}) as Agent

const changed: PushDiffResponse = {
  status: 'changed', checked_at: new Date().toISOString(), service_type: 'mihomo', content: 'full', content_reason: null,
  baseline: { pushed_at: '2026-10-31T08:00:00Z', config_revision: 12 },
  counts: { added: 1, removed: 0, modified: 3, unchanged: 2 },
  files: [
    { path: 'config.yaml', role: 'config', status: 'modified', summary: { kind: 'config', changes: 4, before: 2048, after: 2300 } },
    { path: 'providers/实验机场.yaml', role: 'provider', name: '实验机场', status: 'modified',
      summary: { kind: 'nodes', before: 2, after: 3, added: 2, removed: 1, modified: 1 } },
    { path: 'ruleset/直连.list', role: 'rule', name: '直连', status: 'modified',
      summary: { kind: 'rules', before: 40000, after: 40120, added: 150, removed: 30 } },
    { path: 'ruleset/新规则.list', role: 'rule', name: '新规则', status: 'added',
      summary: { kind: 'rules', before: null, after: 8, added: 8, removed: 0 } },
  ],
  unchanged: [{ path: 'ruleset/广告.list', role: 'rule', name: '广告', status: 'unchanged' },
    { path: 'providers/备用.yaml', role: 'provider', name: '备用', status: 'unchanged' }],
  unchanged_omitted: 0,
  config: {
    format: 'yaml', status: 'compared', omitted: 0, total: 4,
    items: [
      { key: 'hosts.printer.lan', change: 'added', masked: false, old: null, new: '192.168.1.30' },
      { key: 'secret', change: 'modified', masked: true },
      { key: 'dns.fake-ip-filter', change: 'modified', masked: false, list: {
        before: 2, after: 63, added: 61, removed: 0, modified: 0, reordered: false, omitted: 1,
        entries: Array.from({ length: 60 }, (_, index) => ({ change: 'added' as const, text: `+.site-${index}.example` })) } },
      { key: 'proxies', change: 'modified', masked: false, list: {
        before: 1, after: 1, added: 0, removed: 0, modified: 1, reordered: false, omitted: 0, identity: 'name',
        entries: [{ change: 'modified', text: '手动 SS', fields: [{ key: 'password', change: 'modified', masked: true },
          { key: 'port', change: 'modified', masked: false, old: '443', new: '8388' }], fields_omitted: 0 }] } },
    ],
    raw: { text: '--- a/config.yaml\n+++ b/config.yaml\n@@ -1,3 +1,4 @@\n hosts:\n+  printer.lan: 192.168.1.30\n-secret: <已隐藏 1a2b3c>\n+secret: <已隐藏 4d5e6f>', truncated: false, omitted_lines: 0 },
  },
}

async function open(responses: PushDiffResponse[], props: Partial<{ canPush: boolean; pushing: boolean; offline: boolean }> = {},
  item = agent()) {
  responses.forEach(response => vi.mocked(api.get).mockResolvedValueOnce({ data: response } as any))
  const wrapper = mount(AgentPushDiff, { attachTo: document.body, props: {
    agent: item, canPush: true, pushing: false, offline: false, ...props } })
  wrappers.push(wrapper)
  await flushPromises()
  return wrapper
}

const body = () => document.body

describe('push diff copy', () => {
  it('summarises nodes, rules, sizes and configuration changes', () => {
    expect(summaryText(changed.files![1])).toBe('节点 +2 / −1 · 修改 1 · 共 3 个')
    expect(summaryText(changed.files![2])).toBe('规则 +150 / −30 · 共 40120 条')
    expect(summaryText(changed.files![0])).toBe('4 项配置变化 · 2.0 KB → 2.2 KB（+252 B）')
    expect(summaryText({ path: 'extra.txt', role: 'custom', status: 'removed', summary: { kind: 'size', before: 3, after: null } })).toBe('原 3 B')
    expect(summaryText({ path: 'x', role: 'rule', status: 'modified' })).toBe('上次推送的内容未保存，无法细分')
  })

  it('never shows masked values and describes field changes', () => {
    const secret = changed.config!.items[1]!
    expect(valueText(secret, 'old')).toEqual({ text: '已隐藏', masked: true, empty: false })
    expect(valueText(changed.config!.items[0]!, 'old')).toEqual({ text: '—', masked: false, empty: true })
    const [password, port] = changed.config!.items[3]!.list!.entries![0]!.fields!
    expect(fieldText(password!)).toBe('password：已修改（已隐藏）')
    expect(fieldText(port!)).toBe('port：443 → 8388')
    expect(fieldText({ key: 'proxies', change: 'modified', masked: false, list: {
      before: 3, after: 3, added: 1, removed: 1, modified: 0, reordered: false, added_items: ['US'], removed_items: ['JP'] } }))
      .toBe('proxies：+ US；− JP')
  })

  it('explains each missing baseline by its server reason', () => {
    expect(unavailableCopy({ status: 'never-pushed' }).description).toContain('还没有成功推送过')
    expect(unavailableCopy({ status: 'unknown', reason: 'baseline_missing' }).description).toContain('旧版本')
    expect(unavailableCopy({ status: 'unknown', reason: 'deployment_pending' })).toMatchObject({ title: '正在推送', pushable: false })
    expect(unavailableCopy({ status: 'unknown', reason: 'raw private detail' }).description).not.toContain('raw private detail')
    expect(diffLineTone('+a')).toBe('added')
    expect(diffLineTone('+++ b/config.yaml')).toBe('meta')
  })

  it('opens the dialog for every server conclusion, not while pushing or checking', () => {
    for (const status of ['changed', 'synced', 'never-pushed'] as const) {
      expect(getAgentConfigSync({ content_sync: { status } }, 1).diffable).toBe(true)
    }
    expect(getAgentConfigSync({ content_sync: { status: 'unknown', reason: 'baseline_missing' } }, 1).diffable).toBe(true)
    expect(getAgentConfigSync({ content_sync: { status: 'checking' } }, 1).diffable).toBe(false)
    expect(getAgentConfigSync({ content_sync: { status: 'changed' } }, 1, { status: 'uploading' }).diffable).toBe(false)
    expect(getAgentConfigSync({ content_sync: { status: 'changed' } }, 1, { status: 'succeeded', pending: true }).diffable).toBe(false)
    // An old server without a comparison has no /push-diff to open.
    expect(getAgentConfigSync({ config_version: 'abc', config_revision: 3 }, 3).diffable).toBe(false)
  })
})

describe('AgentPushDiff dialog', () => {
  it('polls while the server prepares, then shows files, configuration items and a collapsed raw diff', async () => {
    vi.useFakeTimers()
    vi.mocked(api.get).mockResolvedValueOnce({ data: { status: 'checking' } } as any)
    vi.mocked(api.get).mockResolvedValueOnce({ data: changed } as any)
    const wrapper = mount(AgentPushDiff, { attachTo: document.body, props: { agent: agent(), canPush: true, pushing: false, offline: false } })
    wrappers.push(wrapper)
    await flushPromises()
    expect(body().querySelector('[data-push-diff-loading]')?.textContent).toContain('正在生成这次将要推送的内容')
    await vi.advanceTimersByTimeAsync(1500)
    await flushPromises()
    expect(api.get).toHaveBeenCalledTimes(2)
    expect(api.get).toHaveBeenLastCalledWith('/agents/lab-mihomo/push-diff', expect.anything())

    const files = body().querySelector('[data-push-diff-files]')!
    const rows = [...files.querySelectorAll('tbody tr[data-file]')].map(row => row.textContent!.replace(/\s+/g, ' ').trim())
    expect(rows[0]).toContain('主配置')
    expect(rows[1]).toMatch(/实验机场.*订阅.*修改.*节点 \+2 \/ −1 · 修改 1/)
    expect(rows[2]).toContain('规则 +150 / −30')
    expect(rows[3]).toMatch(/新规则.*新增.*规则 \+8/)
    expect(body().querySelector('[data-push-diff-counts]')!.textContent).toMatch(/修改\s*3.*新增\s*1.*删除\s*0.*未变\s*2/)
    expect(files.textContent).toContain('未变的 2 个文件')
    expect(files.textContent).not.toContain('广告')

    const config = body().querySelector('[data-push-diff-config]')!
    const hosts = config.querySelector('[data-key="hosts.printer.lan"]')!
    expect(hosts.textContent).toContain('新增')
    expect(hosts.textContent).toContain('192.168.1.30')
    const secret = config.querySelector('[data-key="secret"]')!
    expect(secret.textContent!.replace(/\s+/g, '')).toBe('secret修改已隐藏已隐藏')
    const filter = config.querySelector('[data-key="dns.fake-ip-filter"]')!
    expect(filter.textContent).toContain('2 条')
    expect(filter.textContent).toContain('63 条 +61 / −0')
    expect(config.textContent).not.toContain('+.site-0.example')

    const raw = body().querySelector('[data-push-diff-raw]')!
    expect(raw.querySelector('pre')).toBeNull()
    expect(raw.textContent).toContain('密码、令牌等已隐藏')
    vi.useRealTimers()
  })

  it('expands long list details with the truncation note and opens the raw diff', async () => {
    await open([changed])
    const filter = body().querySelector('[data-key="dns.fake-ip-filter"]')!
    const toggle = [...filter.querySelectorAll('button')].find(button => button.textContent!.includes('展开明细'))!
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    toggle.click()
    await flushPromises()
    const detail = body().querySelector(`#${toggle.getAttribute('aria-controls')}`)!
    expect(detail.querySelectorAll('li')).toHaveLength(60)
    expect(detail.textContent).toContain('+ +.site-0.example')
    expect(detail.textContent).toContain('还有 1 条未列出，完整内容见下方「原始差异」')
    const proxies = body().querySelector('[data-key="proxies"]')!
    ;[...proxies.querySelectorAll('button')].find(button => button.textContent!.includes('展开明细'))!.click()
    await flushPromises()
    expect(body().textContent).toContain('~ 手动 SS')
    expect(body().textContent).toContain('password：已修改（已隐藏）')

    const rawToggle = [...body().querySelectorAll('[data-push-diff-raw] button')].find(button => button.textContent!.includes('原始差异'))!
    rawToggle.dispatchEvent(new Event('click'))
    await flushPromises()
    const lines = [...body().querySelectorAll('#push-diff-raw-text > span')]
    expect(lines.map(line => line.textContent)).toContain('+  printer.lan: 192.168.1.30')
    expect(lines.find(line => line.textContent === '+  printer.lan: 192.168.1.30')!.className).toContain('text-success-accent')
  })

  it('offers the existing push and reports it to the page', async () => {
    const wrapper = await open([changed])
    const push = body().querySelector<HTMLButtonElement>('[data-push-diff-push]')!
    expect(push.disabled).toBe(false)
    push.click()
    expect(wrapper.emitted('push')).toHaveLength(1)
  })

  it('disables push while busy and explains offline devices', async () => {
    await open([changed], { canPush: false, pushing: true })
    expect(body().querySelector<HTMLButtonElement>('[data-push-diff-push]')!.disabled).toBe(true)
    wrappers.splice(0).forEach(wrapper => wrapper.unmount())
    await open([changed], { offline: true })
    expect(body().querySelector('[data-push-diff-push]')).toBeNull()
    expect(body().textContent).toContain('Agent 离线，上线后才能推送')
  })

  it('shows no changes when synced and asks the page to refresh an outdated card', async () => {
    const wrapper = await open([{ status: 'synced', baseline: { pushed_at: '2026-10-31T08:00:00Z' } }])
    expect(body().querySelector('[data-push-diff-synced]')!.textContent).toContain('无变更')
    expect(body().querySelector('[data-push-diff-push]')).toBeNull()
    expect(wrapper.emitted('refresh')).toHaveLength(1)
  })

  it('explains a missing baseline and still offers the push that creates one', async () => {
    await open([{ status: 'never-pushed' }], {}, agent({ content_sync: { status: 'synced' } }))
    expect(body().querySelector('[data-push-diff-unavailable]')!.textContent).toContain('没有可对比的基准')
    expect(body().querySelector('[data-push-diff-push]')).not.toBeNull()
  })

  it('lists files by digest only when the previous bundle was not kept', async () => {
    await open([{ ...changed, content: 'digests', content_reason: 'service_without_bundle',
      files: [{ path: 'config.yaml', role: 'config', status: 'modified' }],
      config: { format: 'yaml', status: 'unavailable', reason: 'service_without_bundle', items: [], omitted: 0 } }])
    expect(body().querySelector('[data-push-diff-notice]')!.textContent).toContain('MosDNS 推送时不保存发布包')
    expect(body().querySelector('[data-push-diff-config-notice]')!.textContent).toContain('无法按配置项对比')
    expect(body().querySelector('[data-push-diff-config]')).toBeNull()
    expect(body().querySelector('[data-file="config.yaml"]')!.textContent).toContain('上次推送的内容未保存')
  })

  it('shows a readable request error and retries', async () => {
    vi.mocked(api.get).mockRejectedValueOnce({ response: { data: { message: '找不到这台 Agent，请刷新设备列表。' } } })
    vi.mocked(api.get).mockResolvedValueOnce({ data: changed } as any)
    const wrapper = mount(AgentPushDiff, { attachTo: document.body, props: { agent: agent(), canPush: true, pushing: false, offline: false } })
    wrappers.push(wrapper)
    await flushPromises()
    const error = body().querySelector('[data-push-diff-error]')!
    expect(error.getAttribute('role')).toBe('alert')
    expect(error.textContent).toContain('找不到这台 Agent')
    ;[...error.querySelectorAll('button')].find(button => button.textContent!.includes('重试'))!.click()
    await flushPromises()
    expect(body().querySelector('[data-push-diff-changed]')).not.toBeNull()
  })

  it('stops polling when closed', async () => {
    vi.useFakeTimers()
    vi.mocked(api.get).mockResolvedValue({ data: { status: 'checking' } } as any)
    const wrapper = mount(AgentPushDiff, { attachTo: document.body, props: { agent: agent(), canPush: true, pushing: false, offline: false } })
    await flushPromises()
    wrapper.unmount()
    await vi.advanceTimersByTimeAsync(10_000)
    expect(api.get).toHaveBeenCalledTimes(1)
  })
})
