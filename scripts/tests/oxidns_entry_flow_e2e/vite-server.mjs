import { readFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
const info = JSON.parse(await readFile(process.argv[2], 'utf8'))
const root = resolve(info.repository, 'frontend')
const { createServer } = await import(pathToFileURL(resolve(root, 'node_modules/vite/dist/node/index.js')))
const server = await createServer({ root, configFile: resolve(root, 'vite.config.mts'), server: { host: '0.0.0.0', port: info.frontend_port, strictPort: true, proxy: { '/api': { target: info.backend_url, changeOrigin: true } } } })
await server.listen()
console.log('Disposable full ConfigFlow frontend is ready')
for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, async () => { await server.close(); process.exit(0) })
