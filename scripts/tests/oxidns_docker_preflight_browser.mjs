// Run only on 192.168.0.32 against its deployed, authenticated production preview.
// API responses are real. One response is delayed intact to exercise stale-response handling.
import assert from 'node:assert/strict'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { homedir } from 'node:os'

const root = join(homedir(), 'configflow-docker-preflight-lab')
const base = 'http://192.168.0.32:15810'
const credentials = JSON.parse(await readFile('/home/sunbroke/previews/configflow-oxidns-ha-guide/credentials.json', 'utf8'))
const { chromium } = await import(process.env.CF_PLAYWRIGHT_MODULE || '/home/sunbroke/rt/private__tmp__configflow-real-env-rebuild__browser-runtime/node_modules/playwright/index.mjs')
const resultDir = join(root, 'browser')
const screensOnly = process.argv.includes('--screens-only')
await mkdir(resultDir, { recursive: true })
const sample = async name => readFile(join(root, 'samples', name + '.txt'), 'utf8')
const real = await sample('real-local-rootful')
const unavailable = await sample('real-no-daemon')
const truncated = await sample('fixture-truncated')
const legacy = await sample('fixture-legacy-output')
const rootless = await sample('fixture-rootless')
const malicious = await sample('fixture-malicious-text')
const report = { deployedPreview: base, actualUILogin: true, mockedAPIResponses: false,
  fixtureCasesExplicit: ['rootless', 'legacy/truncated text', 'malicious text', 'UTF-8 oversize'],
  delayedActualResponseForRace: !screensOnly, rawTextareaMaskedInScreenshots: true, scenarios: [], pageErrors: [] }
