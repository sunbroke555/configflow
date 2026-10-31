#!/usr/bin/env node
// 「待推送的变更」 in the real app, against the private lab ConfigFlow of push_diff_lab.py (no API mocks).
// Test server only, after `cf-test typecheck` linked frontend/node_modules:
//   rt --no-sync --back doc/img/guide --back .ci-results/push-diff -- node scripts/tests/push_diff_browser.mjs changed
//   ... push_diff_browser.mjs nobaseline | error | synced
// Reads only: never clicks 推送 (the lab script publishes). `error` deletes the old-version record through the API.
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
const evidence = resolve(root, '.ci-results/push-diff')
const apiPort = 15951
const webPort = 15955
const playwright = process.env.CF_PLAYWRIGHT_MODULE
  || '/home/sunbroke/rt/private__tmp__configflow-real-env-rebuild__browser-runtime/node_modules/playwright/index.mjs'
const LAB = '实验 Mihomo（CT152）'
const SECRETS = ['lab-manual-pass-before-7c1e', 'lab-manual-pass-after-93ab', 'lab-controller-secret-5d20']
const report = { scene, steps: [], browserErrors: [] }
let vite, browser

const credentials = JSON.parse(await readFile('/tmp/cf-pushdiff-lab/credentials.json', 'utf8'))
const login = await fetch(`http://127.0.0.1:${apiPort}/api/auth/login`, {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ username: credentials.username, password: credentials.password }) })
const { token } = await login.json()
assert.ok(token, 'login')

async function page(theme, viewport) {
  const phone = viewport.width < 500
  const context = await browser.newContext({ viewport, deviceScaleFactor: phone ? 2 : 1, colorScheme: theme, hasTouch: phone, isMobile: phone,
    locale: 'zh-CN', timezoneId: 'Asia/Shanghai' })
  await context.addInitScript(([value, mode]) => {
    localStorage.setItem('token', value)
    localStorage.setItem('username', 'admin')
    localStorage.setItem('configflow-theme', mode)
  }, [token, theme])
  const tab = await context.newPage()
  tab.on('pageerror', error => report.browserErrors.push(error.message))
  return tab
}

/** The card's sync label once the server has compared (it shows 正在核对配置 meanwhile). */
async function syncButton(tab, name, label) {
  const deadline = Date.now() + 150000
  await tab.goto(`http://127.0.0.1:${webPort}/agents`)
  for (;;) {
    const card = tab.locator('.cf-list-in').filter({ hasText: name })
    await card.locator('footer').waitFor({ timeout: 60000 })
    // The list renders before content checks answer; give the label a moment to settle.
    await tab.waitForTimeout(1500)
    const button = card.locator('[data-sync-diff]')
    if (await button.count() && (!label || (await button.innerText()).includes(label))) return { card, button }
    if (Date.now() >= deadline) {
      await tab.screenshot({ path: join(evidence, `missing-label-${scene}.png`), fullPage: true })
      report.steps.push({ name: 'cards when the label was missing', cards: (await tab.locator('.cf-list-in').allInnerTexts()).map(text => text.replace(/\s+/g, ' ').slice(0, 300)) })
      assert.fail(`no ${label || 'clickable'} label on ${name}`)
    }
    await tab.waitForTimeout(3000)
    await tab.reload()
  }
}

async function openDialog(tab, name, label, ready) {
  const { button } = await syncButton(tab, name, label)
  await button.scrollIntoViewIfNeeded()
  const box = await button.boundingBox()
  report.steps.push({ name: `${name}: sync label size`, width: box?.width, height: box?.height })
  await button.focus()
  await tab.keyboard.press('Enter')  // a real button: keyboard opens it
  const dialog = tab.locator('[data-push-diff]')
  await dialog.locator(ready).waitFor({ timeout: 180000 })
  await tab.mouse.move(0, 0)
  await tab.waitForTimeout(400)
  return dialog
}

async function noOverflow(tab, dialog, name) {
  const page = await tab.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
  const body = await dialog.evaluate(element => {
    const scroller = element.querySelector('.overflow-y-auto')
    return scroller.scrollWidth - scroller.clientWidth
  })
  const tables = await dialog.evaluate(element => [...element.querySelectorAll('.cf-table-wrap')]
    .map(wrap => wrap.scrollWidth - wrap.clientWidth))
  report.steps.push({ name: `${name}: no horizontal overflow`, page, body, tables })
  assert.ok(page <= 0 && body <= 0 && tables.every(value => value <= 0), `horizontal overflow ${JSON.stringify({ page, body, tables })}`)
}

async function assertNoSecrets(tab) {
  const html = await tab.content()
  for (const secret of SECRETS) assert.ok(!html.includes(secret), `secret visible: ${secret.slice(0, 12)}…`)
}

async function expand(dialog, key) {
  const row = dialog.locator(`[data-key="${key}"]`)
  const toggle = row.getByRole('button', { name: '展开明细' })
  if (await toggle.count()) await toggle.click()
}

