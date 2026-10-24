import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DOMWrapper, flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import PolicySourceEditor from '@/components/routers/PolicySourceEditor.vue'
import PolicyDestinationEditor from '@/components/routers/PolicyDestinationEditor.vue'
import { sourcePolicyIndex } from '@/lib/policySourceEditor'
import api, { ruleContentApi } from '@/api'
import { confirm } from '@/lib/feedback'
import { CLASSICAL_CONTENT_HINT } from '@/lib/ruleset-content'
import { scopedRequests } from '@/profileContext'
vi.mock('@/api', () => ({ default: { post: vi.fn() }, ruleContentApi: { read: vi.fn(), save: vi.fn() } }))
vi.mock('@/lib/feedback', () => ({ confirm: vi.fn(), notify: { success: vi.fn(), warning: vi.fn() } }))
const wrappers: VueWrapper[] = []
const source = { ip: '192.0.2.105', name: 'NAS', mode: 'smart' as const }
const field = (id: string) => new DOMWrapper(document.getElementById(id) as HTMLInputElement)
const button = (text: string) => Array.from(document.querySelectorAll('button')).find(row => row.textContent?.trim() === text)!
const click = async (text: string) => { button(text).click(); await flushPromises() }
const response = () => ({ data: { success: true, revision: 'read-revision', usage_count: 2,
  rule: { id: 'library', name: 'Shared IPs', source_type: 'content', behavior: 'ipcidr', content: '1.1.1.0/24\n8.8.8.8/32', url: '' } } })
async function destination() {
  const w = mount(PolicyDestinationEditor, { attachTo: document.body, props: {
    rules: [{ id: 'profile-rule', library_rule_id: 'library', name: 'IPs', policy: 'Proxy' }], selectedIds: ['profile-rule'], address: '1.1.1.0/24'
  } }); wrappers.push(w); await flushPromises(); return w
}
beforeEach(() => {
  vi.resetAllMocks(); scopedRequests.value = 0
  vi.mocked(confirm).mockResolvedValue(false)
  vi.mocked(api.post).mockImplementation(async (_url, body: any) => ({ data: { content: body.content, changes: [], errors: [] } }) as any)
  vi.mocked(ruleContentApi.read).mockResolvedValue(response() as any)
  vi.mocked(ruleContentApi.save).mockResolvedValue({ data: { success: true, revision: 'saved-revision' } } as any)
})
afterEach(() => { wrappers.splice(0).forEach(w => w.unmount()) })

