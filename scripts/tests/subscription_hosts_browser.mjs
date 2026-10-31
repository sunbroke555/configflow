#!/usr/bin/env node
// Subscription hosts in the real app, against the private lab ConfigFlow of subscription_hosts_lab.py.
// Test server only, after `cf-test typecheck` linked frontend/node_modules:
//   rt --no-sync --back doc/img/guide --back .ci-results/subscription-hosts -- node scripts/tests/subscription_hosts_browser.mjs generate
//   ... node scripts/tests/subscription_hosts_browser.mjs agents
// Reads only; never clicks 推送 or 保存并发布 (publishing is driven by the lab script).
import assert from 'node:assert/strict'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { networkInterfaces } from 'node:os'
import { join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'

assert.ok(Object.values(networkInterfaces()).flat().some(item => item?.address === '192.168.0.32'), 'Run this on the test server')
const scene = process.argv[2]
const root = process.cwd()
const frontend = join(root, 'frontend')
const shots = resolve(root, 'doc/img/guide')
const evidence = resolve(root, '.ci-results/subscription-hosts')
const apiPort = 15951
const webPort = 15950
const playwright = process.env.CF_PLAYWRIGHT_MODULE
  || '/home/sunbroke/rt/private__tmp__configflow-real-env-rebuild__browser-runtime/node_modules/playwright/index.mjs'
const report = { scene, steps: [], browserErrors: [] }
let vite, browser

const credentials = JSON.parse(await readFile('/tmp/cf-subhosts-lab/credentials.json', 'utf8'))
const login = await fetch(`http://127.0.0.1:${apiPort}/api/auth/login`, {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ username: credentials.username, password: credentials.password }) })
const { token } = await login.json()
assert.ok(token, 'login')

async function page(theme, viewport) {
  const context = await browser.newContext({ viewport, deviceScaleFactor: viewport.width < 500 ? 2 : 1, colorScheme: theme,
    hasTouch: viewport.width < 500, isMobile: viewport.width < 500 })
  await context.addInitScript(([value, mode]) => {
    localStorage.setItem('token', value)
    localStorage.setItem('username', 'admin')
    localStorage.setItem('configflow-theme', mode)
  }, [token, theme])
  const tab = await context.newPage()
  tab.on('pageerror', error => report.browserErrors.push(error.message))
  return tab
}

async function generated(tab, phone = false) {
  await tab.goto(`http://127.0.0.1:${webPort}/generate`)
  const run = tab.locator('[aria-label="生成和导出操作"]').getByRole('button', { name: '生成', exact: true })
  await run.click()
  const summary = tab.locator('[data-subscription-hosts]')
  await summary.waitFor({ timeout: 60000 })
  await tab.locator('[aria-busy="true"]').waitFor({ state: 'detached', timeout: 60000 }).catch(() => {})
  await summary.getByRole('button', { name: '详情' }).click()
  await tab.mouse.move(0, 0)
  await tab.evaluate(() => document.activeElement?.blur())
  await tab.waitForTimeout(400)
  const text = (await summary.innerText()).replace(/\s+/g, ' ')
  report.steps.push({ name: phone ? 'generate (phone, dark)' : 'generate (desktop)', text })
  return summary
}

try {
  await mkdir(shots, { recursive: true })
  await mkdir(evidence, { recursive: true })
  const { createServer } = await import(pathToFileURL(join(frontend, 'node_modules/vite/dist/node/index.js')))
  vite = await createServer({ root: frontend, configFile: join(frontend, 'vite.config.mts'), logLevel: 'warn',
    server: { host: '127.0.0.1', port: webPort, strictPort: true, proxy: { '/api': { target: `http://127.0.0.1:${apiPort}`, changeOrigin: true } } } })
  await vite.listen()
  const { chromium } = await import(pathToFileURL(playwright))
  browser = await chromium.launch({ headless: true })

  if (scene === 'generate') {
    const desktop = await page('light', { width: 1280, height: 900 })
    const summary = await generated(desktop)
    const text = report.steps.at(-1).text
    assert.match(text, /已合并 2 条，忽略 2 条/)
    assert.match(text, /其中 1 条是「域名 → 域名」/)
    assert.match(text, /edge-b\.shared\.lab\.invalid → 198\.51\.100\.13/)
    assert.match(text, /target\.lab\.invalid → 198\.51\.100\.66 · 实验机场 不是这个订阅里被使用节点的 server 域名/)
    assert.match(text, /\+\.lab\.invalid · 实验机场 通配符条目不合并/)
    const result = desktop.locator('section[aria-label="生成结果"]')
    const box = await result.boundingBox()
    const bottom = (await summary.boundingBox()).y + (await summary.boundingBox()).height
    await desktop.screenshot({ path: join(shots, 'subscription-hosts-summary.png'),
      clip: { x: box.x, y: box.y, width: box.width, height: Math.min(bottom - box.y + 140, 900 - box.y) } })
    const phone = await page('dark', { width: 375, height: 812 })
    const mobile = await generated(phone, true)
    const overflow = await phone.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
    assert.ok(overflow <= 0, `horizontal overflow ${overflow}px`)
    await phone.locator('[data-sonner-toast]').first().waitFor({ state: 'detached', timeout: 15000 }).catch(() => {})
    await mobile.screenshot({ path: join(evidence, 'summary-phone-dark.png') })
    report.steps.push({ name: 'phone has no horizontal scroll', overflow })
  } else if (scene === 'agents') {
    const desktop = await page('light', { width: 1280, height: 900 })
    await desktop.goto(`http://127.0.0.1:${webPort}/agents`)
    const card = desktop.locator('.cf-list-in').filter({ hasText: '实验 Mihomo（CT152）' })
    const expected = process.argv[3] || '有变更，待推送'
    await card.getByText(expected, { exact: true }).waitFor({ timeout: 90000 })
    report.steps.push({ name: `agent card shows ${expected}`, text: (await card.innerText()).replace(/\s+/g, ' ').slice(0, 400) })
    await card.screenshot({ path: join(evidence, `agent-card-${expected === '已同步' ? 'synced' : 'pending'}.png`) })
  } else {
    throw new Error('scene: generate | agents')
  }
  assert.deepEqual(report.browserErrors, [])
  console.log(JSON.stringify(report, null, 1))
} finally {
  await writeFile(join(evidence, `browser-${scene}.json`), JSON.stringify(report, null, 2))
  await browser?.close()
  await vite?.close()
}
