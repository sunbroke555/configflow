<template>
  <div ref="editorRef" class="yaml-editor"></div>
</template>

<script setup lang="ts">
import { ref, onMounted, watch, onBeforeUnmount } from 'vue'
import { EditorView, basicSetup } from 'codemirror'
import { Compartment, EditorState } from '@codemirror/state'
import { yaml } from '@codemirror/lang-yaml'
import { HighlightStyle, syntaxHighlighting } from '@codemirror/language'
import { tags } from '@lezer/highlight'

/* 编辑器配色全部引用 theme.css 的语义 token，深浅主题自动跟随 */
const flowTheme = EditorView.theme({
  '&': {
    height: '100%',
    minHeight: '0',
    fontSize: '13px',
    color: 'var(--foreground)',
    backgroundColor: 'var(--background)'
  },
  '.cm-scroller': {
    overflow: 'auto',
    fontFamily: 'var(--font-mono)',
    lineHeight: '1.7'
  },
  '.cm-content': { caretColor: 'var(--primary-accent)' },
  '.cm-cursor, .cm-dropCursor': { borderLeftColor: 'var(--primary-accent)' },
  '&.cm-focused .cm-selectionBackground, .cm-selectionBackground, ::selection': {
    backgroundColor: 'oklch(from var(--primary) l c h / 22%) !important'
  },
  '.cm-gutters': {
    backgroundColor: 'var(--background)',
    color: 'var(--muted-foreground)',
    border: 'none',
    opacity: '0.75'
  },
  '.cm-activeLine': { backgroundColor: 'oklch(from var(--foreground) l c h / 4%)' },
  '.cm-activeLineGutter': { backgroundColor: 'transparent', color: 'var(--primary-accent)' },
  '.cm-foldPlaceholder': {
    backgroundColor: 'var(--secondary)',
    border: '1px solid var(--border-strong)',
    color: 'var(--muted-foreground)'
  },
  '.cm-searchMatch': { backgroundColor: 'oklch(from var(--accent-2) l c h / 25%)' },
  '.cm-panels': { backgroundColor: 'var(--card)', color: 'var(--foreground)' },
  '.cm-tooltip': {
    backgroundColor: 'var(--popover)',
    border: '1px solid var(--border-strong)',
    color: 'var(--foreground)'
  }
})

const flowHighlight = HighlightStyle.define([
  { tag: [tags.propertyName, tags.definition(tags.propertyName)], color: 'var(--primary-accent)' },
  { tag: [tags.string, tags.special(tags.string)], color: 'var(--success-accent)' },
  { tag: [tags.number, tags.bool, tags.null, tags.atom], color: 'var(--info-accent)' },
  { tag: [tags.keyword, tags.typeName, tags.labelName], color: 'var(--accent-2)' },
  { tag: [tags.comment, tags.lineComment, tags.blockComment], color: 'var(--muted-foreground)', fontStyle: 'italic' },
  { tag: [tags.punctuation, tags.separator, tags.bracket], color: 'var(--muted-foreground)' },
  { tag: tags.meta, color: 'var(--warning-accent)' }
])

const props = defineProps<{
  modelValue: string
  placeholder?: string
  readOnly?: boolean
}>()

const emit = defineEmits<{
  'update:modelValue': [value: string]
}>()

const editorRef = ref<HTMLElement>()
const readOnlyCompartment = new Compartment()
let editorView: EditorView | null = null

onMounted(() => {
  if (!editorRef.value) return

  const startState = EditorState.create({
    doc: props.modelValue || '',
    extensions: [
      basicSetup,
      yaml(),
      flowTheme,
      syntaxHighlighting(flowHighlight),
      EditorView.updateListener.of((update) => {
        if (update.docChanged) {
          const newValue = update.state.doc.toString()
          emit('update:modelValue', newValue)
        }
      }),
      readOnlyCompartment.of(EditorState.readOnly.of(props.readOnly || false))
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
      effects: readOnlyCompartment.reconfigure(EditorState.readOnly.of(newReadOnly || false))
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
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-lg);
  overflow: hidden;
}
</style>
