// Run on the Linux test server: actual Vue components, controlled mock API.
// No real Agent, router, or production ConfigFlow endpoint is contacted.
import assert from 'node:assert/strict'
import { cp, mkdir, mkdtemp, readFile, rm, symlink, writeFile } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { networkInterfaces } from 'node:os'
import { createHash } from 'node:crypto'

assert.ok(Object.values(networkInterfaces()).flat().some(item => item?.address === '192.168.0.32'), 'Run this test on the test server')
const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '..')
// rt may sync the Mac editor symlink while another remote command is running.
// Resolve the already-installed lockfile cache directly, then isolate source.
const lockHash = createHash('sha256').update(await readFile(join(frontend, 'package-lock.json'))).digest('hex').slice(0, 16)
const dependencies = process.env.CF_NODE_MODULES || `/home/sunbroke/cf-test/node_modules/${lockHash}/node_modules`
async function dependency(name) {
  const root = join(dependencies, name)
  const pkg = JSON.parse(await readFile(join(root, 'package.json'), 'utf8'))
  const entry = pkg.exports['.'].import || pkg.exports['.'].default
  return import(join(root, typeof entry === 'string' ? entry : entry.default))
}
const { createServer } = await dependency('vite')
const { default: vue } = await dependency('@vitejs/plugin-vue')
const { default: tailwindcss } = await dependency('@tailwindcss/vite')
const output = resolve(frontend, '../.ci-results/ipv6-ui')
await mkdir(output, { recursive: true })
const scratch = await mkdtemp('/tmp/configflow-ipv6-ui-')
await cp(join(frontend, 'src'), join(scratch, 'src'), { recursive: true })
await symlink(dependencies, join(scratch, 'node_modules'))
await writeFile(join(scratch, 'index.html'), '<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="app"></div><script type="module" src="/main.ts"></script></body></html>')
await writeFile(join(scratch, 'main.ts'), `import { createApp } from 'vue'; import './src/styles/theme.css'; import './src/styles/tokens.css'; import Harness from './Harness.vue'; createApp(Harness).mount('#app')`)
await writeFile(join(scratch, 'Harness.vue'), `<script setup lang="ts">
import { ref } from 'vue'
import MihomoSettings from './src/components/generate/MihomoSettings.vue'
const open = ref(true)
</script><template><button style="padding:16px" @click="open = true">打开设置</button><MihomoSettings v-model:open="open" profile-id="bound" preferred-agent-id="gw" context-label="家庭配置 · 客厅 Mihomo 网关（IPv4 分流，IPv6 节点出口）" /></template>`)

