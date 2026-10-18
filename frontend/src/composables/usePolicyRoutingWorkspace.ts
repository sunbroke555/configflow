import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { agentApi, routerApi, type PolicyWorkspace, type PolicyWorkspacePreview } from '@/api'
import { confirm, notify } from '@/lib/feedback'
import { beginScopedRequest, endScopedRequest } from '@/profileContext'
import { createDeploymentId, deploymentStatusLabel, type AgentDeployment } from './useAgentDeployments'
import type { OxiDNSDevicePolicy } from '@/types/oxidnsMonitor'

export interface PolicyReleaseRow { id: string; name: string; status: string; message?: string; deployment_id?: string; task?: Partial<AgentDeployment> }
const terminal = new Set(['succeeded', 'failed', 'rolled_back', 'rollback_failed'])
const message = (error: any) => error?.response?.data?.message || error?.message || '操作失败，请重试。'
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value))
const routerDraft = (state: PolicyWorkspace) => ({
  ip_rule_sets: [...state.router.ip_rule_sets], ros_policy_rule_sets: [...state.router.ros_policy_rule_sets],
  proxy_gateway_agent_id: state.router.proxy_gateway_agent_id || ''
})

export function usePolicyRoutingWorkspace(props: { id: string; profile: string }, saved: () => void) {
  const state = ref<PolicyWorkspace | null>(null)
  const policies = ref<OxiDNSDevicePolicy[]>([])
  const settings = ref({ ip_rule_sets: [] as string[], ros_policy_rule_sets: [] as string[], proxy_gateway_agent_id: '' })
  const targets = ref<string[]>([])
  const tab = ref('sources')
  const loading = ref(false), saving = ref(false), publishing = ref(false)
  const error = ref(''), preview = ref<PolicyWorkspacePreview | null>(null)
  const changes = ref<string[]>([]), results = ref<PolicyReleaseRow[]>([]), rosResult = ref<any>(null)
  const unresolved = computed(() => results.value.some(row => row.deployment_id && !terminal.has(row.status)))
  const busy = computed(() => loading.value || saving.value || publishing.value)
  const locked = computed(() => busy.value || unresolved.value)
  const dirty = computed(() => !!state.value && (JSON.stringify(policies.value) !== JSON.stringify(state.value.sources.policies)
    || JSON.stringify(settings.value) !== JSON.stringify(routerDraft(state.value))))
  let epoch = 0, disposed = false
  let timer: ReturnType<typeof setTimeout> | undefined
  let wake: (() => void) | undefined
  const context = () => ({ id: props.id, profile: props.profile, epoch })
  const current = (c: ReturnType<typeof context>) => !disposed && c.id === props.id && c.profile === props.profile && c.epoch === epoch
  const pause = () => new Promise<void>(resolve => { wake = resolve; timer = setTimeout(() => { timer = undefined; wake = undefined; resolve() }, 2000) })
  const invalidate = () => { preview.value = null }
  watch([policies, settings, targets], invalidate, { deep: true })

  async function canLeave() {
    if (busy.value) { notify.info('正在保存或发布，请稍候。'); return false }
    if (unresolved.value) return await confirm('部分设备的发布结果尚未确认。离开后可在 Agent 管理查看，ROS 后续步骤不会自动执行。', { title: '离开发布面板', confirmText: '离开' })
    return !dirty.value || await confirm('策略路由设置尚未保存，离开会丢失本次修改。', { title: '离开策略路由设置', confirmText: '放弃修改' })
  }
  async function load() {
    const c = context()
    loading.value = true; error.value = ''; preview.value = null
    try {
      const { data } = await routerApi.policySettings(c.id, c.profile)
      if (!current(c)) return
      if (!data.success) throw new Error(data.message)
      state.value = data.workspace
      policies.value = clone(data.workspace.sources.policies)
      settings.value = routerDraft(data.workspace)
      targets.value = data.workspace.source_agents.map(agent => agent.id)
      changes.value = []; results.value = []; rosResult.value = null
    } catch (failure) { if (current(c)) error.value = message(failure) }
    finally { if (current(c)) loading.value = false }
  }
  async function reload() { if (await canLeave()) await load() }
  function selectRules(kind: 'ip_rule_sets' | 'ros_policy_rule_sets', values: string[]) {
    if (locked.value) return
    settings.value[kind] = values
    const other = kind === 'ip_rule_sets' ? 'ros_policy_rule_sets' : 'ip_rule_sets'
    settings.value[other] = settings.value[other].filter(id => !values.includes(id))
  }
  function sourceChanges() {
    if (!state.value) return []
    const before = new Map(state.value.sources.policies.map(row => [row.ip, row]))
    const after = new Map(policies.value.map(row => [row.ip, row]))
    const label = (row: OxiDNSDevicePolicy) => `${row.ip}：${row.mode === 'smart' ? '智能分流' : '直连'}${row.name ? `（${row.name}）` : ''}`
    return [...policies.value.filter(row => JSON.stringify(before.get(row.ip)) !== JSON.stringify(row)).map(row =>
      before.has(row.ip) ? `修改来源 ${label(before.get(row.ip)!)} → ${label(row)}` : `新增来源 ${label(row)}`),
    ...state.value.sources.policies.filter(row => !after.has(row.ip)).map(row => `移除来源 ${label(row)}`)]
  }
  async function saveAndPreview() {
    if (!state.value || locked.value) return
    const c = context(); saving.value = true; error.value = ''; preview.value = null; results.value = []; rosResult.value = null
    try {
      if (dirty.value) {
        const diff = sourceChanges()
        const { data } = await routerApi.savePolicySettings(c.id, {
          revision: state.value.revision, policies: clone(policies.value), router: clone(settings.value)
        }, c.profile)
        if (!current(c)) return
        if (!data.success) throw new Error(data.message)
        state.value = data.workspace; policies.value = clone(data.workspace.sources.policies); settings.value = routerDraft(data.workspace)
        changes.value = [...changes.value, ...diff]
        saved()
      }
      const { data } = await routerApi.previewPolicySettings(c.id, state.value!.revision, [...targets.value], c.profile)
      if (!current(c)) return
      if (!data.success) throw new Error(data.message)
      preview.value = data.preview; tab.value = 'preview'
    } catch (failure) { if (current(c)) error.value = message(failure) }
    finally { if (current(c)) saving.value = false }
  }
  const blockers = computed(() => [
    ...(preview.value?.router_plan.blockers || []),
    ...(preview.value?.targets.filter(agent => agent.status !== 'online').map(agent => `${agent.name} 离线，请恢复连接或调整发布目标。`) || [])
  ])
  const canPublish = computed(() => !!preview.value && !dirty.value && !locked.value && !blockers.value.length
    && !results.value.length && (!preview.value.router_plan.empty || !!preview.value.targets.length))

  function updateTask(row: PolicyReleaseRow, task: AgentDeployment) {
    row.task = task
    row.status = task.pending ? 'unknown' : task.status
    row.message = task.error || task.message || deploymentStatusLabel(task)
  }
  async function waitForAgent(row: PolicyReleaseRow, c: ReturnType<typeof context>) {
    // Uncertain requests are polled by their original id, never resubmitted.
    let activationSent = false
    for (let i = 0; i < 120 && current(c) && !terminal.has(row.status); i++) {
      if (row.status === 'ready' && !activationSent) {
        activationSent = true
        try {
          const { data } = await agentApi.activateDeployment(row.id, row.deployment_id!)
          if (!current(c)) return false
          updateTask(row, data)
        } catch (failure: any) {
          if (!current(c)) return false
          if (failure.response?.data?.deployment_id) updateTask(row, failure.response.data)
          else { row.status = 'unknown'; row.message = message(failure) }
        }
        if (terminal.has(row.status)) break
      }
      await pause()
      if (!current(c)) return false
      try { const { data } = await agentApi.getDeployment(row.id, row.deployment_id!); if (current(c)) updateTask(row, data) }
      catch (failure) { row.status = 'unknown'; row.message = message(failure) }
      if (row.status === 'ready' && activationSent) break
    }
    return current(c) && row.status === 'succeeded'
  }
  async function publish() {
    if (!canPublish.value || !preview.value) return
    const c = context(), snapshot = clone(preview.value)
    publishing.value = true; error.value = ''; beginScopedRequest()
    try {
      // Recheck both drafts and bindings before the first external write.
      const fresh = await routerApi.policySettings(c.id, c.profile)
      if (!current(c)) return
      if (fresh.data.workspace.revision !== snapshot.revision || snapshot.targets.some(target =>
        !fresh.data.workspace.source_agents.some(agent => agent.id === target.id && agent.host === target.host))) {
        throw new Error('配置或发布目标已变化，请重新载入并预览。')
      }
      for (const target of snapshot.targets) {
        if (!current(c)) return
        const row: PolicyReleaseRow = { id: target.id, name: target.name, status: 'preparing', deployment_id: createDeploymentId() }
        results.value.push(row)
        // Use the reactive row so progress is visible while awaiting a device.
        const live = results.value[results.value.length - 1]!
        try {
          const { data } = await agentApi.pushConfig(target.id, row.deployment_id, {
            expected_profile_id: snapshot.profile_id, expected_oxidns_revision: snapshot.oxidns_revision,
            expected_config_revision: snapshot.config_revision
          })
          if (!current(c)) return
          updateTask(live, data)
        } catch (failure: any) {
          if (!current(c)) return
          if (failure.response?.data?.deployment_id) updateTask(live, failure.response.data)
          else { live.status = failure.response ? 'failed' : 'unknown'; live.message = message(failure); live.task = failure.response?.data }
        }
        if (!await waitForAgent(live, c)) throw new Error(`${target.name} 未确认发布成功，后续设备及 ROS 已暂停。请核对下方结果。`)
      }
      if (!current(c)) return
      const row = { id: c.id, name: 'ROS', status: 'preparing', message: '' }
      results.value.push(row)
      const live = results.value[results.value.length - 1]!
      // Even an empty preview must be checked again after OxiDNS publishing:
      // device rules may have changed while the previous targets were running.
      try {
        const { data } = await routerApi.apply(c.id, snapshot.router_plan.fingerprint, c.profile)
        if (!current(c)) return
        rosResult.value = data.result
        live.status = data.success && data.result?.ok ? 'succeeded' : 'failed'
        live.message = data.message || (live.status === 'succeeded'
          ? snapshot.router_plan.empty ? '回读确认已同步，无需写入' : '写入及回读校验通过'
          : '请查看发布和回滚记录')
      } catch (failure: any) {
        if (!current(c)) return
        rosResult.value = failure.response?.data?.result || null
        live.status = failure.response ? 'failed' : 'unknown'; live.message = message(failure)
      }
      if (live.status === 'succeeded') {
        notify.success('所选目标发布完成'); changes.value = []; saved()
        // ROS stores ownership after publishing. Refresh that revision without
        // discarding the visible release results or any concurrent editor change.
        try {
          const { data } = await routerApi.policySettings(c.id, c.profile)
          if (current(c) && data.success && JSON.stringify(data.workspace.sources.policies) === JSON.stringify(policies.value)
            && JSON.stringify(routerDraft(data.workspace)) === JSON.stringify(settings.value)) state.value = data.workspace
        } catch { /* A later save still rejects a stale revision safely. */ }
      }
      else throw new Error('ROS 发布未完成，已完成的 OxiDNS 发布结果保留。请核对发布与回滚记录。')
    } catch (failure) { if (current(c)) error.value = message(failure) }
    finally { endScopedRequest(); if (current(c)) publishing.value = false }
  }
  async function refreshResults() {
    if (publishing.value) return
    const c = context(); publishing.value = true
    try {
      for (const row of results.value.filter(row => row.deployment_id && !terminal.has(row.status))) {
        const { data } = await agentApi.getDeployment(row.id, row.deployment_id!)
        if (!current(c)) return
        updateTask(row, data)
      }
    } catch (failure) { if (current(c)) error.value = message(failure) }
    finally { if (current(c)) publishing.value = false }
  }
  function beforeUnload(event: BeforeUnloadEvent) {
    if (dirty.value || busy.value) { event.preventDefault(); event.returnValue = '' }
  }
  window.addEventListener('beforeunload', beforeUnload)
  watch(() => [props.id, props.profile], () => {
    epoch++
    if (timer) clearTimeout(timer)
    wake?.(); timer = undefined; wake = undefined
    state.value = null; tab.value = 'sources'; saving.value = false; publishing.value = false
    results.value = []; rosResult.value = null
    void load()
  }, { immediate: true })
  onBeforeUnmount(() => { disposed = true; epoch++; if (timer) clearTimeout(timer); wake?.(); window.removeEventListener('beforeunload', beforeUnload) })
  return { state, policies, settings, targets, tab, loading, saving, publishing, locked, dirty, error, preview, changes,
    results, rosResult, unresolved, blockers, canPublish, load, reload, canLeave, selectRules, saveAndPreview, publish, refreshResults }
}
