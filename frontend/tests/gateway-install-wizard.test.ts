import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import GatewayInstallWizard from '@/components/agents/GatewayInstallWizard.vue'
import SettingsWorkspace from '@/components/generate/SettingsWorkspace.vue'
import { Checkbox } from '@/components/ui/checkbox'
import { gatewayOnboardingApi } from '@/api/gatewayOnboarding'
import { linuxGatewayApi } from '@/api/linuxGateway'
import { gatewayEnvironmentErrors, installationMatches } from '@/lib/gatewayOnboarding'
vi.mock('@/api/gatewayOnboarding', () => ({ gatewayOnboardingApi: { get: vi.fn(), preview: vi.fn(), save: vi.fn(), create: vi.fn(), sessions: vi.fn(), session: vi.fn(), publish: vi.fn() } }))
vi.mock('@/api/linuxGateway', () => ({ linuxGatewayApi: { status: vi.fn(), preview: vi.fn(), apply: vi.fn(), remove: vi.fn() } }))
vi.mock('@/lib/feedback', () => ({ notify: { success: vi.fn(), error: vi.fn() } }))
const response = (data: unknown) => ({ data }) as any
const spec = { interface: 'eth0', address: '192.168.1.2', lan_cidr: '192.168.1.0/24', upstream_gateway: '192.168.1.1' }
const settings = { rule_set_ids: ['ip'], direct_rule_set_ids: ['cn'], proxy_rule_set_ids: ['global'], local_dns: 'https://223.5.5.5/dns-query' }
const config = () => ({ profile_id: 'office', config_revision: 7, settings, fake_ip_range: '198.18.0.0/16', oxidns_mode: 'template', warnings: [], blockers: [], can_prepare: true,
  ip_candidates: [{ id: 'ip', name: 'Telegram IP', behavior: 'ipcidr', available: true, enabled: true }],
  domain_candidates: [{ id: 'cn', name: '国内域名', behavior: 'domain', available: true, enabled: true }, { id: 'global', name: '国外域名', behavior: 'domain', available: true, enabled: true }],
})
const session = (phase = 'waiting', extra = {}) => ({ id: 'installation-1', profile_id: 'office', name: 'home-gateway', network: { ...spec }, server_url: 'http://192.168.1.3:8000', command: 'curl https://example.test/install | sudo bash', expires_at: '2026-10-20T10:00:00Z', phase,
  agents: phase === 'waiting' ? {} : { mihomo: { id: 'new-mihomo', name: 'Home Mihomo', host: spec.address, port: 8080, profile_id: 'office', service_type: 'mihomo', status: 'online' }, oxidns: { id: 'new-oxidns', name: 'Home OxiDNS', host: spec.address, port: 8082, profile_id: 'office', service_type: 'oxidns', status: 'online' } }, deployments: {}, ...extra,
}) as any
const workspaceStub = defineComponent({ name: 'SettingsWorkspace', props: ['open', 'dirty', 'busy'], emits: ['update:open'], setup(_props, { slots }) { return () => h('section', [slots.default?.(), slots.footer?.()]) } })
const buttonStub = defineComponent({ name: 'Button', setup(_props, { slots, attrs }) { return () => h('button', attrs, slots.default?.()) } })
const wrappers: ReturnType<typeof shallowMount>[] = []
function button(wrapper: ReturnType<typeof shallowMount>, text: string) { return wrapper.findAll('button').find(item => item.text() === text)! }
function stepButton(wrapper: ReturnType<typeof shallowMount>, index: number) { return wrapper.get('ol[aria-label="安装步骤"]').findAll('button')[index] }
async function render() {
  const wrapper = shallowMount(GatewayInstallWizard, { props: { open: true, profileId: 'office', profileName: '办公室' }, global: { renderStubDefaultSlot: true, stubs: { SettingsWorkspace: workspaceStub, Button: buttonStub, Input: false, Textarea: false } } })
  wrappers.push(wrapper); await flushPromises(); return wrapper
}
async function fillEnvironment(wrapper: ReturnType<typeof shallowMount>) {
  for (const [id, value] of Object.entries({ name: 'home-gateway', server: 'http://192.168.1.3:8000', address: spec.address, upstream: spec.upstream_gateway, interface: spec.interface, cidr: spec.lan_cidr })) await wrapper.get(`#onboarding-${id}`).setValue(value)
  await button(wrapper, '下一步：选择规则').trigger('click'); await flushPromises()
}
async function prepare(wrapper: ReturnType<typeof shallowMount>) {
  await fillEnvironment(wrapper)
  await button(wrapper, '检查配置与代理出口').trigger('click'); await flushPromises()
  await button(wrapper, '保存并生成安装命令').trigger('click'); await flushPromises()
}
function deferred<T>() { let resolve!: (data: T) => void; const promise = new Promise<T>(done => { resolve = done }); return { resolve, promise } }
beforeEach(() => {
  vi.resetAllMocks(); localStorage.clear()
  vi.mocked(gatewayOnboardingApi.get).mockResolvedValue(response(config()))
  vi.mocked(gatewayOnboardingApi.sessions).mockResolvedValue(response({ success: true, session: null }))
  vi.mocked(gatewayOnboardingApi.preview).mockResolvedValue(response({ success: true, ready: true, blockers: [], warnings: [], changes: ['启用选择性网关', '配置 OxiDNS'] }))
  vi.mocked(gatewayOnboardingApi.save).mockResolvedValue(response({ success: true, ready: true, config_revision: 8, blockers: [], warnings: [], changes: [] }))
  vi.mocked(gatewayOnboardingApi.create).mockResolvedValue(response(session()))
  vi.mocked(gatewayOnboardingApi.session).mockResolvedValue(response(session('enrolled')))
  vi.mocked(gatewayOnboardingApi.publish).mockResolvedValue(response(session('publishing')))
  vi.mocked(linuxGatewayApi.status).mockResolvedValue(response({ success: true, enabled_in_config: true, supported: true, managed: { active: false } }))
  vi.mocked(linuxGatewayApi.preview).mockResolvedValue(response({ success: true, ready: true, blockers: [], warnings: [], commands: ['设置 IP 转发', '创建本向导专用 nftables 表'], spec, managed: { active: false } }))
  vi.mocked(linuxGatewayApi.apply).mockResolvedValue(response({ success: true, managed: { active: true } }))
  vi.mocked(linuxGatewayApi.remove).mockResolvedValue(response({ success: true, managed: { active: false } }))
})
afterEach(() => { wrappers.splice(0).forEach(item => item.unmount()); vi.useRealTimers(); localStorage.clear() })
describe('gateway installation wizard', () => {
  it('does no writes on entry, and validates environment before continuing', async () => {
    const wrapper = await render()
    expect(button(wrapper, '下一步：选择规则').attributes('disabled')).toBeDefined()
    expect(gatewayOnboardingApi.save).not.toHaveBeenCalled()
    expect(gatewayOnboardingApi.create).not.toHaveBeenCalled()
    expect(linuxGatewayApi.apply).not.toHaveBeenCalled()
    await fillEnvironment(wrapper)
    expect(wrapper.text()).toContain('自动包含 fake-IP 网段')
    expect(wrapper.getComponent(SettingsWorkspace).props('dirty')).toBe(true)
    expect(button(wrapper, '保存并生成安装命令').attributes('disabled')).toBeDefined()
  })
  it('prefills the service address from system settings', async () => {
    vi.mocked(gatewayOnboardingApi.get).mockResolvedValue(response({ ...config(), server_url: 'https://configflow.example.test' }))
    const wrapper = await render()
    expect(wrapper.get('#onboarding-server').element).toHaveProperty('value', 'https://configflow.example.test')
  })
  it('does not overwrite an address being edited while settings load', async () => {
    const pending = deferred<any>()
    vi.mocked(gatewayOnboardingApi.get).mockReturnValue(pending.promise)
    const wrapper = await render()
    await wrapper.get('#onboarding-server').setValue('https://entered.example.test')
    pending.resolve(response({ ...config(), server_url: 'https://stored.example.test' })); await flushPromises()
    expect(wrapper.get('#onboarding-server').element).toHaveProperty('value', 'https://entered.example.test')
  })
  it('requires preview, saves both configs and creates installation from the saved revision', async () => {
    const wrapper = await render(); await prepare(wrapper)
    expect(gatewayOnboardingApi.preview).toHaveBeenCalledWith('office', { ...settings, confirm_template_conversion: false, expected_config_revision: 7 })
    expect(gatewayOnboardingApi.save).toHaveBeenCalledWith('office', { ...settings, confirm_template_conversion: false, expected_config_revision: 7 })
    expect(gatewayOnboardingApi.create).toHaveBeenCalledWith({ profile_id: 'office', name: 'home-gateway', server_url: 'http://192.168.1.3:8000', network: spec, config_revision: 8 })
    expect(wrapper.get('#onboarding-command').element).toHaveProperty('value', session().command)
    expect(gatewayOnboardingApi.publish).not.toHaveBeenCalled()
    expect(linuxGatewayApi.apply).not.toHaveBeenCalled()
    expect(localStorage.getItem('configflow.gateway-installation.office')).toBe('installation-1')
    expect(localStorage.length).toBe(1)
  })
  it('allows correcting network information immediately after creating a session', async () => {
    const wrapper = await render(); await prepare(wrapper)
    await button(wrapper, '修改安装信息 / 准备另一台设备').trigger('click'); await flushPromises()
    expect(wrapper.get('#onboarding-address').element).toHaveProperty('value', spec.address)
    expect(wrapper.get('#onboarding-interface').element).toHaveProperty('value', spec.interface)
    expect(gatewayOnboardingApi.create).toHaveBeenCalledTimes(1)
    expect(linuxGatewayApi.apply).not.toHaveBeenCalled()
  })
  it('allows clearing a selected ruleset that was removed from the library', async () => {
    vi.mocked(gatewayOnboardingApi.get).mockResolvedValue(response({ ...config(), settings: { ...settings, rule_set_ids: ['removed'] } }))
    const wrapper = await render(); await fillEnvironment(wrapper)
    expect(wrapper.text()).toContain('已移除的规则集（removed）')
    const boxes = wrapper.findAllComponents(Checkbox)
    expect(boxes[1].props('disabled')).toBe(false)
    boxes[1].vm.$emit('update:modelValue', false); await flushPromises()
    await button(wrapper, '检查配置与代理出口').trigger('click'); await flushPromises()
    expect(vi.mocked(gatewayOnboardingApi.preview).mock.lastCall?.[1].rule_set_ids).toEqual([])
  })
  it('explains unmatched searches without clearing existing rule selections', async () => {
    const wrapper = await render(); await fillEnvironment(wrapper)
    await wrapper.get('#onboarding-search').setValue('no-such-gateway-ruleset')
    expect(wrapper.findAll('p').filter(item => item.text().includes('没有匹配的规则集'))).toHaveLength(3)
    expect(wrapper.text()).not.toContain('Telegram IP')
    expect(wrapper.text()).not.toContain('当前没有已引用的规则集')
    await button(wrapper, '检查配置与代理出口').trigger('click'); await flushPromises()
    expect(vi.mocked(gatewayOnboardingApi.preview).mock.lastCall?.[1]).toMatchObject(settings)
    await wrapper.get('#onboarding-search').setValue('')
    expect(wrapper.text()).toContain('Telegram IP')
    expect(wrapper.text()).not.toContain('没有匹配的规则集')
  })
  it('guides empty profiles through the rule library and profile rule references', async () => {
    vi.mocked(gatewayOnboardingApi.get).mockResolvedValue(response({ ...config(), ip_candidates: [], domain_candidates: [],
      settings: { ...settings, rule_set_ids: [], direct_rule_set_ids: [], proxy_rule_set_ids: [] } }))
    const wrapper = await render(); await fillEnvironment(wrapper)
    expect(wrapper.text()).toContain('「规则库」添加')
    expect(wrapper.text()).toContain('「策略规则」引用并启用')
    expect(wrapper.text()).not.toContain('没有匹配的规则集')
  })
  it('shows installation expiry in local time instead of raw server timestamps', async () => {
    const expiresAt = '2026-10-20T06:45:38.242868+00:00'
    vi.mocked(gatewayOnboardingApi.sessions).mockResolvedValue(response({ success: true, session: session('waiting', { expires_at: expiresAt }) }))
    const wrapper = await render()
    expect(wrapper.text()).toContain(new Date(expiresAt).toLocaleString())
    expect(wrapper.text()).toContain('本地时间')
    expect(wrapper.text()).not.toContain(expiresAt)
  })
  it.each([undefined, 'not-a-date'])('keeps missing or invalid installation expiry %s understandable', async (expiresAt) => {
    vi.mocked(gatewayOnboardingApi.sessions).mockResolvedValue(response({ success: true, session: session('waiting', { expires_at: expiresAt }) }))
    const wrapper = await render()
    expect(wrapper.text()).toContain('命令有效期暂时无法确认')
    expect(wrapper.text()).not.toContain('Invalid Date')
    expect(wrapper.text()).not.toContain('undefined')
  })
  it('moves overlapping domain choices to one destination and invalidates the preview', async () => {
    const wrapper = await render(); await fillEnvironment(wrapper)
    await button(wrapper, '检查配置与代理出口').trigger('click'); await flushPromises()
    wrapper.findAllComponents(Checkbox)[3].vm.$emit('update:modelValue', true)
    await flushPromises()
    expect(button(wrapper, '保存并生成安装命令').attributes('disabled')).toBeDefined()
    await button(wrapper, '检查配置与代理出口').trigger('click'); await flushPromises()
    expect(vi.mocked(gatewayOnboardingApi.preview).mock.lastCall?.[1]).toMatchObject({ direct_rule_set_ids: [], proxy_rule_set_ids: ['global', 'cn'] })
  })
  it('publishes exact enrolled pair, then requires network preview and explicit apply', async () => {
    const wrapper = await render(); await prepare(wrapper)
    await button(wrapper, '检查安装进度').trigger('click'); await flushPromises()
    await button(wrapper, '按顺序发布两项配置').trigger('click'); await flushPromises()
    expect(gatewayOnboardingApi.publish).toHaveBeenCalledExactlyOnceWith('installation-1', 'office', false)
    expect(linuxGatewayApi.preview).not.toHaveBeenCalled()
    vi.mocked(gatewayOnboardingApi.session).mockResolvedValue(response(session('published')))
    await button(wrapper, '刷新进度').trigger('click'); await flushPromises()
    expect(button(wrapper, '应用网关转发').attributes('disabled')).toBeDefined()
    await button(wrapper, '检查并预览转发').trigger('click'); await flushPromises()
    expect(linuxGatewayApi.preview).toHaveBeenCalledWith('new-mihomo', 'office', spec)
    expect(linuxGatewayApi.apply).not.toHaveBeenCalled()
    await button(wrapper, '应用网关转发').trigger('click'); await flushPromises()
    expect(linuxGatewayApi.apply).toHaveBeenCalledExactlyOnceWith('new-mihomo', 'office', spec)
    expect(wrapper.text()).toContain('网关转发已启用')
  })
  it.each([2, 3])('can return directly to verification after reviewing completed step %s', async (reviewStep) => {
    vi.mocked(gatewayOnboardingApi.sessions).mockResolvedValue(response({ success: true, session: session('published') }))
    const wrapper = await render()
    expect(stepButton(wrapper, 4).attributes('aria-current')).toBe('step')
    await stepButton(wrapper, reviewStep).trigger('click'); await flushPromises()
    expect(stepButton(wrapper, reviewStep).attributes('aria-current')).toBe('step')
    // Reviewing completed work must not require a status refresh to unlock it again.
    expect(stepButton(wrapper, 4).attributes('disabled')).toBeUndefined()
    await stepButton(wrapper, 4).trigger('click'); await flushPromises()
    expect(wrapper.find('section[aria-label="启用与验证"]').exists()).toBe(true)
    expect(gatewayOnboardingApi.session).not.toHaveBeenCalled()
    expect(gatewayOnboardingApi.publish).not.toHaveBeenCalled()
    expect(linuxGatewayApi.apply).not.toHaveBeenCalled()
  })
  it.each(['waiting', 'enrolled', 'publishing', 'publish_failed'])('keeps verification unavailable while the installation is %s', async (phase) => {
    vi.mocked(gatewayOnboardingApi.sessions).mockResolvedValue(response({ success: true, session: session(phase) }))
    const wrapper = await render()
    expect(stepButton(wrapper, 4).attributes('disabled')).toBeDefined()
    await stepButton(wrapper, 4).trigger('click'); await flushPromises()
    expect(wrapper.find('section[aria-label="启用与验证"]').exists()).toBe(false)
    expect(linuxGatewayApi.preview).not.toHaveBeenCalled()
    expect(linuxGatewayApi.apply).not.toHaveBeenCalled()
  })
  it.each(['enrolled', 'publishing'])('keeps manual review open through unchanged %s polls, then advances on completion', async (phase) => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    vi.mocked(gatewayOnboardingApi.sessions).mockResolvedValue(response({ success: true, session: session(phase) }))
    vi.mocked(gatewayOnboardingApi.session).mockResolvedValue(response(session(phase)))
    const wrapper = await render()
    await stepButton(wrapper, 2).trigger('click'); await flushPromises()
    for (let count = 1; count <= 2; count++) {
      await vi.advanceTimersByTimeAsync(4000); await flushPromises()
      expect(gatewayOnboardingApi.session).toHaveBeenCalledTimes(count)
      expect(wrapper.find('section[aria-label="安装核心"]').exists()).toBe(true)
      expect(stepButton(wrapper, 2).attributes('aria-current')).toBe('step')
    }
    // A real phase transition still brings the user to the newly available next action.
    vi.mocked(gatewayOnboardingApi.session).mockResolvedValue(response(session('published')))
    await vi.advanceTimersByTimeAsync(4000); await flushPromises()
    expect(wrapper.find('section[aria-label="启用与验证"]').exists()).toBe(true)
    expect(linuxGatewayApi.apply).not.toHaveBeenCalled()
  })
  it('keeps restoration guidance after removal and stops instructing clients to use the Linux gateway', async () => {
    const published = session('published')
    vi.mocked(gatewayOnboardingApi.sessions).mockResolvedValue(response({ success: true, session: published }))
    vi.mocked(gatewayOnboardingApi.session).mockResolvedValue(response(published))
    vi.mocked(linuxGatewayApi.status)
      .mockResolvedValue(response({ success: true, managed: { active: false } }))
      .mockResolvedValueOnce(response({ success: true, managed: { active: true } }))
    const wrapper = await render()
    expect(wrapper.text()).toContain('客户端接入检查')
    await button(wrapper, '撤销本次网关转发').trigger('click'); await flushPromises()
    expect(linuxGatewayApi.remove).toHaveBeenCalledExactlyOnceWith('new-mihomo', 'office')
    const showsRestoration = () => /(?:网关转发已撤销|已撤销.*网关转发)/.test(wrapper.text())
    expect.soft(showsRestoration(), 'Restoration must remain visible in the workspace after the toast disappears').toBe(true)
    expect.soft(wrapper.text().includes('都填为'), 'Removed networking must not instruct clients to point gateway and DNS at Linux').toBe(false)
    expect.soft(wrapper.text().includes('客户端接入检查')).toBe(false)
    // A read-only refresh must keep the successful removal guidance visible.
    await button(wrapper, '重新读取网络状态').trigger('click'); await flushPromises()
    expect(showsRestoration()).toBe(true)
    expect(wrapper.text()).toMatch(/恢复.{0,16}(?:原|之前)|(?:原|之前).{0,16}(?:网关|DNS)/)
    expect(linuxGatewayApi.apply).not.toHaveBeenCalled()
    await button(wrapper, '检查并预览转发').trigger('click'); await flushPromises()
    await button(wrapper, '应用网关转发').trigger('click'); await flushPromises()
    expect(linuxGatewayApi.apply).toHaveBeenCalledExactlyOnceWith('new-mihomo', 'office', spec)
    expect(showsRestoration()).toBe(false)
    expect(wrapper.text()).toContain('客户端接入检查')
  })
  it('recovers an unfinished session without reinstalling, publishing, or applying', async () => {
    vi.mocked(gatewayOnboardingApi.sessions).mockResolvedValue(response({ success: true, session: session('publishing') }))
    const wrapper = await render()
    expect(wrapper.text()).toContain('已恢复')
    expect(gatewayOnboardingApi.create).not.toHaveBeenCalled()
    expect(gatewayOnboardingApi.publish).not.toHaveBeenCalled()
    expect(linuxGatewayApi.apply).not.toHaveBeenCalled()
    expect(button(wrapper, '继续确认发布').attributes('disabled')).toBeUndefined()
  })
  it('stops on failure and lets explicit retry continue same session', async () => {
    vi.mocked(gatewayOnboardingApi.sessions).mockResolvedValue(response({ success: true, session: session('publish_failed', { error: 'Mihomo 健康检查失败' }) }))
    const wrapper = await render()
    expect(wrapper.text()).toContain('Mihomo 健康检查失败')
    expect(button(wrapper, '应用网关转发')).toBeUndefined()
    await button(wrapper, '处理后继续发布').trigger('click'); await flushPromises()
    expect(gatewayOnboardingApi.publish).toHaveBeenCalledWith('installation-1', 'office', true)
    expect(linuxGatewayApi.apply).not.toHaveBeenCalled()
  })
  it('rejects changed target identity before applying', async () => {
    vi.mocked(gatewayOnboardingApi.sessions).mockResolvedValue(response({ success: true, session: session('published') }))
    const wrapper = await render()
    await button(wrapper, '检查并预览转发').trigger('click'); await flushPromises()
    const wrong = session('published'); wrong.agents.mihomo.id = 'different-device'
    vi.mocked(gatewayOnboardingApi.session).mockResolvedValue(response(wrong))
    await button(wrapper, '应用网关转发').trigger('click'); await flushPromises()
    expect(linuxGatewayApi.apply).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('设备绑定已改变')
  })
  it('reuses a saved revision when installation creation response is lost', async () => {
    vi.mocked(gatewayOnboardingApi.create).mockRejectedValueOnce(new Error('连接中断'))
    const wrapper = await render(); await prepare(wrapper)
    expect(wrapper.text()).toContain('连接中断')
    await button(wrapper, '保存并生成安装命令').trigger('click'); await flushPromises()
    expect(gatewayOnboardingApi.save).toHaveBeenCalledTimes(1)
    expect(gatewayOnboardingApi.create).toHaveBeenCalledTimes(2)
    expect(vi.mocked(gatewayOnboardingApi.create).mock.calls.map(item => item[0].config_revision)).toEqual([8, 8])
  })
  it.each(['config_changed', 'target_changed'])('blocks changed installation %s before forwarding', async (flag) => {
    vi.mocked(gatewayOnboardingApi.sessions).mockResolvedValue(response({ success: true, session: session('published', { [flag]: true }) }))
    const wrapper = await render()
    expect(wrapper.text()).toContain('已停止发布与转发操作')
    expect(button(wrapper, '检查并预览转发').attributes('disabled')).toBeDefined()
    expect(button(wrapper, '应用网关转发').attributes('disabled')).toBeDefined()
    expect(linuxGatewayApi.apply).not.toHaveBeenCalled()
  })
  it('allows registered installations to recover after only the command expires', async () => {
    const expired = session('published', { expired: true })
    vi.mocked(gatewayOnboardingApi.sessions).mockResolvedValue(response({ success: true, session: expired }))
    vi.mocked(gatewayOnboardingApi.session).mockResolvedValue(response(expired))
    const wrapper = await render()
    expect(linuxGatewayApi.status).toHaveBeenCalledWith('new-mihomo', 'office')
    expect(button(wrapper, '检查并预览转发').attributes('disabled')).toBeUndefined()
    await button(wrapper, '检查并预览转发').trigger('click'); await flushPromises()
    await button(wrapper, '应用网关转发').trigger('click'); await flushPromises()
    expect(linuxGatewayApi.apply).toHaveBeenCalledWith('new-mihomo', 'office', spec)
  })
  it('can inspect and restore owned networking when the configuration changed', async () => {
    const stale = session('published', { config_changed: true, target_changed: true })
    vi.mocked(gatewayOnboardingApi.sessions).mockResolvedValue(response({ success: true, session: stale }))
    vi.mocked(gatewayOnboardingApi.session).mockResolvedValue(response(stale))
    vi.mocked(linuxGatewayApi.status).mockResolvedValue(response({ success: true, managed: { active: true } }))
    const wrapper = await render()
    expect(linuxGatewayApi.status).toHaveBeenCalledWith('new-mihomo', 'office')
    expect(button(wrapper, '应用网关转发').attributes('disabled')).toBeDefined()
    await button(wrapper, '撤销本次网关转发').trigger('click'); await flushPromises()
    expect(linuxGatewayApi.remove).toHaveBeenCalledWith('new-mihomo', 'office')
  })
  it('requires a new preparation when the command expired before enrollment', async () => {
    vi.mocked(gatewayOnboardingApi.sessions).mockResolvedValue(response({ success: true, session: session('waiting', { expired: true }) }))
    const wrapper = await render()
    expect(wrapper.text()).toContain('安装凭据已过期')
    expect(button(wrapper, '复制安装命令').attributes('disabled')).toBeDefined()
  })
  it('can resume a publishing session with the same server job after reconnect', async () => {
    vi.mocked(gatewayOnboardingApi.sessions).mockResolvedValue(response({ success: true, session: session('publishing') }))
    const wrapper = await render()
    await button(wrapper, '继续确认发布').trigger('click'); await flushPromises()
    expect(gatewayOnboardingApi.publish).toHaveBeenCalledWith('installation-1', 'office', false)
  })
  it('ignores a delayed response after leaving fixed profile', async () => {
    const pending = deferred<any>()
    vi.mocked(gatewayOnboardingApi.preview).mockReturnValue(pending.promise)
    const wrapper = await render(); await fillEnvironment(wrapper)
    await button(wrapper, '检查配置与代理出口').trigger('click')
    await wrapper.setProps({ open: false })
    pending.resolve(response({ success: true, ready: true, blockers: [], warnings: [], changes: [] })); await flushPromises()
    expect(gatewayOnboardingApi.save).not.toHaveBeenCalled()
    expect(gatewayOnboardingApi.create).not.toHaveBeenCalled()
  })
  it('shows blockers and refuses commands after failed preview', async () => {
    vi.mocked(gatewayOnboardingApi.preview).mockResolvedValue(response({ success: true, ready: false, blockers: ['没有可用的代理节点'], warnings: [], changes: [] }))
    const wrapper = await render(); await fillEnvironment(wrapper)
    await button(wrapper, '检查配置与代理出口').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('没有可用的代理节点')
    expect(button(wrapper, '保存并生成安装命令').attributes('disabled')).toBeDefined()
    expect(gatewayOnboardingApi.create).not.toHaveBeenCalled()
  })
})
describe('gateway installation boundaries', () => {
  it('validates server origin and unambiguous target identity', () => {
    expect(gatewayEnvironmentErrors('home', 'http://192.168.1.3:8000', spec)).toEqual([])
    for (const server of ['http://localhost:8000', 'file:///etc/passwd', 'https://name:secret@example.com', 'https://example.com/path']) expect(gatewayEnvironmentErrors('home', server, spec).length).toBeGreaterThan(0)
    expect(installationMatches(session('published'), 'office')).toBe(true)
    expect(installationMatches(session('published'), 'other')).toBe(false)
    const wrong = session('published'); wrong.agents.mihomo.host = '192.168.1.99'
    expect(installationMatches(wrong, 'office')).toBe(false)
  })
})
