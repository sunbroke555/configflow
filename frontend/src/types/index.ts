export interface Subscription {
  id: string
  name: string
  url: string
  type: string
  enabled: boolean
  interval?: number
  /** 健康检查地址，留空用默认（境外）；回家/内网订阅建议填国内地址 */
  health_check_url?: string
  cached_node_count?: number | null
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
  url?: string
  interval?: number
  subscriptions?: string[]    // 订阅来源
  regex?: string              // 正则过滤规则
  manual_nodes?: string[]     // 手动选择的节点
  include_groups?: string[]   // 引用的策略组
  strategy?: 'round-robin' | 'consistent-hashing' | 'sticky-sessions'  // 负载均衡策略
  lazy?: boolean              // 懒加载
  // 以下为兼容旧数据
  proxies?: string[]          // 已废弃，兼容旧数据
  source?: 'subscription' | 'node' | 'strategy'  // 已废弃，兼容旧数据
}

export interface Agent {
  id: string
  name: string
  host: string
  port: number
  token: string
  service_type: 'mihomo' | 'mosdns'
  deployment_method?: 'shell' | 'docker' | 'unknown'
  status: 'online' | 'offline'
  last_heartbeat: string
  version: string
  config_version: string
  enabled: boolean
  profile_id?: string
  has_update?: boolean
  created_at?: string
  updated_at?: string
  cpu?: number
  memory?: number
}
