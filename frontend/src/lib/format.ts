/** 字节数 → 「12.3 GB」，非数字返回 —— */
export const formatBytes = (bytes: number | null | undefined, digits = 1): string => {
  if (typeof bytes !== 'number' || !Number.isFinite(bytes) || bytes < 0) return '—'
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
  let v = bytes
  let i = 0
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i++
  }
  return `${i === 0 ? v.toFixed(0) : v.toFixed(digits)} ${units[i]}`
}

/** ISO 时间 → 「刚刚 / 4 分钟前 / 3 小时前 / 2 天前」，超过 30 天给日期 */
export const relativeTime = (iso: string | null | undefined): string => {
  if (!iso) return '—'
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return '—'
  const diff = Math.max(0, Date.now() - t) / 1000
  if (diff < 45) return '刚刚'
  if (diff < 3600) return `${Math.round(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.round(diff / 3600)} 小时前`
  if (diff < 86400 * 30) return `${Math.round(diff / 86400)} 天前`
  return new Date(t).toLocaleDateString('zh-CN')
}

/** 「检查于 12 分钟前」；不到一分钟时写「刚刚检查」。verb 换成「读取」等同理 */
export const checkedAgo = (iso: string | null | undefined, verb = '检查'): string => {
  const when = relativeTime(iso)
  return when === '刚刚' ? `刚刚${verb}` : `${verb}于 ${when}`
}

/** 订阅流量信息（subscription-userinfo）的派生值 */
export interface TrafficInfo {
  upload?: number
  download?: number
  total?: number
  expire?: number
}

export const trafficSummary = (traffic: TrafficInfo | null | undefined) => {
  const total = traffic?.total || 0
  const used = (traffic?.upload || 0) + (traffic?.download || 0)
  const expireDays =
    traffic?.expire && traffic.expire > 0
      ? Math.ceil((traffic.expire * 1000 - Date.now()) / 86400000)
      : null
  return {
    metered: total > 0,
    ratio: total > 0 ? Math.min(1, used / total) : 0,
    used,
    left: total > 0 ? Math.max(0, total - used) : null,
    expireDays
  }
}
