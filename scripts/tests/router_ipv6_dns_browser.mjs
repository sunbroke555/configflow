// Run only on 192.168.0.32. Renders the real Routers.vue with isolated API fixtures.
// Never contacts a real ConfigFlow backend, Agent or router, and never publishes.
// CF_EVIDENCE_DIR sets the screenshots/report directory. CF_PLAYWRIGHT_MODULE
// and CF_NODE_MODULES may override the already-installed test-server runtimes.
import assert from 'node:assert/strict'
import { cp, mkdir, mkdtemp, readFile, rm, symlink, writeFile } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { networkInterfaces } from 'node:os'
import { createHash } from 'node:crypto'

assert.ok(Object.values(networkInterfaces()).flat().some(item => item?.address === '192.168.0.32'), 'Run this test on the test server')
const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const frontend = join(root, 'frontend')
const lockHash = createHash('sha256').update(await readFile(join(frontend, 'package-lock.json'))).digest('hex').slice(0, 16)
const dependencies = process.env.CF_NODE_MODULES || `/home/sunbroke/cf-test/node_modules/${lockHash}/node_modules`
async function dependency(name) {
  const folder = join(dependencies, name)
  const pkg = JSON.parse(await readFile(join(folder, 'package.json'), 'utf8'))
  const entry = pkg.exports['.'].import || pkg.exports['.'].default
  return import(join(folder, typeof entry === 'string' ? entry : entry.default))
}
const { createServer } = await dependency('vite')
const { default: vue } = await dependency('@vitejs/plugin-vue')
const { default: tailwindcss } = await dependency('@tailwindcss/vite')
const { chromium } = await import(process.env.CF_PLAYWRIGHT_MODULE || '/home/sunbroke/rt/private__tmp__configflow-real-env-rebuild__browser-runtime/node_modules/playwright/index.mjs')
const output = resolve(process.env.CF_EVIDENCE_DIR || join(root, '.ci-results/router-ipv6-dns-ui'))
await mkdir(output, { recursive: true })
const scratch = await mkdtemp('/tmp/configflow-router-ipv6-dns-ui-')
await cp(join(frontend, 'src'), join(scratch, 'src'), { recursive: true })
await symlink(dependencies, join(scratch, 'node_modules'))
await writeFile(join(scratch, 'index.html'), '<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="app"></div><script type="module" src="/main.ts"></script></body></html>')
await writeFile(join(scratch, 'main.ts'), `
import { createApp, h } from 'vue'
import { createRouter, createMemoryHistory, RouterView } from 'vue-router'
import Routers from './src/views/Routers.vue'
import './src/styles/theme.css'
import './src/styles/tokens.css'
const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/routers', component: Routers }] })
const app = createApp({ render: () => h('main', { class: 'mx-auto w-full max-w-[1200px] p-6 max-sm:p-3' }, [h(RouterView)]) })
app.use(router)
await router.push('/routers')
await router.isReady()
app.mount('#app')
`)

const gateway = { id: 'mihomo', name: 'Mihomo 网关', service_type: 'mihomo', profile_id: 'default', status: 'online', enabled: true, host_ip: '192.0.2.3' }
const dns = { id: 'oxidns', name: 'OxiDNS', service_type: 'oxidns', profile_id: 'default', status: 'online', enabled: true, host_ip: '192.0.2.53' }
const device = { id: 'ros', name: '家庭 ROS', type: 'routeros', endpoint: 'http://192.0.2.1', username: 'admin',
  verify_tls: true, proxy_gateway_agent_id: 'mihomo', manage_return_routing: false, dns_agent_id: 'oxidns', has_password: true,
  last_probe: { ok: true, version: '7.21', at: '2026-10-31T10:00:00Z' } }
