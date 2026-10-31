import type { AgentContentSync } from '@/types'

interface DeploymentRecord {
  status: string
  manual_rollback?: boolean
  profile_id?: string
  config_revision?: number
  config_version?: string
  updated_at?: string
}

interface AgentConfigRecord {
  profile_id?: string
  config_revision?: number
  config_version?: string
  content_sync?: AgentContentSync
  latest_deployment?: DeploymentRecord
  deployments?: Record<string, DeploymentRecord>
}

interface DeploymentProgress {
  status: string
  manual_rollback?: boolean
  pending?: boolean
  query_error?: boolean
}

const validRevision = (value: unknown): value is number =>
  typeof value === 'number' && Number.isSafeInteger(value) && value >= 0

/** The server compares complete deployment contents; revisions are diagnostic only. */
export function getAgentConfigSync(agent: AgentConfigRecord, currentRevision: number | undefined, progress?: DeploymentProgress) {
  const revision = validRevision(agent.config_revision) ? agent.config_revision : undefined
  const current = validRevision(currentRevision) ? currentRevision : undefined
  const hash = agent.config_version && agent.config_version !== '0' ? agent.config_version : undefined
  const title = [
    '同步状态按当前应下发的完整文件与上次成功下发内容比较',
    `已推送修订号：${revision === undefined ? '未记录' : `v${revision}`}`,
    `当前配置修订号：${current === undefined ? '暂未获取' : `v${current}`}`,
    `配置哈希：${hash || '未记录'}`
  ].join('\n')
  // diffable：可点开「待推送的变更」窗口。服务端给出比较结论（含没有基准的原因）时成立；
  // 推送进行中或旧服务端没有比较结果时不成立。手机没有悬停提示，原因要靠窗口说明。
  const state = (label: string, tone: 'success' | 'warning' | 'muted' = 'muted', detail?: string, diffable = false) =>
    ({ label, tone, title: detail ? `${title}\n${detail}` : title, diffable })
  const unknown = (diffable = false) => state('同步状态待确认', 'muted', agent.content_sync?.reason === 'baseline_missing'
    ? '尚无完整下发内容记录，成功推送一次后可准确比较'
    : '暂时无法确认完整下发内容是否一致', diffable)
  const task = progress || agent.latest_deployment
  if (progress?.pending || progress?.query_error || task?.status === 'unknown' || task?.status === 'rollback_failed') return unknown()
  if (task?.status === 'ready') return state('已校验，待应用', 'warning')
  if (task && !['succeeded', 'failed', 'rolled_back'].includes(task.status)) return state(task.manual_rollback ? '正在回滚' : '正在推送')

  switch (agent.content_sync?.status) {
    case 'synced': return state('已同步', 'success', '点击查看对比结果', true)
    case 'changed': return state('有变更，待推送', 'warning', '点击查看这次推送会改动的内容', true)
    case 'never-pushed': return state('尚未推送', 'muted', undefined, true)
    case 'checking': return state('正在核对配置')
    case 'unknown': return unknown(true)
  }

  // Older servers cannot establish equality from a revision or a main-config
  // hash: neither covers every delivered file. Only an empty history is clear.
  const successful = [...Object.values(agent.deployments || {}), ...(agent.latest_deployment ? [agent.latest_deployment] : [])]
    .some(record => record.status === 'succeeded')
  if (!agent.content_sync && !hash && revision === undefined && !successful) return state('尚未推送')
  return unknown()
}
