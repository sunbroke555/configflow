export type AgentSettingsService = 'mihomo' | 'mosdns' | 'oxidns' | 'surge'

export interface AgentSettingsContext {
  agentId: string
  agentName: string
  profileId: string
  profileName: string
  serviceType: AgentSettingsService
}

export const supportsAgentSettings = (service: string): service is AgentSettingsService =>
  ['mihomo', 'mosdns', 'oxidns', 'surge'].includes(service)
