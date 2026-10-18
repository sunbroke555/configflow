import { mount } from '@vue/test-utils'
import { EditorState } from '@codemirror/state'
import { EditorView } from '@codemirror/view'
import { describe, expect, it } from 'vitest'
import YamlEditor from '@/components/YamlEditor.vue'

describe('YamlEditor read-only changes', () => {
  it.each([false, true])('reconfigures a mounted editor from readOnly=%s without losing its document', async initial => {
    const wrapper = mount(YamlEditor, { props: { modelValue: 'port: 1053\n', readOnly: initial } })
    try {
      const view = EditorView.findFromDOM(wrapper.get('.cm-editor').element as HTMLElement)!
      view.dispatch({ selection: { anchor: 4 } })
      expect(view.state.facet(EditorState.readOnly)).toBe(initial)
      await wrapper.setProps({ readOnly: !initial })
      expect(view.state.facet(EditorState.readOnly)).toBe(!initial)
      expect(view.state.doc.toString()).toBe('port: 1053\n')
      expect(view.state.selection.main.anchor).toBe(4)
      expect(wrapper.emitted('update:modelValue')).toBeUndefined()
      await wrapper.setProps({ readOnly: initial })
      expect(view.state.facet(EditorState.readOnly)).toBe(initial)
      await wrapper.setProps({ modelValue: 'port: 53\n' })
      expect(view.state.doc.toString()).toBe('port: 53\n')
    } finally {
      wrapper.unmount()
    }
  })
})
