// Run on the test server against the snapshot-protected real backend.
// CF_GUIDE_CREDENTIALS points to its private JSON; never include that file in reports.
import { cp, mkdir, mkdtemp, readFile, symlink, writeFile, rm } from 'node:fs/promises'
import { tmpdir, homedir } from 'node:os'
import { createHash } from 'node:crypto'
import { join, basename } from 'node:path'
import { pathToFileURL } from 'node:url'
import assert from 'node:assert/strict'

const root = process.cwd()
const backend = process.env.CF_GUIDE_BACKEND || 'http://127.0.0.1:19285'
const credentials = JSON.parse(await readFile(process.env.CF_GUIDE_CREDENTIALS, 'utf8'))
const auth = await fetch(backend + '/api/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ username: credentials.username, password: credentials.password }) })
assert.equal(auth.status, 200)
const token = (await auth.json()).token
const lockHash = createHash('sha256').update(await readFile(join(root, 'frontend/package-lock.json'))).digest('hex').slice(0, 16)
const modules = process.env.CF_NODE_MODULES || join(homedir(), 'cf-test/node_modules', lockHash, 'node_modules')
const { createServer } = await import(pathToFileURL(join(modules, 'vite/dist/node/index.js')).href)
const { chromium } = await import(process.env.CF_PLAYWRIGHT_MODULE)
const results = join(root, '.ci-results/oxidns-docker-guide-browser')
await mkdir(results, { recursive: true })
const frozen = await mkdtemp(join(tmpdir(), 'cf-docker-guide-browser-'))
await cp(join(root, 'frontend'), join(frozen, 'frontend'), { recursive: true,
  filter: source => !['node_modules', 'dist'].includes(basename(source)) })
await symlink(modules, join(frozen, 'frontend/node_modules'))
const report = { realBackend: true, mockedAPIs: false, commandExecution: 'See separate real Docker lab report.', scenarios: [], errors: [] }
let vite, browser, activePage
try {
  vite = await createServer({ root: join(frozen, 'frontend'), configFile: join(frozen, 'frontend/vite.config.mts'),
    server: { host: '0.0.0.0', port: 14381, strictPort: true, hmr: false, watch: { ignored: ['**/*'] },
      proxy: { '/api': { target: backend, changeOrigin: true } } } })
  await vite.listen()
  browser = await chromium.launch({ headless: true })
  let index = 0
  for (const theme of ['light', 'dark']) for (const width of [375, 1440]) for (const network of ['host', 'macvlan']) {
    const accent = ['clay', 'kraft', 'sky'][index++ % 3]
    const context = await browser.newContext({ viewport: { width, height: 960 }, colorScheme: theme,
      reducedMotion: 'reduce', permissions: ['clipboard-read', 'clipboard-write'] })
    await context.addInitScript(({ token, theme, accent }) => {
      localStorage.setItem('token', token)
      localStorage.setItem('configflow-theme', theme)
      localStorage.setItem('configflow-preferences', JSON.stringify({ accent, motion: false, texture: false, density: 1 }))
    }, { token, theme, accent })
    const page = await context.newPage()
    activePage = page
    page.on('pageerror', error => report.errors.push(String(error)))
    const scenario = { theme, width, network, accent, requests: [], metrics: [] }
    page.on('response', response => {
      if (response.status() === 401) report.errors.push('Unauthorized ' + new URL(response.url()).pathname)
      if (/docker-oxidns-ha-guide|oxidns-ha-environment/.test(response.url())) {
        scenario.requests.push({ path: new URL(response.url()).pathname, status: response.status() })
      }
    })
    const capture = async name => {
      if (name === 'prepare') await page.locator('[aria-label="安装步骤"]').evaluate(element => {
        element.closest('.cf-focus-gutter').scrollTop = 0
      })
      const metrics = await page.evaluate(() => {
        const dialog = document.querySelector('[role="dialog"]')
        return { viewport: innerWidth, pageWidth: document.documentElement.scrollWidth,
          dialogWidth: dialog.clientWidth, dialogScroll: dialog.scrollWidth }
      })
      assert.ok(metrics.pageWidth <= width && metrics.dialogScroll <= metrics.dialogWidth + 1, JSON.stringify(metrics))
      scenario.metrics.push({ name, ...metrics })
      await page.screenshot({ path: join(results, `${theme}-${width}-${network}-${name}.png`), fullPage: false })
    }
    const step = async number => page.getByRole('button', { name: new RegExp(`^${number}\\. `) }).click()
    const field = async (host, suffix, value) => page.locator(`#docker-ha-host-${host}-${suffix}`).fill(value)
    const generated = async number => {
      const responsePromise = page.waitForResponse(response => response.url().endsWith('/docker-oxidns-ha-guide') && response.request().method() === 'POST')
      await page.getByRole('button', { name: `生成主机 ${number} 的安装命令`, exact: true }).click()
      const response = await responsePromise
      return { status: response.status(), data: await response.json() }
    }
    await page.goto('http://127.0.0.1:14381/agents')
    await page.getByRole('button', { name: /生成\s*安装脚本/ }).first().click()
    await page.locator('[role="radio"][value="docker-ha"]').click()
    await page.locator('#docker-ha-server').fill('http://192.168.79.30:18080')
    await page.locator('[aria-label="Docker HA 网络模式"]').locator(`[value="${network}"]`).click()
    await page.getByRole('button', { name: '生成环境检查命令', exact: true }).click()
    await page.getByRole('button', { name: '复制两台宿主机分别检查', exact: true }).waitFor()
    await capture('prepare')

    await step(2)
    await field(1, 'name', 'browser-dns-a-with-a-long-device-name')
    await field(1, 'ip', network === 'host' ? '192.168.79.21' : '192.168.79.81')
    await field(1, 'data', '/srv/browser-dns-a')
    await page.locator('#docker-ha-host-1-create-id').click()
    await field(1, 'identity', "/srv/identity path/host's-id")
    if (network === 'macvlan') {
      await field(1, 'parent', 'eth0.91'); await field(1, 'subnet', '192.168.79.0/24'); await field(1, 'gateway', '192.168.79.1')
    }
    await capture('new-identity')
    await step(3)
    await page.locator('[aria-label="主机 2 安装方式"] [value="migrate"]').click()
    await field(2, 'container', 'old-dns-b')
    await field(2, 'existing-name', 'old-dns-b-oxidns')
    await field(2, 'ip', network === 'host' ? '192.168.79.22' : '192.168.79.82')
    for (const mount of ['agent_mount', 'core_mount']) {
      if (theme === 'dark') await page.locator(`[aria-label="docker-ha-host-2-${mount} 类型"] [value="volume"]`).click()
      await field(2, mount, theme === 'dark' ? `old_dns_${mount}` : `/srv/old-dns/${mount}`)
    }
    await field(2, 'ha-data', '/srv/dns-b-ha')
    await field(2, 'backup', '/srv/dns-b-backup')
    if (network === 'macvlan') {
      await field(2, 'parent', 'eth0.91'); await field(2, 'subnet', '192.168.79.0/24'); await field(2, 'gateway', '192.168.79.1')
    }
    await capture('migration')
    await step(4)
    let result = await generated(1)
    assert.equal(result.status, 200, result.data.message)
    assert.ok(result.data.install_command.includes('docker-oxidns-ha-enroll/'))
    assert.ok(result.data.identity_command.includes('noclobber'))
    await page.getByRole('button', { name: '复制2. 创建这台宿主机的身份文件', exact: true }).click()
    assert.equal(await page.evaluate(() => navigator.clipboard.readText()), result.data.identity_command)
    await capture('new-commands')
    await page.locator('[aria-label="执行命令的主机"] [value="1"]').click()
    result = await generated(2)
    assert.equal(result.status, 200, result.data.message)
    assert.ok(result.data.rollback_command.includes('old-id'))
    assert.ok(!result.data.install_command.includes('docker-oxidns-ha-enroll/'))
    assert.ok(result.data.install_command.includes(theme === 'dark' ? 'old_dns_agent_mount' : '/srv/old-dns/agent_mount'))
    await capture('migration-commands')

    // Exercise a real validation failure and repair without discarding the draft.
    await step(2)
    await field(1, 'identity', 'relative/path')
    await step(4)
    await page.locator('[aria-label="执行命令的主机"] [value="0"]').click()
    assert.equal(await page.getByRole('button', { name: '复制2. 创建这台宿主机的身份文件', exact: true }).count(), 0)
    result = await generated(1)
    assert.equal(result.status, 400)
    await page.getByRole('alert').filter({ hasText: '绝对路径' }).waitFor()
    await capture('validation-error')
    await step(2)
    assert.equal(await page.locator('#docker-ha-host-1-identity').inputValue(), 'relative/path')
    await field(1, 'identity', '/srv/browser-host-id')
    await step(4)
    result = await generated(1)
    assert.equal(result.status, 200, result.data.message)
    await step(5)
    await page.getByRole('button', { name: '刷新并检查两台 Agent', exact: true }).click()
    await page.getByText(/实时检查时间：/).waitFor({ timeout: 30000 })
    // Select an actual laboratory Agent, deliberately twice: even a live
    // capability response must not permit pairing a device with itself.
    await page.locator('#docker-ha-verify-1').click()
    if (await page.getByRole('option').count()) {
      const label = await page.getByRole('option').first().innerText()
      await page.getByRole('option').first().click()
      await page.locator('#docker-ha-verify-2').click()
      await page.getByRole('option', { name: label, exact: true }).click()
      await page.getByRole('button', { name: '刷新并检查两台 Agent', exact: true }).click()
      await page.getByText(/实时检查时间：/).waitFor({ timeout: 30000 })
      await page.getByText('需要选择两台不同的 Agent。', { exact: true }).waitFor()
      assert.ok(scenario.requests.some(request => request.path.endsWith('/oxidns-ha-environment')))
      scenario.liveEnvironmentRead = true
    } else await page.keyboard.press('Escape')
    await capture('live-check')
    scenario.copyWorks = true
    scenario.validationRecovery = true
    scenario.realEnrollmentCommand = true
    report.scenarios.push(scenario)
    await context.close()
  }
  assert.deepEqual(report.errors, [])
  await writeFile(join(results, 'report.json'), JSON.stringify(report, null, 2))
  console.log(JSON.stringify({ scenarios: report.scenarios.length, errors: report.errors, report: join(results, 'report.json') }))
} catch (error) {
  if (activePage && !activePage.isClosed()) {
    await activePage.screenshot({ path: join(results, 'failure.png'), fullPage: true })
    await writeFile(join(results, 'failure.json'), JSON.stringify({ error: String(error), url: activePage.url(), errors: report.errors,
      text: (await activePage.locator('body').innerText()).slice(0, 4000) }, null, 2))
  }
  throw error
} finally {
  await browser?.close()
  await vite?.close()
  await rm(frozen, { recursive: true, force: true })
}
