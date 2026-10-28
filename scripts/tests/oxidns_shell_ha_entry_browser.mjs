// Run on the Linux test server against an already-built production preview.
// Real API: generate and download a Shell installation script, without executing it.
// Fixtures: read-only Agent lists/environments for Shell + Shell / Shell + Docker pairing UI.
// Never enables HA, writes an Agent, registers a device, or changes a live service.
import { mkdir, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import assert from 'node:assert/strict'

process.umask(0o077)
const preview = process.env.CF_PREVIEW_ROOT || '/home/sunbroke/previews/configflow-oxidns-ha-guide'
const base = process.env.CF_PREVIEW_URL || 'http://192.168.0.32:15810'
const evidence = process.env.CF_EVIDENCE_DIR || join(preview, 'evidence')
const playwright = process.env.CF_PLAYWRIGHT || '/home/sunbroke/rt/private__tmp__configflow-real-env-rebuild__browser-runtime/node_modules/playwright/index.mjs'
const { chromium } = await import(playwright)
await mkdir(evidence, { recursive: true })
const reportPath = join(evidence, 'oxidns-shell-ha-entry-browser.json')
const report = {
  started_at: new Date().toISOString(), preview: base, cases: [],
  real_api_scope: 'Generate and retrieve Shell Agent + OxiDNS core installation scripts; never execute them',
  agent_binary_check: 'HEAD availability and non-empty binary metadata only; no architecture execution',
  agent_binary_downloads: [],
  fixture_scope: 'Read-only Agent list and HA environment responses for pairing UI; no live HA activation',
  installed_or_registered: false, ha_activated: false, errors: 0,
}
const browser = await chromium.launch({ headless: true })
const pageErrors = []
const forbiddenWrites = []
let phase = 'starting'
let failure

const checkLayout = async (page, width) => {
  const dimensions = await page.evaluate(() => {
    const dialog = [...document.querySelectorAll('[role="dialog"]')].find(item => item.getAttribute('data-state') === 'open')
    return { page: document.documentElement.scrollWidth, dialog: dialog?.clientWidth, content: dialog?.scrollWidth }
  })
  assert.ok(dimensions.page <= width, 'Page overflows horizontally')
  assert.ok(dimensions.content <= dimensions.dialog + 1, 'Installation dialog overflows horizontally')
}

try {
  for (const { width, theme } of [{ width: 1440, theme: 'light' }, { width: 375, theme: 'dark' }]) {
    phase = `${width}: real Shell entry`
    const context = await browser.newContext({ viewport: { width, height: 960 }, colorScheme: theme, reducedMotion: 'reduce' })
    const auth = await context.request.get(base + '/api/auth/status')
    assert.equal((await auth.json()).authEnabled, false, 'Test preview must remain login-free')
    if (!report.agent_binary_downloads.length) {
      phase = `${width}: real Agent binary availability`
      report.agent_binary_downloads = await Promise.all(['amd64', 'arm64', 'armv7'].map(async architecture => {
        const response = await context.request.head(`${base}/api/agents/download/configflow-agent-linux-${architecture}`)
        const headers = response.headers()
        const contentType = (headers['content-type'] || '').split(';')[0].trim()
        const contentLength = Number(headers['content-length'])
        assert.equal(response.status(), 200, `${architecture} Agent download must be available`)
        assert.equal(contentType, 'application/octet-stream', `${architecture} Agent download must be a binary`)
        assert.ok(Number.isFinite(contentLength) && contentLength > 0, `${architecture} Agent binary must not be empty`)
        return { architecture, http_status: response.status(), content_type: contentType, content_length: contentLength }
      }))
      phase = `${width}: real Shell entry`
    }
    const liveAgents = async () => {
      const response = await context.request.get(base + '/api/agents')
      assert.equal(response.status(), 200)
      return (await response.json()).map(agent => agent.id).sort()
    }
    const agentsBefore = await liveAgents()
    const profilesResponse = await context.request.get(base + '/api/profiles')
    assert.equal(profilesResponse.status(), 200)
    const profiles = await profilesResponse.json()
    const profile = profiles.find(item => item.is_default) || profiles[0]
    assert.ok(profile?.id, 'A real configuration profile is required')
    await context.addInitScript(({ theme, base, profileId }) => {
      localStorage.setItem('configflow-theme', theme)
      localStorage.setItem('serverDomain', base)
      sessionStorage.setItem('configflow.activeProfile', profileId)
    }, { theme, base, profileId: profile.id })
    // Explicitly allow only the requested script-generation write; all HA/Agent writes are blocked.
    await context.route('**/api/**', async route => {
      const request = route.request()
      const path = new URL(request.url()).pathname
      if (['GET', 'HEAD'].includes(request.method()) || (request.method() === 'POST' && path === '/api/agents/install-command')) {
        await route.continue()
      } else {
        forbiddenWrites.push(`${request.method()} ${path}`)
        await route.abort('blockedbyclient')
      }
    })
    const page = await context.newPage()
    page.on('pageerror', error => pageErrors.push(error.name))
    await page.goto(base + '/agents')
    report.entry_asset = await page.locator('script[type="module"][src]').first().getAttribute('src')
    await page.getByRole('button', { name: /生成\s*安装脚本/ }).first().click()
    const usage = page.locator('[role="radiogroup"][aria-label="使用模式"]')
    assert.deepEqual(await usage.locator('[role="radio"]').evaluateAll(items => items.map(item => item.getAttribute('value'))), ['ordinary', 'gateway'])
    assert.equal(await page.locator('[role="radio"][value="shell"]').getAttribute('aria-checked'), 'true')
    await page.locator('[role="radio"][value="oxidns"]').click()
    const modes = page.locator('[role="radiogroup"][aria-label="OxiDNS 部署方式"]')
    assert.equal(await modes.locator('[value="standard"]').getAttribute('aria-checked'), 'true')
    await modes.locator('[value="ha"]').click()
    const shellGuide = page.locator('[aria-label="OxiDNS Shell HA 安装引导"]')
    await shellGuide.waitFor()
    assert.equal(await page.locator('[aria-label="OxiDNS Docker HA 安装引导"]').count(), 0)
    assert.ok(await page.locator('[aria-label="Shell 高可用安装准备"]').isVisible())
    assert.ok(await page.locator('#agent-name').count(), 'Shell HA preparation retains the ordinary form')
    await modes.scrollIntoViewIfNeeded()
    await checkLayout(page, width)
    // No generated command, enrollment URL, or token exists in this screenshot.
    await page.screenshot({ path: join(evidence, `oxidns-shell-ha-entry-${width}.png`), fullPage: false })

    phase = `${width}: real Agent + core script generation`
    await page.locator('#agent-name').fill(`shell-ha-ui-${width}`)
    await page.locator('#agent-ip').fill(width === 1440 ? '192.168.0.241' : '192.168.0.242')
    await page.locator('#agent-port').fill('8082')
    assert.equal(await page.locator('#agent-install-core').getAttribute('aria-checked'), 'true', 'HA installation defaults to the OxiDNS core')
    const pending = page.waitForResponse(response => new URL(response.url()).pathname === '/api/agents/install-command' && response.request().method() === 'POST')
    await page.getByRole('button', { name: '生成安装命令', exact: true }).click()
    const response = await pending
    assert.equal(response.status(), 200, 'Real Shell install-command API failed')
    const requested = response.request().postDataJSON()
    assert.equal(requested.type, 'oxidns')
    assert.equal(requested.install_core, true)
    assert.equal(requested.prepare_dns_ha, true)
    assert.equal(requested.port, 8082)
    const generated = await response.json()
    assert.ok(generated.script.startsWith('#!/'), 'Backend must return a shell script')
    assert.equal(generated.script.split('\n')[1], '# ConfigFlow Agent + core installer interface: 1')
    assert.ok(generated.script.split('\n').slice(1, 4).includes('# ConfigFlow DNS HA preparation interface: 1'), 'Downloaded script must declare automatic HA preparation')
    assert.match(generated.download_path, /^\/api\/agents\/i\/[A-Za-z0-9_-]+$/)
    const script = await context.request.get(base + generated.download_path)
    assert.equal(script.status(), 200, 'Real generated installer link must download')
    assert.equal(await script.text(), generated.script, 'Downloaded script differs from generated snapshot')
    const commandField = page.locator('[aria-label="一键安装命令"] textarea').first()
    await commandField.waitFor()
    const command = await commandField.inputValue()
    assert.equal(command, `curl -fsSL '${base + generated.download_path}' -o configflow-install.sh && sh configflow-install.sh`, 'All HA preparation stays inside the short-link script')
    await page.getByRole('button', { name: '2. 验证与配对', exact: true }).click()
    assert.equal(await page.locator('#agent-name').count(), 0)
    assert.equal(await page.getByRole('button', { name: '生成安装命令', exact: true }).count(), 0)
    await page.getByRole('button', { name: '1. 安装与准备', exact: true }).click()
    assert.equal(await commandField.inputValue(), command, 'Completed script must survive a round-trip to verification')
    assert.equal(await page.locator('#agent-name').inputValue(), `shell-ha-ui-${width}`)

    phase = `${width}: fixture existing Shell Agent entry and pairing`
    const fixtures = [
      { id: 'fixture-shell-a', name: 'Fixture Shell A', host: '192.168.0.241', deployment_method: 'shell' },
      { id: 'fixture-shell-b', name: 'Fixture Shell B', host: '192.168.0.242', deployment_method: 'shell' },
      { id: 'fixture-docker-c', name: 'Fixture Docker C', host: '192.168.0.243', deployment_method: 'docker' },
    ].map(agent => ({ ...agent, port: 8082, service_type: 'oxidns', status: 'online', profile_id: profile.id,
      enabled: true, version: 'fixture-read-only', config_version: '', last_heartbeat: new Date().toISOString(), token: '',
    }))
    const environmentReads = new Set()
    await page.route('**/api/agents**', async route => {
      const request = route.request()
      const path = new URL(request.url()).pathname
      if (request.method() !== 'GET') return route.fallback()
      if (path === '/api/agents') return route.fulfill({ json: fixtures })
      const agent = fixtures.find(item => path.startsWith(`/api/agents/${item.id}/`))
      if (!agent) return route.fallback()
      if (path.endsWith('/oxidns-ha-environment')) {
        environmentReads.add(agent.id)
        return route.fulfill({ json: {
          success: true, agent_id: agent.id, service_type: 'oxidns', oxidns_ha_protocols: [1],
          oxidns_ha_handoff_protocols: [1], oxidns_ha_container_protocols: [1],
          oxidns_ha_environment: { kind: agent.deployment_method === 'shell' ? 'native' : 'container',
            network: agent.deployment_method === 'shell' ? '' : 'host', ready: true,
            host_id: `host-${agent.id}`, host_id_source: 'machine-id', blockers: [] },
        } })
      }
      return route.fulfill({ json: { success: true, task: null, config: null, members: [], health: 'disabled' } })
    })
    await page.reload()
    phase = `${width}: fixture card visible`
    await page.locator('header p[title="Fixture Shell A"]').waitFor()
    await page.locator('[data-agent-dns-ha]').first().getByRole('button', { name: '设置 DNS 高可用', exact: true }).click()
    await page.getByRole('button', { name: '打开高可用安装引导', exact: true }).click()
    phase = `${width}: fixture Shell guide visible`
    await shellGuide.waitFor()
    assert.equal(await page.locator('[role="radio"][value="shell"]').getAttribute('aria-checked'), 'true')
    assert.equal(await page.getByRole('button', { name: '2. 验证与配对', exact: true }).getAttribute('aria-current'), 'step')
    assert.equal(await page.locator('#agent-name').count(), 0)
    const refresh = page.getByRole('button', { name: '刷新并检查两台 Agent', exact: true })
    phase = `${width}: fixture initial environment refresh`
    await refresh.click()
    await page.getByText('实时检查时间：', { exact: false }).waitFor()
    const first = page.getByRole('combobox', { name: '主机 1 的 OxiDNS Agent', exact: true })
    const second = page.getByRole('combobox', { name: '主机 2 的 OxiDNS Agent', exact: true })
    assert.ok((await first.innerText()).includes('Fixture Shell A'), 'Existing Shell Agent must be preselected')
    for (const { peer, method } of [{ peer: 'Fixture Shell B', method: 'Shell + Shell' }, { peer: 'Fixture Docker C', method: 'Shell + Docker' }]) {
      phase = `${width}: fixture ${method} environment refresh`
      await second.click()
      await page.getByRole('option', { name: new RegExp(peer) }).click()
      await refresh.click()
      await page.getByText('实时检查时间：', { exact: false }).waitFor()
      assert.equal(await page.getByText('配对前需要处理', { exact: true }).count(), 0, `${method} fixture should pass environment checks`)
      const pairSetting = page.locator('[role="dialog"]').getByRole('button', { name: '设置 DNS 高可用', exact: true })
      assert.ok(await pairSetting.isEnabled(), `${method} fixture should allow opening HA settings`)
    }
    assert.deepEqual([...environmentReads].sort(), fixtures.map(agent => agent.id).sort())
    await page.getByRole('button', { name: '2. 验证与配对', exact: true }).scrollIntoViewIfNeeded()
    await checkLayout(page, width)
    // Fixture names and addresses only; no real installation command is mounted after reload.
    await page.screenshot({ path: join(evidence, `oxidns-shell-ha-pairing-${width}.png`), fullPage: false })
    assert.deepEqual(await liveAgents(), agentsBefore, 'Generating scripts and fixture checks must not register Agents')
    report.cases.push({ width, theme, real_install_api: true, real_download_equal: true, includes_oxidns_core: true, automatic_ha_preparation: true, unchanged_short_command: true,
      completed_command_retained: true, fixture_existing_shell_preselected: true, fixture_shell_shell_ready: true,
      fixture_shell_docker_ready: true, no_live_ha_activation: true, overflow: false })
    console.log(JSON.stringify({ width, theme, real_install_api: true, fixture_pairing: ['Shell + Shell', 'Shell + Docker'], overflow: false }))
    await context.close()
  }
  assert.equal(pageErrors.length, 0, 'Browser page errors were reported')
  assert.equal(forbiddenWrites.length, 0, 'Unexpected Agent or HA mutation was attempted')
  report.status = 'passed'
} catch (error) {
  report.status = 'failed'
  report.failed_phase = phase
  // Never serialize response bodies, installation scripts, enrollment URLs, tokens, or traces.
  report.failure = `${error.name || 'Error'}: ${String(error.message || 'Check failed').split('\n')[0]}`
  failure = report.failure
} finally {
  report.errors = pageErrors.length
  report.blocked_write_count = forbiddenWrites.length
  report.completed_at = new Date().toISOString()
  await writeFile(reportPath, JSON.stringify(report, null, 2) + '\n', { mode: 0o600 })
  await browser.close()
}
if (failure) { console.error(failure); process.exitCode = 1 }
