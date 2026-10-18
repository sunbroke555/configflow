import { normalizeClientRange } from '@/lib/oxidnsClientRange'

export function deviceAddress(value: string): string {
  try { return normalizeClientRange(value) } catch { return '' }
}
export function deviceNote(notes: Record<string, string>, ip: string): string {
  if (notes[ip]) return notes[ip]
  // Normal IPv4 connections dominate the table; only canonicalize aliases.
  return ip.includes(':') || ip.includes('/') ? notes[deviceAddress(ip)] || '' : ''
}
/** Only actual domain metadata is eligible; do not guess a domain from an IP. */
export function connectionDomains(metadata: { host?: string; sniffHost?: string }): string[] {
  return [...new Set([metadata.host, metadata.sniffHost].flatMap(raw => {
    if (!raw || /[\s/@:#?%\\]/.test(raw)) return []
    try {
      const host = new URL(`https://${raw.toLowerCase().replace(/\.$/, '')}`).hostname
      if (deviceAddress(host) || !host.includes('.') || host.length > 253 ||
        host.split('.').some(label => !/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/i.test(label))) return []
      return [host]
    } catch { return [] }
  }))]
}
/** Keep domain actions unchanged; IP-only connections use the recorded destination, never the source. */
export function connectionRuleValues(metadata: { host?: string; sniffHost?: string; destinationIP?: string; dnsMode?: string }): string[] {
  const domains = connectionDomains(metadata)
  if (domains.length) return domains
  // A synthetic address is not a stable destination rule.
  if (metadata.dnsMode === 'fake-ip') return []
  for (const value of [metadata.destinationIP, metadata.host, metadata.sniffHost]) {
    if (!value || /[/%\s]/.test(value)) continue
    const address = deviceAddress(value)
    if (address) return [address]
  }
  return []
}
export type ConnectionRuleType = 'DOMAIN' | 'DOMAIN-SUFFIX' | 'IP-CIDR' | 'IP-CIDR6'
export interface DeviceNotes {
  profile_id: string
  profile_name: string
  notes: Record<string, string>
}
export interface ConnectionRuleTarget {
  id: string
  name: string
  problem: string | null
  active: boolean
  behavior?: string
}
export interface ConnectionIPRuleTarget extends ConnectionRuleTarget {
  behavior: 'classical' | 'ipcidr'
  targets: ('direct' | 'proxy')[]
}
export interface ConnectionRuleTargets {
  profile_id: string
  profile_name: string
  direct: ConnectionRuleTarget | null
  proxy: ConnectionRuleTarget | null
  ip_rulesets?: ConnectionIPRuleTarget[]
}
