<template>
  <SettingsWorkspace
    :open="open"
    title="OxiDNS 设置"
    :back-label="backLabel"
    description="常用设置、完整参数与 YAML 共用一份草稿。保存草稿不会下发；保存并发布必须通过规则检查和目标 OxiDNS 核心检查。"
    size="wide"
    :dirty="dirty"
    :busy="saving"
    :save-disabled="loading || editorLocked || nativePending || projecting || !settings || Boolean(loadError) || Boolean(editorError)"
    save-label="保存草稿"
    @update:open="emit('update:open', $event)"
    @save="save"
  >
    <template #footer="{ disabled }">
      <SettingsPublishBar :open="open" :profile-id="profileId" :preferred-agent-id="preferredAgentId" service-type="oxidns" :disabled="disabled" :save="() => save(false)" @save-draft="save()" />
    </template>
    <div class="flex min-h-0 min-w-0 flex-1 flex-col gap-3 p-3 md:p-5" @input.capture="markEdited">
      <p v-if="contextLabel" class="m-0 break-words text-xs text-muted-foreground">{{ contextLabel }}</p>
      <LoadingRows v-if="loading" :rows="5" />
      <div v-else-if="loadError" role="alert" class="space-y-3 py-4">
        <p class="text-destructive-accent">{{ loadError }}</p><Button variant="outline" @click="load">重新加载</Button>
      </div>
      <div v-else-if="settings" class="flex min-h-0 min-w-0 flex-1 flex-col gap-3">
        <div v-if="tab === 'files'" class="flex shrink-0 flex-wrap items-center justify-between gap-2">
          <OxidnsEditorModeToggle v-model="tab" :options="contentTabs" label="OxiDNS 设置内容" :disabled="editorLocked" />
          <div v-if="settings.legacy_native_config" class="flex items-center gap-1">
            <Button variant="ghost" size="sm" class="min-h-11 max-sm:w-11 max-sm:px-0" :disabled="saving" aria-label="旧草稿备份" @click="downloadLegacy"><Download class="size-3.5" /><span class="max-sm:sr-only">旧草稿备份</span></Button>
            <FieldHelp label="旧草稿备份" text="旧版快捷模板和完整配置分别保存。当前已载入原来生效的配置，另一份未生效的完整配置已保留在备份中，可下载查看或复制所需参数。保存后备份仍保留。" />
          </div>
        </div>
        <OxidnsNativeEditor ref="nativeEditor" v-show="tab === 'config'" :model-value="settings.native_config" :reconcile="reconcileNative" :disabled="editorLocked" @update:model-value="acceptNativeConfig" @pending="nativeSyncPending" @view="nativeView = $event" @error="editorErrors.native_config = $event">
          <template #navigation>
            <OxidnsEditorModeToggle v-model="tab" :options="contentTabs" label="OxiDNS 设置内容" :disabled="editorLocked || nativePending || Boolean(editorErrors.native_config)" />
            <template v-if="settings.legacy_native_config">
              <Button variant="ghost" size="icon" class="size-11" :disabled="saving" aria-label="旧草稿备份" @click="downloadLegacy"><Download class="size-3.5" /></Button>
              <FieldHelp label="旧草稿备份" text="旧版快捷模板和完整配置分别保存。当前已载入原来生效的配置，另一份未生效的完整配置已保留在备份中，可下载查看或复制所需参数。保存后备份仍保留。" />
            </template>
          </template>
          <template #common>
            <LoadingRows v-if="projecting" :rows="4" />
            <div v-else-if="projectionError" class="space-y-3 p-3">
              <p role="alert" class="text-sm text-destructive-accent">{{ projectionError }}</p>
              <Button variant="outline" @click="projectCommon">重新读取常用设置</Button>
            </div>
            <OxidnsCommonSettings v-else-if="commonValues" :key="commonKey" :initial-feature="initialFeature" :values="commonValues" :capabilities="commonCapabilities" :rules="rules" :rule-sets="ruleSets" :disabled="saving" :navigation-disabled="editorLocked" @change="queueChanges" @error="editorErrors.common = $event" @navigate="nativeEditor?.showFull($event)" @full="nativeEditor?.showFull()" />
          </template>
        </OxidnsNativeEditor>
        <div ref="filesPanel" v-show="tab === 'files'" class="min-h-0 flex-1 overflow-y-auto">
          <OxidnsFilesEditor v-model="settings.additional_files" :disabled="editorLocked || nativePending" @error="editorErrors.additional_files = $event" />
        </div>
        <div v-if="applyError" class="flex shrink-0 flex-wrap items-center gap-2">
          <p role="alert" class="min-w-0 flex-1 break-words text-sm text-destructive-accent">{{ applyError }}</p>
          <Button variant="outline" size="sm" :disabled="applying || Boolean(editorErrors.common)" @click="retryChanges">重试同步</Button>
          <Button variant="ghost" size="sm" :disabled="applying" @click="discardPending">撤销未同步修改</Button>
        </div>
        <p v-else-if="applying || pendingCount" role="status" class="flex shrink-0 items-center gap-2 text-xs text-muted-foreground"><Loader2 class="size-3.5 animate-spin" />正在同步当前草稿…</p>
      </div>
    </div>
    <template #status>
      <div class="max-h-[35dvh] space-y-2 overflow-y-auto overscroll-contain">
        <section v-if="settings && !loading && !loadError" class="shrink-0 text-xs" aria-label="配置检查结果">
          <p v-if="validationState === 'pending'" role="status" class="flex min-h-8 items-center gap-2 text-muted-foreground"><Loader2 class="size-3.5 animate-spin" />正在检查草稿参数，可继续编辑或保存…</p>
          <div v-else-if="validationError" class="flex flex-wrap items-center gap-2">
            <p role="alert" class="min-w-0 flex-1 break-words text-destructive-accent">{{ validationError }} 可先保存草稿，发布前需重新检查。</p>
            <Button variant="outline" size="sm" class="min-h-11" :disabled="saving" @click="validateDraft">重新检查</Button>
          </div>
          <details v-else-if="validationIssues.length" class="rounded-md border border-border px-3">
            <summary class="min-h-11 cursor-pointer content-center font-medium" :class="validationErrors.length ? 'text-destructive-accent' : 'text-warning-accent'">
              {{ validationErrors.length ? `${validationErrors.length} 项发布前需要修正` : '规则检查通过，存在待目标环境验证项' }}<span v-if="validationWarnings.length"> · {{ validationWarnings.length }} 项提示</span>
            </summary>
            <p class="border-t border-border py-2 text-muted-foreground">草稿可以保存；发布必须通过规则检查和目标 OxiDNS 核心检查。</p>
            <ul class="space-y-1 border-t border-border py-2" aria-label="配置检查问题">
              <li v-for="(issue, index) in validationIssues" :key="index">
                <button type="button" class="flex min-h-11 w-full min-w-0 flex-col gap-0.5 rounded-md px-2 py-1.5 text-left hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-accent disabled:opacity-50" :class="issue.severity === 'error' ? 'text-destructive-accent' : 'text-warning-accent'" :disabled="editorLocked || nativePending || Boolean(editorError)" :aria-label="`定位${issue.severity === 'error' ? '错误' : '提示'} ${index + 1}`" @click="locateValidationIssue(issue)">
                  <span class="break-words">{{ issue.message }}</span><code class="break-all text-[11px] text-muted-foreground">{{ issue.source ? `${issue.source} · ` : '' }}{{ formatOxidnsIssuePath(issue.path) }}</code>
                </button>
              </li>
            </ul>
          </details>
          <p v-else-if="validationCurrent" role="status" class="flex min-h-8 items-center gap-1.5 text-muted-foreground"><Check class="size-3.5 text-success-accent" />规则检查通过；发布时还需目标核心检查</p>
        </section>
        <p v-if="saveError" role="alert" class="shrink-0 break-words text-sm text-destructive-accent">{{ saveError }}</p>
        <p v-else-if="editorError && !(tab === 'config' && editorError === editorErrors.native_config)" role="alert" class="shrink-0 break-words text-sm text-destructive-accent">{{ editorError }}</p>
      </div>
    </template>
  </SettingsWorkspace>
