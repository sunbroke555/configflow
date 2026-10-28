<script setup lang="ts">
import { getScrollOffset } from 'vitepress'
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

type Tab = { id: string; label: string }

const props = withDefaults(defineProps<{
  id: string
  tabs: Tab[]
  label?: string
}>(), { label: '选择适合你的方式' })

const root = ref<HTMLElement>()
const selected = ref(props.tabs[0]?.id ?? '')
const buttons = new Map<string, HTMLButtonElement>()
const panels = new Map<string, HTMLElement>()
let mounted = false
let frame = 0

function rememberButton(id: string, element: unknown) {
  if (element instanceof HTMLButtonElement) buttons.set(id, element)
  else buttons.delete(id)
}

function rememberPanel(id: string, element: unknown) {
  if (element instanceof HTMLElement) panels.set(id, element)
  else panels.delete(id)
}

function hashTarget(hash: string) {
  if (!hash || hash === '#') return null
  try { return document.getElementById(decodeURIComponent(hash.slice(1))) }
  catch { return null }
}

async function revealHash(hash = window.location.hash, scroll = true) {
  const target = hashTarget(hash)
  if (!target) return

  let openedDetails = false
  let details = target.closest('details')
  while (details) {
    if (!details.open) {
      details.open = true
      openedDetails = true
    }
    details = details.parentElement?.closest('details') ?? null
  }

  if (!root.value?.contains(target) && !openedDetails) return

  const containing = props.tabs.find(tab => panels.get(tab.id)?.contains(target))
  if (containing) selected.value = containing.id
  await nextTick()

  // Each nested group reveals its own containing panel before scrolling.
  if (!scroll) return
  cancelAnimationFrame(frame)
  frame = requestAnimationFrame(() => {
    frame = requestAnimationFrame(() => {
      if (!mounted || !target.isConnected || window.location.hash !== hash) return
      if (target.closest('[hidden]')) return
      window.scrollTo({
        top: Math.max(0, window.scrollY + target.getBoundingClientRect().top - getScrollOffset()),
        behavior: 'auto',
      })
    })
  })
}

function onHashNavigation() {
  if (!window.location.hash || window.location.hash === '#') {
    selected.value = props.tabs[0]?.id ?? ''
    return
  }
  void revealHash()
}

function onLinkClick(event: MouseEvent) {
  if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
  const link = (event.target as Element | null)?.closest('a[href]')
  if (!(link instanceof HTMLAnchorElement) || link.target === '_blank' || link.hasAttribute('download')) return
  const url = new URL(link.href, window.location.href)
  if (url.origin !== window.location.origin || url.pathname !== window.location.pathname || url.search !== window.location.search) return
  // Capture before VitePress computes the destination's scroll position.
  void revealHash(url.hash)
}

async function selectTab(id: string, focus = false) {
  if (selected.value !== id) {
    const scrollTop = window.scrollY
    selected.value = id
    const url = new URL(window.location.href)
    url.hash = id
    window.history.pushState(window.history.state, '', url)
    await nextTick()
    window.scrollTo({ top: scrollTop, behavior: 'auto' })
  }
  if (focus) buttons.get(id)?.focus({ preventScroll: true })
}

function onKeydown(event: KeyboardEvent, index: number) {
  let target: number
  switch (event.key) {
    case 'ArrowRight': target = (index + 1) % props.tabs.length; break
    case 'ArrowLeft': target = (index - 1 + props.tabs.length) % props.tabs.length; break
    case 'Home': target = 0; break
    case 'End': target = props.tabs.length - 1; break
    default: return
  }
  event.preventDefault()
  void selectTab(props.tabs[target].id, true)
}

watch(() => props.tabs, () => {
  if (!props.tabs.some(tab => tab.id === selected.value)) selected.value = props.tabs[0]?.id ?? ''
}, { deep: true })

onMounted(() => {
  mounted = true
  window.addEventListener('hashchange', onHashNavigation)
  window.addEventListener('popstate', onHashNavigation)
  document.addEventListener('click', onLinkClick, true)
  void revealHash()
})

onBeforeUnmount(() => {
  mounted = false
  cancelAnimationFrame(frame)
  window.removeEventListener('hashchange', onHashNavigation)
  window.removeEventListener('popstate', onHashNavigation)
  document.removeEventListener('click', onLinkClick, true)
})
</script>

<template>
  <div :id="id" ref="root" class="docs-tabs">
    <div class="docs-tabs__label" :id="`${id}-label`">{{ label }}</div>
    <div class="docs-tabs__choices" role="tablist" :aria-labelledby="`${id}-label`">
      <button
        v-for="(tab, index) in tabs"
        :id="`${id}-tab-${tab.id}`"
        :key="tab.id"
        :ref="element => rememberButton(tab.id, element)"
        type="button"
        role="tab"
        :aria-selected="selected === tab.id"
        :aria-controls="tab.id"
        :tabindex="selected === tab.id ? 0 : -1"
        @click="selectTab(tab.id)"
        @keydown="onKeydown($event, index)"
      >{{ tab.label }}</button>
    </div>
    <section
      v-for="tab in tabs"
      :id="tab.id"
      :key="tab.id"
      :ref="element => rememberPanel(tab.id, element)"
      class="docs-tabs__panel"
      role="tabpanel"
      :aria-labelledby="`${id}-tab-${tab.id}`"
      :hidden="selected !== tab.id"
      tabindex="0"
    >
      <slot :name="tab.id" />
    </section>
  </div>
</template>

<style scoped>
.docs-tabs {
  margin: 24px 0;
  min-width: 0;
}

.docs-tabs__label {
  margin-bottom: 10px;
  color: var(--vp-c-text-2);
  font-size: 13px;
  font-weight: 600;
}

.docs-tabs__choices {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  padding-bottom: 16px;
  border-bottom: 1px solid var(--vp-c-divider);
}

.docs-tabs__choices button {
  flex: 1 1 150px;
  min-width: 0;
  min-height: 44px;
  padding: 10px 12px;
  border: 1px solid var(--vp-c-divider);
  border-radius: 6px;
  color: var(--vp-c-text-1);
  background: var(--vp-c-bg);
  font-size: 14px;
  font-weight: 500;
  line-height: 1.5;
  text-align: center;
  overflow-wrap: anywhere;
  cursor: pointer;
}

.docs-tabs__choices button:hover {
  border-color: var(--vp-c-brand-1);
}

.docs-tabs__choices button[aria-selected='true'] {
  border-color: var(--vp-c-brand-1);
  color: var(--vp-c-brand-1);
  background: var(--vp-c-brand-soft);
  font-weight: 600;
}

.docs-tabs__choices button:focus-visible,
.docs-tabs__panel:focus-visible {
  outline: 2px solid var(--vp-c-brand-1);
  outline-offset: 3px;
}

.docs-tabs__panel {
  min-width: 0;
  padding-top: 4px;
}

.docs-tabs__panel[hidden] {
  display: none;
}

@media print {
  .docs-tabs__label,
  .docs-tabs__choices {
    display: none;
  }

  .docs-tabs__panel[hidden] {
    display: block;
  }
}
</style>
