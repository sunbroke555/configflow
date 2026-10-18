import { onUnmounted, reactive } from 'vue'
import { agentApi } from '@/api'
import { notify } from '@/lib/feedback'
import { deploymentErrorSummary } from '@/lib/agentFailure'

export interface AgentDeployment {
  deployment_id: string
  status: string
  message?: string
  error?: string
  error_details?: string
  failed_stage?: string
  rollback_error?: string
  rollback_error_details?: string
  config_version?: string
  config_revision?: number
  profile_id?: string
  service_type?: string
  created_at?: string
  updated_at?: string
  manual_rollback?: boolean
  rollback_rejected?: boolean
  rollback_message?: string
  query_error?: boolean
  pending?: boolean
  not_recorded?: boolean
  retrying?: boolean
}

export const deploymentLabels: Record<string, string> = {
  preparing: '准备文件', uploading: '上传中', receiving: '接收中',
  verifying: '校验中', ready: '已校验，待激活', stopping: '停止服务',
  backing_up: '备份中', replacing: '替换中', reloading: '热重载', starting: '启动服务',
  checking: '启动检查', rolling_back: '回滚中', succeeded: '上次配置推送：成功',
  recovery_pending: '旧文件已恢复，等待服务检查',
  failed: '发布失败', rolled_back: '发布失败，已回滚',
  rollback_failed: '回滚失败，需要处理', unknown: '结果待确认'
}

const terminal = new Set(['succeeded', 'failed', 'rolled_back', 'rollback_failed'])

export function deploymentStatusLabel(task: AgentDeployment) {
  if (task.rollback_rejected) return '手动回滚未执行'
  if (task.manual_rollback) {
    if (task.status === 'rolled_back') return '手动回滚成功'
    if (task.status === 'rolling_back') return '正在恢复发布前配置'
    if (task.status === 'unknown') return '手动回滚结果待确认'
  }
  return deploymentLabels[task.status] || task.status
}

export function createDeploymentId() {
  // LAN HTTP installations do not expose crypto.randomUUID (secure-context only).
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6] & 15) | 64
  bytes[8] = (bytes[8] & 63) | 128
  const hex = Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

