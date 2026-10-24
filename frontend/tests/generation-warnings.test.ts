import { describe, expect, it } from 'vitest'
import { Blob } from 'node:buffer'
import { generationWarnings } from '@/lib/generationWarnings'

describe('generation compatibility metadata', () => {
  it('reads preview warnings without inserting them into generated content', () => {
    const response = { data: { content: 'rules:\n  - MATCH,DIRECT\n', warnings: ['MosDNS 不按设备组生成。'] } }
    const before = JSON.stringify(response)
    expect(generationWarnings(response)).toEqual(['MosDNS 不按设备组生成。'])
    expect(JSON.stringify(response)).toBe(before)
  })
  it('decodes the percent encoded JSON header independently of archive bytes', async () => {
    const blob = new Blob(['原始归档内容'])
    const bytes = await blob.text()
    expect(generationWarnings({ data: blob, headers: { 'x-configflow-warnings': encodeURIComponent(JSON.stringify(['Surge 不按设备组生成。', 'Surge 不按设备组生成。'])) } })).toEqual(['Surge 不按设备组生成。'])
    expect(await blob.text()).toBe(bytes)
    expect(generationWarnings({ headers: { get: () => encodeURIComponent(JSON.stringify(['Mihomo 需要 1.19.1 或更新内核。'])) } })).toEqual(['Mihomo 需要 1.19.1 或更新内核。'])
  })
  it('leaves old responses and malformed optional metadata usable', () => {
    expect(generationWarnings({ data: 'rules: []' })).toEqual([])
    expect(generationWarnings({ headers: { 'x-configflow-warnings': '%invalid' } })).toEqual([])
    expect(generationWarnings({ data: { warnings: [false, '中文提醒', null, ''] } })).toEqual(['中文提醒'])
  })
})
