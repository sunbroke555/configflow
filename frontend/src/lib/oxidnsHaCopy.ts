import { optionCopy } from './optionCopy'

export const oxidnsHaCopy = {
  peer: optionCopy('选另一台 OxiDNS 作备机，本机为主机。',
    '同一配置空间、同一局域网内的另一台 OxiDNS，且尚未加入其他高可用组。',
    '开启时自动同步配置与规则。后续配置发布先更新未持有虚拟 IP 的设备，迁移虚拟 IP 并确认解析正常后，再更新原持有设备；当前主备以实际虚拟 IP 持有情况为准。'),
  vip: optionCopy('填写局域网内未被占用的地址，作为 DNS 入口。',
    '地址需与两台 OxiDNS 在同一网段，且不在 DHCP 地址池中、未被其他设备使用。',
    '路由器 DHCP 与 DNS 劫持会改为下发此地址。设备始终查询虚拟 IP，主机故障时由备机接管同一地址，无需改客户端。'),
  vrid: optionCopy('留空时由虚拟 IP 末位自动决定。',
    '同一局域网已有其他 keepalived 或 VRRP 设备使用相同编号时修改。',
    'VRRP 用编号区分不同的虚拟 IP 组，范围 1–255。同一网段内的不同组不能重复；两台设备会使用同一编号。'),
}
