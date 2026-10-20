<template>
  <section aria-label="附加文件" class="min-w-0 space-y-3">
    <div class="flex flex-wrap items-center justify-between gap-2">
      <div class="flex items-center gap-1">
        <h3 class="text-sm font-medium">附加文件 <span class="num text-muted-foreground">{{ files.length }}</span></h3>
        <FieldHelp label="附加文件" :text="optionHelp('配置需要规则、证书、脚本或 include 文件时上传。', '文件随完整下载和 Agent 发布打包，路径相对工作目录。单文件限 16 MiB，总计 64 MiB；state/ 禁用。绝对路径及环境变量引用的文件需自行准备。')" />
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <OxidnsEditorModeToggle :model-value="mode" :options="[{ value: 'form', label: '文件' }, { value: 'yaml', label: 'YAML' }]" label="附加文件编辑模式" :disabled="disabled || importing" @update:model-value="switchMode" />
        <Button type="button" variant="outline" class="h-9 max-sm:h-11" :disabled="disabled || importing || mode === 'yaml'" @click="upload?.click()"><Upload class="size-3.5" />{{ importing ? '正在读取…' : '上传文件' }}</Button>
        <Button type="button" variant="outline" class="h-9 max-sm:h-11" :disabled="disabled || importing || mode === 'yaml'" @click="add"><Plus class="size-3.5" />添加</Button>
        <input ref="upload" type="file" multiple class="hidden" :disabled="disabled || importing" aria-label="上传附加文件" @change="importFiles" />
      </div>
    </div>
    <Textarea v-if="mode === 'yaml'" :model-value="draft" aria-label="附加文件 YAML" class="min-h-64 font-mono text-xs" :disabled="disabled || importing" @update:model-value="editYaml(String($event))" />
    <template v-else>
      <p v-if="!files.length" class="py-3 text-xs text-muted-foreground">尚未添加文件。配置引用的相对路径文件需要在此上传。</p>
      <Collapsible v-for="(file, index) in files" :key="index" class="group min-w-0 rounded-lg border border-border">
        <div class="flex min-w-0 items-center gap-2 p-2">
          <CollapsibleTrigger type="button" class="flex size-9 shrink-0 items-center justify-center rounded-md hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-accent max-sm:size-11" :aria-label="`展开文件 ${index + 1}`"><ChevronDown class="size-4 group-data-[state=open]:rotate-180" /></CollapsibleTrigger>
          <Input v-model="file.path" :aria-label="`文件 ${index + 1} 路径`" placeholder="例如 rules/custom.txt" class="h-9 min-w-0 flex-1 font-mono text-xs max-sm:h-11" :disabled="disabled || importing" @update:model-value="commit" />
          <FieldHelp :label="`文件 ${index + 1} 路径`" :text="optionHelp('上传后需要设置文件的部署位置或修改文件名。', '配置中应引用同一路径，使用不含 ../ 的相对路径。不能使用 config.yaml、state/ 或 ConfigFlow 内部目录，以免覆盖主配置及运行数据。')" />
          <Button type="button" variant="ghost" size="icon" class="size-9 shrink-0 text-destructive-accent max-sm:size-11" :disabled="disabled || importing" :aria-label="`删除文件 ${index + 1}`" @click="files.splice(index, 1); commit()"><Trash2 class="size-4" /></Button>
        </div>
        <CollapsibleContent class="space-y-3 border-t border-border p-3">
          <div class="flex flex-wrap items-center gap-2">
            <span class="text-xs text-muted-foreground">{{ file.encoding === 'base64' ? '二进制文件（Base64）' : 'UTF-8 文本' }}</span>
            <FieldHelp :label="`文件 ${index + 1} 内容`" :text="optionHelp('需要修改文本或随配置发布二进制文件时使用。', '文本可直接编辑，二进制以 Base64 保存，发布时还原。私钥也会随配置保存和发布；include 支持 plugins 和 include，按官方顺序展开到主配置。')" />
          </div>
          <Textarea v-model="file.content" :aria-label="`文件 ${index + 1} 内容`" :rows="8" class="font-mono text-xs" :disabled="disabled || importing" spellcheck="false" @update:model-value="commit" />
        </CollapsibleContent>
      </Collapsible>
    </template>
    <p v-if="error" role="alert" class="break-words text-sm text-destructive-accent">{{ error }}</p>
    <p v-if="uploadError" role="alert" class="break-words text-sm text-destructive-accent">{{ uploadError }}</p>
  </section>
</template>

<script setup lang="ts">
import { onUnmounted, ref, toRaw, watch } from 'vue'
import { dump, load, JSON_SCHEMA } from 'js-yaml'
import { ChevronDown, Plus, Trash2, Upload } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import FieldHelp from '@/components/common/FieldHelp.vue'
import { optionHelp } from '@/lib/optionCopy'
import OxidnsEditorModeToggle from './OxidnsEditorModeToggle.vue'

