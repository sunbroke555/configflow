export interface IPv6ReadinessCheck {
  id: string
  label: string
  status: 'ok' | 'warning' | 'error' | 'unknown'
  message: string
  details?: string[]
}

export interface IPv6Connectivity {
  status: 'not_checked' | 'ok' | 'error'
  message: string
  details?: string[]
}

export interface MihomoIPv6Readiness {
  success: boolean
  agent_id: string
  profile_id: string
  config_revision?: number
  configuration: {
    ipv6: boolean | null
    dns_ipv6: boolean | null
    fake_ipv6_range: string | null
    errors: string[]
  }
  host: Record<string, unknown>
  routers: Array<{ id: string; name: string; status: string; checks: IPv6ReadinessCheck[]; can_prepare: boolean }>
  checks: IPv6ReadinessCheck[]
  can_prepare: boolean
  prepare_interface: string
  check_token: string
  connectivity: IPv6Connectivity
  message?: string
}

export interface IPv6VerificationInput {
  profile_id: string
  expected_config_revision?: number
  deployment_id?: string
}

export interface IPv6Verification {
  status: 'running' | 'ok' | 'warning' | 'error'
  checks: IPv6ReadinessCheck[]
  checked_at?: string
  config_revision: number
  deployment_id?: string
  scope: 'mihomo_runtime' | 'client_path'
  message?: string
}

export interface IPv6VerificationResult {
  success: boolean
  agent_id: string
  profile_id: string
  verification: IPv6Verification | null
  message?: string
}

export interface IPv6VerificationSettings {
  client_agent_id: string
  test_url: string
}

export interface IPv6VerificationSettingsResult {
  success: boolean
  agent_id: string
  profile_id: string
  settings: IPv6VerificationSettings
  candidates: Array<{ id: string; name: string; host: string; status?: string; enabled?: boolean; platform?: string; supported?: boolean }>
  message?: string
}

export interface MihomoPublishedTarget {
  agent_id: string
  profile_id: string
  deployment_id: string
  config_revision: number
}

export interface IPv6PrepareInput {
  profile_id: string
  interface: string
  check_token: string
}

export interface IPv6ProbeResult {
  success: boolean
  agent_id: string
  profile_id: string
  connectivity: IPv6Connectivity
  results?: IPv6Connectivity[]
  message?: string
}
