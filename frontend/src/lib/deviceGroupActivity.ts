import { normalizeClientRange } from '@/lib/oxidnsClientRange'

/** Display a query sample within one entered range, not effective group ownership. */
export function memberSample(member: string, clients: Array<{ ip: string; count: number }>) {
  const range = normalizeClientRange(member)
  const [network, prefix] = range.split('/')
  const matched = clients.filter(client => {
    try {
      const ip = normalizeClientRange(client.ip)
      if (network.includes(':') !== ip.includes(':')) return false
      return prefix ? normalizeClientRange(`${ip}/${prefix}`) === range : ip === range
    } catch { return false }
  })
  return { clients: matched.length, queries: matched.reduce((total, client) => total + client.count, 0) }
}
