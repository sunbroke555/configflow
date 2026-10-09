<template>
  <div ref="editorRef" class="yaml-editor"></div>
</template>

<script setup lang="ts">
import { ref, onMounted, watch, onBeforeUnmount } from 'vue'
import { EditorView, basicSetup } from 'codemirror'
import { EditorState } from '@codemirror/state'
import { yaml } from '@codemirror/lang-yaml'
import { oneDark } from '@codemirror/theme-one-dark'

const props = defineProps<{
  modelValue: string
  placeholder?: string
  readOnly?: boolean
}>()

const emit = defineEmits<{
  'update:modelValue': [value: string]
}>()

const editorRef = ref<HTMLElement>()
let editorView: EditorView | null = null

onMounted(() => {
  if (!editorRef.value) return

  const startState = EditorState.create({
    doc: props.modelValue || '',
    extensions: [
      basicSetup,
      yaml(),
      oneDark,
      EditorView.updateListener.of((update) => {
        if (update.docChanged) {
          const newValue = update.state.doc.toString()
          emit('update:modelValue', newValue)
        }
      }),
      EditorState.readOnly.of(props.readOnly || false),
      EditorView.theme({
        '&': {
          height: '100%',
          minHeight: '0',
          fontSize: '14px'
        },
        '.cm-scroller': {
          overflow: 'auto',
          fontFamily: "'Consolas', 'Monaco', 'Courier New', monospace"
        }
      })
    ]
  })

  editorView = new EditorView({
    state: startState,
    parent: editorRef.value
  })
})

watch(() => props.modelValue, (newValue) => {
  if (editorView && newValue !== editorView.state.doc.toString()) {
    editorView.dispatch({
      changes: {
        from: 0,
        to: editorView.state.doc.length,
        insert: newValue || ''
      }
    })
  }
})

watch(() => props.readOnly, (newReadOnly) => {
  if (editorView) {
    editorView.dispatch({
      effects: EditorState.readOnly.reconfigure(newReadOnly || false)
    })
  }
})

onBeforeUnmount(() => {
  if (editorView) {
    editorView.destroy()
  }
})
</script>

<style scoped>
.yaml-editor {
  height: 100%;
  min-height: 0;
  border: 1px solid var(--cf-bd-strong);
  border-radius: var(--cf-r-md);
  overflow: hidden;
}
</style>
