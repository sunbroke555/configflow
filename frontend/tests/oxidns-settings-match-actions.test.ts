import { describe, expect, it, vi } from 'vitest'
import { flushPromises, type VueWrapper } from '@vue/test-utils'
import { load as loadYaml, dump as dumpYaml } from 'js-yaml'
import OxidnsCommonSettings from '@/components/generate/OxidnsCommonSettings.vue'
import OxidnsMatchEditor from '@/components/generate/OxidnsMatchEditor.vue'
import OxidnsNativeEditor from '@/components/generate/OxidnsNativeEditor.vue'
import { Select } from '@/components/ui/select'
import { oxidnsApi } from '@/api'

import { settings, render, button, click, settleValidation, input, savedPayload, views } from './helpers/oxidns-settings'

vi.mock('@/api', () => ({
  agentApi: { getAll: vi.fn(), summaries: vi.fn(), pushConfig: vi.fn(), getDeployment: vi.fn(), activateDeployment: vi.fn() },
  profileApi: { list: vi.fn() },
  oxidnsApi: { getSettings: vi.fn(), saveSettings: vi.fn(), prepareEditor: vi.fn(), readCommonSettings: vi.fn(), applyCommonSettings: vi.fn(), applyNativeSettings: vi.fn(), validateSettings: vi.fn() },
}))

type Match = Record<string, any>
type Target = { tag: string; type: string; exec: string; source: string }
const targets: Target[] = [
  { tag: 'local_dns', type: 'sequence', exec: 'goto local_dns', source: 'config.yaml' },
  { tag: 'local_dns_backup', type: 'sequence', exec: 'goto local_dns_backup', source: 'plugins/extra.yaml' },
  { tag: 'office_forward', type: 'forward', exec: '$office_forward', source: 'config.yaml' },
  { tag: 'backup_forward', type: 'forward', exec: '$backup_forward', source: 'plugins/extra.yaml' },
]
const targetTags = ['main', 'local_dns', 'local_dns_backup', 'office_forward', 'backup_forward', 'domain_rules', 'unknown_executor']
const existing = () => ({ matches: 'qname full:kept.test', exec: 'goto local_dns', extension: { keep: [0, false, null] } })
function source(matches: Match[] = [existing()]) {
  return {
    ...settings(),
    native_config: dumpYaml({
      runtime: { worker_threads: 8 },
      include: ['plugins/extra.yaml'],
      plugins: [
        { tag: 'office_forward', type: 'forward', args: { upstreams: [{ addr: '192.0.2.53' }] } },
        { tag: 'local_dns', type: 'sequence', args: [{ exec: '$office_forward' }, { exec: 'accept' }] },
        { tag: 'domain_rules', type: 'domain_set', args: { exps: ['full:example.test'] } },
        { tag: 'unknown_executor', type: 'future_plugin', args: { keep: false } },
        { tag: 'main', type: 'sequence', args: [...matches, { exec: 'accept' }] },
      ],
      unknown: { keep: [0, false, null] },
    }),
    additional_files: [{ path: 'plugins/extra.yaml', encoding: 'text', content: dumpYaml({ plugins: [
      { tag: 'backup_forward', type: 'forward', args: { upstreams: [{ addr: '192.0.2.54' }] } },
      { tag: 'local_dns_backup', type: 'sequence', args: [{ exec: '$backup_forward' }, { exec: 'accept' }] },
    ] }) }],
  }
}
function rows(value: { native_config: string }): Match[] {
  const doc = loadYaml(value.native_config) as any
  return doc.plugins.find((plugin: any) => plugin.tag === 'main')?.args.filter((entry: Match) => 'matches' in entry) ?? []
}
function projected(value: { native_config: string }, options = targets, tags = targetTags) {
  return {
    values: { worker_threads: 8, custom_matches: rows(value), custom_match_position: 'head' },
    capabilities: {},
    custom_match_targets: options,
    custom_match_target_tags: tags,
  }
}
function mockSource(value = source()) {
  vi.mocked(oxidnsApi.getSettings).mockResolvedValue({ data: value } as any)
  vi.mocked(oxidnsApi.readCommonSettings).mockImplementation(async current => ({ data: projected(current as typeof value) }) as any)
  vi.mocked(oxidnsApi.applyCommonSettings).mockImplementation(async (current: any, changes) => {
    const next = JSON.parse(JSON.stringify(current))
    const doc = loadYaml(next.native_config) as any
    if ('custom_matches' in changes) doc.plugins.find((plugin: any) => plugin.tag === 'main').args = [...changes.custom_matches as Match[], { exec: 'accept' }]
    next.native_config = dumpYaml(doc)
    return { data: { settings: next, ...projected(next) } } as any
  })
  return value
}
async function select(editor: VueWrapper, label: string, value: string) {
  const control = editor.findAllComponents(Select).find(component => component.find(`[aria-label="${label}"]`).exists())
  expect(control, label).toBeDefined()
  control!.vm.$emit('update:modelValue', value)
  await flushPromises()
}
async function synchronize() {
  await vi.advanceTimersByTimeAsync(180)
  await flushPromises()
  await settleValidation()
}

