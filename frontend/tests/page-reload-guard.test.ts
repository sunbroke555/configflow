import { afterEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, effectScope, ref } from 'vue'
import { mount, type VueWrapper } from '@vue/test-utils'
import { canReloadPage, usePageReloadGuard, withPageReloadBlocked } from '@/lib/pageReloadGuard'
import { useReorder } from '@/composables/useReorder'

const scopes: ReturnType<typeof effectScope>[] = []
const wrappers: VueWrapper[] = []
afterEach(() => {
  scopes.splice(0).forEach(scope => scope.stop())
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})
function guard(blocked: () => boolean) {
  const scope = effectScope()
  scopes.push(scope)
  scope.run(() => usePageReloadGuard(blocked))
  return scope
}

describe('automatic page reload protection', () => {
  it('checks current draft state and unregisters it when the editing scope ends', () => {
    const dirty = ref(false)
    const scope = guard(() => dirty.value)
    expect(canReloadPage()).toBe(true)
    dirty.value = true
    expect(canReloadPage()).toBe(false)
    dirty.value = false
    expect(canReloadPage()).toBe(true)
    dirty.value = true
    scope.stop()
    expect(canReloadPage()).toBe(true)
  })

  it('keeps independent editors protected until all are clean', () => {
    const saving = ref(true)
    const dirty = ref(true)
    guard(() => saving.value)
    guard(() => dirty.value)
    saving.value = false
    expect(canReloadPage()).toBe(false)
    dirty.value = false
    expect(canReloadPage()).toBe(true)
  })

  it('respects existing beforeunload guards without opening a native confirmation', () => {
    const confirm = vi.spyOn(window, 'confirm')
    const listener = vi.fn((event: Event) => event.preventDefault())
    window.addEventListener('beforeunload', listener)
    try {
      expect(canReloadPage()).toBe(false)
      expect(listener).toHaveBeenCalledOnce()
      expect(listener.mock.calls[0]![0].cancelable).toBe(true)
      expect(listener.mock.calls[0]![0].isTrusted).toBe(false)
      expect(confirm).not.toHaveBeenCalled()
    } finally { window.removeEventListener('beforeunload', listener) }
    expect(canReloadPage()).toBe(true)
  })

  it.each(['dialog', 'alertdialog'])('protects an open %s but not closed dialogs or plain search input', role => {
    document.body.innerHTML = `<div role="${role}" data-state="closed"></div><input value="search" />`
    expect(canReloadPage()).toBe(true)
    document.querySelector('[role]')!.setAttribute('data-state', 'open')
    expect(canReloadPage()).toBe(false)
    document.querySelector('[role]')!.remove()
    expect(canReloadPage()).toBe(true)
  })
})

describe('manual list sorting reload protection', () => {
  function setup(persist: (items: string[]) => Promise<unknown> = vi.fn(async () => undefined)) {
    const items = ref(['first', 'second', 'third'])
    let reorder!: ReturnType<typeof useReorder<string>>
    wrappers.push(mount(defineComponent({ setup() {
      reorder = useReorder({ items, container: ref(null), persist, labelOf: value => value })
      return () => null
    } })))
    return { items, reorder }
  }

  it('protects changed order, and allows recovery after undoing or cancelling sorting', () => {
    const { items, reorder } = setup()
    reorder.enter()
    expect(canReloadPage()).toBe(true)
    reorder.moveDown(0)
    expect(items.value).toEqual(['second', 'first', 'third'])
    expect(canReloadPage()).toBe(false)
    reorder.moveUp(1)
    expect(canReloadPage()).toBe(true)
    reorder.moveDown(0)
    reorder.cancel()
    expect(canReloadPage()).toBe(true)
    expect(items.value).toEqual(['first', 'second', 'third'])
  })

  it('blocks only until the save response completes, even for unchanged order', async () => {
    let resolve!: () => void
    const persist = vi.fn(() => new Promise<void>(done => { resolve = done }))
    const { reorder } = setup(persist)
    reorder.enter()
    const pending = reorder.save()
    expect(canReloadPage()).toBe(false)
    resolve()
    await pending
    expect(canReloadPage()).toBe(true)
  })

  it('permits recovery after a rejected save restores the original order', async () => {
    const { reorder, items } = setup(vi.fn(async () => { throw new Error('save failed') }))
    reorder.enter()
    reorder.moveDown(0)
    await expect(reorder.save()).rejects.toThrow('save failed')
    expect(items.value).toEqual(['first', 'second', 'third'])
    expect(canReloadPage()).toBe(true)
  })
})


describe('submitted write reload protection', () => {
  it('starts before submission and releases on success without changing the returned value', async () => {
    let resolve!: (value: string) => void
    const operation = vi.fn(() => {
      expect(canReloadPage()).toBe(false)
      return new Promise<string>(done => { resolve = done })
    })
    const pending = withPageReloadBlocked(operation)
    expect(operation).toHaveBeenCalledOnce()
    expect(canReloadPage()).toBe(false)
    resolve('server response')
    expect(await pending).toBe('server response')
    expect(canReloadPage()).toBe(true)
  })

  it.each(['synchronous', 'asynchronous'])('releases on a %s failure and preserves the original error', async kind => {
    const failure = new Error('write rejected')
    const operation = vi.fn(() => {
      expect(canReloadPage()).toBe(false)
      if (kind === 'synchronous') throw failure
      return Promise.reject(failure)
    })
    await expect(withPageReloadBlocked(operation)).rejects.toBe(failure)
    expect(operation).toHaveBeenCalledOnce()
    expect(canReloadPage()).toBe(true)
  })

  it('keeps simultaneous writes independent after an editing scope is disposed', async () => {
    const scope = guard(() => true)
    let resolveFirst!: () => void
    let rejectSecond!: (failure: Error) => void
    const first = withPageReloadBlocked(() => new Promise<void>(done => { resolveFirst = done }))
    const second = withPageReloadBlocked(() => new Promise<void>((_done, fail) => { rejectSecond = fail }))
    scope.stop()
    expect(canReloadPage()).toBe(false)
    resolveFirst()
    await first
    expect(canReloadPage()).toBe(false)
    const rejected = expect(second).rejects.toThrow('second write failed')
    rejectSecond(new Error('second write failed'))
    await rejected
    expect(canReloadPage()).toBe(true)
  })
})
