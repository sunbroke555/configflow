import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import RuleSourceEditor from '@/components/rules/RuleSourceEditor.vue'
import { Select } from '@/components/ui/select'

// Only HTTP boundaries are replaced; fields, selectors and content validation stay real.
const network = vi.hoisted(() => ({ post: vi.fn(), put: vi.fn() }))
vi.mock('@/api', () => ({ default: { post: network.post, put: network.put } }))

const wrappers: VueWrapper[] = []
const remoteSource = (patch: Record<string, unknown> = {}) => ({
  id: 'shared-source', name: 'Shared source', source_type: 'url',
  url: 'https://rules.test/original.list', behavior: 'classical', format: 'text', enabled: true,
  ...patch
})
const contentSource = (patch: Record<string, unknown> = {}) => remoteSource({
  source_type: 'content', url: '', content: '# original\nDOMAIN,example.test\n', ...patch
})

beforeEach(() => {
  network.post.mockReset().mockImplementation(async (_path: string, body: Record<string, unknown>) => ({
    data: { content: body.content, changes: [], errors: [] }
  }))
  network.put.mockReset().mockImplementation(async (_path: string, body: Record<string, unknown>) => ({
    data: { success: true, data: structuredClone(body) }
  }))
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  vi.stubGlobal('matchMedia', vi.fn((query: string) => ({
    matches: false, media: query, onchange: null, addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn()
  })))
})

afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

async function render(source: Record<string, unknown>) {
  const wrapper = mount(RuleSourceEditor, { props: { source }, attachTo: document.body })
  wrappers.push(wrapper)
  await flushPromises()
  return wrapper
}
const save = (wrapper: VueWrapper) => (wrapper.vm as unknown as { save: () => Promise<boolean> }).save()
const lastBusy = (wrapper: VueWrapper) => wrapper.emitted('busy')?.at(-1)

async function choose(wrapper: VueWrapper, label: string, value: string) {
  const select = wrapper.findAllComponents(Select).find(item => item.find(`[aria-label="${label}"]`).exists())
  expect(select, label).toBeTruthy()
  select!.vm.$emit('update:modelValue', value)
  await flushPromises()
}

