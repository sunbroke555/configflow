import type { Agent } from '@/types'
import type { Profile } from '@/stores/profile'

export type AgentSettingsService = 'mihomo' | 'mosdns' | 'oxidns' | 'surge'

export interface AgentSettingsContext {
  agentId: string
  agentName: string
  profileId: string
  profileName: string
  serviceType: AgentSettingsService
  /** 打开前刚读取的设备与配置空间列表，发布栏打开时沿用一次；发布前仍会重新读取 */
  targets?: AgentSettingsTargets
}

/** 设备与配置空间列表（设备取自 GET /agents?summary=1） */
export interface AgentSettingsTargets {
  agents: Agent[]
  profiles: Profile[]
}

export const supportsAgentSettings = (service: string): service is AgentSettingsService =>
  ['mihomo', 'mosdns', 'oxidns', 'surge'].includes(service)
