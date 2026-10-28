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
const basePath = `/${(process.env.DOCS_BASE || '/').replace(/^\/+|\/+$/g, '')}`.replace(/\/?$/, '/')
const base = `http://127.0.0.1:${port}${basePath}`
const errors = []
const tabPages = [
  { route: 'guide/install', groups: { 'panel-install-method': ['compose', 'docker-run', 'nas'] } },
  { route: 'guide/agent-install', groups: {
    'agent-install-method': ['linux-install', 'docker', 'gateway', 'routeros', 'ha'],
    'linux-install-choice': ['shell-single', 'shell-aio', 'shell-existing', 'cores-only'],
    'docker-install-content': ['docker-single', 'docker-aio'],
  } },
  { route: 'guide/first-run', groups: {
    'first-run-mode': ['mode-router', 'mode-gateway'],
    'first-run-connection': ['router', 'client'],
    'first-run-router-method': ['manual-routes', 'auto-routes'],
  } },
  { route: 'guide/routeros-install', groups: {
    'routeros-container-layout': ['routeros-two-containers', 'routeros-aio'],
    'routeros-create-method': ['routeros-winbox', 'routeros-command'],
  } },
  { route: 'guide/oxidns-ha', groups: {
    'ha-combinations': ['combination-shell-shell', 'combination-host-host', 'combination-macvlan-macvlan',
      'combination-shell-host', 'combination-shell-macvlan', 'combination-host-macvlan'],
    'ha-preparation': ['prepare-shell', 'prepare-host', 'prepare-macvlan'],
    'ha-existing-install': ['migration-shell', 'migration-docker', 'migration-existing-ha'],
  } },
  { route: 'manual/core-settings', groups: {
    'core-software': ['mihomo-settings', 'surge-loon-settings', 'linux-gateway-settings', 'mosdns-settings', 'oxidns-settings'],
    'oxidns-topics': ['oxidns-basic', 'oxidns-features', 'oxidns-management', 'oxidns-full-fields'],
  } },
]
const tabChecks = { groups: [], hashTargets: [], keyboardGroups: [], mobilePanels: 0, stableSwitches: 0 }
let browser, preview, page

const group = id => page.locator(`.docs-tabs[id="${id}"]`)
const tab = (id, panel) => group(id).locator(`:scope > [role="tablist"] > [role="tab"][aria-controls="${panel}"]`)
const panel = (id, name) => group(id).locator(`:scope > [role="tabpanel"][id="${name}"]`)
const settle = () => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))

