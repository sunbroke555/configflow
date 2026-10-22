import { afterEach } from 'vitest'

// jsdom resolves every selector by walking the whole document. Vue's Teleport
// looks up `body` for each portal, which adds up on large forms.
if (typeof Document !== 'undefined') {
  const querySelector = Document.prototype.querySelector
  Document.prototype.querySelector = function (this: Document, selectors: string) {
    return selectors === 'body' ? this.body : querySelector.call(this, selectors)
  } as typeof Document.prototype.querySelector
}

// nwsapi caches compiled resolvers that keep detached DOM trees alive between
// tests (hundreds of MB per worker on the heavy settings suites).
type Resolvers = { selectResolvers?: Map<unknown, unknown>; matchResolvers?: Map<unknown, unknown> }
afterEach(() => {
  if (typeof document === 'undefined') return
  const key = Object.getOwnPropertySymbols(document).find(symbol => symbol.description === 'impl')
  const impl = key ? (document as unknown as Record<symbol, { _nwsapi?: Resolvers; _nwsapiDontThrow?: Resolvers }>)[key] : undefined
  for (const nwsapi of [impl?._nwsapi, impl?._nwsapiDontThrow]) {
    nwsapi?.selectResolvers?.clear?.()
    nwsapi?.matchResolvers?.clear?.()
  }
})