describe('custom match actions in unified OxiDNS settings', () => {
  it('passes server targets including attached-file plugins through the settings page without changing existing actions', async () => {
    const originalRows = [existing(), { matches: ['qtype 28'], exec: '$unknown_executor', extra: { off: false } }]
    const original = mockSource(source(originalRows))
    const wrapper = await render()
    await click('高级')
    const common = wrapper.getComponent(OxidnsCommonSettings)
    const editor = wrapper.getComponent(OxidnsMatchEditor)
    expect(common.props('customMatchTargets')).toEqual(targets)
    expect(common.props('customMatchTargetTags')).toEqual(targetTags)
    expect(editor.props('targets')).toEqual(targets)
    expect(editor.props('targetTags')).toEqual(targetTags)
    expect(editor.get('[aria-label="匹配项 1 目标流程"]').text()).toContain('local_dns')
    expect((editor.get('[aria-label="匹配项 2 自定义动作"]').element as HTMLInputElement).value).toBe('$unknown_executor')
    expect(oxidnsApi.applyCommonSettings).not.toHaveBeenCalled()
    expect(editor.props('modelValue')).toEqual(originalRows)
    await click('保存草稿')
    expect(savedPayload()).toEqual(original)
  })

  it('requires explicit choices among multiple targets and saves the selected sequence and plugin with existing fields intact', async () => {
    const original = mockSource()
    const wrapper = await render()
    await click('高级')
    await click('添加匹配项')
    let editor = wrapper.getComponent(OxidnsMatchEditor)
    await editor.get('[aria-label="匹配项 2 条件 1"]').setValue('qname full:backup.test')
    await select(editor, '匹配项 2 执行动作', 'sequence')
    await synchronize()
    expect(button('保存草稿').disabled).toBe(true)
    expect(oxidnsApi.applyCommonSettings).not.toHaveBeenCalled()
    await select(editor, '匹配项 2 目标流程', 'goto local_dns_backup')
    await synchronize()
    const sequenceRows = [existing(), { matches: ['qname full:backup.test'], exec: 'goto local_dns_backup' }]
    expect(oxidnsApi.applyCommonSettings).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ native_config: original.native_config }), { custom_matches: sequenceRows }, 'oxi-profile')
    expect(button('保存草稿').disabled).toBe(false)

    await click('添加匹配项')
    editor = wrapper.getComponent(OxidnsMatchEditor)
    await editor.get('[aria-label="匹配项 3 条件 1"]').setValue('qname full:forward.test')
    await select(editor, '匹配项 3 执行动作', 'plugin')
    await synchronize()
    expect(button('保存草稿').disabled).toBe(true)
    expect(oxidnsApi.applyCommonSettings).toHaveBeenCalledTimes(1)
    await select(editor, '匹配项 3 目标插件', '$backup_forward')
    await synchronize()
    const expectedRows = [...sequenceRows, { matches: ['qname full:forward.test'], exec: '$backup_forward' }]
    expect(vi.mocked(oxidnsApi.applyCommonSettings).mock.calls.at(-1)?.[1]).toEqual({ custom_matches: expectedRows })
    await click('保存草稿')
    expect(rows(savedPayload() as { native_config: string })).toEqual(expectedRows)
    expect(savedPayload().additional_files).toEqual(original.additional_files)
    expect((loadYaml(savedPayload().native_config) as any).unknown).toEqual({ keep: [0, false, null] })
  })

  it('uses refreshed targets and the preserved action returned by native reconciliation', async () => {
    const original = mockSource()
    const updated = source([{ ...existing(), exec: 'goto office_dns' }])
    updated.native_config = updated.native_config.replace('tag: local_dns\n', 'tag: office_dns\n')
    const updatedTargets = targets.map(target => target.tag === 'local_dns' ? { ...target, tag: 'office_dns', exec: 'goto office_dns' } : target)
    const updatedTags = targetTags.map(tag => tag === 'local_dns' ? 'office_dns' : tag)
    vi.mocked(oxidnsApi.applyNativeSettings).mockResolvedValueOnce({ data: { settings: updated, ...projected(updated, updatedTargets, updatedTags), notices: [] } } as any)
    const wrapper = await render()
    await click('高级')
    await click('完整参数', views())
    const native = wrapper.getComponent(OxidnsNativeEditor)
    const reconcile = native.props('reconcile') as (before: string, candidate: string, renames?: Record<string, string>) => Promise<{ native_config: string }>
    const result = await reconcile(original.native_config, updated.native_config, { local_dns: 'office_dns' })
    native.vm.$emit('update:modelValue', result.native_config)
    await flushPromises()
    await settleValidation()
    await click('常用设置', views())
    const editor = wrapper.getComponent(OxidnsMatchEditor)
    expect(editor.props('targets')).toEqual(updatedTargets)
    expect(editor.props('targetTags')).toEqual(updatedTags)
    expect(editor.get('[aria-label="匹配项 1 目标流程"]').text()).toContain('office_dns')
    expect(editor.props('modelValue')).toEqual([{ ...existing(), exec: 'goto office_dns' }])
    expect(oxidnsApi.readCommonSettings).toHaveBeenCalledOnce()
    await click('保存草稿')
    expect(savedPayload()).toEqual(updated)
  })

  it('refreshes targets after editing an attached include without changing the main YAML or replacing its existing action', async () => {
    const original = mockSource(source([{ ...existing(), exec: 'goto local_dns_backup' }]))
    const changedContent = original.additional_files[0].content.replace('tag: local_dns_backup\n', 'tag: replacement_dns\n')
    const changedTargets = targets.map(target => target.tag === 'local_dns_backup'
      ? { ...target, tag: 'replacement_dns', exec: 'goto replacement_dns' } : target)
    const changedTags = targetTags.map(tag => tag === 'local_dns_backup' ? 'replacement_dns' : tag)
    vi.mocked(oxidnsApi.readCommonSettings).mockImplementation(async (current: any) => ({
      data: current.additional_files[0].content === changedContent
        ? projected(current, changedTargets, changedTags) : projected(current),
    }) as any)
    const wrapper = await render()
    await click('高级')
    expect(wrapper.getComponent(OxidnsMatchEditor).get('[aria-label="匹配项 1 目标流程"]').text()).toContain('local_dns_backup')
    await click('附加文件')
    ;(document.querySelector('[aria-label="展开文件 1"]') as HTMLButtonElement).click()
    await flushPromises()
    await input('[aria-label="文件 1 内容"]', changedContent)
    expect(oxidnsApi.readCommonSettings).toHaveBeenCalledTimes(2)
    expect(vi.mocked(oxidnsApi.readCommonSettings).mock.calls.at(-1)?.[0]).toMatchObject({
      native_config: original.native_config,
      additional_files: [{ ...original.additional_files[0], content: changedContent }],
    })
    await click('配置')
    await click('高级')
    const editor = wrapper.getComponent(OxidnsMatchEditor)
    expect(editor.props('targets')).toEqual(changedTargets)
    expect(editor.props('targetTags')).toEqual(changedTags)
    expect(editor.get('[aria-label="匹配项 1 目标流程"]').text()).toContain('local_dns_backup')
    expect(editor.get('[role="status"]').text()).toContain('目标「local_dns_backup」不在当前配置中')
    expect(editor.props('modelValue')).toEqual([{ ...existing(), exec: 'goto local_dns_backup' }])
    expect(oxidnsApi.applyCommonSettings).not.toHaveBeenCalled()
    expect(button('保存草稿').disabled).toBe(false)
    await click('保存草稿')
    expect(savedPayload().native_config).toBe(original.native_config)
    expect(savedPayload().additional_files).toEqual([{ ...original.additional_files[0], content: changedContent }])
  })

  it('replaces available targets when switching configuration profiles and clears omitted metadata', async () => {
    mockSource()
    const wrapper = await render()
    await click('高级')
    expect(wrapper.getComponent(OxidnsMatchEditor).props('targets')).toEqual(targets)
    const next = source([{ matches: ['qtype 1'], exec: 'goto branch_dns' }])
    next.native_config = next.native_config.replace('tag: local_dns\n', 'tag: branch_dns\n')
    const nextTargets = [{ tag: 'branch_dns', type: 'sequence', exec: 'goto branch_dns', source: 'config.yaml' }]
    vi.mocked(oxidnsApi.getSettings).mockResolvedValue({ data: next } as any)
    vi.mocked(oxidnsApi.readCommonSettings).mockResolvedValue({ data: projected(next, nextTargets, ['main', 'branch_dns']) } as any)
    await wrapper.setProps({ profileId: 'branch-profile' })
    await flushPromises()
    await click('高级')
    expect(wrapper.getComponent(OxidnsMatchEditor).props('targets')).toEqual(nextTargets)
    expect(wrapper.getComponent(OxidnsMatchEditor).props('targetTags')).toEqual(['main', 'branch_dns'])
    expect(wrapper.getComponent(OxidnsMatchEditor).get('[aria-label="匹配项 1 目标流程"]').text()).toContain('branch_dns')

    vi.mocked(oxidnsApi.getSettings).mockResolvedValue({ data: settings() } as any)
    vi.mocked(oxidnsApi.readCommonSettings).mockResolvedValue({ data: { values: { custom_matches: [] }, capabilities: {} } } as any)
    await wrapper.setProps({ profileId: 'empty-profile' })
    await flushPromises()
    await click('高级')
    expect(wrapper.getComponent(OxidnsMatchEditor).props('targets')).toEqual([])
    expect(wrapper.getComponent(OxidnsMatchEditor).props('targetTags')).toEqual([])
    expect(oxidnsApi.applyCommonSettings).not.toHaveBeenCalled()
  })
})
