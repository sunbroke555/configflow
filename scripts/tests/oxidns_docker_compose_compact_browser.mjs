// Run on the Linux test server against an already-built production preview.
// Generates/downloads Compose only: never installs containers or registers Agents.
import { readFile, writeFile, mkdir, mkdtemp, chmod, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import assert from 'node:assert/strict'

process.umask(0o077)
const preview = process.env.CF_PREVIEW_ROOT || '/home/sunbroke/previews/configflow-oxidns-ha-guide'
const base = process.env.CF_PREVIEW_URL || 'http://192.168.0.32:15810'
const evidence = process.env.CF_EVIDENCE_DIR || join(preview, 'evidence')
const reportPath = join(evidence, 'oxidns-docker-compose-compact-browser.json')
const screenshotPath = join(evidence, 'oxidns-docker-compose-compact-mobile.png')

const playwright = process.env.CF_PLAYWRIGHT || '/home/sunbroke/rt/private__tmp__configflow-real-env-rebuild__browser-runtime/node_modules/playwright/index.mjs'
const python = process.env.CF_PYTHON || '/home/sunbroke/cf-test/.venv/bin/python'
const { chromium } = await import(playwright)
const scratch = await mkdtemp(join(tmpdir(), 'cf-compact-compose-private-'))
await mkdir(evidence, { recursive: true })
const execute = promisify(execFile)
const inspectYaml = String.raw`
import sys
import yaml
path, mode, address, project, root, identity = sys.argv[1:]
with open(path) as source:
    text = source.read()
assert 'prepare.sh' not in text and '.env' not in text, 'Compose must not require a preparation script or env sidecar'
document = yaml.safe_load(text)
assert document['name'] == project, 'Wrong project name'
assert not document.get('configs') and not document.get('secrets'), 'Unexpected external configuration dependency'
services = document['services']
agent = services['agent']
def volumes(service):
    result = {}
    for value in service['volumes']:
        if isinstance(value, str):
            source, target, *options = value.split(':')
            result[target] = {'source': source, 'read_only': 'ro' in options}
        else:
            result[value['target']] = value
    assert all('docker.sock' not in v['source'] for v in result.values()), 'Docker socket must not be exposed'
    return result
assert not document.get('volumes'), 'Compact install must not require a global identity volume'
for service in services.values():
    assert not service.get('env_file') and not service.get('ports'), 'Unexpected sidecar or published port dependency'
    assert not service.get('entrypoint') and 'CONFIGFLOW_COMPOSE_INSTALL' not in service.get('environment', {}), 'Private installer details must stay in image'
main = volumes(agent)
for target, directory in [('/opt/configflow-agent', 'agent'), ('/etc/oxidns', 'oxidns'), ('/var/lib/configflow-ha', 'ha')]:
    assert main[target]['source'] == root + '/' + directory and main[target].get('read_only') is False, 'Main persistent mount mismatch'
boot = main['/run/configflow-host-boot-id']
assert boot['source'] == '/proc/sys/kernel/random/boot_id' and boot['read_only'] is True and boot['bind']['create_host_path'] is False, 'Boot evidence must be a strict read-only bind'
assert agent['environment']['SERVICE_TYPE'] == 'oxidns-ha-' + mode
assert identity == 'installation'
if mode == 'host':
    assert set(services) == {'agent', 'ha-companion'}, 'Host mode requires exactly main and companion'
    helper = services['ha-companion']
    assert agent['network_mode'] == helper['network_mode'] == 'host', 'Wrong host networking'
    assert helper['environment']['INSTALL_SCOPE'] == agent['environment']['INSTALL_KEY'].split('.')[0], 'Companion must verify its installation scope'
    assert not {'NET_ADMIN', 'NET_RAW'} & set(agent.get('cap_add', [])), 'Main Host container must not receive HA network capabilities'
    assert {'NET_ADMIN', 'NET_RAW'} <= set(helper.get('cap_add', [])), 'Companion needs HA network capabilities'
    assert main['/run/configflow-host-netns']['source'] == '/proc/1/ns/net' and main['/run/configflow-host-netns']['read_only'] is True, 'Host namespace proof must be read-only'
    mounted = volumes(helper)
    for target in ('/opt/configflow-agent', '/etc/oxidns'):
        assert mounted[target]['source'] == main[target]['source'] and mounted[target]['read_only'] is True, 'Companion may not write main data'
    assert mounted['/var/lib/configflow-ha']['source'] == main['/var/lib/configflow-ha']['source'] and mounted['/var/lib/configflow-ha']['read_only'] is False, 'Companion needs shared HA state'
else:
    assert set(services) == {'agent'} and 'network_mode' not in agent, 'Macvlan must use one isolated container'
    assert agent['networks']['dns-ha']['ipv4_address'] == address, 'Wrong fixed IP'
    network = document['networks']['dns-ha']
    assert network['driver'] == 'macvlan' and network['driver_opts']['parent'] == 'eth0', 'Wrong Macvlan driver/parent'
    assert network['ipam']['config'][0]['subnet'] == '192.168.0.0/24', 'Wrong subnet'
    assert {'NET_ADMIN', 'NET_RAW'} <= set(agent.get('cap_add', [])), 'Macvlan container needs HA network capabilities'
    assert '/run/configflow-host-netns' not in main, 'Macvlan may not use host namespace proof'
`
const report = { started_at: new Date().toISOString(), preview: base, cases: [], downloads: 0,
  clipboard_check: 'UI copy action recorded at browser clipboard API; exact response equality asserted',
  installed_or_registered: false, errors: 0 }
const browser = await chromium.launch({ headless: true })
const pageErrors = []
let phase = 'starting'
let failure
try {
  for (const { width, theme, network } of [{ width: 1440, theme: 'light', network: 'host' }, { width: 375, theme: 'dark', network: 'macvlan' }]) {
    phase = `${network}: login`
    const context = await browser.newContext({ viewport: { width, height: 960 }, colorScheme: theme, reducedMotion: 'reduce', acceptDownloads: true })
    await context.addInitScript(theme => {
      localStorage.setItem('configflow-theme', theme)
      Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
        writeText: async text => { globalThis.__cfCopiedText = String(text) },
      } })
    }, theme)
    const page = await context.newPage()
    page.on('pageerror', error => pageErrors.push(error.name))
    const auth = await page.request.get(base + '/api/auth/status')
    assert.equal((await auth.json()).authEnabled, false, 'Test preview must remain login-free')
    await page.goto(base + '/agents')
    report.entry_asset = await page.locator('script[type="module"][src]').first().getAttribute('src')
    const ids = async () => {
      const response = await page.request.get(base + '/api/agents')
      assert.equal(response.status(), 200)
      return (await response.json()).map(agent => agent.id).sort()
    }
    const agentsBefore = await ids()
    phase = `${network}: fill hosts and identity choice`
    await page.getByRole('button', { name: /生成\s*安装脚本/ }).first().click()
    await page.locator('[role="radio"][value="docker-ha"]').click()
    await page.locator('#docker-ha-server').fill(base)
    await page.locator(`[aria-label="Docker HA 网络模式"] [value="${network}"]`).click()
    const step = async number => page.getByRole('button', { name: new RegExp(`^${number}\\. `) }).click()
    const field = async (host, key, value) => page.locator(`#docker-ha-host-${host}-${key}`).fill(value)
    const addresses = network === 'host' ? ['192.168.0.221', '192.168.0.222'] : ['192.168.0.231', '192.168.0.232']
    const roots = [1, 2].map(host => `/srv/cf-compact-ui-${network}-${host}`)
    for (const host of [1, 2]) {
      await step(host + 1)
      await field(host, 'name', `compact-ui-${network}-${host}`)
      await field(host, 'ip', addresses[host - 1])
      await field(host, 'data', roots[host - 1])
      assert.equal(await page.locator(`#docker-ha-host-${host}-identity`).count(), 0, 'Compact install should not ask for a host identity file')
      assert.equal(await page.locator(`#docker-ha-host-${host}-create-id`).count(), 0, 'Compact install should not ask for an identity volume')
      if (network === 'macvlan') {
        await field(host, 'parent', 'eth0')
        await field(host, 'subnet', '192.168.0.0/24')
        await field(host, 'gateway', '192.168.0.1')
      }
      if (host === 1) {
        await page.getByRole('button', { name: '只安装这一台，前往部署', exact: true }).click()
        await page.getByRole('button', { name: '生成主机 1 的Compose', exact: true }).waitFor()
        assert.equal(await page.locator('[aria-label="新安装部署方式"] [value="compose_compact"]').getAttribute('aria-checked'), 'true')
        const singlePending = page.waitForResponse(response => response.url().endsWith('/docker-oxidns-ha-guide') && response.request().method() === 'POST')
        await page.getByRole('button', { name: '生成主机 1 的Compose', exact: true }).click()
        const single = await singlePending
        assert.equal(single.status(), 200, 'Single-host generation must work before host 2 is filled')
        assert.equal((await single.json()).compose_deployment.identity_mode, 'installation')
      }
    }
    await step(4)
    for (const host of [1, 2]) {
      phase = `${network}: host ${host} generation and download`
      await page.locator(`[aria-label="执行命令的主机"] [value="${host - 1}"]`).click()
      assert.equal(await page.locator('[aria-label="新安装部署方式"] [value="compose_compact"]').getAttribute('aria-checked'), 'true')
      const pending = page.waitForResponse(response => response.url().endsWith('/docker-oxidns-ha-guide') && response.request().method() === 'POST')
      await page.getByRole('button', { name: `生成主机 ${host} 的Compose`, exact: true }).click()
      const response = await pending
      assert.equal(response.status(), 200, 'Compact Compose generation must succeed')
      assert.equal(response.request().postDataJSON().install_method, 'compose_compact')
      const result = await response.json()
      assert.ok(result.success && result.compose_deployment && result.compose?.trim(), 'Missing single-file Compose')
      assert.equal(result.compose_deployment.filename, 'compose.yaml')
      assert.ok(!result.compose_bundle && !result.identity_command, 'Compact Compose must not require extra files or an identity command')
      const identity = 'installation'
      assert.equal(result.compose_deployment.identity_mode, identity)
      assert.equal(result.compose_deployment.service_count, network === 'host' ? 2 : 1)
      assert.ok(!('ha_host_id_path' in response.request().postDataJSON()), 'Hidden legacy identity must not be submitted')
      const view = page.locator('[data-compose-deployment]')
      await view.waitFor()
      assert.equal(await view.locator('pre').innerText(), 'docker compose up -d')
      assert.ok((await view.innerText()).includes('只需这一份 compose.yaml'))
      const blocks = await page.locator('pre').evaluateAll(elements => elements.map(element => ({
        length: element.textContent.length,
        painted: element.checkVisibility({ checkVisibilityCSS: true }),
        closedDetails: Boolean(element.closest('details:not([open])')),
      })))
      assert.ok(blocks.filter(block => block.painted && !block.closedDetails).every(block => block.length < 400), 'Long initialization code must not appear by default')
      const downloading = page.waitForEvent('download')
      await page.getByRole('button', { name: '下载 compose.yaml', exact: true }).click()
      const download = await downloading
      assert.equal(download.suggestedFilename(), 'compose.yaml')
      const artifact = join(scratch, `${network}-${host}.yaml`)
      await download.saveAs(artifact)
      await chmod(artifact, 0o600)
      assert.ok((await readFile(artifact)).equals(Buffer.from(result.compose, 'utf8')), 'Downloaded bytes differ from the API Compose')
      await execute(python, ['-c', inspectYaml, artifact, network, addresses[host - 1], result.compose_deployment.project_name, roots[host - 1], identity])
      await rm(artifact)
      await page.getByRole('button', { name: '复制 Compose', exact: true }).click()
      assert.ok(await page.evaluate(expected => {
        const matched = globalThis.__cfCopiedText === expected
        delete globalThis.__cfCopiedText
        return matched
      }, result.compose), 'Copy action differs from the API Compose')
      report.downloads++
      const dimensions = await page.evaluate(() => {
        const dialog = document.querySelector('[role="dialog"]')
        return { viewport: innerWidth, page: document.documentElement.scrollWidth, dialog: dialog.clientWidth, content: dialog.scrollWidth }
      })
      assert.ok(dimensions.page <= width && dimensions.content <= dimensions.dialog + 1, 'Horizontal overflow')
      if (width === 375 && host === 2) {
        await page.waitForFunction(() => !document.body.innerText.includes('Compose 已复制，可粘贴到容器管理器的项目中'))
        await view.scrollIntoViewIfNeeded()
        // Closed YAML details keep enrollment credentials out of this screenshot.
        await page.screenshot({ path: screenshotPath, fullPage: false })
      }
    }
    phase = `${network}: alternative routes and verification help`
    await page.locator('[aria-label="执行命令的主机"] [value="0"]').click()
    await page.locator('[aria-label="新安装部署方式"] [value="script"]').click()
    await page.getByRole('button', { name: '生成主机 1 的安装命令', exact: true }).waitFor()
    assert.equal(await page.locator('[data-compose-deployment]').count(), 0)
    await step(3)
    await page.locator('[aria-label="主机 2 安装方式"] [value="migrate"]').click()
    assert.equal(await page.locator('#docker-ha-host-2-identity').count(), 1, 'Migration still needs its explicit identity file path')
    await step(4)
    await page.locator('[aria-label="执行命令的主机"] [value="1"]').click()
    assert.equal(await page.locator('[aria-label="新安装部署方式"]').count(), 0)
    await page.getByRole('button', { name: '生成主机 2 的安装命令', exact: true }).waitFor()
    await step(5)
    assert.ok(await page.getByText('可选择已存在的 Shell 节点与新 Docker 节点。', { exact: false }).isVisible())
    assert.deepEqual(await ids(), agentsBefore, 'Generating configuration must not register Agents')
    report.cases.push({ width, theme, network, identity_mode: 'installation', service_count: network === 'host' ? 2 : 1, download_bytes_equal: true,
      clipboard_equal: true, no_sidecar_files: true, no_identity_form: true, single_host_shortcut: true, script_and_migration: true,
      mixed_pair_help: true, overflow: false })
    console.log(JSON.stringify({ width, theme, network, downloads_validated: 2, clipboard_equal: true, overflow: false }))
    await context.close()
  }
  assert.equal(pageErrors.length, 0, 'Browser page errors were reported')
  report.status = 'passed'
} catch (error) {
  report.status = 'failed'
  report.failed_phase = phase
  // Never serialize request/response bodies, downloaded YAML, credential values or traces.
  report.failure = String(error.name || 'Error') + ': ' + String(error.message || 'Check failed').split('\n')[0]
  failure = report.failure
} finally {
  report.errors = pageErrors.length
  report.finished_at = new Date().toISOString()
  await browser.close()
  await rm(scratch, { recursive: true, force: true })
  await mkdir(dirname(reportPath), { recursive: true })
  await writeFile(reportPath, JSON.stringify(report, null, 2) + '\n', { mode: 0o600 })
}
console.log(JSON.stringify({ status: report.status, cases: report.cases.length, downloads: report.downloads,
  errors: report.errors, report: reportPath, screenshot: report.status === 'passed' ? screenshotPath : null,
  ...(failure ? { phase, failure } : {}) }))
if (failure) process.exitCode = 1