const initialYAML = '# 保留用户注释\nipv6: true\nmixed-port: 7890\ndns:\n  enable: true\n  ipv6: false # 单独控制 AAAA 答案\n  listen: 0.0.0.0:1053\n  enhanced-mode: fake-ip\n  fake-ip-range: 198.18.0.1/16\ntun:\n  enable: true\n  auto-route: true\n'
const target = { id: 'gw', name: '客厅 Mihomo 网关（保持 IPv4 分流）', service_type: 'mihomo', profile_id: 'bound', status: 'online', enabled: true }
function readiness(prepared = false) {
  return { success: true, agent_id: 'gw', profile_id: 'bound', config_revision: 7, host: {},
    configuration: { ipv6: true, dns_ipv6: false, fake_ipv6_range: null, errors: [] },
    checks: [
      { id: 'configuration', label: '已保存的 IPv6 设置', status: 'ok', message: '已读取保存草稿；设备生效配置需要另行发布。' },
      { id: 'host_interface', label: '上联网卡', status: 'ok', message: '实际网卡为 eth0，已取得可用的 IPv6 地址。' },
      { id: 'host_default_route', label: '默认路由与 RA', status: 'ok', message: '通过 ROS 的 RA 自动获得默认路由，有效期正常更新。' },
      { id: 'host_persistence', label: '重启恢复配置', status: prepared ? 'ok' : 'warning', message: prepared ? '已保存恢复配置，尚未实测重启。' : '需要保存 eth0 接受 RA 的设置，保证开启转发后仍能获得默认网关。', details: ['将保存 net.ipv6.conf.eth0.accept_ra=2，保留已有主机网络配置。'] },
    ],
    routers: [{ id: 'ros', name: '家庭 ROS', status: 'warning', can_prepare: false, checks: [
      { id: 'dns', label: 'DNS 路径', status: 'warning', message: '检测到上游 DNS。请核对是否仍统一使用 OxiDNS；不会自动关闭 use-peer-dns。' },
      { id: 'fake4_0', label: 'IPv4 分流', status: 'ok', message: '已有 IPv4 Fake-IP 路由，沿用现有配置。' },
      { id: 'ra_input', label: 'RA 接收规则', status: 'ok', message: '已放行必要的 IPv6 通知。', details: ['#2 chain=input action=accept protocol=icmpv6'] },
      { id: 'fake_ipv6', label: 'IPv6 Fake-IP 路由', status: 'ok', message: '当前保存配置未启用 IPv6 Fake-IP，本次不新增相关路由' },
    ] }],
    can_prepare: !prepared, prepare_interface: 'eth0', check_token: 'a'.repeat(64),
    connectivity: { status: 'not_checked', message: '尚未验证节点连接。网络参数检查通过不代表代理连接已经成功。' },
  }
}
const groupIds = ['ipv6-network', 'restart', 'dns-router', 'node', 'client']
async function checkFiveGroups(summary) {
  await summary.waitFor()
  assert.deepEqual(await summary.locator('[data-ipv6-check-group]').evaluateAll(elements => elements.map(element => element.dataset.ipv6CheckGroup)), groupIds)
  const buttons = summary.locator('[data-ipv6-check-group] > [data-agent-status-row] > div:first-child > button')
  assert.equal(await buttons.count(), 5, 'Each group has one accessible details control')
  const heights = await buttons.evaluateAll(elements => elements.map(element => element.getBoundingClientRect().height))
  assert.ok(heights.every(height => height >= 44), `Group controls must be at least 44px high: ${heights}`)
}
async function checkUnconfiguredClient(summary) {
  const client = summary.locator('[data-ipv6-check-group="client"]')
  const header = client.locator('[data-agent-status-row] > div:first-child > button')
  await header.waitFor()
  assert.equal(await client.locator('[data-agent-status-row]').getAttribute('data-tone'), 'neutral', 'An unconfigured client is not an acceptance failure or success')
  assert.match(await header.innerText(), /未配置|无需检查|未验证/)
  assert.equal(await client.locator('[data-status-details]').isVisible(), false, 'Unconfigured raw checks stay in details')
}
const report = { mockedAPI: true, productionEndpointsContacted: false, scenarios: [], pageErrors: [] }
const { chromium } = await import(process.env.CF_PLAYWRIGHT_MODULE || '/home/sunbroke/rt/private__tmp__configflow-real-env-rebuild__browser-runtime/node_modules/playwright/index.mjs')
let browser
let server
let activePage
try {
  server = await createServer({ configFile: false, root: scratch, plugins: [vue(), tailwindcss()],
    resolve: { alias: { '@': join(scratch, 'src') } },
    server: { host: '0.0.0.0', port: 0, strictPort: false, watch: null, fs: { allow: [scratch, dependencies] } },
    cacheDir: join(scratch, '.vite'), logLevel: 'error' })
  await server.listen()
  const base = `http://127.0.0.1:${server.httpServer.address().port}`
  browser = await chromium.launch({ headless: true })
  for (const width of [1440, 375]) for (const theme of ['dark', 'light']) {
    const context = await browser.newContext({ viewport: { width, height: width === 375 ? 900 : 1000 }, colorScheme: theme, reducedMotion: 'reduce' })
    const page = await context.newPage()
    activePage = page
    page.on('pageerror', error => report.pageErrors.push(String(error)))
    page.on('console', message => { if (message.type() === 'error') console.error(message.text()) })
    await page.addInitScript(theme => {
      const apply = () => { document.documentElement.dataset.theme = theme; document.documentElement.dataset.accent = 'clay'; document.documentElement.dataset.motion = 'off' }
      if (document.documentElement) apply()
      else document.addEventListener('DOMContentLoaded', apply, { once: true })
    }, theme)
    let yaml = initialYAML
    let state = 'ready'
    let verification = null
    let publishedId = ''
    let release
    const writes = []
    await page.route('**/api/**', async route => {
      const request = route.request()
      const url = new URL(request.url())
      if (!url.pathname.startsWith('/api/')) return route.continue()
      let data
      if (url.pathname === '/api/custom-config/mihomo') {
        if (request.method() === 'POST') {
          yaml = request.postDataJSON().config
          writes.push('save')
          data = { success: true, publish_guard: { expected_profile_id: 'bound', expected_service_type: 'mihomo', expected_config_revision: 7 } }
        } else data = { config: yaml, default_config: initialYAML, provider_update_interval: 3600 }
      } else if (url.pathname === '/api/agents') data = [target]
      else if (url.pathname === '/api/profiles') data = [{ id: 'bound', name: '家庭配置', is_default: true }]
      else if (url.pathname === '/api/agents/gw/ipv6-readiness') {
        assert.equal(url.searchParams.get('profile_id'), 'bound')
        if (state === 'loading') await new Promise(done => { release = done })
        if (state === 'failure') return route.fulfill({ status: 502, contentType: 'application/json', body: JSON.stringify({ success: false, message: '无法读取设备的 IPv6 网络，请确认 Agent 在线后重新检查。' }) })
        data = readiness()
      } else if (url.pathname === '/api/agents/gw/ipv6-readiness/prepare') {
        assert.deepEqual(request.postDataJSON(), { profile_id: 'bound', interface: 'eth0', check_token: 'a'.repeat(64) })
        writes.push('prepare')
        data = { ...readiness(true), message: '已补齐缺少的配置；节点连接需单独验证。' }
      } else if (url.pathname === '/api/agents/gw/ipv6-readiness/verify') {
        if (request.method() === 'POST') {
          const body = request.postDataJSON()
          assert.deepEqual(body, { profile_id: 'bound', expected_config_revision: 7,
            ...(body.deployment_id ? { deployment_id: publishedId } : {}) })
          writes.push('verify')
          verification = { status: 'ok', scope: 'mihomo_runtime', config_revision: 7,
            ...(body.deployment_id ? { deployment_id: body.deployment_id } : {}),
            checked_at: '2026-10-30T12:00:00Z', checks: [
              ...readiness(true).checks,
              { id: 'runtime_deployed_config', label: '配置与主机网络', status: 'ok', message: '当前验收对应已确认的配置版本。' },
              { id: 'runtime_dns_a', label: 'DNS 实际应答', status: 'ok', message: '实际 DNS 应答符合配置。' },
              { id: 'runtime_proxy_http', label: '代理网站访问', status: 'ok', message: '检测网站已通过 Mihomo 代理访问。' },
              { id: 'runtime_node_ipv6_transport', label: '节点 IPv6 连接', status: 'ok', message: '已取得本次连接使用 IPv6 的证据。' },
              { id: 'client_not_configured', label: '客户端全链路', status: 'unknown', message: '尚未配置独立检测客户端，客户端全链路未验证。' },
            ] }
        }
        data = { success: true, agent_id: 'gw', profile_id: 'bound', verification }
      } else if (url.pathname === '/api/agents/gw/ipv6-readiness/verification-settings') {
        assert.equal(request.method(), 'GET')
        data = { success: true, agent_id: 'gw', profile_id: 'bound', settings: { client_agent_id: '', test_url: '' }, candidates: [
          { id: 'client', name: '独立检测客户端', host: '192.0.2.30', status: 'online', supported: true },
        ] }
      } else if (url.pathname === '/api/agents/gw/push-config') {
        const body = request.postDataJSON()
        assert.equal(body.expected_config_revision, 7)
        publishedId = body.deployment_id
        writes.push('publish')
        data = { deployment_id: publishedId, profile_id: 'bound', service_type: 'mihomo', config_revision: 7, status: 'succeeded' }
      } else if (url.pathname === '/api/auth/status') data = { authEnabled: false }
      else data = {}
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) })
    })
    await page.goto(base)
    await page.getByRole('switch', { name: 'Mihomo 使用 IPv6', exact: true }).waitFor()
    assert.equal(await page.getByRole('switch', { name: 'Mihomo 使用 IPv6', exact: true }).getAttribute('aria-checked'), 'true')
    assert.equal(await page.getByRole('switch', { name: 'DNS 返回 IPv6 地址', exact: true }).getAttribute('aria-checked'), 'false')
    await page.getByRole('button', { name: '检查设备 IPv6', exact: true }).click()
    await page.getByRole('button', { name: '补齐缺少的配置', exact: true }).waitFor()
    assert.deepEqual(writes, [])
    const manualSummary = page.locator('[data-mihomo-ipv6-readiness] [data-ipv6-check-summary]')
    await checkFiveGroups(manualSummary)
    const network = manualSummary.locator('[data-ipv6-check-group="ipv6-network"]')
    assert.equal(await network.locator('[data-agent-status-row]').getAttribute('data-tone'), 'success')
    assert.equal(await network.locator('[data-status-notice]').count(), 0, 'Passed groups occupy one line')
    assert.equal(await network.locator('[data-status-details]').isVisible(), false, 'Passed technical evidence is collapsed')
    await manualSummary.locator('[data-ipv6-check-group="restart"] [data-status-notice]').filter({ hasText: '开机恢复配置尚未确认齐全' }).waitFor()
    const dnsGroup = manualSummary.locator('[data-ipv6-check-group="dns-router"]')
    await dnsGroup.locator('[data-status-notice]').filter({ hasText: '需要核对路由器和客户端是否使用预期的 DNS' }).waitFor()
    const rawRule = dnsGroup.locator('[data-ipv6-check-id$="ra_input"]')
    assert.equal(await rawRule.isVisible(), false, 'Router rules do not fill the default view')
    const dnsHeader = dnsGroup.locator('[data-agent-status-row] > div:first-child > button')
    await dnsHeader.click()
    await rawRule.getByText('#2 chain=input action=accept protocol=icmpv6', { exact: true }).waitFor()
    await page.screenshot({ path: join(output, `${width}-${theme}-expanded-rule.png`) })
    await dnsHeader.click()
    assert.equal(await rawRule.isVisible(), false)
    report.scenarios.push({ width, theme, state: 'five-groups-collapsed-details-visible-problems', touchTargetMin: 44, rawRulePreserved: true })
    for (const accent of ['clay', 'kraft', 'sky']) {
      await page.evaluate(accent => { document.documentElement.dataset.accent = accent }, accent)
      const metrics = await page.evaluate(() => {
        const dialog = document.querySelector('[data-mihomo-ipv6-dialog]')
        const body = document.querySelector('[data-mihomo-ipv6-readiness]')
        return { viewport: innerWidth, page: document.documentElement.scrollWidth, dialog: dialog.clientWidth,
          dialogScroll: dialog.scrollWidth, panel: body.clientWidth, panelScroll: body.scrollWidth }
      })
      assert.ok(metrics.page <= width && metrics.dialogScroll <= metrics.dialog + 1 && metrics.panelScroll <= metrics.panel + 1, JSON.stringify(metrics))
      report.scenarios.push({ width, theme, accent, state: 'ready', metrics })
    }
    await page.evaluate(() => { document.documentElement.dataset.accent = 'clay' })
    await page.locator('[data-mihomo-ipv6-dialog]').scrollIntoViewIfNeeded()
    await page.screenshot({ path: join(output, `${width}-${theme}-ready.png`) })
    await page.getByRole('button', { name: '验收设置', exact: true }).click()
    const clientPicker = page.getByRole('combobox', { name: '独立检测客户端', exact: true })
    await clientPicker.waitFor()
    assert.ok((await clientPicker.innerText()).includes('不配置'))
    await page.getByRole('button', { name: '保存验收设置', exact: true }).scrollIntoViewIfNeeded()
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false)
    await page.screenshot({ path: join(output, `${width}-${theme}-verification-settings.png`) })
    await page.getByRole('button', { name: '验收设置', exact: true }).click()
    report.scenarios.push({ width, theme, state: 'optional-client-not-preselected' })
    if (width === 1440 && theme === 'dark') {
      await page.getByRole('button', { name: '补齐缺少的配置', exact: true }).click()
      await page.getByText('已补齐缺少的配置；节点连接需单独验证。', { exact: true }).waitFor()
      await page.getByRole('button', { name: '验证连接与分流', exact: true }).click()
      await manualSummary.locator('[data-ipv6-check-group="node"] [data-agent-status-row][data-tone="success"]').waitFor()
      await checkFiveGroups(manualSummary)
      await checkUnconfiguredClient(manualSummary)
      assert.equal(await manualSummary.locator('[data-ipv6-check-group="node"] [data-status-details]').isVisible(), false)
      assert.equal(await page.getByRole('button', { name: '主机自检：已通过', exact: true }).count(), 0, 'Manual verification merges into the five existing groups')
      assert.deepEqual(writes, ['prepare', 'verify'])
      await page.getByRole('button', { name: '返回设置', exact: true }).click()
      await page.getByRole('switch', { name: 'DNS 返回 IPv6 地址', exact: true }).click()
      await page.getByRole('button', { name: '检查设备 IPv6', exact: true }).click()
      await page.getByText('有未保存的修改，请先保存草稿，再检查设备 IPv6。', { exact: true }).waitFor()
      assert.equal(await page.getByRole('button', { name: '重新检查', exact: true }).isDisabled(), true)
      await page.getByRole('button', { name: '返回设置', exact: true }).click()
      await page.getByRole('button', { name: '保存草稿', exact: true }).click()
      await page.waitForFunction(() => !document.querySelector('[data-settings-workspace] [inert]'))
      assert.equal(await page.locator('[data-settings-workspace]').isVisible(), true, 'Saving the draft keeps the current editor open')
      await page.getByRole('switch', { name: 'DNS 返回 IPv6 地址', exact: true }).waitFor()
      assert.equal(await page.getByRole('switch', { name: 'DNS 返回 IPv6 地址', exact: true }).getAttribute('aria-checked'), 'true')
      assert.ok(yaml.includes('# 保留用户注释') && yaml.includes('# 单独控制 AAAA 答案'))
      assert.ok(yaml.includes('fake-ip-range6: fc00::1/64'), 'Enabling DNS IPv6 fills the missing range in the draft')
      await page.screenshot({ path: join(output, `${width}-${theme}-saved-draft-open.png`) })
      report.scenarios.push({ width, theme, state: 'prepare-probe-edit-save-stays-open', writes })
    }
    if (width === 375) {
      const prepare = page.getByRole('button', { name: '补齐缺少的配置', exact: true })
      await prepare.scrollIntoViewIfNeeded()
      assert.equal(await prepare.evaluate(element => {
        const rect = element.getBoundingClientRect()
        const hit = document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2)
        return hit === element || element.contains(hit)
      }), true, 'Preparation remains reachable above the fixed footer after scrolling')
      await page.screenshot({ path: join(output, `${width}-${theme}-scrolled.png`) })
      report.scenarios.push({ width, theme, state: 'scroll-to-network-actions', reachable: true })
      state = 'loading'
      await page.getByRole('button', { name: '重新检查', exact: true }).click()
      await page.getByText('正在检查 IPv6 配置…', { exact: true }).waitFor()
      await page.screenshot({ path: join(output, `${width}-${theme}-loading.png`) })
      state = 'failure'
      release()
      await page.getByRole('alert').filter({ hasText: '无法读取设备的 IPv6 网络' }).waitFor()
      await page.screenshot({ path: join(output, `${width}-${theme}-failure.png`) })
      assert.equal(await page.getByRole('button', { name: '重新检查', exact: true }).isDisabled(), false)
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)
      assert.equal(overflow, false)
      report.scenarios.push({ width, theme, state: 'loading-and-failure', retryAvailable: true, overflow })
    }
    if (await page.locator('[data-mihomo-ipv6-dialog]').count()) await page.getByRole('button', { name: '返回设置', exact: true }).click()
    await page.getByRole('button', { name: '保存并发布', exact: true }).click()
    const automatic = page.locator('[data-ipv6-verifier][aria-label="发布后验收"]')
    const summary = automatic.getByRole('button', { name: '主机自检：已通过', exact: true })
    await summary.waitFor()
    assert.deepEqual(writes.slice(-3), ['save', 'publish', 'verify'])
    await summary.click()
    const automaticGroups = automatic.locator('[data-ipv6-check-summary]')
    await checkFiveGroups(automaticGroups)
    await checkUnconfiguredClient(automaticGroups)
    assert.equal(await automatic.getByText('客户端全链路验收', { exact: true }).count(), 0)
    await summary.click()
    await automatic.scrollIntoViewIfNeeded()
    await page.screenshot({ path: join(output, `${width}-${theme}-automatic-summary.png`) })
    report.scenarios.push({ width, theme, state: 'confirmed-publication-auto-verification', scope: 'host-only', client: 'not-verified' })
    await context.close()
  }
  assert.deepEqual(report.pageErrors, [])
} catch (failure) {
  report.failure = String(failure)
  if (activePage && !activePage.isClosed()) {
    report.body = await activePage.locator('body').innerText()
    await activePage.screenshot({ path: join(output, 'failed-run.png') })
  }
  throw failure
} finally {
  await browser?.close()
  await server?.close()
  await writeFile(join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n')
  await rm(scratch, { recursive: true, force: true })
}
console.log(JSON.stringify({ scenarios: report.scenarios.length, pageErrors: report.pageErrors.length, output }))
