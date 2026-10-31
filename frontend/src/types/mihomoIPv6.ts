export interface IPv6ReadinessCheck {
  id: string
  label: string
  status: 'ok' | 'warning' | 'error' | 'unknown'
  message: string
  details?: string[]
  /** Router path checks: allow / drop / translate / unknown. */
  decision?: string
  /** A read-only command from the backend, e.g. the same-port forward accept rule. */
  command?: string
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
  /** 这次结果的检查时间（服务端时区的 ISO 时间） */
  checked_at?: string
  /** 依赖未变，沿用上次检查 */
  cached?: boolean
  /** 结果依赖的设备或配置已变化，但这次未能重新检查 */
  stale?: boolean
  /** 这次重新检查失败的原因；仍显示上次结果 */
  refresh_error?: { code: string; message: string; at: string }
  /**
   * 已保存的配置是否就是这台 Mihomo 上次收到的内容：只比较 Mihomo 实际下发的文件，
   * 路由器发布记录、其他服务的设置不算。checking 表示服务端正在后台比较，每次读取都重新取得。
   */
  publication?: 'synced' | 'changed' | 'checking' | 'unknown'
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
  /** false: the verified configuration (the publication's own, else the saved one) has IPv6 off. */
  applicable?: boolean
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
