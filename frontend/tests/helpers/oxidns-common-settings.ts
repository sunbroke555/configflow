import { afterEach, expect, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import OxidnsCommonSettings from '@/components/generate/OxidnsCommonSettings.vue'
import { featureGroups } from '@/lib/oxidns'

// These suites mount the whole OxiDNS settings form per test; leave headroom for
// parallel workers on a busy machine without raising the global timeout.
vi.setConfig({ testTimeout: 15_000 })

const wrappers: ReturnType<typeof mount>[] = []
const values = () => ({ worker_threads: 8, listen: '127.0.0.1:5353', local_dns: '- addr: 192.0.2.53\n  extension: false\n', remote_dns: '192.0.2.54', fallback_dns: '', block_qtypes: [65], local_concurrent: 2,
  direct_rules: ['direct'], proxy_rules: [], direct_rulesets: [], proxy_rulesets: [], custom_matches: [], custom_hosts: '', custom_match_position: 'head',
  api: {enabled: true, listen: '127.0.0.1:9199'}, log: {level: 'info'},
  ...Object.fromEntries(featureGroups.map(feature => [feature.key, {enabled: false, args: {}}])) })
async function render(extra: Record<string, any> = {}) {
  const wrapper = mount(OxidnsCommonSettings, {attachTo: document.body, props: {values: values(), capabilities: {}, ...extra}})
  wrappers.push(wrapper)
  await flushPromises()
  return wrapper
}
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); document.body.innerHTML = '' })
async function click(text: string) {
  const target = Array.from(document.querySelectorAll('button')).find(button => button.textContent?.trim() === text)
  expect(target, text).toBeDefined()
  target!.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, button: 0})); target!.click()
  await flushPromises()
}
async function input(id: string, value: string) {
  const target = document.getElementById(id) as HTMLInputElement
  expect(target, id).not.toBeNull()
  target.value = value; target.dispatchEvent(new Event('input', {bubbles: true}))
  await flushPromises()
}
const lastChange = (wrapper: ReturnType<typeof mount>) => wrapper.emitted('change')?.at(-1)?.[0]


export { values, render, click, input, lastChange }
