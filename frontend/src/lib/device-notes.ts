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
}
export interface ConnectionRuleTargets {
  profile_id: string
  profile_name: string
  direct: ConnectionRuleTarget | null
  proxy: ConnectionRuleTarget | null
}