const reasons = {
  all: '当前通告来自 all，会同时影响多个接口。请先在路由器上明确目标 LAN 的通告配置，再重新预览。',
  dynamic: '该 LAN 的 IPv6 通告是动态或无效条目，请先在路由器上处理。',
}
function result(state) {
  const unlinked = state === 'unlinked'
  const base = {
    in_sync: state === 'synced' || unlinked,
    summary: { synced: 1, not_required: 1 },
    desired: { fake_ip_range: '198.18.0.0/16', proxy_gateway: '192.0.2.3', proxy_cidrs: [], warnings: [] },
    routes: { items: [{ cidr: '198.18.0.0/16', sources: ['fake-ip'], status: 'synced', detail: '已有对应路由。' }], extra: [], unmanaged_other: [] },
    nat_bypass: { items: [{ cidr: '198.18.0.0/16', status: 'not_required', detail: '不匹配源 NAT。' }], disabled_rules: [] },
    dhcp_dns: unlinked
      ? { status: 'disabled', message: '未选择 DHCP DNS 服务器，本次保留现有 DHCP DNS 配置。', target_dns: '', items: [], blockers: [] }
      : { status: 'synced', message: 'IPv4 DHCP 已下发所选 OxiDNS。', target_dns: '192.0.2.53', items: [], blockers: [] },
    return_routing: { status: 'disabled', message: '自动配置回流已关闭，保留已有配置。', nft_available: true, iproute_available: true,
      return_tables: [], fwmark_rules: [], table_routes: {}, tun_devices: [] },
  }
  if (state === 'legacy') return base
  const common = { enabled: !unlinked, interface: unlinked ? '' : 'bridge-lan', target_dns: unlinked ? '' : '192.0.2.53', items: [], blockers: [], warnings: [] }
  const states = {
    pending: { ...common, status: 'pending', message: '该 LAN 正在通过 IPv6 另发 DNS，需要关闭以统一使用 OxiDNS。',
      items: [{ id: '*1', interface: 'bridge-lan', source_interface: 'bridge-lan', advertise_dns: 'yes', dns_servers: ['2001:db8::53'], status: 'pending', detail: '发布时关闭该 LAN 的 IPv6 DNS 下发。' }] },
    blocked: { ...common, status: 'blocked', message: '尚未确定可以安全修改的 LAN 配置，请先处理下方问题，再重新检查。', blockers: [reasons.all], warnings: [reasons.dynamic] },
    synced: { ...common, status: 'synced', message: '该 LAN 未下发 IPv6 DNS，无需调整。' },
    unlinked: { ...common, status: 'disabled', message: '未关联 OxiDNS，本次不检查或修改 IPv6 DNS 下发。' },
  }
  return { ...base, ipv6_dns: states[state] }
}
function plan(state) {
  const blocked = state === 'blocked'
  return { fingerprint: `preview-${state}`, empty: state !== 'pending', warnings: [], blockers: blocked ? [reasons.all] : [], stages: [
    { key: 'dhcp_dns', title: 'DHCP DNS', enabled: true, ops: [] },
    { key: 'ipv6_dns', title: 'IPv6 DNS 下发', enabled: true, ops: state === 'pending' ? [{
      kind: 'ipv6_dns_update', summary: '关闭 bridge-lan 的 IPv6 DNS 下发', interface: 'bridge-lan', source_interface: 'bridge-lan',
      target_dns: '192.0.2.53', expected: { 'advertise-dns': 'yes', 'ra-lifetime': '30m' }, values: { 'advertise-dns': 'no' },
      expected_rows: [{ interface: 'pppoe-out', 'advertise-dns': 'self' }],
    }] : [] },
  ] }
}
const evidence = { startedAt: new Date().toISOString(), actualComponent: 'frontend/src/views/Routers.vue', mockedAPI: true,
  productionEndpointsContacted: false, writesAttempted: [], unexpectedRequests: [], pageErrors: [], cases: [], screenshots: [] }