export function useAgentDeployments(onSuccess: () => void, shouldAnnounce: (agentId: string, task: AgentDeployment) => boolean = () => true) {
  const deployments = reactive<Record<string, AgentDeployment>>({})
  const timers = new Map<string, ReturnType<typeof setTimeout>>()
  const inFlight = new Set<string>()
  const submissions = new Map<string, { id: string; observed: boolean }>()
  const announced = new Set<string>()
  let disposed = false

  function busy(agentId: string) {
    const task = deployments[agentId]
    return !!task && !terminal.has(task.status) && task.status !== 'ready'
  }

  function schedule(agentId: string, delay = 2000) {
    clearTimeout(timers.get(agentId))
    if (!disposed) timers.set(agentId, setTimeout(() => {
      timers.delete(agentId)
      void refresh(agentId)
    }, delay))
  }

  function beginSubmission(agentId: string, deploymentId: string) {
    if (disposed || submissions.has(agentId) || busy(agentId)) return false
    submissions.set(agentId, { id: deploymentId, observed: false })
    track(agentId, { deployment_id: deploymentId, status: 'preparing' })
    return true
  }

  function endSubmission(agentId: string, deploymentId: string, result: AgentDeployment | 'unknown' | 'rejected') {
    const submission = submissions.get(agentId)
    if (!submission || submission.id !== deploymentId) return false
    submissions.delete(agentId)
    const current = deployments[agentId]
    if (disposed || current?.deployment_id !== deploymentId) return false
    // Polls can reach activation or completion while the original POST is still
    // pending. Its accepted snapshot (or transport error) is then older evidence.
    if (submission.observed && (typeof result === 'string' || !terminal.has(result.status))) return true
    if (result === 'rejected') {
      clearTimeout(timers.get(agentId))
      timers.delete(agentId)
      delete deployments[agentId]
      return false
    }
    track(agentId, result === 'unknown' ? { deployment_id: deploymentId, status: 'unknown' } : result)
    return true
  }

  function track(agentId: string, task: AgentDeployment, announce = true) {
    if (disposed || !task.deployment_id) return
    announce = announce && shouldAnnounce(agentId, task)
    const previous = deployments[agentId]
    if (task.rollback_rejected && !(previous?.deployment_id === task.deployment_id && previous.manual_rollback && terminal.has(previous.status))) {
      // The server has resolved an uncertain request as rejected; the original release remains active.
      deployments[agentId] = { ...task, manual_rollback: false, query_error: false }
      clearTimeout(timers.get(agentId))
      const key = `${agentId}:${task.deployment_id}:rollback_rejected:${task.rollback_message || ''}`
      if (announce && !announced.has(key)) {
        notify.error('无法手动回滚', task.rollback_message || 'Agent 拒绝了回滚请求，原配置继续运行。')
        onSuccess()
      }
      announced.add(key)
      return
    }
    if (previous?.deployment_id === task.deployment_id && previous.manual_rollback && !task.manual_rollback) {
      // A cached publication result cannot confirm a rollback request whose transport timed out.
      if (!terminal.has(previous.status)) schedule(agentId, 2000)
      return
    }
    if (previous?.deployment_id === task.deployment_id && terminal.has(previous.status) && task.status !== previous.status) {
      // A failed rollback can recover after an Agent restart; completed releases cannot regress.
      const recovering = previous.status === 'rollback_failed' && ['recovery_pending', 'rolling_back', 'rolled_back'].includes(task.status)
      const manualRollback = previous.status === 'succeeded' && task.manual_rollback && ['rolling_back', 'rolled_back', 'rollback_failed', 'recovery_pending'].includes(task.status)
      if (!recovering && !manualRollback) return
    }
    deployments[agentId] = { ...task, query_error: !!task.pending }
    clearTimeout(timers.get(agentId))
    if (task.pending) {
      schedule(agentId, 5000)
      return
    }
    if (terminal.has(task.status)) {
      const key = `${agentId}:${task.deployment_id}:${task.status}`
      if (announce && !announced.has(key)) {
        if (task.status === 'succeeded' || (task.manual_rollback && task.status === 'rolled_back')) {
          notify.success(task.manual_rollback ? '手动回滚成功，发布前配置已恢复并通过服务检查' : '配置发布成功，服务检查已通过')
          onSuccess()
        } else {
          notify.error(deploymentLabels[task.status], deploymentErrorSummary(task))
        }
      }
      announced.add(key)
    } else if (task.status !== 'ready') {
      // An older list response must not replace an in-progress status request.
      schedule(agentId, previous?.deployment_id === task.deployment_id ? 2000 : 0)
    }
  }

  async function refresh(agentId: string) {
    const task = deployments[agentId]
    if (!task || disposed || inFlight.has(agentId)) return
    inFlight.add(agentId)
    try {
      const { data } = await agentApi.getDeployment(agentId, task.deployment_id)
      if (!disposed && deployments[agentId] === task) {
        const submission = submissions.get(agentId)
        if (submission?.id === task.deployment_id) submission.observed = true
        track(agentId, data)
      }
    } catch (error: any) {
      if (!disposed && deployments[agentId] === task) {
        if (error.response?.status === 404 && submissions.get(agentId)?.id === task.deployment_id) {
          // The POST may not have registered its ID yet. It is still in flight,
          // so this is not evidence that the user should submit it again.
          schedule(agentId)
          return
        }
        deployments[agentId] = { ...task, query_error: true, not_recorded: error.response?.status === 404 }
        schedule(agentId, 5000)
      }
    } finally {
      inFlight.delete(agentId)
      const current = deployments[agentId]
      if (!disposed && current && !terminal.has(current.status) && current.status !== 'ready' && !timers.has(agentId)) schedule(agentId)
    }
  }

  async function retryPublish(agentId: string) {
    const task = deployments[agentId]
    if (!task?.not_recorded || task.manual_rollback || task.retrying || disposed || terminal.has(task.status)) return
    clearTimeout(timers.get(agentId))
    deployments[agentId] = { ...task, retrying: true, not_recorded: false, query_error: false }
    try {
      // Reuse the original identity: a delayed first request and this retry are one publication.
      const { data } = await agentApi.pushConfig(agentId, task.deployment_id)
      if (!disposed && deployments[agentId]?.deployment_id === task.deployment_id) track(agentId, data)
    } catch (error: any) {
      const current = deployments[agentId]
      if (disposed || current?.deployment_id !== task.deployment_id || terminal.has(current.status)) return
      if (error.response?.data?.deployment_id) track(agentId, error.response.data)
      else {
        deployments[agentId] = { ...current, retrying: false, status: 'unknown', query_error: true }
        schedule(agentId, 5000)
        if (error.response) notify.error('重新提交未完成', error.response.data?.message)
      }
    } finally {
      const current = deployments[agentId]
      if (current?.deployment_id === task.deployment_id && current.retrying) current.retrying = false
    }
  }

  async function activate(agentId: string) {
    const task = deployments[agentId]
    if (!task || task.status !== 'ready') return
    deployments[agentId] = { ...task, status: 'verifying' }
    try {
      const { data } = await agentApi.activateDeployment(agentId, task.deployment_id)
      track(agentId, data)
    } catch (error: any) {
      if (error.response?.data?.deployment_id) track(agentId, error.response.data)
      else {
        deployments[agentId] = { ...task, status: 'unknown', query_error: true, message: error.response?.data?.message, error_details: error.response?.data?.error_details }
        schedule(agentId)
      }
    }
  }

  async function rollback(agentId: string, deploymentId: string) {
    const task = deployments[agentId]
    if (!task || task.deployment_id !== deploymentId || task.status !== 'succeeded' || disposed) return
    clearTimeout(timers.get(agentId))
    const rejectedKeyPrefix = `${agentId}:${deploymentId}:rollback_rejected:`
    for (const key of announced) if (key.startsWith(rejectedKeyPrefix)) announced.delete(key)
    deployments[agentId] = { ...task, manual_rollback: true, rollback_rejected: false, rollback_message: undefined, rollback_error_details: undefined, status: 'rolling_back', query_error: false }
    try {
      const { data } = await agentApi.rollbackDeployment(agentId, deploymentId)
      if (!disposed) track(agentId, { ...data, manual_rollback: true })
    } catch (error: any) {
      if (disposed) return
      if (error.response?.data?.deployment_id && error.response?.data?.manual_rollback) {
        track(agentId, error.response.data)
      } else if (error.response && error.response.status >= 400 && error.response.status < 500) {
        const reason = error.response.data?.message || '当前部署没有可用备份，或另一个操作正在进行。'
        deployments[agentId] = { ...task, rollback_rejected: true, rollback_message: reason, rollback_error_details: error.response.data?.rollback_error_details || error.response.data?.error_details }
        notify.error('无法手动回滚', deploymentErrorSummary({ error: reason }))
      } else {
        deployments[agentId] = { ...task, manual_rollback: true, rollback_rejected: false, rollback_message: undefined, rollback_error_details: error.response?.data?.rollback_error_details || error.response?.data?.error_details, status: 'unknown', query_error: true }
        schedule(agentId, 0)
      }
    }
  }

  onUnmounted(() => {
    disposed = true
    timers.forEach(clearTimeout)
    timers.clear()
    submissions.clear()
  })

  return { deployments, busy, track, refresh, beginSubmission, endSubmission, activate, retryPublish, rollback }
}
