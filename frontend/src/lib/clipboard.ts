/** Copy on both HTTPS and HTTP deployments, rejecting when neither method succeeds. */
export async function copyText(text: string): Promise<void> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return
    }
  } catch {
    // Permissions or browser policy can reject the modern API; try selection copy.
  }

  const active = document.activeElement instanceof HTMLElement ? document.activeElement : null
  const selection = window.getSelection()
  const ranges = selection ? Array.from({ length: selection.rangeCount }, (_, i) => selection.getRangeAt(i).cloneRange()) : []
  const field = active instanceof HTMLInputElement || active instanceof HTMLTextAreaElement ? active : null
  const fieldSelection = field?.selectionStart != null
    ? { start: field.selectionStart, end: field.selectionEnd!, direction: field.selectionDirection ?? undefined }
    : null
  const dialogs = document.querySelectorAll<HTMLElement>('[role="dialog"][data-state="open"], [role="alertdialog"][data-state="open"], dialog[open]')
  // A modal's focus trap prevents selecting a textarea appended outside it.
  const host = active?.closest('[role="dialog"], [role="alertdialog"], dialog')
    || dialogs[dialogs.length - 1] || document.body
  const textarea = document.createElement('textarea')
  textarea.value = text
  textarea.readOnly = true
  textarea.tabIndex = -1
  textarea.style.cssText = 'position:fixed;top:0;left:0;opacity:0;pointer-events:none'
  host.appendChild(textarea)
  try {
    textarea.focus({ preventScroll: true })
    textarea.select()
    if (!document.execCommand('copy')) throw new Error('Clipboard copy failed')
  } finally {
    textarea.remove()
    if (active?.isConnected) active.focus({ preventScroll: true })
    if (field?.isConnected && fieldSelection) field.setSelectionRange(fieldSelection.start, fieldSelection.end, fieldSelection.direction)
    if (selection) {
      selection.removeAllRanges()
      for (const range of ranges) {
        if (range.commonAncestorContainer.isConnected) selection.addRange(range)
      }
    }
  }
}