export interface OxidnsManagedFile { path: string; content: string; encoding: 'text' | 'base64' }
const props = defineProps<{ modelValue: OxidnsManagedFile[]; disabled?: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: OxidnsManagedFile[]]; error: [message: string] }>()
const files = ref<OxidnsManagedFile[]>([])
const mode = ref<'form' | 'yaml'>('form')
const draft = ref('')
const error = ref('')
const uploadError = ref('')
const importing = ref(false)
const upload = ref<HTMLInputElement>()
let lastEmitted: OxidnsManagedFile[] | undefined
let revision = 0
onUnmounted(() => { revision++ })
function report(message = '') { error.value = message; emit('error', importing.value ? '附加文件正在读取，请稍候。' : message) }
function validate(value: unknown): asserts value is OxidnsManagedFile[] {
  if (!Array.isArray(value)) throw new Error('附加文件必须是 YAML 列表。')
  if (value.length > 256) throw new Error('最多添加 256 个文件。')
  const seen = new Set<string>()
  let totalBytes = 0
  for (const item of value) {
    if (!item || typeof item !== 'object' || typeof item.path !== 'string' || typeof item.content !== 'string' || !['text', 'base64'].includes(item.encoding)) throw new Error('每个文件必须包含 path、content 和 encoding（text 或 base64）。')
    const path = item.path.replace(/^\.\//, '')
    if (!path || path.startsWith('/') || path.includes('\\') || /[\x00-\x1f]/.test(path) || path.split('/').some((part: string) => !part || part === '..' || part === '.')) throw new Error('文件路径必须是有效相对路径，不能包含 ../。')
    if (path === 'config.yaml' || path.startsWith('state/') || path === 'state' || path.startsWith('.configflow') || path === 'manifest.json') throw new Error('此路径由 ConfigFlow 或运行数据使用，请更换文件路径。')
    if (seen.has(path)) throw new Error(`文件路径重复：${path}`)
    if ([...seen].some(previous => path.startsWith(`${previous}/`) || previous.startsWith(`${path}/`))) throw new Error(`文件与目录路径冲突：${path}`)
    seen.add(path)
    if (item.content.length > 32 * 1024 * 1024) throw new Error('单个附加文件不能超过 16 MiB。')
    let bytes: number
    if (item.encoding === 'base64') {
      const padding = item.content.endsWith('==') ? 2 : item.content.endsWith('=') ? 1 : 0
      if (item.content.length % 4 || /[^A-Za-z0-9+/]/.test(item.content.slice(0, item.content.length - padding))) throw new Error(`文件 ${path} 的 Base64 内容无效。`)
      bytes = item.content.length / 4 * 3 - padding
    } else bytes = new TextEncoder().encode(item.content).length
    if (bytes > 16 * 1024 * 1024) throw new Error('单个附加文件不能超过 16 MiB。')
    totalBytes += bytes
    if (totalBytes > 64 * 1024 * 1024) throw new Error('附加文件总大小不能超过 64 MiB。')
  }
}
function publish() { lastEmitted = files.value.map(file => ({ ...file })); emit('update:modelValue', lastEmitted) }
function commit() {
  // Keep incomplete form entries as drafts; parent validity prevents saving them.
  publish()
  try { validate(files.value); report() } catch (failure: any) { report(failure.message) }
}
function editYaml(text: string) {
  draft.value = text
  try { const value = load(text, { schema: JSON_SCHEMA }); validate(value); files.value = value; publish(); report() }
  catch (failure: any) { report(failure.message || '无法读取附加文件 YAML。') }
}
function switchMode(next: 'form' | 'yaml') {
  if (disabledOrBusy() || next === mode.value) return
  if (next === 'form' && error.value) return
  if (next === 'yaml') draft.value = dump(files.value, { schema: JSON_SCHEMA, noRefs: true, lineWidth: -1 })
  mode.value = next
}
function add() { files.value.push({ path: '', content: '', encoding: 'text' }); commit() }
function disabledOrBusy() { return props.disabled || importing.value }
async function importFiles(event: Event) {
  const input = event.target as HTMLInputElement
  if (disabledOrBusy() || !input.files?.length) return
  const startedAt = revision
  uploadError.value = ''
  importing.value = true
  report(error.value)
  try {
    const additions: OxidnsManagedFile[] = []
    for (const file of Array.from(input.files || [])) {
      if (file.size > 16 * 1024 * 1024) throw new Error(`${file.name} 超过 16 MiB，请拆分文件。`)
      const bytes = new Uint8Array(await file.arrayBuffer())
      if (revision !== startedAt) return
      let content: string, encoding: 'text' | 'base64'
      try {
        content = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(bytes)
        if (content.includes('\0')) throw new Error('binary')
        encoding = 'text'
      } catch {
        let binary = ''
        for (let offset = 0; offset < bytes.length; offset += 8192) binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192))
        content = btoa(binary); encoding = 'base64'
      }
      additions.push({ path: file.name, content, encoding })
    }
    validate([...files.value, ...additions])
    files.value.push(...additions); commit()
  } catch (failure: any) { if (revision === startedAt) uploadError.value = failure.message || '文件读取失败。' }
  finally { input.value = ''; if (revision === startedAt) { importing.value = false; report(error.value) } }
}
watch(() => props.modelValue, value => {
  if (toRaw(value) === lastEmitted) { lastEmitted = undefined; return }
  lastEmitted = undefined
  revision++
  importing.value = false
  uploadError.value = ''
  files.value = (value || []).map(file => ({ ...file }))
  draft.value = dump(files.value, { schema: JSON_SCHEMA, noRefs: true, lineWidth: -1 })
  try { validate(files.value); report() } catch (failure: any) { report(failure.message) }
}, { immediate: true })
</script>
