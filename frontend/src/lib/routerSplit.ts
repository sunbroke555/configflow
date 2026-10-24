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

/** 设备上的旧网关记录：已生效或未完成为 present，记录无法读取为 unknown（与服务端检查一致，都需清理）。 */
export function gatewayLeftover(status?: LinuxGatewayStatus | null): 'present' | 'unknown' | null {
  const managed = status?.managed
  if (managed?.active || managed?.pending) return 'present'
  return managed?.error ? 'unknown' : null
}

/** 完整 TUN 可以接入主路由：转发已开启并持久化、TUN 运行中，且没有旧网关记录。 */
export function fullTunReady(status?: LinuxGatewayStatus | null): boolean {
  if (!status || status.success === false) return false
  const { ipv4_forwarding: forwarding, tun } = status
  return forwarding?.current === '1' && forwarding.persisted === true
    && tun?.configured === true && tun.present === true && tun.gateway_marker !== true
    && !gatewayLeftover(status) && !status.enabled_in_config
}

export const ROUTER_SPLIT_MESSAGES: Record<string, string> = {
  ROUTER_SPLIT_FULL_TUN: '此配置空间使用 Mihomo + OxiDNS 分流（完整 TUN），主路由转来的连接已全部交给 Mihomo，不能再开启 Linux 网关的选择性接管。',
  ROUTER_SPLIT_LEGACY: '这是旧版主路由分流：Mihomo 靠 Linux 网关接管主路由转来的连接。关闭 Linux 网关会同时去掉 TUN 并还原转发，主路由转来的连接会中断。',
  ROUTER_SPLIT_RESTORE_BLOCKED: '主路由分流的配置空间不能恢复试用前快照：快照中没有完整 TUN，恢复会让分流中断。',
  ROUTER_SPLIT_LEGACY_TEARDOWN: '撤销会还原 TUN 接管与 IPv4 转发，主路由转来的连接将中断。确认后重试。',
  ROUTER_SPLIT_INVALID: '主路由分流标记 mihomo.router_split 无效，已停止生成和网关操作。请从备份恢复，或重新运行首次使用向导。',
  UPGRADE_REQUIRED: `请先将 Mihomo Agent 更新到 ${FORWARDING_AGENT_VERSION} 或更新版本。`,
}
/** 旧版主路由分流下，快照会关闭 Linux 网关时服务端拒绝恢复的说明。 */
export const LEGACY_RESTORE_BLOCKED = '恢复后 Linux 网关为关闭状态，主路由转来的连接会中断，已阻止。'
/** 旧网关记录仍在且 IPv4 转发未持久化时，撤销记录或发布完整 TUN 会让转发被还原为关闭，服务端拒绝时的说明。 */
export const FORWARDING_NOT_PERSISTED = '设备上仍有旧版 Linux 网关记录，且 IPv4 转发尚未持久化：Agent 撤销该记录时会把 IPv4 转发还原为关闭，主路由转来的连接会中断。请先在 Linux 网关安装向导的「检查转发与 TUN」中开启并持久化 IPv4 转发。'
/** 旧版主路由分流的基础 YAML 已有完整 TUN，但设备未持久化 IPv4 转发时，服务端拒绝关闭 Linux 网关的说明。 */
export const GATEWAY_OFF_NOT_PERSISTED = '关闭 Linux 网关会让 Agent 撤销设备上的旧版网关记录，并把这台 Linux 的 IPv4 转发还原为关闭，主路由分流转来的连接会中断。这台设备尚未持久化 IPv4 转发，已阻止；请保持 Linux 网关开启。如这台设备是路由器的代理网关，可在 Agent 页这台设备的卡片上开启并持久化 IPv4 转发后再操作。'
/** 不在 ROUTER_SPLIT_MESSAGES 中的错误码（该表与服务端 MESSAGES 逐条同步）。 */
const EXTRA_MESSAGES: Record<string, string> = {
  FORWARDING_NOT_PERSISTED,
  ROUTER_SPLIT_FORWARDING_NOT_PERSISTED: GATEWAY_OFF_NOT_PERSISTED,
}
/** 主路由分流错误码的文案：服务端原文优先（同一错误码按接管方式可能有不同说明），缺失时用本地文案。 */
export function routerSplitErrorMessage(code: string | undefined | null, fallback = ''): string {
  if (fallback) return fallback
  return (code && (ROUTER_SPLIT_MESSAGES[code] || EXTRA_MESSAGES[code])) || '请求未完成，请稍后重试。'
}
