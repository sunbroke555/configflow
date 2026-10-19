#!/usr/bin/env node
/**
 * Serve a frozen copy of the real frontend against an isolated lab SSH tunnel,
 * without changing product files or loading developer .env. Both listeners are local.
 *
 * node scripts/tests/linux_gateway_lab_frontend.mjs --api-port 18000 --port 13001
 *
 * The printed /tmp manifest records the PID, fixed proxy target and shutdown.
 * Stop with Ctrl-C or `kill -TERM <pid>`; the Vite listener/cache are cleaned up.
 */
import { mkdtemp, writeFile, rename, chmod, rm, cp, mkdir, access, symlink, readdir, readFile, realpath } from 'node:fs/promises'
import { dirname, resolve, join, relative } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { parseArgs } from 'node:util'
import { createHash } from 'node:crypto'

const { values } = parseArgs({
  options: {
    'api-port': { type: 'string', default: '18000' },
    port: { type: 'string', default: '13001' },
    'snapshot-from': { type: 'string' },
    'refresh-file': { type: 'string', multiple: true, default: [] },
    help: { type: 'boolean', short: 'h', default: false },
  },
  allowPositionals: false,
})
if (values.help) {
  console.log('Usage: node scripts/tests/linux_gateway_lab_frontend.mjs [--api-port 18000] [--port 13001]\n  [--snapshot-from /tmp/previous-workspace --refresh-file src/path/to/file.vue]\nFreezes frontend sources in /tmp; HMR and file watching are disabled.\nOnly binds 127.0.0.1 and only proxies /api to 127.0.0.1. No backend is started.')
  process.exit(0)
}
const port = (value, label) => {
  if (!/^\d+$/.test(value) || Number(value) < 1024 || Number(value) > 65535) throw new Error(`${label} must be an integer from 1024 through 65535`)
  return Number(value)
}
const frontendPort = port(values.port, '--port')
const apiPort = port(values['api-port'], '--api-port')
if (frontendPort === apiPort) throw new Error('Frontend and tunnel ports must differ')

const repository = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const frontendRoot = join(repository, 'frontend')
let inputRoot = frontendRoot
let previousSnapshot = null
const refreshedFiles = [...new Set(values['refresh-file'])]
if (refreshedFiles.length && !values['snapshot-from']) throw new Error('--refresh-file requires --snapshot-from')
if (values['snapshot-from']) {
  const previousWorkspace = await realpath(values['snapshot-from'])
  previousSnapshot = JSON.parse(await readFile(join(previousWorkspace, 'manifest.json'), 'utf8'))
  if (previousSnapshot.kind !== 'configflow-disposable-gateway-ui' || !/^[a-f0-9]{64}$/.test(previousSnapshot.snapshot_sha256)) {
    throw new Error('--snapshot-from must be a recorded lab frontend workspace')
  }
  inputRoot = join(previousWorkspace, 'frontend')
  for (const filename of refreshedFiles) {
    if (!filename.startsWith('src/') || filename.split('/').some(part => !part || part === '.' || part === '..') || filename.includes('\\')) {
      throw new Error('--refresh-file must name an explicit file under src/')
    }
  }
}
const { createServer } = await import(pathToFileURL(join(frontendRoot, 'node_modules/vite/dist/node/index.js')).href)
// macOS maps /tmp to /private/tmp. Canonicalize before Vite creates its module
// filters, otherwise filesystem IDs and root/alias paths can disagree.
const workspace = await realpath(await mkdtemp('/tmp/configflow-gateway-lab-frontend-'))
await chmod(workspace, 0o700)
const snapshotRoot = join(workspace, 'frontend')
await mkdir(snapshotRoot, { mode: 0o700 })
const snapshotEntries = ['src', 'public', 'index.html', 'icon.png', 'vite.config.mts',
  'package.json', 'package-lock.json', 'tsconfig.json', 'tsconfig.node.json', 'components.json']
