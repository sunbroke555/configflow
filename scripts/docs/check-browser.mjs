#!/usr/bin/env node
// Run on the test server after building doc/.vitepress/dist.
import assert from 'node:assert/strict'
import { mkdir, writeFile } from 'node:fs/promises'
import { spawn } from 'node:child_process'
import { createServer } from 'node:net'
import { resolve, join } from 'node:path'
import { pathToFileURL } from 'node:url'

const root = process.cwd()
const port = Number(process.env.DOCS_PREVIEW_PORT || 15673)
const output = resolve(root, process.env.DOCS_QA_OUTPUT || '.ci-results/docs')
const base = `http://127.0.0.1:${port}`
const errors = []
let browser, preview, page
await new Promise((yes, no) => {
  const server = createServer()
  server.once('error', no)
  server.listen(port, '0.0.0.0', () => server.close(yes))
})
try {
  await mkdir(output, { recursive: true })
  preview = spawn(process.execPath, [resolve(root, 'doc/node_modules/vitepress/bin/vitepress.js'),
    'preview', '--host', '0.0.0.0', '--port', String(port), '--strictPort'],
  { cwd: resolve(root, 'doc'), stdio: ['ignore', 'pipe', 'pipe'] })
  let ready = false
  for (let n = 0; n < 60; n++) {
    try { if ((await fetch(base)).ok) { ready = true; break } } catch {}
    if (preview.exitCode !== null) throw new Error('Documentation preview exited early')
    await new Promise(resolve => setTimeout(resolve, 200))
  }
  assert(ready, 'Documentation preview did not start')
  const { chromium } = process.env.PLAYWRIGHT_MODULE
    ? await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE)) : await import('playwright')
  browser = await chromium.launch({ headless: true })
  page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
  page.on('pageerror', error => errors.push(error.message))
  page.on('response', response => { if (response.status() >= 400) errors.push(`${response.status()}: ${response.url()}`) })
  await page.goto(`${base}/README`)
  await page.getByRole('heading', { name: /ConfigFlow 使用手册/ }).waitFor()
  await page.screenshot({ path: join(output, 'desktop.png') })
  assert(await page.locator('.VPSidebar').isVisible(), 'Desktop sidebar missing')
  assert(await page.locator('.VPDocAsideOutline').isVisible(), 'Desktop page outline missing')
  await page.getByRole('button', { name: '搜索文档' }).click()
  await page.locator('#localsearch-input').fill('高可用')
  await page.locator('.VPLocalSearchBox .result').first().waitFor()
  const searchResults = await page.locator('.VPLocalSearchBox .result').count()
  assert(searchResults > 0, 'Local search returned no results')
  await page.keyboard.press('Escape')
  await page.goto(`${base}/guide/oxidns-ha#combinations`)
  await page.getByRole('heading', { name: /先选部署组合/ }).waitFor()
  assert.equal(await page.locator('.vp-doc table').first().locator('tbody tr').count(), 6)
  await page.screenshot({ path: join(output, 'ha-combinations.png') })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto(`${base}/guide/install`)
  await page.getByRole('heading', { name: /安装 ConfigFlow 管理面板/ }).waitFor()
  await page.getByRole('button', { name: '文档目录' }).click()
  assert(await page.locator('.VPSidebar').isVisible(), 'Mobile directory missing')
  await page.locator('.VPSidebar a[href="/guide/oxidns-ha"]').click()
  await page.getByRole('heading', { name: /OxiDNS 高可用：两台设备共用一个 DNS 地址/ }).waitFor()
  await page.locator('.VPSidebar.open').waitFor({ state: 'detached' })
  // Let the directory's CSS closing transition finish before recording the page.
  await page.waitForTimeout(350)
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), 'Page overflows mobile width')
  await page.screenshot({ path: join(output, 'mobile.png'), animations: 'disabled' })
  await writeFile(join(output, 'browser-report.json'), JSON.stringify({
    checkedAt: new Date().toISOString(), desktop: '1440x1000', mobile: '390x844',
    sidebar: true, pageOutline: true, searchResults, haCombinations: 6,
    mobileNavigation: true, noPageOverflow: true, errors,
  }, null, 2) + '\n')
  assert.deepEqual(errors, [], 'Browser reported page or asset failures')
  console.log(`Documentation browser checks passed; report and screenshots: ${output}`)
} catch (error) {
  if (page) {
    console.error({ url: page.url(), headings: await page.locator('h1, h2').allTextContents(), errors })
    await page.screenshot({ path: join(output, 'failure.png') }).catch(() => {})
  }
  throw error
} finally {
  await browser?.close()
  if (preview && preview.exitCode === null) {
    const stopped = new Promise(resolve => preview.once('exit', resolve))
    preview.kill('SIGTERM')
    await stopped
  }
}
