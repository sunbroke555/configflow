/** OxiDNS 高可用（keepalived 虚拟 IP）：`/api/agents/<id>/oxidns-ha` 的词汇表 */

/** Agent 记录上的 `oxidns_ha`；关闭后后端删除整个字段，前端只认 `enabled === true` */
export interface AgentDNSHAConfig {
  enabled: boolean
  group_id: string
  peer_id: string
  vip: string
  vrid: number
  role: 'primary' | 'secondary'
}

export interface AgentDNSHAState {
  status: 'applying' | 'succeeded' | 'failed'
  error?: string
  /** epoch 秒 */
  updated_at?: number
  /** 首次开启失败且回滚未完成时才有（此时没有 oxidns_ha）：停用会清理这些 Agent 上的残留配置 */
  members?: string[]
}

/** 开启请求；省略 vrid 时由后端按虚拟 IP 末位派生 */
export interface AgentDNSHAInput {
  peer_id: string
  vip: string
  vrid?: number
}

export interface AgentDNSHARuntime {
  keepalived_running: boolean
  vip_present: boolean
  role: 'master' | 'backup' | 'unknown'
  healthy: boolean
}

export interface AgentDNSHAMember {
  agent_id: string
  name: string
  host: string
  online: boolean
  role_config: 'primary' | 'secondary'
  runtime: AgentDNSHARuntime | null
  error?: string
}

export type AgentDNSHAHealth = 'ok' | 'degraded' | 'split' | 'down'

export interface AgentDNSHAStatus {
  success?: boolean
  message?: string
  config: AgentDNSHAConfig | null
  state?: AgentDNSHAState | null
  members?: AgentDNSHAMember[]
  health?: AgentDNSHAHealth
  /** 仅开启接口返回：需要用户自行确认的事项（如无法确认两台转发到同一个 mihomo） */
  warnings?: string[]
  /** 仅开启接口返回：DHCP DNS 指向本组、需要重新发布的路由器 */
  routers?: ({ id?: string; name?: string } | string)[]
}

export interface AgentDNSHARemoveResult {
  success?: boolean
  message?: string
  /** 仍把 DHCP DNS 指向本组的路由器，停用后需重新发布 */
  routers?: ({ id?: string; name?: string } | string)[]
}

export const isDNSHAEnabled = (value?: { enabled?: unknown } | null) => value?.enabled === true
