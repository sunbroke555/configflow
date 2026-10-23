import type { LinuxGatewayStatus } from '@/api/linuxGateway'

export const FORWARDING_AGENT_VERSION = '1.8.40-go'
const NEEDS_AGENT = `需要 Agent ${FORWARDING_AGENT_VERSION}`
export type StatusTone = 'success' | 'warning' | 'danger' | 'progress' | 'neutral'

/** IPv4 转发的真实状态；旧 Agent 不报告此字段时提示更新。 */
export function forwardingLabel(status?: LinuxGatewayStatus | null): string {
  const forwarding = status?.ipv4_forwarding
  if (!forwarding) return NEEDS_AGENT
  if (forwarding.current !== '1') return '未开启'
  return forwarding.persisted === true ? '已开启并持久化' : '已开启，未持久化'
}
export function forwardingTone(status?: LinuxGatewayStatus | null): StatusTone {
  const forwarding = status?.ipv4_forwarding
  if (!forwarding) return 'warning'
  if (forwarding.current !== '1') return 'danger'
  return forwarding.persisted === true ? 'success' : 'warning'
}

/** 已部署 Mihomo 的 TUN 状态，设备名取自已部署配置。 */
export function tunLabel(status?: LinuxGatewayStatus | null): string {
  const tun = status?.tun
  if (!tun) return NEEDS_AGENT
  if (tun.configured !== true) return '未启用完整 TUN'
  return tun.present === true ? `运行中 · ${tun.device || 'Meta'}` : '未运行'
}
export function tunTone(status?: LinuxGatewayStatus | null): StatusTone {
  const tun = status?.tun
  if (!tun || tun.configured !== true) return 'warning'
  return tun.present === true ? 'success' : 'danger'
}

/** 完整 TUN 可以接入主路由：转发已开启并持久化、TUN 运行中，且没有旧网关记录。 */
export function fullTunReady(status?: LinuxGatewayStatus | null): boolean {
  if (!status || status.success === false) return false
  const { ipv4_forwarding: forwarding, tun, managed } = status
  return forwarding?.current === '1' && forwarding.persisted === true
    && tun?.configured === true && tun.present === true && tun.gateway_marker !== true
    && !managed?.active && !managed?.pending && !status.enabled_in_config
}

export const ROUTER_SPLIT_MESSAGES: Record<string, string> = {
  ROUTER_SPLIT_FULL_TUN: '此配置空间使用 Mihomo + OxiDNS 分流（完整 TUN），主路由转来的连接已全部交给 Mihomo，不能再开启 Linux 网关的选择性接管。',
  ROUTER_SPLIT_LEGACY: '这是旧版主路由分流：Mihomo 靠 Linux 网关接管主路由转来的连接。关闭 Linux 网关会同时去掉 TUN 并还原转发，主路由转来的连接会中断。',
  ROUTER_SPLIT_RESTORE_BLOCKED: '主路由分流的配置空间不能恢复试用前快照：快照中没有完整 TUN，恢复会让分流中断。',
  ROUTER_SPLIT_LEGACY_TEARDOWN: '撤销会还原 TUN 接管与 IPv4 转发，主路由转来的连接将中断。确认后重试。',
  ROUTER_SPLIT_INVALID: '主路由分流标记 mihomo.router_split 无效，已停止生成和网关操作。请从备份恢复，或重新运行首次使用向导。',
  UPGRADE_REQUIRED: `请先将 Mihomo Agent 更新到 ${FORWARDING_AGENT_VERSION} 或更新版本。`,
}
/** 旧版主路由分流下恢复快照会关闭 Linux 网关，因此不提供恢复。 */
export const LEGACY_RESTORE_BLOCKED = '恢复后 Linux 网关为关闭状态，主路由转来的连接会中断，已阻止。'
/** 主路由分流错误码的文案：服务端原文优先（同一错误码按接管方式可能有不同说明），缺失时用本地文案。 */
export function routerSplitErrorMessage(code: string | undefined | null, fallback = ''): string {
  return fallback || (code && ROUTER_SPLIT_MESSAGES[code]) || '请求未完成，请稍后重试。'
}
