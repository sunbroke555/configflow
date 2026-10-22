import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, ref } from 'vue'
import MultiSelect from '@/components/common/MultiSelect.vue'

const options = [
  { value: 'hk1', label: '香港 01' },
  { value: 'hk2', label: '香港 02' },
  { value: 'jp1', label: '日本 01' },
]

const wrappers: ReturnType<typeof mount>[] = []
beforeEach(() => {
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  Element.prototype.scrollIntoView ??= () => {}
})
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  document.body.innerHTML = ''
  vi.unstubAllGlobals()
})

async function open(initial: string[] = []) {
  const selected = ref<string[]>(initial)
  const wrapper = mount(defineComponent({
    components: { MultiSelect },
    setup: () => ({ selected, options }),
    template: '<MultiSelect v-model="selected" :options="options" />',
  }), { attachTo: document.body })
  wrappers.push(wrapper)
  await wrapper.get('button[role="combobox"]').trigger('click')
  await flushPromises()
  return selected
}

const buttonByText = (text: string) =>
  [...document.querySelectorAll<HTMLButtonElement>('button')].find(button => button.textContent?.includes(text))

async function search(term: string) {
  const input = document.querySelector<HTMLInputElement>('[data-slot="command-input"]')!
  input.value = term
  input.dispatchEvent(new Event('input'))
  await flushPromises()
}

describe('MultiSelect bulk actions', () => {
  it('selects every option and clears the selection', async () => {
    const selected = await open()
    buttonByText('全选 3 项')!.click()
    await flushPromises()
    expect(selected.value).toEqual(['hk1', 'hk2', 'jp1'])

    expect(buttonByText('取消全选')).toBeTruthy()
    buttonByText('清空已选')!.click()
    await flushPromises()
    expect(selected.value).toEqual([])
    expect(buttonByText('清空已选')!.disabled).toBe(true)
  })

  it('only adds or removes options matching the search term', async () => {
    const selected = await open(['jp1', 'hk2'])
    await search('香港')
    buttonByText('选中 2 个结果')!.click()
    await flushPromises()
    expect(selected.value).toEqual(['jp1', 'hk2', 'hk1'])

    buttonByText('取消选中 2 个结果')!.click()
    await flushPromises()
    expect(selected.value).toEqual(['jp1'])
  })
})
