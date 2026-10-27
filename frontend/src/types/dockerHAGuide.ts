export type DockerHANetwork = 'host' | 'macvlan'
export interface DockerHAMount { type: 'bind' | 'volume'; source: string }
export interface DockerHAHostForm {
  mode: 'new' | 'migrate'
  agent_name: string
  agent_ip: string
  oxidns_port: number
  data_dir: string
  container_name: string
  existing_agent_name: string
  agent_mount: DockerHAMount
  core_mount: DockerHAMount
  ha_data_dir: string
  backup_dir: string
  ha_host_id_path: string
  create_host_identity: boolean
  ha_parent: string
  ha_subnet: string
  ha_gateway: string
}
export interface DockerHAGuideRequest extends Partial<DockerHAHostForm> {
  action?: 'inspect' | 'generate' | 'verify' | 'check'
  output?: string
  vip?: string
  server_url?: string
  network_mode?: DockerHANetwork
  image?: string
  enable_dns_ha?: boolean
  enable_oxidns?: boolean
  enable_mihomo?: boolean
}
export interface DockerHAGuideResult {
  success: boolean
  message?: string
  inspection_command: string
  identity_command: string
  compose: string
  install_command: string
  rollback_command: string
  preflight_report?: DockerHAPreflightReport
  verification_command?: string
  command_blocks?: { id: string; title: string; command: string }[]
  warnings?: string[]
}
export interface DockerHAEnvironment {
  success: boolean
  message?: string
  agent_id: string
  service_type: string
  oxidns_ha_protocols: number[]
  oxidns_ha_container_protocols: number[]
  oxidns_ha_handoff_protocols: number[]
  oxidns_ha_environment: {
    kind: string
    ready: boolean
    network?: string
    host_id?: string
    blockers?: string[]
  }
}

export type DockerHAPreflightStatus = 'pass' | 'warning' | 'fail' | 'unknown'
export interface DockerHAPreflightCheck {
  id: string
  label: string
  status: DockerHAPreflightStatus
  detail: string
  remedy?: string
}
export interface DockerHAPreflightReport {
  status: DockerHAPreflightStatus
  summary: string
  checks: DockerHAPreflightCheck[]
  host_fingerprint?: string
}
