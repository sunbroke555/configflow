import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { copyText } from '@/lib/clipboard'

const originalClipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
const originalCommand = Object.getOwnPropertyDescriptor(document, 'execCommand')
const text = '中文配置\n  - https://example.test/a?x=1&y=2\n'
const writeText = vi.fn()
const execCommand = vi.fn()

beforeEach(() => {
  writeText.mockReset().mockResolvedValue(undefined)
  execCommand.mockReset().mockReturnValue(true)
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
  Object.defineProperty(document, 'execCommand', { configurable: true, value: execCommand })
})
afterEach(() => {
  document.body.innerHTML = ''
  window.getSelection()?.removeAllRanges()
  if (originalClipboard) Object.defineProperty(navigator, 'clipboard', originalClipboard)
  else delete (navigator as any).clipboard
  if (originalCommand) Object.defineProperty(document, 'execCommand', originalCommand)
  else delete (document as any).execCommand
})
const useHttp = () => Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined })

describe('clipboard copy', () => {
  it('waits for the browser to accept the exact text before reporting success', async () => {
    let resolve!: () => void
    writeText.mockReturnValue(new Promise<void>(done => { resolve = done }))
    const done = vi.fn()
    const pending = copyText(text).then(done)
    await Promise.resolve()
    expect(done).not.toHaveBeenCalled()
    expect(writeText).toHaveBeenCalledExactlyOnceWith(text)
    resolve()
    await pending
    expect(done).toHaveBeenCalledOnce()
    expect(execCommand).not.toHaveBeenCalled()
  })

  it.each(['unavailable', 'rejected'])('uses selection copy when the modern API is %s', async kind => {
    if (kind === 'unavailable') useHttp()
    else writeText.mockRejectedValue(new Error('Permission denied'))
    execCommand.mockImplementation(command => {
      expect(command).toBe('copy')
      const field = document.activeElement as HTMLTextAreaElement
      expect(field.tagName).toBe('TEXTAREA')
      expect(field.value.slice(field.selectionStart, field.selectionEnd)).toBe(text)
      return true
    })
    await expect(copyText(text)).resolves.toBeUndefined()
    expect(execCommand).toHaveBeenCalledOnce()
    expect(document.querySelector('textarea')).toBeNull()
  })

  it.each(['false', 'throws', 'unavailable'])('rejects and cleans up when fallback %s', async failure => {
    useHttp()
    if (failure === 'false') execCommand.mockReturnValue(false)
    if (failure === 'throws') execCommand.mockImplementation(() => { throw new Error('Denied') })
    if (failure === 'unavailable') delete (document as any).execCommand
    await expect(copyText(text)).rejects.toThrow()
    expect(document.querySelector('textarea')).toBeNull()
  })

  it('keeps copying inside the modal focus scope and returns to the existing field', async () => {
    useHttp()
    document.body.innerHTML = '<div role="dialog" data-state="open"><input value="unsaved draft"></div>'
    const dialog = document.querySelector('[role="dialog"]')!
    const field = document.querySelector('input')!
    field.focus()
    field.setSelectionRange(2, 7, 'backward')
    const trap = () => { if (!dialog.contains(document.activeElement)) field.focus() }
    document.addEventListener('focusin', trap)
    execCommand.mockImplementation(() => {
      const temporary = document.activeElement as HTMLTextAreaElement
      expect(dialog.contains(temporary)).toBe(true)
      expect(temporary.value).toBe(text)
      expect(temporary.selectionEnd - temporary.selectionStart).toBe(text.length)
      return true
    })
    try {
      await copyText(text)
      expect(document.activeElement).toBe(field)
      expect(field.value).toBe('unsaved draft')
      expect([field.selectionStart, field.selectionEnd, field.selectionDirection]).toEqual([2, 7, 'backward'])
    } finally { document.removeEventListener('focusin', trap) }
  })

  it('finds an open modal even if the clicked button did not receive focus', async () => {
    useHttp()
    document.body.innerHTML = '<div role="dialog" data-state="closed"></div><div role="dialog" data-state="open"></div>'
    execCommand.mockImplementation(() => document.activeElement?.parentElement?.getAttribute('data-state') === 'open')
    await expect(copyText(text)).resolves.toBeUndefined()
  })

  it.each([true, false])('preserves selected page text after fallback returns %s', async result => {
    useHttp()
    document.body.innerHTML = '<p>Keep my selected content</p>'
    const node = document.querySelector('p')!.firstChild!
    const range = document.createRange()
    range.setStart(node, 5)
    range.setEnd(node, 16)
    window.getSelection()!.addRange(range)
    execCommand.mockReturnValue(result)
    await copyText(text).catch(() => {})
    expect(window.getSelection()!.toString()).toBe('my selected')
  })
})