for (const name of snapshotEntries) {
  const source = join(inputRoot, name)
  try { await access(source) } catch (error) {
    if (error.code === 'ENOENT' && ['public', 'tsconfig.node.json'].includes(name)) continue
    throw error
  }
  await cp(source, join(snapshotRoot, name), { recursive: true, dereference: true, preserveTimestamps: true })
}
const hashFiles = async (directory, files) => {
  for (const entry of (await readdir(directory, { withFileTypes: true })).sort((left, right) => left.name.localeCompare(right.name, 'en'))) {
    const filename = join(directory, entry.name)
    if (entry.isDirectory()) await hashFiles(filename, files)
    else if (entry.isFile()) {
      const contents = await readFile(filename)
      files.push({ path: relative(snapshotRoot, filename).replaceAll('\\', '/'),
        bytes: contents.length, sha256: createHash('sha256').update(contents).digest('hex') })
    } else throw new Error(`Snapshot contains an unexpected special file: ${filename}`)
  }
}
if (previousSnapshot) {
  const copiedFiles = []
  await hashFiles(snapshotRoot, copiedFiles)
  const copiedDigest = createHash('sha256').update(JSON.stringify(copiedFiles)).digest('hex')
  if (copiedDigest !== previousSnapshot.snapshot_sha256) throw new Error('Previous snapshot contents no longer match its recorded digest')
  for (const filename of refreshedFiles) {
    // readFile rejects directories; writeFile cannot silently refresh an entire tree.
    const contents = await readFile(join(frontendRoot, filename))
    await writeFile(join(snapshotRoot, filename), contents)
  }
}
const sourceFiles = []
await hashFiles(snapshotRoot, sourceFiles)
const snapshotDigest = createHash('sha256').update(JSON.stringify(sourceFiles)).digest('hex')
await writeFile(join(workspace, 'source-manifest.json'), `${JSON.stringify(sourceFiles, null, 2)}\n`, { mode: 0o600 })
// Dependencies are reused read-only. Application source, aliases and config all
// resolve inside the immutable per-run copy, never the active shared checkout.
await symlink(join(frontendRoot, 'node_modules'), join(snapshotRoot, 'node_modules'), 'dir')
const manifestPath = join(workspace, 'manifest.json')
const cacheDirectory = join(workspace, 'vite-cache')
const frontendURL = `http://127.0.0.1:${frontendPort}`
const apiURL = `http://127.0.0.1:${apiPort}`
const manifest = {
  kind: 'configflow-disposable-gateway-ui', pid: process.pid,
  started_at: new Date().toISOString(), status: 'starting',
  frontend_url: frontendURL, login_url: `${frontendURL}/login`,
  api_proxy_target: apiURL, proxy_prefix: '/api',
  workspace, source_root: frontendRoot,
  input_root: inputRoot,
  parent_snapshot_sha256: previousSnapshot?.snapshot_sha256 ?? null,
  refreshed_files: refreshedFiles,
  snapshot_root: snapshotRoot, snapshot_sha256: snapshotDigest, snapshot_file_count: sourceFiles.length,
  source_manifest: join(workspace, 'source-manifest.json'),
  dependency_directory: join(frontendRoot, 'node_modules'),
  hmr: false, file_watching: false,
  original_product_config: join(frontendRoot, 'vite.config.mts'),
  product_config: join(snapshotRoot, 'vite.config.mts'),
}
const saveManifest = async () => {
  const temporary = `${manifestPath}.tmp`
  await writeFile(temporary, `${JSON.stringify(manifest, null, 2)}\n`, { mode: 0o600 })
  await rename(temporary, manifestPath)
}
await saveManifest()

let server
let closing
const close = (reason, code = 0) => {
  if (closing) return closing
  closing = (async () => {
    try {
      if (server) await server.close()
      await rm(cacheDirectory, { recursive: true, force: true })
      manifest.status = 'stopped'
      manifest.stopped_at = new Date().toISOString()
      manifest.stop_reason = reason
      await saveManifest()
    } finally {
      process.exitCode = code
    }
  })()
  return closing
}
for (const signal of ['SIGINT', 'SIGTERM']) {
  process.once(signal, () => { void close(signal).then(() => process.exit(0)) })
}

try {
  const previousTermListeners = new Set(process.listeners('SIGTERM'))
  const previousInputListeners = new Set(process.stdin.listeners('end'))
  server = await createServer({
    root: snapshotRoot,
    configFile: manifest.product_config,
    envFile: false,
    cacheDir: cacheDirectory,
    clearScreen: false,
    resolve: { alias: { '@': join(snapshotRoot, 'src') } },
    server: {
      host: '127.0.0.1', port: frontendPort, strictPort: true, open: false,
      hmr: false, watch: null,
      proxy: { '/api': { target: apiURL, changeOrigin: true } },
    },
  })
  // Vite's standalone API installs a handler that calls process.exit directly.
  // This launcher owns shutdown so its private PID record and cache cleanup
  // finish first; retain all listeners that predate this Vite instance.
  for (const listener of process.listeners('SIGTERM')) {
    if (!previousTermListeners.has(listener)) process.off('SIGTERM', listener)
  }
  for (const listener of process.stdin.listeners('end')) {
    if (!previousInputListeners.has(listener)) process.stdin.off('end', listener)
  }
  process.stdin.once('end', () => { void close('stdin-ended').then(() => process.exit(0)) })
  await server.listen()
  const bound = server.httpServer?.address()
  if (!bound || typeof bound === 'string' || bound.address !== '127.0.0.1' || bound.port !== frontendPort) {
    throw new Error('Vite did not bind the requested loopback interface')
  }
  manifest.status = 'running'
  await saveManifest()
  console.log(JSON.stringify({ ready: true, frontend_url: frontendURL, login_url: manifest.login_url,
    api_proxy_target: apiURL, pid: process.pid, manifest: manifestPath,
    snapshot_sha256: snapshotDigest, snapshot_root: snapshotRoot }))
} catch (error) {
  console.error(`Lab frontend failed: ${error.message}`)
  await close('startup-failed', 1)
}
