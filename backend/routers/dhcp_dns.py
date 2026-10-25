"""DHCP DNS scope and drift shared by both router drivers.

Selecting a DNS Agent opts into replacing DNS in the unique enabled DHCP
network containing the proxy gateway. No DHCP network is created implicitly.
"""
import ipaddress
from typing import Any, Dict, Optional

from backend.routers.base import DhcpDnsState, RouterError


def dns_agent_settings(agent_id: str, agent: Optional[Dict[str, Any]]) -> Dict[str, str]:
    settings = {'dns_agent_id': agent_id or '', 'dns_server': '', 'dns_error': ''}
    if not agent_id:
        return settings
    if not agent:
        settings['dns_error'] = '所选 DNS Agent 不存在，请重新选择'
    elif agent.get('service_type') != 'oxidns':
        settings['dns_error'] = 'DHCP DNS 必须选择 OxiDNS Agent'
    elif agent.get('status') != 'online':
        settings['dns_error'] = '所选 DNS Agent 离线，无法下发 DHCP DNS'
    else:
        try:
            host = agent.get('host')
            if not isinstance(host, str):
                raise ValueError
            address = ipaddress.IPv4Address(host)
            if address.is_unspecified or address.is_loopback or address.is_multicast or int(address) == 0xffffffff:
                raise ValueError
            settings['dns_server'] = str(address)
        except (ValueError, TypeError):
            settings['dns_error'] = '所选 DNS Agent 的主机地址必须是客户端可访问的 IPv4 地址'
    return settings


def _client_ipv4(value) -> str:
    """The same client-reachable IPv4 rule as dns_agent_settings, or ''."""
    try:
        if not isinstance(value, str):
            raise ValueError
        address = ipaddress.IPv4Address(value)
    except (ValueError, TypeError):
        return ''
    if address.is_unspecified or address.is_loopback or address.is_multicast or int(address) == 0xffffffff:
        return ''
    return str(address)


def dns_ha_settings(agent_id: str, agent: Dict[str, Any], peer: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """DHCP DNS for an OxiDNS pair sharing a virtual IP; only called while HA is enabled.

    The VIP is served by whichever member holds it, so one member being offline
    does not block publishing. ``dns_ha`` is present only when the pair is usable.
    """
    settings = {'dns_agent_id': agent_id, 'dns_server': '', 'dns_error': ''}
    ha = agent.get('oxidns_ha') or {}
    peer_ha = (peer or {}).get('oxidns_ha') or {}
    if agent.get('service_type') != 'oxidns':
        settings['dns_error'] = 'DHCP DNS 必须选择 OxiDNS Agent'
    elif (not peer or peer.get('service_type') != 'oxidns' or peer_ha.get('enabled') is not True
          or not ha.get('group_id') or peer_ha.get('group_id') != ha.get('group_id')
          or ha.get('peer_id') != peer.get('id') or peer_ha.get('peer_id') != agent.get('id')
          or peer_ha.get('vip') != ha.get('vip')):
        settings['dns_error'] = '所选 DNS Agent 的高可用配对不完整，请到 Agent 页重新设置 DNS 高可用'
    elif any((item.get('oxidns_ha_state') or {}).get('status') != 'succeeded' for item in (agent, peer)):
        settings['dns_error'] = '所选 DNS Agent 的高可用尚未部署成功，请到 Agent 页查看'
    elif all(item.get('status') != 'online' for item in (agent, peer)):
        settings['dns_error'] = '所选 DNS 高可用的两台 Agent 均离线，无法下发 DHCP DNS'
    else:
        vip = _client_ipv4(ha.get('vip'))
        hosts = [_client_ipv4(item.get('host')) for item in (agent, peer)]
        if not vip or not all(hosts):
            settings['dns_error'] = '所选 DNS 高可用的虚拟 IP 和两台主机地址必须是客户端可访问的 IPv4 地址'
        else:
            settings['dns_server'] = vip
            settings['dns_ha'] = {'vip': vip, 'hosts': hosts, 'peer_id': peer['id']}
    return settings


def matching_networks(state: DhcpDnsState, gateway: str):
    try:
        address = ipaddress.IPv4Address(gateway)
    except (ValueError, TypeError):
        raise RouterError('无法确定 DHCP DNS 下发范围：mihomo 网关不是有效 IPv4 地址')
    matches = []
    seen = set()
    for entry in state.entries:
        if not entry.enabled:
            continue
        if not entry.id or entry.id in seen:
            raise RouterError('DHCP 配置缺少唯一标识，无法安全下发 DNS')
        seen.add(entry.id)
        try:
            network = ipaddress.IPv4Network(entry.network, strict=False)
        except (ValueError, TypeError):
            detail = '；'.join(entry.blockers)
            raise RouterError('DHCP 配置包含无法识别的网段，无法安全确定 DNS 下发范围'
                              + (f'：{detail}' if detail else ''))
        if address in network:
            matches.append(entry)
    return matches


def build_dns_report(desired: Dict[str, Any], state: Optional[DhcpDnsState]) -> Dict[str, Any]:
    report = {'status': 'disabled', 'target_dns': desired.get('dns_server') or '',
              'message': '未选择 DHCP DNS 服务器，本次保留现有 DHCP DNS 配置。', 'items': [], 'blockers': []}
    if not desired.get('dns_agent_id'):
        return report
    report['status'] = 'unknown'
    error = desired.get('dns_error')
    matches = []
    if not error:
        if not report['target_dns']:
            error = '无法确定所选 DNS Agent 的 IPv4 地址'
        elif state is None:
            error = '无法读取 DHCP DNS 配置'
        else:
            try:
                matches = matching_networks(state, desired.get('proxy_gateway') or '')
            except RouterError as exc:
                error = str(exc)
    if not error and len(matches) != 1:
        error = ('未找到包含所选 mihomo 网关的已启用 DHCP 网段，不会自动创建 DHCP 配置' if not matches
                 else '多个已启用 DHCP 网段包含所选 mihomo 网关，无法确定唯一 DNS 下发范围')
    if not error and matches[0].blockers:
        error = '；'.join(matches[0].blockers)
    for entry in matches:
        status = ('unknown' if error else
                  ('synced' if entry.dns == [report['target_dns']] and entry.dns_explicit else 'pending'))
        report['items'].append({'id': entry.id, 'network': entry.network, 'current_dns': list(entry.dns),
                                'desired_dns': [report['target_dns']], 'status': status,
                                'detail': error or ('' if entry.dns_explicit else
                                                   'DNS 槽位仍含自动/未指定值，将明确设置为所选服务器')})
    if error:
        report.update(message=error, blockers=[error])
    else:
        report['status'] = report['items'][0]['status']
        report['message'] = ('该 DHCP 网段的 DNS 已与所选服务器一致。' if report['status'] == 'synced' else
                             '将替换该 DHCP 网段的 DNS 列表；客户端续租或重新连接后获取新 DNS。')
    return report


def checked_dns_target(check: Dict[str, Any], state: DhcpDnsState):
    matches = matching_networks(state, check['gateway'])
    if len(matches) != 1 or matches[0].id != check['id'] or matches[0].network != check['network']:
        raise RouterError('回读校验失败：DHCP DNS 的目标网段发生变化')
    entry = matches[0]
    if entry.blockers:
        raise RouterError('回读校验失败：' + '；'.join(entry.blockers))
    return entry


def verify_dns(check: Dict[str, Any], state: DhcpDnsState) -> None:
    entry = checked_dns_target(check, state)
    if entry.dns != check['dns'] or not entry.dns_explicit:
        raise RouterError('回读校验失败：DHCP DNS 与所选服务器不一致')