describe('policy authoring editors', () => {
  it('edits the original source address, normalizes it and rejects duplicates without touching drafts', async () => {
    const policies = [source, { ...source, ip: '192.0.2.106' }]
    const w = mount(PolicySourceEditor, { attachTo: document.body, props: { policy: source, policies } }); wrappers.push(w)
    await flushPromises()
    await field('source-edit-ip').setValue('192.0.2.106/32')
    await field('policy-source-form').trigger('submit')
    expect(w.emitted('apply')).toBeUndefined()
    expect(document.body.textContent).toContain('此 IP 或网段已存在')
    expect(policies[0].ip).toBe('192.0.2.105')
    await field('source-edit-ip').setValue('192.0.2.119/28')
    await field('policy-source-form').trigger('submit')
    expect(w.emitted('apply')?.[0]).toEqual([{ ...source, ip: '192.0.2.112/28' }])
  })
  it('maps split ranges back to the most specific original source but not merged unrelated sources', () => {
    const rows = [{ ...source, ip: '192.0.2.0/24' }, { ...source, ip: '192.0.2.64/26' }]
    expect(sourcePolicyIndex('192.0.2.80/28', rows)).toBe(1)
    expect(sourcePolicyIndex('192.0.2.105/32', [source])).toBe(0)
    expect(sourcePolicyIndex('192.0.2.0/24', [{ ...source, ip: '192.0.2.0/25' }, { ...source, ip: '192.0.2.128/25' }])).toBe(-1)
    expect(sourcePolicyIndex('invalid', rows)).toBe(-1)
  })
  it('protects unsaved source changes on cancel', async () => {
    const w = mount(PolicySourceEditor, { attachTo: document.body, props: { policies: [] } }); wrappers.push(w)
    await flushPromises(); await field('source-edit-ip').setValue('192.0.2.101')
    await click('取消'); expect(w.emitted('close')).toBeUndefined()
    vi.mocked(confirm).mockResolvedValueOnce(true)
    await click('取消'); expect(w.emitted('close')).toHaveLength(1)
  })
  it('loads only the exact shared source and submits content with a pinned revision', async () => {
    const w = await destination()
    expect(ruleContentApi.read).toHaveBeenCalledWith('library')
    expect(document.body.textContent).toContain('所有引用它的配置空间')
    await field('destination-edit-content').setValue('9.9.9.9/32')
    await click('保存规则内容')
    expect(ruleContentApi.save).toHaveBeenCalledWith('library', 'read-revision', '9.9.9.9/32')
    expect(w.emitted('saved')).toHaveLength(1)
    expect(scopedRequests.value).toBe(0)
  })
  it('shows IP content errors beside the draft and prevents saving', async () => {
    const w = await destination()
    await field('destination-edit-content').setValue('+.tmdb.org\nDOMAIN-SHUFFIX,example.com')
    vi.mocked(api.post).mockResolvedValueOnce({ data: { content: '+.tmdb.org', changes: [], errors: [
      { line: 1, message: 'IPCIDR 只接受 CIDR' }, { line: 2, message: '不是有效 IP 网段' }
    ] } })
    await click('保存规则内容')
    expect(document.querySelector('[data-rule-content-issues]')?.textContent).toContain('第 1 行：IPCIDR 只接受 CIDR')
    expect(document.querySelector('[data-rule-content-issues]')?.textContent).toContain('第 2 行：不是有效 IP 网段')
    expect(ruleContentApi.save).not.toHaveBeenCalled()
    expect(w.emitted('saved')).toBeUndefined()
    await field('destination-edit-content').setValue('2001:db8::/32')
    expect(document.querySelector('[data-rule-content-issues]')).toBeNull()
  })
  it('explains classical delivery and saves the original destination content without confirmation', async () => {
    const data = response()
    data.data.rule.behavior = 'classical'
    data.data.rule.content = 'DOMAIN-SUFFIX,tmdb.org'
    Object.assign(data.data.rule, { format: 'yaml' })
    vi.mocked(ruleContentApi.read).mockResolvedValueOnce(data as any)
    const original = '# keep\npayload: ["+.tmdb.org", "192.0.2.1", "*.test"]\n'
    vi.mocked(api.post).mockResolvedValueOnce({ data: { content: original, errors: [], changes: [],
      warnings: [{ line: 2, before: '*.test', message: '请手动调整' }] } })
    const w = await destination()
    expect(document.body.textContent).toContain(CLASSICAL_CONTENT_HINT)
    await field('destination-edit-content').setValue(original)
    await click('保存规则内容')
    expect(api.post).toHaveBeenCalledWith('/rule-library/check-content', {
      behavior: 'classical', format: 'yaml', content: original, append_lines: []
    })
    expect(confirm).not.toHaveBeenCalled()
    expect(ruleContentApi.save).toHaveBeenCalledWith('library', 'read-revision', original)
    expect(w.emitted('saved')).toHaveLength(1)
    expect((field('destination-edit-content').element as HTMLTextAreaElement).value).toBe(original)
    expect(scopedRequests.value).toBe(0)
  })
  it('does not save if classical content checking finishes after the destination editor unmounts', async () => {
    const data = response(); data.data.rule.behavior = 'classical'
    vi.mocked(ruleContentApi.read).mockResolvedValueOnce(data as any)
    let resolve!: (value: any) => void
    vi.mocked(api.post).mockImplementationOnce(() => new Promise(done => { resolve = done }))
    const w = await destination()
    await field('destination-edit-content').setValue('192.0.2.1')
    await click('保存规则内容')
    w.unmount(); wrappers.splice(wrappers.indexOf(w), 1)
    resolve({ data: { content: '192.0.2.1', errors: [], changes: [] } }); await flushPromises()
    expect(ruleContentApi.save).not.toHaveBeenCalled()
    expect(confirm).not.toHaveBeenCalled()
    expect(scopedRequests.value).toBe(0)
  })
  it('retains input on a version conflict and requires confirmation before reloading', async () => {
    const w = await destination()
    await field('destination-edit-content').setValue('9.9.9.9/32')
    vi.mocked(ruleContentApi.save).mockRejectedValueOnce({ response: { data: { message: '规则集已在其他页面更新' } } })
    await click('保存规则内容')
    expect((document.getElementById('destination-edit-content') as HTMLTextAreaElement).value).toBe('9.9.9.9/32')
    expect(w.emitted('saved')).toBeUndefined()
    expect(document.body.textContent).toContain('规则集已在其他页面更新')
    await click('重新读取'); expect(ruleContentApi.read).toHaveBeenCalledTimes(1)
    vi.mocked(confirm).mockResolvedValueOnce(true)
    await click('重新读取'); expect(ruleContentApi.read).toHaveBeenCalledTimes(2)
  })
  it('does not convert a URL subscription into manual content', async () => {
    const data = response(); data.data.rule.source_type = 'url'; data.data.rule.url = 'https://example.test/ips.txt'; data.data.rule.behavior = 'classical'
    vi.mocked(ruleContentApi.read).mockResolvedValueOnce(data as any)
    await destination()
    expect(document.getElementById('destination-edit-content')).toBeNull()
    expect(document.body.textContent).toContain('不能在这里修改下载内容')
    expect(document.body.textContent).toContain(CLASSICAL_CONTENT_HINT)
    expect(ruleContentApi.save).not.toHaveBeenCalled()
  })
  it('ignores a response after its profile editor was unmounted', async () => {
    let resolve!: (value: any) => void
    vi.mocked(ruleContentApi.read).mockImplementationOnce(() => new Promise(done => { resolve = done }) as any)
    const w = await destination(); w.unmount(); wrappers.splice(wrappers.indexOf(w), 1)
    resolve(response()); await flushPromises()
    expect(document.getElementById('destination-edit-content')).toBeNull()
  })
})
