/** Keep automatic overlay focus on a control rather than opening label help. */
export function focusOverlayControl(event: Event) {
  if (event.defaultPrevented || !(event.target instanceof HTMLElement)) return

  const container = event.target
  const document = container.ownerDocument
  const view = document.defaultView
  if (!view) return

  let skippedHelp = false
  for (const candidate of container.querySelectorAll<HTMLElement>('*')) {
    if (candidate.tabIndex < 0 || typeof candidate.focus !== 'function') continue
    if (candidate.matches(':disabled, [aria-disabled="true"], input[type="hidden"]')) continue
    if (candidate.closest('[hidden], [inert]')) continue
    let visible = true
    for (let parent: HTMLElement | null = candidate; parent && container.contains(parent); parent = parent.parentElement) {
      const style = view.getComputedStyle(parent)
      if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') {
        visible = false
        break
      }
    }
    if (!visible) continue

    const isHelp = candidate.matches('[data-field-help-trigger]')
    // Leave normal autofocus untouched, without scanning the rest of a large form.
    if (!skippedHelp && !isHelp) return
    if (isHelp) {
      skippedHelp = true
      continue
    }

    candidate.focus({ preventScroll: true })
    const focused = document.activeElement
    // Roving groups synchronously delegate focus from their container to an item.
    if (!(focused instanceof HTMLElement) || !candidate.contains(focused) || focused.closest('[data-field-help-trigger]')) continue
    if (candidate.tagName === 'INPUT') (candidate as HTMLInputElement).select()
    event.preventDefault()
    return
  }

  if (!skippedHelp) return
  // Reka Popover dispatches from the positioning wrapper; only its inner dialog
  // has tabindex=-1. Keep fallback focus on that semantic content, not the wrapper.
  const focusTarget = container.matches('[data-reka-popper-content-wrapper]')
    ? container.querySelector<HTMLElement>(':scope > [role="dialog"][tabindex="-1"]') ?? container
    : container
  focusTarget.focus({ preventScroll: true })
  if (document.activeElement === focusTarget) event.preventDefault()
}
