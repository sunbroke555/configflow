// @vitest-environment node
import { describe, expect, it, vi } from 'vitest'

const pwa = vi.hoisted(() => ({ options: {} as any }))
vi.mock('vite-plugin-pwa', () => ({
  VitePWA: (options: unknown) => {
    pwa.options = options
    return []
  }
}))
import { frontendVersionPlugin } from '../vite.config.mts'

function emitVersion(bundle: Record<string, unknown>) {
  const emitFile = vi.fn()
  const plugin = frontendVersionPlugin()
  const hook = plugin.generateBundle as Function
  hook.call({ emitFile, error: (message: string) => { throw new Error(message) } }, {}, bundle)
  return emitFile
}

describe('frontend build version', () => {
  it('reports the final application entry, not a lazy chunk or stylesheet', () => {
    const emitFile = emitVersion({
      'assets/OxiDNS-old.js': { type: 'chunk', isEntry: false, fileName: 'assets/OxiDNS-old.js' },
      'assets/index-style.css': { type: 'asset', fileName: 'assets/index-style.css' },
      'assets/index-B9_2x-0z.js': { type: 'chunk', isEntry: true, fileName: 'assets/index-B9_2x-0z.js' }
    })
    expect(emitFile).toHaveBeenCalledOnce()
    expect(emitFile).toHaveBeenCalledWith({
      type: 'asset', fileName: 'frontend-version.json',
      source: '{"entry":"assets/index-B9_2x-0z.js"}\n'
    })
  })

  it('does not silently publish an ambiguous or missing version', () => {
    expect(() => emitVersion({})).toThrow('exactly one entry chunk')
    expect(() => emitVersion({
      first: { type: 'chunk', isEntry: true, fileName: 'assets/first.js' },
      second: { type: 'chunk', isEntry: true, fileName: 'assets/second.js' }
    })).toThrow('exactly one entry chunk')
  })
})

describe('service worker deployment recovery', () => {
  it('always reads the version from the server and excludes it from precaching', () => {
    const workbox = pwa.options.workbox
    expect(workbox.globIgnores).toContain('**/frontend-version.json')
    const versionRoute = workbox.runtimeCaching.find((route: any) =>
      route.urlPattern({ url: new URL('https://example.test/frontend-version.json') }))
    expect(versionRoute?.handler).toBe('NetworkOnly')
  })

  it('never substitutes application HTML for missing scripts, versions or APIs', () => {
    const denied = (path: string) => pwa.options.workbox.navigateFallbackDenylist
      .some((pattern: RegExp) => pattern.test(path))
    for (const path of [
      '/assets/OxiDNS-old.js', '/frontend-version.json', '/frontend-version.json?retry=1', '/api/status',
      '/oxidns?__cf_reload=assets%2Findex-new.js', '/generate?tab=mihomo&__cf_reload=assets%2Findex-new.js'
    ]) {
      expect(denied(path), path).toBe(true)
    }
    for (const path of ['/', '/oxidns', '/generate', '/settings']) {
      expect(denied(path), path).toBe(false)
    }
  })
})
