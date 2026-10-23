import { isAxiosError } from 'axios'
import { setupApi, type SetupAbandonPlan, type SetupGoal } from '@/api/setup'
import { confirm, confirmDanger, notify } from '@/lib/feedback'

export const SETUP_STEPS = ['使用方式', '节点来源', '规则与 DNS', '安装与检查', '路由与 DNS', '开始使用']

export function setupLocalDnsWarning(value: string): string {
  const text = value.trim()
  if (!text) return '请填写直连 DNS 上游。'
  // Advanced YAML remains supported and is checked by the server's shared validator.
  if (/^(?:[-[{]|addr:)/.test(text)) return ''
  if (text.split('\n').some(line => /^(?:(?:udp|tcp):\/\/)?\d+(?:\.\d+){3}(?::\d+)?$/.test(line.trim()))) {
    return '普通 DNS 地址不能用于此方案，请使用 IP 地址的 DoH / DoT，例如 https://223.5.5.5/dns-query。'
  }
  return ''
}
export const SETUP_GOALS: { id: SetupGoal; title: string; detail: string; help: string }[] = [
  { id: 'router', title: 'Mihomo + OxiDNS 分流', detail: '有 Linux 主机，能修改主路由的静态路由和 DNS', help: 'Mihomo 负责代理，OxiDNS 负责域名分流。需要一台有管理员权限、固定局域网 IP 的 Linux 主机。主路由需添加静态路由，将代理目标网段转给这台 Linux，并将客户端 DNS 指向 OxiDNS。路由器品牌不限：iKuai / RouterOS 可接入现有功能自动发布，其他路由器按向导手动配置。客户端默认网关仍指向主路由。' },
  { id: 'gateway', title: 'Linux 网关', detail: '有 Linux 主机，能修改客户端的网关和 DNS', help: '将 Linux 作为客户端的网关，由 Mihomo + OxiDNS 处理分流。需要原生 Linux（systemd / OpenRC）、管理员权限、与客户端同网段的固定 IPv4 地址，以及可用的 DNS 53 端口。向导安装两项核心、发布配置并显式启用 Linux 转发；最后将测试客户端的网关和 DNS 指向该主机，验证后再扩大使用范围。' }
]
/** Show what abandoning removes or keeps, then abandon; null when cancelled. */
export async function abandonSetup(id: string): Promise<SetupAbandonPlan | null> {
  const { data: plan } = await setupApi.abandon(id, true)
  const title = '放弃向导'
  const confirmed = plan.delete_profile
    ? await confirmDanger(`结束向导「${plan.name}」，并删除它创建的配置空间「${plan.profile.name}」${plan.subscription ? `和订阅「${plan.subscription.name}」` : ''}。其他配置空间和已有设备不受影响，删除后无法恢复。`, { title, confirmText: '放弃并删除' })
    : await confirm(`结束向导「${plan.name}」。配置空间「${plan.profile.name}」会保留，因为${plan.kept_reason}。${plan.guidance}`, { title, confirmText: '放弃向导' })
  if (!confirmed) return null
  const { data } = await setupApi.abandon(id)
  notify.success(!data.delete_profile ? '已放弃向导，配置空间已保留。'
    : data.subscription_kept ? '已放弃向导并删除配置空间；订阅仍被其他配置使用，已保留。' : '已放弃向导，并删除了它创建的配置。')
  return data
}
export function setupError(error: unknown): string {
  if (isAxiosError<{ message?: string }>(error)) return error.response?.data?.message || '请求未完成，请检查连接后重试'
  return error instanceof Error ? error.message : '操作未完成，请重试'
}
export function setupServerError(value: string): string {
  try {
    const url = new URL(value)
    if (!['http:', 'https:'].includes(url.protocol) || !url.hostname || url.username || url.password || url.search || url.hash) throw new Error()
    if (['localhost', '0.0.0.0', '[::1]', '[::]'].includes(url.hostname) || url.hostname.startsWith('127.')) return '请使用 Linux 设备能访问的局域网地址或域名'
    return ''
  } catch { return '请输入完整的 HTTP(S) 服务地址' }
}