</template>
<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { Check, Download, Loader2 } from '@lucide/vue'
import { oxidnsApi, ruleApi, ruleSetApi, type PublishGuard } from '@/api'
import { notify } from '@/lib/feedback'
import { formatOxidnsIssuePath, readOxidnsValidationResult, type OxidnsValidationIssue } from '@/lib/oxidnsNativeValidation'
import OxidnsNativeEditor from './OxidnsNativeEditor.vue'
import OxidnsCommonSettings from './OxidnsCommonSettings.vue'
import OxidnsFilesEditor from './OxidnsFilesEditor.vue'
import OxidnsEditorModeToggle from './OxidnsEditorModeToggle.vue'
import FieldHelp from '@/components/common/FieldHelp.vue'
import LoadingRows from '@/components/common/LoadingRows.vue'
import { Button } from '@/components/ui/button'
import SettingsWorkspace from './SettingsWorkspace.vue'
import SettingsPublishBar from './SettingsPublishBar.vue'

const props = defineProps<{ open: boolean; profileId: string; preferredAgentId?: string; initialFeature?: string; contextLabel?: string; backLabel?: string }>()
const emit = defineEmits<{ (event: 'update:open', value: boolean): void; (event: 'saved'): void }>()
const settings = ref<Record<string, any> | null>(null)
const rules = ref<any[]>([])
const ruleSets = ref<any[]>([])
const loading = ref(false)
const saving = ref(false)
const loadError = ref('')
const saveError = ref('')
const contentTabs = [{ value: 'config', label: '配置' }, { value: 'files', label: '附加文件' }]
const tab = ref('config')
const nativeView = ref('common')
const nativeEditor = ref<InstanceType<typeof OxidnsNativeEditor>>()
const filesPanel = ref<HTMLElement>()
const editorErrors = reactive<Record<string, string>>({})
const editorError = computed(() => Object.values(editorErrors).find(Boolean) || '')
const commonValues = ref<Record<string, any> | null>(null)
const commonCapabilities = ref<Record<string, any>>({})
const commonKey = ref(0)
const projecting = ref(false)
const projectionError = ref('')
const applying = ref(false)
const nativePending = ref(false)
let reconciledNative: Record<string, any> | undefined
let nativeRequest = 0
const applyError = ref('')
const pending = ref<Record<string, unknown>>({})
const pendingCount = computed(() => Object.keys(pending.value).length)
const editorLocked = computed(() => saving.value || applying.value || Boolean(pendingCount.value || editorErrors.common || applyError.value))
let loadVersion = 0
let projectionVersion = 0
let projectionSource: string | undefined
let changeTimer: ReturnType<typeof setTimeout> | undefined
const validationState = ref<'pending' | 'ready' | 'failed'>('pending')
const validationIssues = ref<OxidnsValidationIssue[]>([])
const validationError = ref('')
const validationSource = computed(() => settings.value ? JSON.stringify(settings.value) : '')
const savedSource = ref('')
const editedInput = ref(false)
const dirty = computed(() => Boolean(settings.value) && (validationSource.value !== savedSource.value || applying.value || nativePending.value || Boolean(pendingCount.value) || (editedInput.value && Boolean(editorError.value))))
function markEdited() { if (!loading.value) editedInput.value = true }
function nativeSyncPending(value: boolean) { nativePending.value = value; if (value) markEdited() }
async function reconcileNative(before: string, candidate: string, renames?: Record<string, string>) {
  const request = ++nativeRequest
  const version = loadVersion
  const profileId = props.profileId
  if (!settings.value || settings.value.native_config !== before) throw new Error('配置已变化，请撤销未同步修改后重新编辑。')
  const source = JSON.stringify(settings.value)
  const response = await oxidnsApi.applyNativeSettings(settings.value, candidate, profileId, renames)
  if (request !== nativeRequest || version !== loadVersion || profileId !== props.profileId || !props.open || JSON.stringify(settings.value) !== source) {
    throw new Error('当前配置已切换，本次同步结果已忽略。')
  }
  // Keep the candidate local until the editor accepts this exact response.
  reconciledNative = response.data
  return { native_config: response.data.settings.native_config, notices: response.data.notices }
}
function acceptNativeConfig(text: string) {
  if (!settings.value) return
  if (reconciledNative?.settings.native_config === text) {
    ++projectionVersion
    projectionSource = text
    commonValues.value = reconciledNative.values
    commonCapabilities.value = reconciledNative.capabilities
    projecting.value = false
    projectionError.value = ''
    settings.value = reconciledNative.settings
  } else settings.value.native_config = text
  reconciledNative = undefined
}
const validatedSource = ref('')
const validationErrors = computed(() => validationIssues.value.filter(issue => issue.severity === 'error'))
const validationWarnings = computed(() => validationIssues.value.filter(issue => issue.severity === 'warning'))
const validationCurrent = computed(() => validationState.value === 'ready' && validatedSource.value === validationSource.value)
let validationVersion = 0
let validationTimer: ReturnType<typeof setTimeout> | undefined

