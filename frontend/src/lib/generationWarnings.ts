/** Metadata stays separate from generated YAML, text and archive bytes. */
export function generationWarnings(response: { data?: unknown; headers?: Record<string, unknown> | { get(name: string): unknown } }): string[] {
  const body = response.data
  const warnings = body && typeof body === 'object' && 'warnings' in body ? body.warnings : undefined
  if (Array.isArray(warnings)) return [...new Set(warnings.filter((value): value is string => typeof value === 'string' && !!value.trim()))]
  const headers = response.headers
  const raw = headers && ('get' in headers && typeof headers.get === 'function' ? headers.get('X-ConfigFlow-Warnings')
    : (headers as Record<string, unknown>)['x-configflow-warnings'] || (headers as Record<string, unknown>)['X-ConfigFlow-Warnings'])
  if (typeof raw !== 'string' || !raw) return []
  try {
    const values = JSON.parse(decodeURIComponent(raw))
    return Array.isArray(values) ? [...new Set(values.filter((value): value is string => typeof value === 'string' && !!value.trim()))] : []
  } catch { return [] }
}
