import { readFile, writeFile, readdir } from 'node:fs/promises'
import { resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import strictAssert from 'node:assert/strict'
let assertionsExecuted = 0
const assert = Object.fromEntries(['ok', 'equal', 'deepEqual'].map(method => [method, (...args) => {
  assertionsExecuted++
  return strictAssert[method](...args)
}]))
const info = JSON.parse(await readFile(process.argv[2], 'utf8'))
const { chromium } = await import(pathToFileURL(resolve(process.env.PLAYWRIGHT_RUNTIME || '/tmp/configflow-entry-flow-e2e-runtime', 'node_modules/playwright/index.mjs')))
const base = `http://127.0.0.1:${info.frontend_port}`
const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] })
const report = { success: false, checks: [], errors: [], saves: [] }
let activePage
const screenshot = (page, name) => page.screenshot({ path: resolve(info.output, `${name}.png`), fullPage: false })
const waitEnabled = async locator => {
  await locator.waitFor({ state: 'visible' })
  await locator.evaluate(element => new Promise((resolve, reject) => {
    const deadline = Date.now() + 15000
    const poll = () => !element.disabled ? resolve() : Date.now() > deadline ? reject(new Error('Control stayed disabled')) : setTimeout(poll, 30)
    poll()
  }))
}
async function openSettings(page) {
  await page.goto(base + '/generate?oxidns=config')
  await page.locator('[data-settings-workspace]').waitFor({ state: 'visible', timeout: 30000 })
  await page.getByRole('button', { name: '常用设置', exact: true }).click()
  await page.getByRole('tab', { name: '高级', exact: true }).click()
  await page.getByRole('button', { name: '保存草稿', exact: true }).waitFor({ state: 'visible' })
}
async function chooseRoot(page, fixture) {
  const entry = page.getByRole('combobox', { name: '监听入口流程', exact: true })
  await entry.waitFor({ state: 'visible' })
  if (!(await entry.innerText()).includes(fixture.edit_tag)) {
    await entry.click()
    await page.getByRole('option', { name: `${fixture.edit_tag} · ${fixture.listeners.length > 1 && fixture.id === 'e2e-shared' ? 2 : 1} 个监听入口`, exact: true }).click()
  }
  await page.getByRole('button', { name: `编辑流程参数 ${fixture.edit_tag}`, exact: true }).waitFor({ state: 'visible' })
}
async function layout(page, name) {
  const metrics = await page.evaluate(() => ({ viewport: innerWidth, document: document.documentElement.scrollWidth, body: document.body.scrollWidth }))
  assert.ok(metrics.document <= metrics.viewport && metrics.body <= metrics.viewport, `${name}: ${JSON.stringify(metrics)}`)
  const controls = page.locator('[aria-label="入口流程可视化"] [data-slot="select-trigger"]')
  const count = await controls.count()
  assert.ok(count >= 2, `Expected nonempty flow controls, found ${count}`)
  const dimensions = await controls.evaluateAll(elements => elements.map(el => ({ name: el.getAttribute('aria-label'), width: el.getBoundingClientRect().width, height: el.getBoundingClientRect().height })))
  assert.equal(dimensions.length, count)
  assert.ok(dimensions.every(item => item.height >= 44 && item.width >= 44), JSON.stringify(dimensions))
  report.checks.push({ name, ...metrics, flow_controls: dimensions })
}
async function editStep(page, fixture) {
  const step = fixture.id === 'e2e-handwritten' ? 2 : 1
  await page.getByRole('button', { name: `编辑步骤 ${step}`, exact: true }).click()
  return { panel: page.locator('[aria-label="节点参数编辑"]'), step }
}
async function save(page, fixture, api) {
  const button = page.getByRole('button', { name: '保存草稿', exact: true })
  await waitEnabled(button)
  const [response] = await Promise.all([
    page.waitForResponse(response => new URL(response.url()).pathname === '/api/oxidns/settings' && response.request().method() === 'POST'),
    button.click(),
  ])
  assert.equal(response.status(), 200, await response.text())
  const payload = await response.json()
  assert.equal(payload.success, true)
  const headers = await response.allHeaders()
  assert.ok(headers.etag, 'Save must return actual stored ETag')
  assert.ok(response.request().headers()['if-match'], 'UI must save with the loaded ETag')
  const stored = await api('/api/oxidns/settings')
  assert.deepEqual(stored, payload.settings)
  report.saves.push({ profile: fixture.id, status: response.status(), etag: headers.etag, if_match_present: true })
  return stored
}
try {
  for (const fixture of info.profiles) {
    const context = await browser.newContext({ viewport: { width: 1280, height: 1000 }, reducedMotion: 'reduce' })
    await context.addInitScript(({ token, profile }) => {
      localStorage.setItem('token', token)
      localStorage.setItem('username', 'entry-flow-e2e')
      localStorage.setItem('configflow-theme', 'dark')
      sessionStorage.setItem('configflow.activeProfile', profile)
    }, { token: info.token, profile: fixture.id })
    const page = await context.newPage()
    activePage = page
    page.on('pageerror', error => report.errors.push(String(error)))
    const api = async path => {
      const response = await page.request.get(base + path, { headers: { Authorization: `Bearer ${info.token}`, 'X-ConfigFlow-Profile': fixture.id } })
      assert.equal(response.status(), 200)
      return response.json()
    }
    const before = await api('/api/oxidns/settings')
    await openSettings(page)
    assert.deepEqual(await api('/api/oxidns/settings'), before, 'Opening editor must not persist changes')
    report.checks.push({ profile: fixture.id, full_settings_page_opened: true, no_write_on_open: true })
    if (info.bootstrap_only) {
      await screenshot(page, `${fixture.id}-bootstrap`)
      await context.close()
      continue
    }
    await chooseRoot(page, fixture)
    if (fixture.id === 'e2e-http') {
      await page.getByRole('button', { name: '编辑监听 http_listener /dns-a', exact: true }).waitFor()
      assert.equal(await page.getByRole('button', { name: '编辑监听 http_listener /dns-b', exact: true }).count(), 0)
      await page.getByRole('combobox', { name: '监听入口流程', exact: true }).click()
      await page.getByRole('option', { name: 'entry_b · 1 个监听入口', exact: true }).click()
      await page.getByRole('button', { name: '编辑监听 http_listener /dns-b', exact: true }).waitFor()
      assert.equal(await page.getByRole('button', { name: '编辑监听 http_listener /dns-a', exact: true }).count(), 0)
      await chooseRoot(page, fixture)
      report.checks.push({ profile: fixture.id, http_paths_select_distinct_flows: true })
    }
    if (fixture.id === 'e2e-shared') {
      await page.getByRole('status').filter({ hasText: '以上 2 个监听入口共用 shared_entry' }).waitFor()
      report.checks.push({ profile: fixture.id, shared_effect_visible: true })
    }
    if (fixture.id === 'e2e-independent') {
      // Invalid node drafts cannot be applied or globally saved. Cancel is lossless.
      const { panel, step } = await editStep(page, fixture)
      await panel.getByRole('button', { name: 'YAML', exact: true }).click()
      await panel.getByRole('textbox', { name: `步骤 ${step} YAML`, exact: true }).fill('exec: [\n')
      await panel.getByRole('alert').first().waitFor()
      assert.equal(await panel.getByRole('button', { name: '应用节点修改', exact: true }).isDisabled(), true)
      assert.equal(await page.getByRole('button', { name: '保存草稿', exact: true }).isDisabled(), true)
      await panel.getByRole('button', { name: '取消节点修改', exact: true }).click()
      assert.deepEqual(await api('/api/oxidns/settings'), before)
      report.checks.push({ profile: fixture.id, invalid_yaml_blocks_apply_and_save: true, cancel_preserves_persisted_config: true })
    }
    if (fixture.case === 'forward') {
      await page.getByRole('button', { name: `编辑流程参数 ${fixture.edit_tag}`, exact: true }).click()
      const panel = page.locator('[aria-label="节点参数编辑"]')
      // Use the real schema form; opening does not convert a forward to sequence.
      const address = panel.getByRole('textbox', { name: 'local_dns 参数.args.upstreams[1].addr 值', exact: true })
      if (!(await address.isVisible())) {
        const expand = panel.getByRole('button', { name: 'local_dns 参数.args.upstreams[1] 展开参数', exact: true })
        if (await expand.count()) await expand.click()
      }
      await address.fill(info.remote_upstream)
    } else {
      const { panel } = await editStep(page, fixture)
      await panel.getByRole('combobox', { name: '步骤动作快捷选择', exact: true }).click()
      await page.getByRole('option', { name: '转入流程 · remote_flow · sequence', exact: true }).click()
    }
    const apply = page.getByRole('button', { name: '应用节点修改', exact: true })
    await waitEnabled(apply)
    const [applied] = await Promise.all([
      page.waitForResponse(response => new URL(response.url()).pathname === '/api/oxidns/common-settings/apply' && response.request().method() === 'POST'),
      apply.click(),
    ])
    assert.equal(applied.status(), 200, await applied.text())
    const patch = applied.request().postDataJSON().changes.entry_flow
    assert.equal(patch.source, fixture.edit_source)
    assert.equal(patch.before.tag, fixture.edit_tag)
    assert.deepEqual(await api('/api/oxidns/settings'), before, 'Applying a node must still be a nonpersistent draft')
    if (fixture.id === 'e2e-handwritten') {
      const actualApply = async button => {
        await waitEnabled(button)
        const [response] = await Promise.all([
          page.waitForResponse(response => new URL(response.url()).pathname === '/api/oxidns/common-settings/apply' && response.request().method() === 'POST'),
          button.click(),
        ])
        assert.equal(response.status(), 200, await response.text())
        return (await response.json()).entry_flow.plugins.find(node => node.tag === fixture.edit_tag).value.args
      }
      const originalSteps = structuredClone(patch.value.args)
      const moved = structuredClone(originalSteps)
      ;[moved[0], moved[1]] = [moved[1], moved[0]]
      assert.deepEqual(await actualApply(page.getByRole('button', { name: '上移步骤 2', exact: true })), moved)
      assert.deepEqual(await actualApply(page.getByRole('button', { name: '下移步骤 1', exact: true })), originalSteps)
      const insert = page.getByRole('button', { name: '在步骤 1 后插入', exact: true })
      await waitEnabled(insert)
      await insert.click()
      const panel = page.locator('[aria-label="节点参数编辑"]')
      await panel.getByRole('combobox', { name: '步骤动作快捷选择', exact: true }).click()
      await page.getByRole('option', { name: '返回上级序列', exact: true }).click()
      const inserted = structuredClone(originalSteps)
      inserted.splice(1, 0, { exec: 'return' })
      assert.deepEqual(await actualApply(panel.getByRole('button', { name: '应用节点修改', exact: true })), inserted)
      assert.deepEqual(await actualApply(page.getByRole('button', { name: '删除步骤 2', exact: true })), originalSteps)
      assert.deepEqual(await api('/api/oxidns/settings'), before)
      report.checks.push({ profile: fixture.id, move_up_down_and_insert_delete_actual_api: true, exact_order_and_unknown_fields_restored: true })
    }
    const saved = await save(page, fixture, api)
    await openSettings(page)
    await chooseRoot(page, fixture)
    assert.deepEqual(await api('/api/oxidns/settings'), saved, 'Reload must retain exactly the committed settings')
    if (fixture.case === 'forward') {
      await page.getByRole('button', { name: `编辑流程参数 ${fixture.edit_tag}`, exact: true }).click()
      const panel = page.locator('[aria-label="节点参数编辑"]')
      await panel.getByRole('button', { name: 'YAML', exact: true }).click()
      const text = await panel.getByRole('textbox', { name: 'local_dns 参数 YAML', exact: true }).inputValue()
      assert.ok(text.includes(info.remote_upstream))
      await panel.getByRole('button', { name: '取消节点修改', exact: true }).click()
    } else {
      const step = fixture.id === 'e2e-handwritten' ? 2 : 1
      assert.ok((await page.getByRole('button', { name: `编辑步骤 ${step}`, exact: true }).innerText()).includes('goto remote_flow'))
    }
    report.checks.push({ profile: fixture.id, actual_node_apply_http_200: true, actual_save_http_200: true, reopened_saved_value: true, source: patch.source })
    await screenshot(page, `${fixture.id}-saved`)
    if (fixture.id === 'e2e-independent') {
      for (const width of [1280, 375]) {
        await page.setViewportSize({ width, height: width === 375 ? 900 : 1000 })
        await openSettings(page)
        await chooseRoot(page, fixture)
        for (const theme of ['dark', 'light']) for (const accent of ['clay', 'kraft', 'sky']) {
          await page.evaluate(({ theme, accent }) => { document.documentElement.dataset.theme = theme; document.documentElement.dataset.accent = accent; document.documentElement.dataset.motion = 'off' }, { theme, accent })
          const name = `${width}-${theme}-${accent}`
          await page.getByRole('combobox', { name: '监听入口流程', exact: true }).scrollIntoViewIfNeeded()
          await layout(page, `${name}-graph`)
          await screenshot(page, `${name}-graph`)
          const { panel } = await editStep(page, fixture)
          await panel.scrollIntoViewIfNeeded()
          await layout(page, `${name}-form`)
          const applyDimensions = await panel.getByRole('button', { name: '应用节点修改', exact: true }).boundingBox()
          assert.ok(applyDimensions && applyDimensions.height >= 44)
          await screenshot(page, `${name}-form`)
          await panel.getByRole('button', { name: '取消节点修改', exact: true }).click()
        }
      }
    }
    await context.close()
  }
  assert.deepEqual(report.errors, [])
  report.success = true
} catch (error) {
  report.failure = String(error.stack || error)
  if (activePage && !activePage.isClosed()) await screenshot(activePage, 'failure').catch(() => {})
  throw error
} finally {
  await browser.close()
  report.screenshots = (await readdir(info.output)).filter(name => name.endsWith('.png')).sort()
  report.screenshot_count = report.screenshots.length
  report.assertions_executed = assertionsExecuted
  await writeFile(resolve(info.output, 'browser-report.json'), JSON.stringify(report, null, 2))
}
console.log(JSON.stringify({ success: report.success, checks: report.checks.length, saves: report.saves.length, assertions: report.assertions_executed, screenshots: report.screenshot_count }))
