/** Submitting either member already publishes the pair; do not enqueue it twice. */
export function uniquePublicationTargets<T extends { id: string; oxidns_ha?: { enabled?: boolean; group_id?: string; peer_id?: string } | null }>(items: readonly T[]): T[] {
  const groups = new Set<string>()
  return items.filter(item => {
    const ha = item.oxidns_ha
    if (ha?.enabled !== true || !ha.group_id || !ha.peer_id) return true
    const key = JSON.stringify([ha.group_id, ...[item.id, ha.peer_id].sort()])
    if (groups.has(key)) return false
    groups.add(key)
    return true
  })
}
/** Keep backend workers available for progress queries during large publishes. */
export async function submitAgentRequests<T, R>(items: readonly T[], submit: (item: T) => Promise<R>): Promise<R[]> {
  const results = new Array<R>(items.length)
  let next = 0
  await Promise.all(Array.from({ length: Math.min(2, items.length) }, async () => {
    while (next < items.length) {
      const index = next++
      results[index] = await submit(items[index])
    }
  }))
  return results
}
