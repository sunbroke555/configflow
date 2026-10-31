import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { installScrollingMarker } from '@/lib/scrollingMarker'

beforeAll(() => {
  vi.useFakeTimers()
  installScrollingMarker()
})

afterAll(() => {
  vi.useRealTimers()
})

afterEach(() => {
  vi.clearAllTimers()
  document.body.innerHTML = ''
  document.documentElement.removeAttribute('data-scrolling')
})

describe('installScrollingMarker', () => {
  it('marks an inner scroll container while it scrolls and clears the mark 800ms after the last scroll', () => {
    const list = document.createElement('div')
    document.body.append(list)

    list.dispatchEvent(new Event('scroll'))
    expect(list.hasAttribute('data-scrolling')).toBe(true)

    vi.advanceTimersByTime(500)
    list.dispatchEvent(new Event('scroll'))
    vi.advanceTimersByTime(799)
    expect(list.hasAttribute('data-scrolling')).toBe(true)

    vi.advanceTimersByTime(1)
    expect(list.hasAttribute('data-scrolling')).toBe(false)
  })

  it('never marks the root element when the page itself scrolls', () => {
    document.dispatchEvent(new Event('scroll'))
    document.documentElement.dispatchEvent(new Event('scroll'))

    expect(document.documentElement.hasAttribute('data-scrolling')).toBe(false)
    expect(vi.getTimerCount()).toBe(0)
  })
})
