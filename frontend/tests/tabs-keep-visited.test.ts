import { describe, expect, it } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, onMounted, onUnmounted, ref } from 'vue'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'

const mounts: string[] = []
const unmounts: string[] = []
const Panel = defineComponent({
  props: { name: { type: String, required: true } },
  setup(props) {
    onMounted(() => mounts.push(props.name))
    onUnmounted(() => unmounts.push(props.name))
    return () => h('p', props.name)
  }
})

function render(keepVisited: boolean) {
  mounts.length = 0
  unmounts.length = 0
  const tab = ref('a')
  const wrapper = mount(defineComponent(() => () => h(Tabs, { keepVisited, modelValue: tab.value, 'onUpdate:modelValue': (value: any) => { tab.value = value } }, () => [
    h(TabsList, () => ['a', 'b', 'c'].map(value => h(TabsTrigger, { value }, () => value))),
    ...['a', 'b', 'c'].map(value => h(TabsContent, { value }, () => h(Panel, { name: value })))
  ])))
  return { wrapper, show: async (value: string) => { tab.value = value; await flushPromises() } }
}

describe('Tabs keep-visited', () => {
  it('mounts a tab on first visit and keeps it mounted but hidden afterwards', async () => {
    const { wrapper, show } = render(true)
    expect(mounts).toEqual(['a'])
    expect(wrapper.findAll('[role="tabpanel"]')).toHaveLength(3)
    await show('b')
    await show('a')
    await show('b')
    expect(mounts).toEqual(['a', 'b'])
    expect(unmounts).toEqual([])
    const panels = wrapper.findAll('[role="tabpanel"]')
    expect(panels.map(panel => panel.attributes('data-state'))).toEqual(['inactive', 'active', 'inactive'])
    expect(panels[0]!.text()).toBe('a')
    expect(panels[0]!.classes()).toContain('data-[state=inactive]:hidden')
    expect(panels[2]!.text()).toBe('')
    wrapper.unmount()
    expect(unmounts.sort()).toEqual(['a', 'b'])
  })

  it('still unmounts hidden tabs by default', async () => {
    const { wrapper, show } = render(false)
    await show('b')
    await show('a')
    expect(mounts).toEqual(['a', 'b', 'a'])
    expect(unmounts).toEqual(['a', 'b'])
    wrapper.unmount()
  })
})