describe('shared source editor', () => {
  it('preserves shared enablement and submits only source fields', async () => {
    const source = remoteSource({
      enabled: false, has_ip_part: true, library_enabled: false, itemType: 'ruleset',
      policy: 'Proxy', dns: 'follow', remark: 'Local remark', no_resolve: true, update_interval: 3600
    })
    const original = structuredClone(source)
    const wrapper = await render(source)
    await wrapper.get('#source-name').setValue('Renamed source')
    expect(await save(wrapper)).toBe(true)
    expect(network.put).toHaveBeenCalledExactlyOnceWith('/rule-library/shared-source', {
      id: 'shared-source', name: 'Renamed source', source_type: 'url', behavior: 'classical',
      format: 'text', enabled: false, url: 'https://rules.test/original.list'
    })
    expect(network.post).not.toHaveBeenCalled()
    expect(wrapper.emitted('saved')?.[0]?.[0]).toMatchObject({ name: 'Renamed source', enabled: false })
    expect(source).toEqual(original)
    expect(lastBusy(wrapper)).toEqual([false])
    expect(wrapper.emitted('dirty')?.at(-1)).toEqual([false])
  })

  it('validates and preserves handwritten source text with its explicitly selected YAML format', async () => {
    const wrapper = await render(contentSource())
    const content = '# keep formatting\r\npayload:\r\n  - "DOMAIN,example.test"\r\n  - "IP-CIDR,192.0.2.0/24"\r\n'
    await wrapper.get('#source-content').setValue(content)
    await choose(wrapper, '内容格式', 'yaml')
    const input = (wrapper.get('#source-content').element as HTMLTextAreaElement).value
    expect(await save(wrapper)).toBe(true)
    expect(network.post).toHaveBeenCalledExactlyOnceWith('/rule-library/check-content', {
      behavior: 'classical', format: 'yaml', content: input, append_lines: []
    })
    expect(network.put).toHaveBeenCalledWith('/rule-library/shared-source', expect.objectContaining({
      content: input, format: 'yaml', source_type: 'content', enabled: true
    }))
    expect(network.put.mock.calls[0]![1]).not.toHaveProperty('url')
    expect(wrapper.emitted('saved')?.[0]?.[0]).toMatchObject({ content: input, format: 'yaml' })
  })

  it('shows content errors without writing or replacing the draft', async () => {
    const wrapper = await render(contentSource({ behavior: 'domain', content: 'example.test' }))
    await wrapper.get('#source-content').setValue('DOMAIN-SUFFIX,invalid.test')
    network.post.mockResolvedValueOnce({ data: {
      content: 'converted.test', changes: [], errors: [{ line: 1, message: 'Domain 不接受 DOMAIN-SUFFIX 前缀' }]
    } })
    expect(await save(wrapper)).toBe(false)
    expect(network.put).not.toHaveBeenCalled()
    expect(wrapper.get('[data-rule-content-issues]').text()).toContain('第 1 行：Domain 不接受 DOMAIN-SUFFIX 前缀')
    expect((wrapper.get('#source-content').element as HTMLTextAreaElement).value).toBe('DOMAIN-SUFFIX,invalid.test')
    expect(wrapper.emitted('saved')).toBeUndefined()
    expect(wrapper.emitted('dirty')?.at(-1)).toEqual([true])
    expect(lastBusy(wrapper)).toEqual([false])
  })

  it('keeps a failed URL edit available for retry and exposes the server reason', async () => {
    const wrapper = await render(remoteSource())
    await wrapper.get('#source-url').setValue('https://rules.test/revised.list')
    network.put.mockRejectedValueOnce(Object.assign(new Error('request failed'), {
      isAxiosError: true, response: { data: { message: '规则源下载失败，原配置与有效缓存已保留' } }
    }))
    expect(await save(wrapper)).toBe(false)
    expect(wrapper.text()).toContain('规则源下载失败，原配置与有效缓存已保留')
    expect((wrapper.get('#source-url').element as HTMLInputElement).value).toBe('https://rules.test/revised.list')
    expect(wrapper.emitted('saved')).toBeUndefined()
    expect(lastBusy(wrapper)).toEqual([false])
    expect(await save(wrapper)).toBe(true)
    expect(network.put).toHaveBeenCalledTimes(2)
    expect(network.put.mock.calls[1]![1]).toMatchObject({ url: 'https://rules.test/revised.list' })
    expect(wrapper.emitted('saved')).toHaveLength(1)
    expect(wrapper.text()).not.toContain('规则源下载失败，原配置与有效缓存已保留')
  })

  it('does not submit twice while content validation is pending', async () => {
    const wrapper = await render(contentSource())
    await wrapper.get('#source-content').setValue('DOMAIN,changed.test\n')
    let resolveCheck!: (result: { data: { content: string; changes: unknown[]; errors: unknown[] } }) => void
    network.post.mockImplementationOnce(() => new Promise(resolve => { resolveCheck = resolve }))
    const firstSave = save(wrapper)
    await flushPromises()
    expect(lastBusy(wrapper)).toEqual([true])
    expect(await save(wrapper)).toBe(false)
    expect(network.post).toHaveBeenCalledTimes(1)
    expect(network.put).not.toHaveBeenCalled()
    resolveCheck({ data: { content: 'DOMAIN,changed.test\n', changes: [], errors: [] } })
    expect(await firstSave).toBe(true)
    expect(network.put).toHaveBeenCalledTimes(1)
    expect(wrapper.emitted('saved')).toHaveLength(1)
    expect(lastBusy(wrapper)).toEqual([false])
  })

  it('recovers from a failed content check without losing unsaved text', async () => {
    const wrapper = await render(contentSource())
    await wrapper.get('#source-content').setValue('DOMAIN,retry.test\n')
    network.post.mockRejectedValueOnce(new Error('检查规则内容失败'))
    expect(await save(wrapper)).toBe(false)
    expect(network.put).not.toHaveBeenCalled()
    expect((wrapper.get('#source-content').element as HTMLTextAreaElement).value).toBe('DOMAIN,retry.test\n')
    expect(wrapper.text()).toContain('检查规则内容失败')
    expect(lastBusy(wrapper)).toEqual([false])
    expect(await save(wrapper)).toBe(true)
    expect(network.put).toHaveBeenCalledTimes(1)
  })
})
