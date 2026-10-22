import { describe, expect, it } from 'vitest'

import { values, render, input, lastChange } from './helpers/oxidns-common-settings'

const authNote = () => document.querySelector('[aria-labelledby="oxi-api-auth-title"]')?.textContent || ''

describe('OxiDNS management API access', () => {
  it('opens the API tab and offers optional credentials while the API listens locally', async () => {
    const wrapper = await render({ initialFeature: 'api' })
    expect(wrapper.findAll('[role="tab"]').find(tab => tab.text() === 'API / 日志')!.attributes('data-state')).toBe('active')
    expect(authNote()).toContain('访问认证（可选）')
    expect(authNote()).toContain('未设置')
    expect(authNote()).toContain('当前仅本机可访问 API')
    expect(document.getElementById('oxi-api.username')).not.toBeNull()
    expect(wrapper.emitted('change')).toBeUndefined()
  })

  it('warns when the API is reachable from the network without credentials', async () => {
    await render({ initialFeature: 'api', values: {...values(), api: {enabled: true, listen: '0.0.0.0:9199'}} })
    expect(authNote()).toContain('建议设置用户名和密码')
    expect(document.querySelector('[aria-labelledby="oxi-api-auth-title"] [role="status"]')).not.toBeNull()
  })

  it('requires the username and password together and accepts the pair', async () => {
    const wrapper = await render({ initialFeature: 'api' })
    await input('oxi-api.username', 'admin')
    expect(wrapper.emitted('error')?.at(-1)?.[0]).toBe('还需填写密码；不需要认证时请清空用户名')
    expect(document.querySelector('[aria-labelledby="oxi-api-auth-title"] [role="alert"]')).toBeNull()
    await input('oxi-api.password', 'secret')
    expect(wrapper.emitted('error')?.at(-1)?.[0]).toBe('')
    expect(lastChange(wrapper)).toEqual({'api.password': 'secret'})
    expect(authNote()).toContain('已设置账号')
    await input('oxi-api.username', 'ad:min')
    expect(wrapper.emitted('error')?.at(-1)?.[0]).toBe('用户名不能包含英文冒号')
  })

  it('hides API details and ignores their errors while the API is off', async () => {
    const wrapper = await render({ initialFeature: 'api', values: {...values(), api: {enabled: false, listen: '127.0.0.1:9199', username: 'admin', password: ''}} })
    expect(document.getElementById('oxi-api.listen')).toBeNull()
    expect(document.getElementById('oxi-api.username')).toBeNull()
    expect(wrapper.emitted('error')?.at(-1)?.[0]).toBe('')
  })
  it('reports saved credentials the Agent cannot forward as soon as the form opens', async () => {
    const wrapper = await render({ initialFeature: 'api', values: {...values(), api: {enabled: true, listen: '127.0.0.1:9199', username: 'admin', password: '${PASS}'}} })
    expect(wrapper.emitted('error')?.at(-1)?.[0]).toContain('不能包含 ${')
    await input('oxi-api.password', 'pa\u0085ss')
    expect(wrapper.emitted('error')?.at(-1)?.[0]).toBe('用户名和密码不能包含控制字符')
  })

  it('does not block the editor on credentials the form cannot edit', async () => {
    const wrapper = await render({ initialFeature: 'api', values: {...values(), api: {enabled: true, listen: '127.0.0.1:9199', username: 'admin', password: ''}},
      capabilities: {'api.username': {editable: false, reason: '管理 API 使用自定义认证'}, 'api.password': {editable: false, reason: '管理 API 使用自定义认证'}} })
    expect(wrapper.emitted('error')?.at(-1)?.[0] ?? '').toBe('')
  })

  it('unlocks native API arguments in the same edit that enables the API', async () => {
    const locked = {editable: false, reason: '请先启用 API / WebUI，再编辑其参数', target: 'api.http'}
    const wrapper = await render({ initialFeature: 'api', values: {...values(), api: {enabled: false, listen: '', username: '', password: ''}},
      capabilities: {'api.enabled': {editable: true}, 'api.listen': locked, 'api.username': locked, 'api.password': locked} })
    document.getElementById('oxi-api.enabled')!.click()
    await new Promise(resolve => setTimeout(resolve))
    expect(lastChange(wrapper)).toEqual({'api.enabled': true})
    expect(authNote()).not.toContain('暂不可编辑')
    await input('oxi-api.username', 'admin')
    expect(lastChange(wrapper)).toEqual({'api.username': 'admin'})
  })
})