async function shot(tab, dialog, file) {
  await tab.locator('[data-sonner-toast]').first().waitFor({ state: 'detached', timeout: 15000 }).catch(() => {})
  await tab.evaluate(() => document.activeElement?.blur())
  await tab.mouse.move(0, 0)
  await tab.waitForTimeout(250)
  await dialog.screenshot({ path: file })
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

  if (scene === 'changed') {
    const desktop = await page('light', { width: 1280, height: 900 })
    const dialog = await openDialog(desktop, LAB, '有变更，待推送', '[data-push-diff-changed]')
    const text = (await dialog.innerText()).replace(/\s+/g, ' ')
    report.steps.push({ name: 'changed (desktop, light)', text: text.slice(0, 2400) })
    assert.match(text, /主配置.*修改/)
    assert.match(text, /实验机场.*订阅.*修改.*节点 \+1 \/ −0/)
    assert.match(text, /实验直连.*规则集.*修改.*规则 \+2 \/ −1/)
    assert.match(text, /hosts\.printer\.lab\.invalid 新增 — 198\.51\.100\.30/)
    assert.match(text, /secret 新增 — 已隐藏/)
    await expand(dialog, 'proxies')
    const proxies = (await dialog.locator('[data-key="proxies"] + tr').innerText()).replace(/\s+/g, ' ')
    assert.match(proxies, /~ 手动 SS password：已修改（已隐藏）/)
    await assertNoSecrets(desktop)
    await shot(desktop, dialog, join(shots, 'push-diff-changed.png'))
    await dialog.locator('[data-push-diff-config]').scrollIntoViewIfNeeded()
    await dialog.getByRole('button', { name: '原始差异' }).click()
    const raw = await dialog.locator('#push-diff-raw-text').innerText()
    assert.match(raw, /\+ {2}printer\.lab\.invalid: 198\.51\.100\.30/)
    assert.match(raw, /\+secret: <已隐藏 [0-9a-f]{6}>/)
    report.steps.push({ name: 'raw diff (redacted)', lines: raw.split('\n').length })
    await assertNoSecrets(desktop)
    await dialog.locator('[data-push-diff-raw]').scrollIntoViewIfNeeded()
    await shot(desktop, dialog, join(shots, 'push-diff-raw.png'))
    await noOverflow(desktop, dialog, 'desktop')

    const phone = await page('dark', { width: 375, height: 812 })
    const mobile = await openDialog(phone, LAB, '有变更，待推送', '[data-push-diff-changed]')
    const touch = report.steps.at(-1)
    assert.ok(touch.height >= 44, `phone sync label ${touch.height}px high`)
    await noOverflow(phone, mobile, 'phone')
    await shot(phone, mobile, join(shots, 'push-diff-mobile-dark.png'))
    await expand(mobile, 'proxies')
    await noOverflow(phone, mobile, 'phone, details open')
    await assertNoSecrets(phone)
    await mobile.locator('[data-key="proxies"]').scrollIntoViewIfNeeded()
    await shot(phone, mobile, join(shots, 'push-diff-config-mobile-dark.png'))
  } else if (scene === 'nobaseline') {
    const desktop = await page('light', { width: 1280, height: 900 })
    const never = await openDialog(desktop, '实验 · 从未推送', '尚未推送', '[data-push-diff-unavailable]')
    const neverText = (await never.innerText()).replace(/\s+/g, ' ')
    assert.match(neverText, /没有可对比的基准.*还没有成功推送过配置/)
    report.steps.push({ name: 'never pushed', text: neverText })
    await shot(desktop, never, join(shots, 'push-diff-no-baseline.png'))
    await desktop.keyboard.press('Escape')
    const legacy = await openDialog(desktop, '实验 · 旧版本推送', '同步状态待确认', '[data-push-diff-unavailable]')
    const legacyText = (await legacy.innerText()).replace(/\s+/g, ' ')
    assert.match(legacyText, /没有可对比的基准.*旧版本/)
    report.steps.push({ name: 'old version push', text: legacyText })
    await shot(desktop, legacy, join(evidence, 'legacy-desktop-light.png'))
  } else if (scene === 'error') {
    const desktop = await page('light', { width: 1280, height: 900 })
    const { button } = await syncButton(desktop, '实验 · 旧版本推送', '同步状态待确认')
    // The record disappears after the page loaded (another tab deleted it): the request fails for real.
    const removed = await fetch(`http://127.0.0.1:${apiPort}/api/agents/lab-old-version`, {
      method: 'DELETE', headers: { Authorization: `Bearer ${token}` } })
    assert.equal(removed.status, 200)
    await button.click()
    const dialog = desktop.locator('[data-push-diff]')
    await dialog.locator('[data-push-diff-error]').waitFor({ timeout: 30000 })
    const text = (await dialog.innerText()).replace(/\s+/g, ' ')
    assert.match(text, /无法读取待推送的变更.*找不到这台 Agent/)
    report.steps.push({ name: 'request error', text })
    await desktop.mouse.move(0, 0)
    await shot(desktop, dialog, join(shots, 'push-diff-error.png'))
    await dialog.getByRole('button', { name: '重试' }).click()
    await dialog.locator('[data-push-diff-error]').waitFor({ timeout: 30000 })
    report.steps.push({ name: 'retry fails again with the same reason' })
  } else if (scene === 'synced') {
    const phone = await page('dark', { width: 375, height: 812 })
    const dialog = await openDialog(phone, LAB, '已同步', '[data-push-diff-synced]')
    const text = (await dialog.innerText()).replace(/\s+/g, ' ')
    assert.match(text, /无变更/)
    assert.doesNotMatch(text, /推送 *$/)
    report.steps.push({ name: 'synced after push (phone, dark)', text })
    await shot(phone, dialog, join(shots, 'push-diff-synced-mobile-dark.png'))
  } else {
    throw new Error('scene: changed | nobaseline | error | synced')
  }
  assert.deepEqual(report.browserErrors, [])
  console.log(JSON.stringify(report, null, 1))
} finally {
  await writeFile(join(evidence, `browser-${scene}.json`), JSON.stringify(report, null, 2))
  await browser?.close()
  await vite?.close()
}
