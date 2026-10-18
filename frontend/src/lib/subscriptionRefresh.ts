export function isRefreshInterval(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value)
    && (value === 0 || (value >= 60 && value <= 604800))
}

export function referencedProfileNames(usages: { profile_name?: unknown }[]): string {
  return [...new Set(usages.map(item => item.profile_name)
    .filter((name): name is string => typeof name === 'string' && Boolean(name.trim()))
    .map(name => name.trim()))].join('、')
}
