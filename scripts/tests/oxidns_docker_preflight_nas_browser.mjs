// Actual authenticated preview, user-derived redacted NAS fixtures, no mocked API.
// Run on .32 only; no NAS connection or Docker mutations are performed.
import assert from 'node:assert/strict'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { networkInterfaces } from 'node:os'
assert.ok(Object.values(networkInterfaces()).flat().some(value => value?.address === '192.168.0.32'))
const root = '/home/sunbroke/configflow-docker-preflight-nas-lab'
const base = 'http://192.168.0.32:15810'
const credentials = JSON.parse(await readFile('/home/sunbroke/previews/configflow-oxidns-ha-guide/credentials.json', 'utf8'))
const { chromium } = await import(process.env.CF_PLAYWRIGHT_MODULE || '/home/sunbroke/rt/private__tmp__configflow-real-env-rebuild__browser-runtime/node_modules/playwright/index.mjs')
const out = join(root, 'browser')
await mkdir(out, { recursive: true })
const report = { authenticatedPreview: base, mockedResponses: false, rawTextareaMasked: true,
  provenance: 'User sample reconstructed after replacing machine fingerprint and reformatting horizontal whitespace; Markdown variant adds trailing line escapes. Not an actual NAS test.',
  scenarios: [], pageErrors: [] }
let browser
try {
  browser = await chromium.launch({ headless: true })
  for (const [width, theme, fixture] of [[1440, 'light', 'nas-redacted'], [375, 'dark', 'nas-markdown']]) {
    const context = await browser.newContext({ viewport: { width, height: 1000 }, colorScheme: theme, reducedMotion: 'reduce' })
    await context.addInitScript(theme => {
      localStorage.setItem('configflow-theme', theme)
      localStorage.setItem('configflow-preferences', JSON.stringify({ accent: 'clay', motion: false, texture: false, density: 1 }))
    }, theme)
    const page = await context.newPage()
    page.on('pageerror', error => report.pageErrors.push(String(error)))
    await page.goto(base + '/agents')
    await page.locator('#login-username').fill(credentials.username)
    await page.locator('#login-password').fill(credentials.password)
    await page.getByRole('button', { name: '登录', exact: true }).click()
    await page.waitForURL(url => !url.pathname.startsWith('/login'))
    await page.goto(base + '/agents')
    await page.getByRole('button', { name: /生成\s*安装脚本/ }).first().click()
    await page.locator('[role="radio"][value="docker-ha"]').click()
    const input = page.locator('#docker-ha-preflight-output-1')
    const result = page.locator('[data-preflight-report="1"]')
    const raw = await readFile(join(root, 'samples', fixture + '.txt'), 'utf8')
    await input.fill(raw)
    const check = async mode => {
      const response = page.waitForResponse(r => r.url().endsWith('/docker-oxidns-ha-guide') && r.request().postDataJSON()?.action === 'check')
      await page.getByRole('button', { name: '检测主机 1', exact: true }).click()
      const resolved = await response
      assert.equal(resolved.status(), 200)
      const value = (await resolved.json()).preflight_report
      const checks = Object.fromEntries(value.checks.map(item => [item.id, item]))
      assert.equal(checks.compose.status, 'warning')
      assert.equal(checks.udp53.status, mode === 'host' ? 'unknown' : 'pass')
      assert.equal(checks.tcp53.status, mode === 'host' ? 'unknown' : 'pass')
      assert.equal(value.status, mode === 'host' ? 'unknown' : 'warning')
      assert.match(checks.ipv4.detail, /ovs_eth0.*192\.168\.0\.105/)
      await result.waitFor()
      assert.ok(await result.locator('[data-preflight-check]').count() >= 12)
      return { mode, http: resolved.status(), status: value.status, checkCount: value.checks.length }
    }
    const checks = [await check('host')]
    assert.match(await result.locator('[data-preflight-check="compose"]').innerText(), /需注意/)
    assert.match(await result.locator('[data-preflight-check="udp53"]').innerText(), /无法判断/)
    await page.locator('[aria-label="Docker HA 网络模式"] [role="radio"][value="macvlan"]').click()
    assert.equal(await result.count(), 0)
    assert.equal(await input.inputValue(), raw)
    checks.push(await check('macvlan'))
    assert.match(await result.locator('[data-preflight-check="udp53"]').innerText(), /通过/)
    await result.locator('[data-preflight-check="compose"]').scrollIntoViewIfNeeded()
    const metrics = await page.evaluate(() => {
      const dialog = document.querySelector('[role="dialog"]')
      return { viewport: innerWidth, pageWidth: document.documentElement.scrollWidth,
        dialogWidth: dialog.clientWidth, dialogScroll: dialog.scrollWidth }
    })
    assert.ok(metrics.pageWidth <= width && metrics.dialogScroll <= metrics.dialogWidth + 1)
    const screenshot = width === 1440 ? 'desktop-nas-itemized.png' : 'mobile-nas-markdown.png'
    await page.screenshot({ path: join(out, screenshot), mask: [page.locator('textarea')], maskColor: '#a8a29e' })
    report.scenarios.push({ width, theme, fixture, checks, metrics, screenshot })
    await context.close()
  }
  assert.deepEqual(report.pageErrors, [])
} catch (error) {
  report.failure = String(error)
  throw error
} finally {
  await browser?.close()
  await writeFile(join(out, 'report.json'), JSON.stringify(report, null, 2) + '\n')
}
console.log(JSON.stringify({ scenarios: report.scenarios.length, pageErrors: report.pageErrors.length }))