let browser, activePage
try {
  browser = await chromium.launch({ headless: true })
  const cases = [[0, 1440, 'light', 'clay'], [1, 375, 'dark', 'kraft'], [2, 375, 'light', 'sky'], [3, 1440, 'dark', 'clay']]
  for (const [index, width, theme, accent] of (screensOnly ? cases.slice(0, 2) : cases)) {
    const context = await browser.newContext({ viewport: { width, height: 1000 }, colorScheme: theme, reducedMotion: 'reduce' })
    await context.addInitScript(({ theme, accent }) => {
      localStorage.setItem('configflow-theme', theme)
      localStorage.setItem('configflow-preferences', JSON.stringify({ accent, motion: false, texture: false, density: 1 }))
    }, { theme, accent })
    const page = await context.newPage(); activePage = page
    const scenario = { width, theme, accent, checkResponses: [], metrics: [], assertions: [] }
    let checkRequests = 0
    page.on('pageerror', error => report.pageErrors.push(String(error)))
    page.on('request', request => {
      if (request.url().endsWith('/docker-oxidns-ha-guide') && request.method() === 'POST' && request.postDataJSON()?.action === 'check') checkRequests++
    })
    page.on('response', response => {
      if (response.url().endsWith('/docker-oxidns-ha-guide') && response.request().postDataJSON()?.action === 'check') {
        scenario.checkResponses.push({ status: response.status(), mode: response.request().postDataJSON().network_mode })
      }
    })
    await page.goto(base + '/agents')
    await page.locator('#login-username').fill(credentials.username)
    await page.locator('#login-password').fill(credentials.password)
    await page.getByRole('button', { name: '登录', exact: true }).click()
    await page.waitForURL(url => !url.pathname.startsWith('/login'))
    await page.goto(base + '/agents')
    await page.getByRole('button', { name: /生成\s*安装脚本/ }).first().click()
    await page.locator('[role="radio"][value="docker-ha"]').click()
    await page.getByRole('region', { name: '粘贴输出检查环境', exact: true }).waitFor()
    const input = n => page.locator(`#docker-ha-preflight-output-${n}`)
    const result = n => page.locator(`[data-preflight-report="${n}"]`)
    const mode = value => page.locator('[aria-label="Docker HA 网络模式"] [role="radio"][value="' + value + '"]').click()
    const check = async (n, value) => {
      if (value !== undefined) await input(n).fill(value)
      const response = page.waitForResponse(r => r.url().endsWith('/docker-oxidns-ha-guide') && r.request().postDataJSON()?.action === 'check')
      await page.getByRole('button', { name: `检测主机 ${n}`, exact: true }).click()
      assert.equal((await response).status(), 200)
      await result(n).waitFor()
    }
    const capture = async (name, target) => {
      if (target) await target.scrollIntoViewIfNeeded()
      // The fingerprint is the final collector field. Keep raw text at its start,
      // and never screenshot the private identity fingerprint or login form.
      await page.locator('textarea').evaluateAll(elements => elements.forEach(element => { element.blur(); element.scrollTop = 0; element.scrollLeft = 0 }))
      const metrics = await page.evaluate(() => {
        const dialog = document.querySelector('[role="dialog"]')
        return { viewport: innerWidth, pageWidth: document.documentElement.scrollWidth,
          dialogWidth: dialog.clientWidth, dialogScroll: dialog.scrollWidth }
      })
      assert.ok(metrics.pageWidth <= width && metrics.dialogScroll <= metrics.dialogWidth + 1, JSON.stringify(metrics))
      scenario.metrics.push({ name, ...metrics })
      await page.screenshot({ path: join(resultDir, name + '.png'), fullPage: false, mask: [page.locator('textarea')], maskColor: '#a8a29e' })
    }
    assert.equal(await page.getByRole('button', { name: '检测主机 1', exact: true }).isDisabled(), true)
    if (index === 0) {
      const inspectResponse = page.waitForResponse(r => r.url().endsWith('/docker-oxidns-ha-guide') && r.request().postDataJSON()?.action === 'inspect')
      await page.getByRole('button', { name: '生成环境检查命令', exact: true }).click()
      const inspection = await (await inspectResponse).json()
      assert.ok(inspection.inspection_command.includes('CONFIGFLOW_DOCKER_HA_PREFLIGHT_V1_BEGIN'))
      await check(1, real); await check(2, unavailable)
      assert.match(await result(1).locator('[data-preflight-check="udp53"]').innerText(), /未通过/)
      assert.match(await result(1).locator('[data-preflight-check="tcp53"]').innerText(), /未通过/)
      await page.getByRole('alert').filter({ hasText: '指纹相同' }).waitFor()
      await capture('desktop-host-conflict', result(1))
      await mode('macvlan')
      assert.equal(await result(1).count(), 0); assert.equal(await input(1).inputValue(), real)
      await check(1)
      assert.match(await result(1).innerText(), /基础检查通过/)
      assert.match(await result(1).locator('[data-preflight-check="udp53"]').innerText(), /不因此阻断/)
      assert.match(await result(1).locator('[data-preflight-check="ipv4"]').innerText(), /192\.168\.0\.32/)
      await capture('desktop-macvlan-pass', result(1).locator('[data-preflight-check="ipv4"]'))
      scenario.assertions.push('Real same-host UDP/TCP53 blocks Host but not Macvlan', 'Distinct real captures identify same host without exposing fingerprint', 'Mode change clears result and preserves text')
    } else if (index === 1) {
      await check(1, truncated); await check(2, legacy)
      assert.match(await result(1).innerText(), /无法判断/); assert.match(await result(2).innerText(), /无法判断/)
      await capture('mobile-truncated-unknown', result(1))
      await input(1).fill(real)
      assert.equal(await result(1).count(), 0)
      await mode('macvlan'); await check(1)
      assert.match(await result(1).innerText(), /基础检查通过/)
      await page.getByRole('button', { name: /^2\. / }).click()
      await page.getByRole('button', { name: /^1\. / }).click()
      assert.equal(await input(1).inputValue(), real)
      assert.match(await result(1).innerText(), /基础检查通过/)
      await capture('mobile-macvlan-pass', result(1))
      scenario.assertions.push('Truncated and old output stay unknown', 'Editing invalidates result', 'Draft and result survive guide step navigation')
    } else if (index === 2) {
      await mode('macvlan'); await check(1, rootless)
      assert.match(await result(1).locator('[data-preflight-check="docker_rootful"]').innerText(), /未通过/)
      await capture('mobile-rootless-fixture', result(1))
      await check(2, malicious)
      assert.equal(await page.evaluate(() => window.preflightInjected), undefined)
      assert.equal(await page.locator('img[src="x"]').count(), 0)
      await input(2).fill(rootless)
      await page.getByRole('alert').filter({ hasText: '检查输出相同' }).waitFor()
      const count = checkRequests
      const tooLong = '中'.repeat(33000)
      await input(2).fill(tooLong)
      await page.getByRole('button', { name: '检测主机 2', exact: true }).click()
      await page.locator('#docker-ha-preflight-error-2').filter({ hasText: '96 KiB' }).waitFor()
      assert.equal(await input(2).inputValue(), tooLong)
      assert.equal(checkRequests, count)
      scenario.assertions.push('Rootless fixture cannot pass Macvlan', 'Malicious pasted text is inert', 'Exact duplicates warn before detection', '96KiB uses UTF-8 bytes and preserves rejected input without API request')
    } else {
      await input(1).fill(real)
      let release, started
      const gate = new Promise(resolve => { release = resolve })
      const held = new Promise(resolve => { started = resolve })
      let holdOnce = true
      await page.route('**/api/agents/docker-oxidns-ha-guide', async route => {
        const data = route.request().postDataJSON()
        if (holdOnce && data?.action === 'check' && data.network_mode === 'host') {
          holdOnce = false
          const response = await route.fetch()
          assert.equal(response.status(), 200)
          started(); await gate
          await route.fulfill({ response })
        } else await route.continue()
      })
      await page.getByRole('button', { name: '检测主机 1', exact: true }).click()
      await held
      await mode('macvlan'); await check(1)
      assert.match(await result(1).innerText(), /基础检查通过/)
      const late = page.waitForResponse(r => r.url().endsWith('/docker-oxidns-ha-guide') && r.request().postDataJSON()?.network_mode === 'host')
      release(); await late
      await page.waitForTimeout(150)
      assert.match(await result(1).innerText(), /基础检查通过/)
      await capture('desktop-stale-response-ignored', result(1))
      scenario.assertions.push('Late real Host response cannot overwrite current Macvlan success')
    }
    report.scenarios.push(scenario)
    await context.close()
  }
  assert.deepEqual(report.pageErrors, [])
} catch (error) {
  report.failure = String(error)
  throw error
} finally {
  await browser?.close()
  await writeFile(join(resultDir, screensOnly ? 'screenshots.json' : 'browser.json'), JSON.stringify(report, null, 2) + '\n')
}
console.log(JSON.stringify({ scenarios: report.scenarios.length, pageErrors: report.pageErrors.length, results: resultDir }))
