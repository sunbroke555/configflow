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
