import api from '@/api'

export type PushDiffChange = 'added' | 'removed' | 'modified'
export type PushDiffRole = 'config' | 'provider' | 'rule' | 'custom'

/** 文件摘要：订阅按节点、规则集按规则条数、主配置按配置项，其余按大小 */
export interface PushDiffSummary {
  kind: 'nodes' | 'rules' | 'size' | 'config'
  before?: number | null
  after?: number | null
  added?: number
  removed?: number
  modified?: number
  changes?: number
}

export interface PushDiffFile {
  path: string
  role: PushDiffRole | null
  /** 订阅名、规则集名；主配置没有 */
  name?: string
  status: PushDiffChange | 'unchanged'
  /** 缺失表示上次推送的内容没有保存，无法细分 */
  summary?: PushDiffSummary
}

/** 列表类配置项只给条数和增减；条目已在服务端截断并打码 */
export interface PushDiffList {
  before: number
  after: number
  added: number
  removed: number
  modified: number
  reordered: boolean
  entries?: PushDiffListEntry[]
  omitted?: number
  /** 按 name / tag 对应的列表（节点、策略组、插件） */
  identity?: string
  /** 条目内部的列表字段，只带少量增减条目 */
  added_items?: string[]
  removed_items?: string[]
}

export interface PushDiffListEntry {
  change: PushDiffChange
  text: string
  fields?: PushDiffItem[]
  fields_omitted?: number
}

/** 主配置的一个配置项；masked 时服务端不返回原值和新值 */
export interface PushDiffItem {
  key: string
  change: PushDiffChange
  masked: boolean
  old?: string | null
  new?: string | null
  list?: PushDiffList
}

export interface PushDiffRaw {
  text: string | null
  truncated?: boolean
  omitted_lines?: number
  reason?: 'too_large'
  lines?: number
}

export interface PushDiffConfig {
  format: 'yaml'
  /** compared：有配置项变化；formatting：只有顺序或格式不同；unparsable / unavailable：只能按文件比较 */
  status: 'compared' | 'formatting' | 'unparsable' | 'unavailable'
  reason?: string
  items: PushDiffItem[]
  omitted: number
  total?: number
  raw?: PushDiffRaw
}

export interface PushDiffResponse {
  /** checking 时继续轮询；never-pushed / unknown 时看 reason */
  status: 'checking' | 'synced' | 'changed' | 'never-pushed' | 'unknown'
  reason?: string
  checked_at?: string
  service_type?: string
  baseline?: { pushed_at?: string | null; config_revision?: number | null }
  /** full：有上次推送的发布包；digests：只能按文件摘要比较 */
  content?: 'full' | 'digests'
  content_reason?: string | null
  counts?: Record<PushDiffChange | 'unchanged', number>
  files?: PushDiffFile[] | null
  files_omitted?: number
  unchanged?: PushDiffFile[]
  unchanged_omitted?: number
  config?: PushDiffConfig | null
}

export const agentPushDiffApi = {
  get: (agentId: string, signal?: AbortSignal) =>
    api.get<PushDiffResponse>(`/agents/${encodeURIComponent(agentId)}/push-diff`, { signal }),
}
