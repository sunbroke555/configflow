export interface Subscription {
  id: string
  name: string
  url: string
  type: string
  enabled: boolean
  interval?: number
  /** 健康检查地址，留空用默认（境外）；回家/内网订阅建议填国内地址 */
  health_check?: Record<string, any>
  health_check_url?: string
  /** 节点名筛选正则，只保留匹配的节点；留空表示不筛选 */
  regex_filter?: string
  /** 筛选后的节点数 */
  cached_node_count?: number | null
  /** 缓存中的原始节点数（筛选前） */
  cached_total_count?: number | null
  cached_updated_at?: string | null
}

export interface ProxyNode {
  id: string
  name: string
  proxy_string: string  // 节点字符串，如 ss://..., vmess://..., trojan://... 等
  enabled: boolean
  remark?: string  // 备注
  subscription_id?: string
  subscription_name?: string
  params?: Record<string, unknown>
}

export interface Rule {
  id: string
  rule_type: string
  value: string
  policy: string
  enabled: boolean
  order?: number  // 排序顺序
  remark?: string  // 备注
  no_resolve?: boolean  // no-resolve 开关
  group_name?: string  // 分组名称
}

export interface RuleSet {
  id: string
  name: string
  url: string
  behavior: string
  enabled: boolean
  policy?: string
  order?: number  // 排序顺序
  library_rule_id?: string  // 关联的规则仓库ID
  remark?: string  // 备注
  no_resolve?: boolean  // no-resolve 开关
  group_name?: string  // 分组名称
}

export interface ProxyGroup {
  id: string
  name: string
  type: string
  enabled: boolean
  chain?: { entry: { type: 'node' | 'group'; id: string }; exit: { type: 'node' | 'group'; id: string } }
  aggregations?: string[]
  aggregation_regex?: string
  aggregation_exclude_regex?: string
  follow_group?: string
  proxies_order?: { type: string; id: string }[]
  proxy_order?: string
  use?: string[]
  include_all?: boolean
  'include-all'?: boolean
  url?: string
  interval?: number
  subscriptions?: string[]    // 订阅来源
  regex?: string              // 正则过滤规则
  exclude_regex?: string      // 排除匹配的订阅节点
  manual_nodes?: string[]     // 手动选择的节点
  include_groups?: string[]   // 引用的策略组
  strategy?: 'round-robin' | 'consistent-hashing' | 'sticky-sessions'  // 负载均衡策略
  timeout?: number
  tolerance?: number
  'expected-status'?: string
  lazy?: boolean              // 懒加载
  // 以下为兼容旧数据
  proxies?: string[]          // 已废弃，兼容旧数据
  source?: 'subscription' | 'node' | 'strategy'  // 已废弃，兼容旧数据
}

export interface AgentContentSync {
  status: 'synced' | 'changed' | 'unknown' | 'never-pushed' | 'checking'
  /** Stable server reason code; never display raw errors as UI copy. */
  reason?: string
  checked_at?: string
}

export interface AgentSystemMetrics {
  cpu?: { usage_percent: number; core_count: number }
  memory?: { total: number; used: number; available: number; used_percent: number }
  disk?: { total: number; used: number; free: number; used_percent: number }
  network?: { bytes_sent: number; bytes_recv: number; speed_sent: number; speed_recv: number }
  collected_at: string
}

export interface Agent {
  id: string
  name: string
  host: string
  port: number
  token: string
  service_type: 'mihomo' | 'surge' | 'mosdns' | 'oxidns'
  deployment_method?: 'shell' | 'docker' | 'unknown'
  status: 'online' | 'offline'
  last_heartbeat: string
  version: string
  config_version: string
  config_revision?: number
  content_sync?: AgentContentSync
  system_metrics?: AgentSystemMetrics
  latest_deployment?: import('@/composables/useAgentDeployments').AgentDeployment
  deployments?: Record<string, import('@/composables/useAgentDeployments').AgentDeployment>
  enabled: boolean
  profile_id?: string
  has_update?: boolean
  upgrade_available?: boolean
  latest_upgrade?: import('@/composables/useAgentUpgrades').AgentUpgrade
  dns_follow?: import('./agentDNSFollow').AgentDNSFollowBinding | null
  dns_follow_state?: import('./agentDNSFollow').AgentDNSFollowState | null
  created_at?: string
  updated_at?: string
  cpu?: number
  memory?: number
}