async function open(route, hash = '') {
  await page.goto(`${base}${route}${hash ? `#${encodeURIComponent(hash)}` : ''}`)
  await page.locator('.vp-doc h1').waitFor()
  await page.evaluate(() => { document.documentElement.style.scrollBehavior = 'auto' })
  await settle()
}

async function assertActive(id, selected) {
  await page.waitForFunction(({ id, selected }) => {
    const root = document.getElementById(id)
    const active = root?.querySelector(':scope > [role="tablist"] > [aria-selected="true"]')
    const panel = document.getElementById(selected)
    return active?.getAttribute('aria-controls') === selected && panel && !panel.closest('[hidden]') && panel.getClientRects().length > 0
  }, { id, selected })
  const state = await group(id).evaluate(root => ({
    tabs: [...root.querySelectorAll(':scope > [role="tablist"] > [role="tab"]')].map(button => ({
      panel: button.getAttribute('aria-controls'), selected: button.getAttribute('aria-selected'), tabindex: button.tabIndex,
    })),
    panels: [...root.querySelectorAll(':scope > [role="tabpanel"]')].map(panel => ({ id: panel.id, hidden: panel.hidden })),
  }))
  assert.equal(state.tabs.filter(item => item.selected === 'true').length, 1, `${id}: multiple selected tabs`)
  assert.equal(state.panels.filter(item => !item.hidden).length, 1, `${id}: multiple displayed panels`)
  for (const item of state.tabs) {
    assert.equal(item.tabindex, item.panel === selected ? 0 : -1, `${id}/${item.panel}: incorrect keyboard tab stop`)
    assert.equal(item.selected, String(item.panel === selected), `${id}/${item.panel}: incorrect selected state`)
    assert.equal(await panel(id, item.panel).isVisible(), item.panel === selected, `${id}/${item.panel}: incorrect actual visibility`)
  }
}

async function select(id, selected, checkScroll = true) {
  const button = tab(id, selected)
  await button.waitFor({ state: 'visible' })
  // Put the row in the viewport first; Playwright's own scroll must not be mistaken for a component jump.
  await button.evaluate(element => window.scrollTo({ top: window.scrollY + element.getBoundingClientRect().top - 160, behavior: 'auto' }))
  await settle()
  const before = await page.evaluate(() => window.scrollY)
  const changed = await button.getAttribute('aria-selected') !== 'true'
  await button.click()
  await assertActive(id, selected)
  await settle()
  if (checkScroll) {
    const after = await page.evaluate(() => window.scrollY)
    assert(Math.abs(after - before) <= 2, `${id}/${selected}: tab switch moved page from ${before} to ${after}`)
    tabChecks.stableSwitches++
  }
  if (changed) assert.equal(decodeURIComponent(new URL(page.url()).hash.slice(1)), selected, `${id}: selection not reflected in URL`)
}

async function discoverGroups() {
  return page.locator('.vp-doc .docs-tabs').evaluateAll(roots => roots.map(root => {
    const ancestors = []
    let containing = root.parentElement?.closest('[role="tabpanel"]')
    while (containing) {
      ancestors.unshift({ group: containing.parentElement.id, panel: containing.id })
      containing = containing.parentElement.parentElement?.closest('[role="tabpanel"]')
    }
    return {
      id: root.id, ancestors,
      tabs: [...root.querySelectorAll(':scope > [role="tablist"] > [role="tab"]')].map(button => button.getAttribute('aria-controls')),
    }
  }))
}

async function revealAncestors(spec) {
  for (const ancestor of spec.ancestors) await select(ancestor.group, ancestor.panel)
}

async function assertOutline() {
  const badLinks = await page.locator('.VPDocAsideOutline a[href*="#"]').evaluateAll(links => links.flatMap(link => {
    const hash = new URL(link.href).hash.slice(1)
    let target
    try { target = document.getElementById(decodeURIComponent(hash)) } catch { return [link.href] }
    return target?.closest('.docs-tabs__panel') ? [link.textContent.trim()] : []
  }))
  assert.deepEqual(badLinks, [], `Page outline exposes tab-only headings: ${badLinks.join(', ')}`)
}

async function checkKeyboard(spec) {
  await select(spec.id, spec.tabs[0])
  await tab(spec.id, spec.tabs[0]).focus()
  for (const [key, expected] of [
    ['End', spec.tabs.at(-1)], ['Home', spec.tabs[0]], ['ArrowLeft', spec.tabs.at(-1)],
    ['ArrowRight', spec.tabs[0]], ['ArrowRight', spec.tabs[1]],
  ]) {
    await page.keyboard.press(key)
    await assertActive(spec.id, expected)
    assert(await tab(spec.id, expected).evaluate(element => document.activeElement === element), `${spec.id}: ${key} lost focus`)
  }
  tabChecks.keyboardGroups.push(spec.id)
}

async function checkDeepLink(route, id, selected, targetSelector) {
  await open(route)
  const targetId = targetSelector ? await page.locator(targetSelector).first().getAttribute('id') : selected
  assert(targetId, `${route}: missing deep-link target ${targetSelector}`)
  await open(route, targetId)
  await assertActive(id, selected)
  const target = page.locator(`[id="${targetId}"]`)
  await target.waitFor({ state: 'visible' })
  await settle()
  const position = await target.boundingBox()
  assert(position && position.y >= 0 && position.y < 1000, `${route}#${targetId}: visible target not scrolled into view`)
  await page.reload()
  await assertActive(id, selected)
  await target.waitFor({ state: 'visible' })
  tabChecks.hashTargets.push({ route, target: targetId, selected, reload: true })
}

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
  page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, reducedMotion: 'reduce' })
  page.on('pageerror', error => errors.push(error.message))
  page.on('response', response => { if (response.status() >= 400) errors.push(`${response.status()}: ${response.url()}`) })
  await open('README')
  await page.getByRole('heading', { name: /ConfigFlow 使用手册/ }).waitFor()
  await page.screenshot({ path: join(output, 'desktop.png') })
  assert(await page.locator('.VPSidebar').isVisible(), 'Desktop sidebar missing')
  assert(await page.locator('.VPDocAsideOutline').isVisible(), 'Desktop page outline missing')
  await page.getByRole('button', { name: '搜索文档' }).click()
  await page.locator('#localsearch-input').fill('高可用')
  await page.locator('.VPLocalSearchBox .result').first().waitFor()
  const searchResults = await page.locator('.VPLocalSearchBox .result').count()
  assert(searchResults > 0, 'Local search returned no results')
  const resultLinks = await page.locator('.VPLocalSearchBox .result').evaluateAll(results => results
    .map(result => result.matches('a') ? result.href : result.querySelector('a')?.href).filter(Boolean))
  assert(resultLinks.length > 0, 'Search results have no links')
  assert(resultLinks.every(link => new URL(link).pathname.startsWith(basePath)), 'Search result escaped the deployment base path')
  await page.keyboard.press('Escape')

  // Visit every group, including nested groups, without accidentally counting a child's tabs in its parent.
  for (const entry of tabPages) {
    await open(entry.route)
    const groups = await discoverGroups()
    assert.deepEqual(groups.map(item => item.id).sort(), Object.keys(entry.groups).sort(), `${entry.route}: missing/unexpected tab groups`)
    await assertOutline()
    for (const spec of groups) {
      assert.deepEqual(spec.tabs, entry.groups[spec.id], `${spec.id}: incorrect choices/order`)
      await revealAncestors(spec)
      for (const selected of spec.tabs) await select(spec.id, selected)
      await checkKeyboard(spec)
      await assertOutline()
      tabChecks.groups.push({ route: entry.route, id: spec.id, tabs: spec.tabs, nested: spec.ancestors.length > 0 })
    }
  }

  // Both Bridge/Host code groups must work after choosing the enclosing installation method.
  await open('guide/install')
  const codeModes = []
  for (const parent of ['compose', 'docker-run']) {
    await select('panel-install-method', parent)
    const codeGroup = panel('panel-install-method', parent).locator('.vp-code-group')
    assert.equal(await codeGroup.count(), 1, `${parent}: missing network-mode code group`)
    for (const mode of ['Host 模式', 'Bridge 模式（推荐）']) {
      await codeGroup.locator('.tabs label').filter({ hasText: mode }).click()
      const active = codeGroup.locator('.blocks > .active')
      await active.waitFor({ state: 'visible' })
      assert.equal(await codeGroup.locator('.blocks > div:visible').count(), 1, `${parent}: multiple network commands visible`)
      const command = await active.innerText()
      assert(command.includes(mode.startsWith('Host') ? (parent === 'compose' ? 'network_mode: host' : '--network host') : (parent === 'compose' ? '5010:80' : '--network bridge')), `${parent}/${mode}: wrong command shown`)
      codeModes.push(`${parent}/${mode}`)
    }
  }
  await group('panel-install-method').evaluate(element => window.scrollTo({ top: window.scrollY + element.getBoundingClientRect().top - 100, behavior: 'auto' }))
  await settle()
  await page.screenshot({ path: join(output, 'install-tabs.png'), animations: 'disabled' })

  // Existing links to headings inside hidden panels must reveal the matching parent(s), even after reload.
  await checkDeepLink('guide/install', 'panel-install-method', 'nas', '#nas h3[id]')
  await checkDeepLink('guide/routeros-install', 'routeros-create-method', 'routeros-command')
  await assertActive('routeros-container-layout', 'routeros-two-containers')
  await checkDeepLink('guide/first-run', 'first-run-router-method', 'auto-routes', '#auto-routes h4[id]')
  await assertActive('first-run-connection', 'router')
  await checkDeepLink('manual/core-settings', 'oxidns-topics', 'oxidns-management', '#oxidns-management h4[id], #oxidns-management h3[id]')
  await assertActive('core-software', 'oxidns-settings')
  for (const combination of tabPages.find(entry => entry.route === 'guide/oxidns-ha').groups['ha-combinations']) {
    await checkDeepLink('guide/oxidns-ha', 'ha-combinations', combination)
  }

  // A same-page link in one panel can reveal another panel; browser history restores each choice.
  await open('guide/install', 'nas')
  await panel('panel-install-method', 'nas').locator('a[href="#compose"]').click()
  await assertActive('panel-install-method', 'compose')
  await open('guide/install')
  await select('panel-install-method', 'docker-run')
  await select('panel-install-method', 'nas')
  await page.goBack()
  await assertActive('panel-install-method', 'docker-run')
  await page.goBack()
  assert.equal(new URL(page.url()).hash, '', 'History did not return to the original unhashed URL')
  await assertActive('panel-install-method', 'compose')
  await page.goForward()
  await assertActive('panel-install-method', 'docker-run')
  await page.goForward()
  await assertActive('panel-install-method', 'nas')

  // Search must index hidden prose and open its containing panel, not leave the result invisible.
  await open('guide/install')
  assert.equal(await panel('panel-install-method', 'nas').isVisible(), false)
  const nasTargets = await panel('panel-install-method', 'nas').locator('[id]').evaluateAll(elements => elements.map(element => element.id))
  nasTargets.push('nas')
  await page.getByRole('button', { name: '搜索文档' }).click()
  await page.locator('#localsearch-input').fill('NAS')
  await page.locator('.VPLocalSearchBox .result').first().waitFor()
  await page.waitForFunction(({ basePath, targets }) => [...document.querySelectorAll('.VPLocalSearchBox a[href]')].some(link => {
    const url = new URL(link.href)
    return url.pathname.startsWith(`${basePath}guide/install`) && targets.includes(decodeURIComponent(url.hash.slice(1)))
  }), { basePath, targets: nasTargets })
  const hiddenSearch = await page.locator('.VPLocalSearchBox a[href]').evaluateAll((links, { basePath, targets }) => {
    const index = links.findIndex(link => {
      const url = new URL(link.href)
      return url.pathname.startsWith(`${basePath}guide/install`) && targets.includes(decodeURIComponent(url.hash.slice(1)))
    })
    return { index, href: links[index]?.href }
  }, { basePath, targets: nasTargets })
  const hiddenSearchHref = hiddenSearch.href
  await page.locator('.VPLocalSearchBox a[href]').nth(hiddenSearch.index).click()
  await page.locator('#localsearch-input').waitFor({ state: 'hidden' })
  await assertActive('panel-install-method', 'nas')
  await page.locator(`[id="${decodeURIComponent(new URL(hiddenSearchHref).hash.slice(1))}"]`).waitFor({ state: 'visible' })

  await open('guide/oxidns-ha', 'combinations')
  await page.getByRole('heading', { name: /先选部署组合/ }).waitFor()
  assert.equal(await page.locator('.vp-doc table').first().locator('tbody tr').count(), 6)
  await page.screenshot({ path: join(output, 'ha-combinations.png') })

  // Repeat all choices at phone width, including nested panels; buttons must remain usable by touch.
  await page.setViewportSize({ width: 390, height: 844 })
  let minTouchHeight = Infinity
  for (const entry of tabPages) {
    await open(entry.route)
    for (const spec of await discoverGroups()) {
      await revealAncestors(spec)
      for (const selected of spec.tabs) {
        await select(spec.id, selected)
        const sizes = await group(spec.id).locator(':scope > [role="tablist"] > [role="tab"]').evaluateAll(buttons => buttons.map(button => ({
          width: button.getBoundingClientRect().width, height: button.getBoundingClientRect().height,
        })))
        for (const size of sizes) {
          assert(size.width >= 44 && size.height >= 44, `${spec.id}: undersized phone touch target ${JSON.stringify(size)}`)
          minTouchHeight = Math.min(minTouchHeight, size.height)
        }
        assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), `${entry.route}/${selected}: page overflows mobile width`)
        tabChecks.mobilePanels++
      }
    }
  }
  await open('guide/oxidns-ha', 'combination-host-macvlan')
  await group('ha-combinations').evaluate(element => window.scrollTo({ top: window.scrollY + element.getBoundingClientRect().top - 100, behavior: 'auto' }))
  await settle()
  await page.screenshot({ path: join(output, 'ha-mobile.png'), animations: 'disabled' })

  await open('guide/install')
  await page.getByRole('heading', { name: /安装 ConfigFlow 管理面板/ }).waitFor()
  const screenshotImage = page.locator('.vp-doc img').first()
  await screenshotImage.scrollIntoViewIfNeeded()
  await page.waitForFunction(() => {
    const image = document.querySelector('.vp-doc img')
    return image?.complete && image.naturalWidth > 0
  })
  await page.evaluate(() => window.scrollTo(0, 0))
  await page.getByRole('button', { name: '文档目录' }).click()
  assert(await page.locator('.VPSidebar').isVisible(), 'Mobile directory missing')
  await page.locator(`.VPSidebar a[href="${basePath}guide/oxidns-ha"]`).click()
  await page.getByRole('heading', { name: /OxiDNS 高可用：两台设备共用一个 DNS 地址/ }).waitFor()
  await page.locator('.VPSidebar.open').waitFor({ state: 'detached' })
  // Let the directory's CSS closing transition finish before recording the page.
  await page.waitForTimeout(350)
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), 'Page overflows mobile width')
  await page.screenshot({ path: join(output, 'mobile.png'), animations: 'disabled' })
  await writeFile(join(output, 'browser-report.json'), JSON.stringify({
    checkedAt: new Date().toISOString(), basePath, desktop: '1440x1000', mobile: '390x844',
    sidebar: true, pageOutline: true, searchResults, haCombinations: 6,
    mobileNavigation: true, noPageOverflow: true, searchLinksWithinBase: true, imageLoaded: true,
    tabs: { ...tabChecks, codeModes, minTouchHeight, outlineHidesPanelHeadings: true,
      hiddenSearchHref, hiddenSearchRevealed: true, history: 'back, unhashed initial state, forward', samePagePanelLink: true },
    errors,
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