function invalidateValidation() {
  if (validationTimer) clearTimeout(validationTimer)
  validationTimer = undefined
  ++validationVersion
  validatedSource.value = ''
  validationState.value = 'pending'
  validationError.value = ''
  validationIssues.value = []
}
function scheduleValidation() {
  invalidateValidation()
  if (!props.open || !settings.value || loading.value) return
  validationTimer = setTimeout(() => { validationTimer = undefined; void validateDraft() }, 300)
}
async function validateDraft(): Promise<boolean> {
  if (!settings.value || !props.open) return false
  if (validationTimer) clearTimeout(validationTimer)
  validationTimer = undefined
  const request = ++validationVersion
  const version = loadVersion
  const profileId = props.profileId
  const source = validationSource.value
  validationState.value = 'pending'
  validationError.value = ''
  try {
    const response = await oxidnsApi.validateSettings(JSON.parse(source), profileId)
    if (request !== validationVersion || version !== loadVersion || profileId !== props.profileId || !props.open || validationSource.value !== source) return false
    const result = readOxidnsValidationResult(response.data)
    validationIssues.value = result.issues
    validatedSource.value = source
    validationState.value = 'ready'
    return result.valid
  } catch (error: any) {
    if (request !== validationVersion || version !== loadVersion || profileId !== props.profileId || !props.open || validationSource.value !== source) return false
    validationState.value = 'failed'
    validatedSource.value = ''
    validationIssues.value = []
    validationError.value = error.response?.data?.message || error.message || '配置检查失败，请重试后保存。'
    return false
  }
}
async function locateValidationIssue(issue: OxidnsValidationIssue) {
  if (editorLocked.value || nativePending.value || editorError.value) return
  const path = issue.source?.replace(/^\.\//, '')
  if ((path && path !== 'config.yaml') || issue.path[0] === 'additional_files') {
    tab.value = 'files'
    await nextTick()
    const index = issue.path[0] === 'additional_files' && typeof issue.path[1] === 'number'
      ? issue.path[1] : settings.value?.additional_files?.findIndex((file: any) => file.path.replace(/^\.\//, '') === path)
    if (typeof index === 'number' && index >= 0) {
      const field = filesPanel.value?.querySelector<HTMLInputElement>(`[aria-label="文件 ${index + 1} 路径"]`)
      field?.focus()
      field?.scrollIntoView?.({ block: 'nearest' })
    }
  } else {
    tab.value = 'config'
    nativeEditor.value?.locateIssue(issue.path)
  }
}


function clearTimer() { if (changeTimer) clearTimeout(changeTimer); changeTimer = undefined }
function resetCommon() {
  ++nativeRequest
  nativePending.value = false
  reconciledNative = undefined
  clearTimer()
  ++projectionVersion
  ++commonKey.value
  commonValues.value = null
  commonCapabilities.value = {}
  projectionSource = undefined
  projectionError.value = ''
  projecting.value = false
  applying.value = false
  applyError.value = ''
  pending.value = {}
}
async function projectCommon() {
  if (!settings.value || applying.value || pendingCount.value) return
  const version = loadVersion
  const request = ++projectionVersion
  const source = settings.value.native_config
  projecting.value = true
  projectionError.value = ''
  try {
    const response = await oxidnsApi.readCommonSettings(settings.value, props.profileId)
    if (version !== loadVersion || request !== projectionVersion || settings.value?.native_config !== source) return
    commonValues.value = response.data.values
    commonCapabilities.value = response.data.capabilities
    projectionSource = source
  } catch (error: any) {
    if (version === loadVersion && request === projectionVersion) projectionError.value = error.response?.data?.message || '读取常用设置失败，请重试或使用完整参数编辑。'
  } finally { if (version === loadVersion && request === projectionVersion) projecting.value = false }
}
async function load() {
  const version = ++loadVersion
  loading.value = true
  saving.value = false
  loadError.value = ''
  saveError.value = ''
  settings.value = null
  savedSource.value = ''
  editedInput.value = false
  resetCommon()
  invalidateValidation()
  Object.keys(editorErrors).forEach(key => delete editorErrors[key])
  try {
    const [config, availableRules, availableSets] = await Promise.all([
      oxidnsApi.getSettings(props.profileId), ruleApi.getAll(props.profileId), ruleSetApi.getAll(props.profileId)
    ])
    if (version !== loadVersion) return
    const prepared = await oxidnsApi.prepareEditor(config.data, props.profileId)
    if (version !== loadVersion) return
    settings.value = prepared.data.settings
    savedSource.value = JSON.stringify(settings.value)
    tab.value = 'config'
    nativeView.value = 'common'
    rules.value = (availableRules.data || []).filter((item: any) => item.itemType === 'rule')
    ruleSets.value = availableSets.data || []
    await projectCommon()
  } catch (error: any) {
    if (version === loadVersion) loadError.value = error.response?.data?.message || '加载 OxiDNS 配置失败，请重试。'
  } finally {
    if (version === loadVersion) {
      loading.value = false
      if (settings.value && !loadError.value) await validateDraft()
    }
  }
}
function queueChanges(changes: Record<string, unknown>) {
  if (!settings.value || saving.value) return
  pending.value = { ...pending.value, ...Object.fromEntries(Object.entries(changes).map(([key, value]) => [key, value === undefined ? null : value])) }
  applyError.value = ''
  saveError.value = ''
  clearTimer()
  changeTimer = setTimeout(() => { changeTimer = undefined; void flushChanges() }, 150)
}
async function flushChanges() {
  if (!settings.value || applying.value || !pendingCount.value || editorErrors.common || applyError.value) return
  const changes = pending.value
  pending.value = {}
  const version = loadVersion
  const profileId = props.profileId
  const source = settings.value.native_config
  applying.value = true
  try {
    const response = await oxidnsApi.applyCommonSettings(settings.value, changes, profileId)
    if (version !== loadVersion || profileId !== props.profileId) return
    if (settings.value?.native_config !== source) throw new Error('配置已变化，请撤销未同步修改后重新编辑。')
    projectionSource = response.data.settings.native_config
    settings.value = response.data.settings
    if (!pendingCount.value) {
      commonValues.value = response.data.values
      commonCapabilities.value = response.data.capabilities
    }
  } catch (error: any) {
    if (version !== loadVersion) return
    const newerChanges = pendingCount.value > 0
    pending.value = { ...changes, ...pending.value }
    applyError.value = newerChanges ? '' : error.response?.data?.message || error.message || '同步草稿失败，请重试。'
  } finally {
    if (version === loadVersion) {
      applying.value = false
      if (pendingCount.value && !applyError.value && !editorErrors.common) void flushChanges()
    }
  }
}
function retryChanges() { applyError.value = ''; void flushChanges() }
async function discardPending() {
  if (applying.value) return
  clearTimer()
  pending.value = {}
  applyError.value = ''
  editorErrors.common = ''
  ++commonKey.value
  await projectCommon()
}
function downloadLegacy() {
  if (typeof settings.value?.legacy_native_config !== 'string') return
  const url = URL.createObjectURL(new Blob([settings.value.legacy_native_config], { type: 'text/yaml;charset=utf-8' }))
  const link = document.createElement('a')
  link.href = url
  link.download = 'oxidns-legacy-draft.yaml'
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
async function save(close = true): Promise<PublishGuard | null> {
  if (!settings.value || loading.value || loadError.value || editorLocked.value || nativePending.value || projecting.value || editorError.value) return null
  saving.value = true
  const version = loadVersion
  const profileId = props.profileId
  saveError.value = ''
  try {
    // Draft storage does not require an online validator or a publishable
    // configuration. Publication performs rule and target-core checks again.
    const source = validationSource.value
    const response = await oxidnsApi.saveSettings({ ...settings.value, config_mode: 'native' }, profileId)
    if (version !== loadVersion) return null
    savedSource.value = source
    if (close) notify.success('OxiDNS 草稿已保存，尚未发布')
    emit('saved')
    if (close) emit('update:open', false)
    return response.data.publish_guard || null
  } catch (error: any) {
    if (version !== loadVersion) return null
    if (Array.isArray(error.response?.data?.issues)) {
      try {
        const result = readOxidnsValidationResult({ valid: false, issues: error.response.data.issues })
        validationIssues.value = result.issues
        validationState.value = 'ready'
        validatedSource.value = validationSource.value
      } catch { validationState.value = 'failed'; validationError.value = '配置检查未完成，请重新检查。' }
    }
    saveError.value = error.response?.data?.message || error.message || '保存 OxiDNS 设置失败。'
    notify.error(saveError.value)
    return null
  } finally { if (version === loadVersion) saving.value = false }
}
watch(validationSource, () => { if (!loading.value && props.open) scheduleValidation() }, { flush: 'sync' })
watch(() => editorErrors.common, value => { if (!value && pendingCount.value) { applyError.value = ''; void flushChanges() } })
watch(() => [nativeView.value, settings.value?.native_config], () => {
  if (!loading.value && props.open && nativeView.value === 'common' && !applying.value && !pendingCount.value
    && settings.value && settings.value.native_config !== projectionSource) void projectCommon()
}, { flush: 'post' })
watch(() => [props.open, props.profileId], () => {
  if (props.open) void load()
  else { ++loadVersion; resetCommon(); invalidateValidation() }
}, { immediate: true })
onBeforeUnmount(() => { ++loadVersion; resetCommon(); invalidateValidation() })
</script>
