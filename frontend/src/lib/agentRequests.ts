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