let browser, server, page
let phase = 'starting'
try {
  server = await createServer({ configFile: false, root: scratch, plugins: [vue(), tailwindcss()],
    resolve: { alias: { '@': join(scratch, 'src') } },
    server: { host: '127.0.0.1', port: 0, strictPort: false, watch: null, fs: { allow: [scratch, dependencies] } },
    cacheDir: join(scratch, '.vite'), logLevel: 'error' })
  await server.listen()
  const base = `http://127.0.0.1:${server.httpServer.address().port}`
  browser = await chromium.launch({ headless: true })
  for (const { width, height, label, theme } of [
    { width: 1440, height: 1000, label: 'desktop', theme: 'light' },
    { width: 390, height: 844, label: 'mobile', theme: 'light' },
    { width: 1440, height: 1000, label: 'desktop', theme: 'dark' },
  ]) {
    const context = await browser.newContext({ viewport: { width, height }, colorScheme: theme, reducedMotion: 'reduce' })
    await context.addInitScript(theme => {
      const apply = () => { document.documentElement.dataset.theme = theme; document.documentElement.dataset.accent = 'clay'; document.documentElement.dataset.motion = 'off' }
      if (document.documentElement) apply()
      else document.addEventListener('DOMContentLoaded', apply, { once: true })
    }, theme)
    page = await context.newPage()
    page.on('pageerror', error => evidence.pageErrors.push(String(error)))
    let state = 'pending'
    await context.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url())
      if (url.origin !== base) {
        evidence.unexpectedRequests.push(`${request.method()} ${url.origin}${url.pathname}`)
        return route.abort('blockedbyclient')
      }
      if (!url.pathname.startsWith('/api/')) return route.continue()
      const key = `${request.method()} ${url.pathname}`
      let data
      if (key === 'GET /api/routers') data = { routers: [{ ...device, dns_agent_id: state === 'unlinked' ? '' : 'oxidns' }] }
      else if (key === 'GET /api/agents') data = [gateway, dns]
      else if (key === 'GET /api/rules') data = []
      else if (key === 'GET /api/routers/gateway-forwarding') data = { gateways: [] }
      else if (key === 'GET /api/routers/ros/report') data = { report: result(state), generated_at: '2026-10-31T10:00:00Z' }
      else if (key === 'POST /api/routers/ros/plan') data = { plan: plan(state) }
      else {
        if (request.method() !== 'GET') evidence.writesAttempted.push(key)
        evidence.unexpectedRequests.push(key)
        return route.fulfill({ status: 501, contentType: 'application/json', body: JSON.stringify({ success: false, message: 'Unexpected browser test request' }) })
      }
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) })
    })
    async function open(next) {
      state = next
      phase = `${label}/${theme}/${state}`
      await page.goto(base)
      await page.getByRole('button', { name: '差异报告', exact: true }).click()
      await page.getByRole('button', { name: '预览发布', exact: true }).waitFor()
    }
    async function screenshot(name, locator) {
      const file = `router-ipv6-dns-${name}-${label}-${theme}.png`
      if (locator) await locator.screenshot({ path: join(output, file), animations: 'disabled' })
      else await page.screenshot({ path: join(output, file), animations: 'disabled' })
      evidence.screenshots.push(file)
    }
    const card = () => page.locator('[data-router-ipv6-dns]')
    const dhcp = () => page.locator('section').filter({ has: page.getByRole('heading', { name: /^DHCP DNS(?:\s|$)/ }) })
    async function assertFits() {
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `${phase}: page exceeds viewport width`)
      assert.ok(await card().evaluate(node => node.scrollWidth <= node.clientWidth + 1), `${phase}: IPv6 DNS card overflows`)
    }
    await open('pending')
    await card().waitFor()
    const text = await card().innerText()
    for (const expected of ['目标 LAN：bridge-lan', '待关闭', '统一使用 OxiDNS', '在「预览发布」确认后关闭', 'IPv6 地址和默认网关继续通告']) assert.ok(text.includes(expected), `${phase}: missing ${expected}`)
    assert.equal(await card().locator('table, details, [role="switch"]').count(), 0, 'No second write control or technical table')
    assert.ok((await card().getByRole('button').evaluateAll(buttons => buttons.map(button => button.getAttribute('aria-label')))).every(label => label?.includes('说明')))
    assert.equal(await page.getByRole('button', { name: '预览发布', exact: true }).count(), 1)
    assert.equal(await dhcp().evaluate(node => node.nextElementSibling?.hasAttribute('data-router-ipv6-dns')), true, 'IPv6 DNS is next to DHCP DNS')
    await dhcp().evaluate(node => node.scrollIntoView({ block: 'start', behavior: 'instant' }))
    await assertFits()
    await screenshot('pending')
    evidence.cases.push({ viewport: label, theme, state: 'pending', targetLan: true, reasonVisible: true, preservesAddressAndGateway: true, singlePreviewEntry: true, fitsViewport: true })

    await page.getByRole('button', { name: '预览发布', exact: true }).click()
    const dialog = page.getByRole('dialog')
    await dialog.waitFor()
    assert.ok((await dialog.innerText()).includes('关闭 bridge-lan 的 IPv6 DNS 下发'))
    await dialog.getByRole('button', { name: '查看详情', exact: true }).click()
    assert.ok((await dialog.innerText()).includes('LAN 接口'))
    await dialog.getByRole('combobox', { name: '变更内容分类' }).click()
    await page.getByRole('option', { name: /^发布改动/ }).click()
    const detail = await dialog.innerText()
    for (const expected of ['DNS 下发 · advertise-dns', '原：yes', '新：no', 'IPv6 地址和默认网关继续通告']) assert.ok(detail.includes(expected), `${phase}: missing preview detail ${expected}`)
    assert.ok(!detail.includes('pppoe-out') && !detail.includes('ra-lifetime'), 'Inspection snapshots are not published changes')
    assert.ok(await dialog.getByRole('button', { name: '确认发布', exact: true }).isEnabled())
    await screenshot('preview', dialog)
    await dialog.getByRole('button', { name: '取消', exact: true }).click()
    evidence.cases.push({ viewport: label, theme, state: 'preview', exactChangeShown: true, unrelatedSnapshotHidden: true, published: false })

    await open('blocked')
    await card().waitFor()
    assert.ok((await card().innerText()).includes('需处理'))
    assert.ok(await card().getByRole('alert').isVisible())
    assert.equal(await card().getByRole('alert').innerText(), reasons.all)
    assert.equal(await card().getByRole('status').innerText(), reasons.dynamic)
    await dhcp().evaluate(node => node.scrollIntoView({ block: 'start', behavior: 'instant' }))
    await assertFits()
    await screenshot('blocked')
    await page.getByRole('button', { name: '预览发布', exact: true }).click()
    await dialog.waitFor()
    assert.ok(await dialog.getByRole('button', { name: '确认发布', exact: true }).isDisabled(), 'Unsafe interface scope blocks publication')
    await dialog.getByRole('button', { name: '取消', exact: true }).click()
    evidence.cases.push({ viewport: label, theme, state: 'blocked', reasonsVisible: true, publishDisabled: true, fitsViewport: true })

    await open('unlinked')
    await card().waitFor()
    const disabled = await card().innerText()
    assert.ok(disabled.includes('未检查') && disabled.includes('未关联 OxiDNS') && disabled.includes('不检查或修改'))
    assert.ok(!disabled.includes('已关闭') && !disabled.includes('无需调整'))
    await dhcp().evaluate(node => node.scrollIntoView({ block: 'start', behavior: 'instant' }))
    await assertFits()
    await screenshot('unlinked')
    evidence.cases.push({ viewport: label, theme, state: 'unlinked', notFalselyPassed: true, fitsViewport: true })

    await open('synced')
    await card().waitFor()
    assert.ok((await card().innerText()).includes('无需调整'))
    assert.ok(!(await card().innerText()).includes('已关闭'), 'No-change result does not claim this release changed anything')
    evidence.cases.push({ viewport: label, theme, state: 'synced', noChangeClaimed: true })
    await open('legacy')
    assert.equal(await card().count(), 0, 'Missing optional report must not imply an IPv6 check passed')
    evidence.cases.push({ viewport: label, theme, state: 'legacy', omittedReportNotFalselyPassed: true })
    await context.close()
  }
  assert.deepEqual(evidence.writesAttempted, [], 'Browser checks must never publish or change config')
  assert.deepEqual(evidence.unexpectedRequests, [], 'All backend requests must match read-only fixtures')
  assert.deepEqual(evidence.pageErrors, [], 'Browser reported runtime errors')
  evidence.finishedAt = new Date().toISOString()
  evidence.ok = true
  console.log(`Router IPv6 DNS browser checks passed; screenshots and report: ${output}`)
} catch (error) {
  evidence.ok = false
  evidence.failedPhase = phase
  evidence.error = String(error)
  if (page && !page.isClosed()) await page.screenshot({ path: join(output, 'failure.png'), fullPage: true }).catch(() => {})
  throw error
} finally {
  await writeFile(join(output, 'router-ipv6-dns-browser-report.json'), JSON.stringify(evidence, null, 2) + '\n')
  await browser?.close()
  await server?.close()
  await rm(scratch, { recursive: true, force: true })
}
