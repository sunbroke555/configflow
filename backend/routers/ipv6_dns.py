"""Stop RA DNS announcements only on the LAN whose DHCP DNS uses OxiDNS.

RA prefixes, router lifetimes and upstream DNS settings remain untouched. A
shared ``interface=all`` row is not safe to edit on behalf of one LAN.
"""
import copy
import ipaddress
import re

from backend.routers.base import RouterError
from backend.routers.dhcp_dns import _client_ipv4, checked_dns_target, verify_dns
from backend.routers.ipv6_readiness import _lan

ND = '/ipv6/nd'
IPV4_ADDRESSES = '/ip/address'
# Status flags and the fields a dedicated LAN row sets itself; every other
# setting is copied from the shared all row so only DNS advertising changes.
NOT_COPIED = {'interface', 'advertise-dns', 'disabled', 'comment', 'dynamic', 'invalid', 'default', 'running'}


def _flag(row, key):
    value = str(row.get(key, 'false')).lower()
    if value in ('false', 'no', '0'):
        return False
    if value in ('true', 'yes', '1'):
        return True
    raise RouterError(f'IPv6 DNS 下发的 {key} 状态无法识别，请检查路由器')


def _dns_servers(row):
    servers = []
    # RouterOS versions expose this setting as dns or dns-servers. Keep both
    # in the snapshot; changing advertise-dns must not erase either setting.
    for key in ('dns', 'dns-servers'):
        value = row.get(key, '')
        if isinstance(value, str):
            values = [part.strip() for part in value.split(',') if part.strip()]
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            values = value
        else:
            raise RouterError('IPv6 DNS 下发的服务器列表无法识别，请检查路由器')
        try:
            servers.extend(str(ipaddress.IPv6Address(value)) for value in values)
        except (ValueError, TypeError):
            raise RouterError('IPv6 DNS 下发包含无法识别的服务器地址，请检查路由器') from None
    return list(dict.fromkeys(servers))


def _scope(desired):
    target = _client_ipv4(desired.get('dns_server'))
    scope = copy.deepcopy(desired.get('ipv6_dns_scope') or {})
    gateway = _client_ipv4(desired.get('proxy_gateway'))
    if desired.get('dns_error') or not target or not gateway:
        raise RouterError('请先选择可用的 OxiDNS 和 Mihomo 网关，再检查 IPv6 DNS 下发')
    if (not isinstance(scope, dict) or not scope.get('id') or not scope.get('network') or scope.get('gateway') != gateway
            or scope.get('dns') != [target]):
        raise RouterError('尚未确认唯一的 DHCP DNS 目标网段，不能调整 IPv6 DNS 下发')
    try:
        network = ipaddress.IPv4Network(scope['network'], strict=False)
        if ipaddress.IPv4Address(gateway) not in network:
            raise ValueError
    except (ValueError, TypeError):
        raise RouterError('DHCP DNS 目标网段与 Mihomo 网关不匹配，请重新预览') from None
    return gateway, target, scope


def _read(driver, gateway):
    addresses = driver._list(IPV4_ADDRESSES)
    if not isinstance(addresses, list) or any(not isinstance(row, dict) for row in addresses):
        raise RouterError('无法读取路由器 LAN 地址，不能确定 IPv6 DNS 下发范围')
    for row in addresses:
        if _flag(row, 'disabled'):
            continue
        try:
            network = ipaddress.IPv4Network(row.get('address'), strict=False)
        except (ValueError, TypeError):
            raise RouterError('路由器包含无法识别的 LAN 地址，不能确认 IPv6 DNS 下发范围') from None
        if ipaddress.IPv4Address(gateway) not in network:
            continue
        if _flag(row, 'invalid'):
            raise RouterError('Mihomo 所在的路由器 LAN 地址无效，请先检查地址配置')
        interface = row.get('actual-interface') or row.get('interface')
        if not isinstance(interface, str) or not interface:
            raise RouterError('Mihomo 所在的路由器 LAN 地址缺少接口信息')
    interface = _lan({'ipv4_addresses': addresses}, gateway)
    if not isinstance(interface, str) or not interface or interface == 'all':
        raise RouterError('无法确认 Mihomo 所在的唯一 LAN，请先检查路由器地址配置')
    rows = driver._list(ND)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RouterError('无法读取 IPv6 DNS 下发设置，请检查路由器连接和权限')
    if any(not isinstance(row.get('interface'), str) or not row['interface'] for row in rows):
        raise RouterError('IPv6 通告条目缺少接口信息，不能确定 DNS 下发范围')
    relevant = [row for row in rows if row['interface'] in (interface, 'all')]
    ids = [row.get('.id') for row in relevant]
    if any(not isinstance(row_id, str) or not row_id for row_id in ids) or len(ids) != len(set(ids)):
        raise RouterError('IPv6 通告条目缺少唯一标识，无法安全调整 DNS 下发')
    for row in relevant:
        if _flag(row, 'dynamic') or _flag(row, 'invalid'):
            raise RouterError('该 LAN 的 IPv6 通告是动态或无效条目，请先在路由器上处理')
        _flag(row, 'disabled')
        if row.get('advertise-dns') not in ('yes', 'self', 'no'):
            raise RouterError('IPv6 DNS 下发方式无法识别，请先在路由器上核对')
        _dns_servers(row)
    return interface, sorted(relevant, key=lambda row: row['.id'])


def _entries(driver, rows):
    return [{'id': row['.id'], 'attributes': driver._ownership_snapshot(ND, row)} for row in rows]


def _effective(rows, interface):
    # A running interface-specific ND entry takes precedence over all. Disabled
    # entries do not provide that override; leave the shared row untouched.
    explicit = [row for row in rows if row['interface'] == interface and not _flag(row, 'disabled')]
    return explicit or [row for row in rows if row['interface'] == 'all' and not _flag(row, 'disabled')]


def _constraints(rows, interface):
    if len([row for row in rows if row['interface'] == interface]) > 1:
        raise RouterError('该 LAN 有多个 IPv6 通告条目，无法确认应修改哪一条，请先在路由器上整理')
    if len([row for row in rows if row['interface'] == 'all']) > 1:
        raise RouterError('发现多个共享 IPv6 通告条目，无法确认 DNS 下发范围，请先在路由器上整理')
    if any(row['interface'] == 'all' and row['advertise-dns'] != 'no'
           for row in _effective(rows, interface)):
        raise RouterError('该 LAN 使用共享的 all 通告下发 DNS；直接关闭会影响其他接口，请先为该 LAN 单独设置 IPv6 通告')


def _cli(value):
    value = ','.join(value) if isinstance(value, list) else str(value)
    value = {'true': 'yes', 'false': 'no'}.get(value, value)
    if re.fullmatch(r'[A-Za-z0-9_.:/,%+-]+', value):
        return value
    return '"' + re.sub(r'(["\\$])', r'\\\1', value) + '"'


def _override_command(rows, interface):
    """A copyable RouterOS command giving the LAN its own RA row without DNS."""
    shared = [row for row in rows if row['interface'] == 'all']
    if (any(row['interface'] == interface for row in rows) or len(shared) != 1
            or _flag(shared[0], 'disabled') or shared[0]['advertise-dns'] == 'no'):
        return ''
    values = [f'{key}={_cli(value)}' for key, value in sorted(shared[0].items())
              if not key.startswith('.') and key not in NOT_COPIED
              and re.fullmatch(r'[a-z0-9-]+', key) and value not in ('', None, [])]
    return ' '.join([f'/ipv6 nd add interface={_cli(interface)} advertise-dns=no', *values])


def inspect(desired, driver):
    plan = {'enabled': bool(desired.get('dns_agent_id')), 'status': 'disabled', 'interface': '',
            'target_dns': desired.get('dns_server') or '', 'items': [], 'blockers': [], 'warnings': [],
            'ops': [], 'check': {}, 'evidence': {}, 'fix_command': '',
            'message': '未关联 OxiDNS，本次保留现有 IPv6 DNS 下发设置。'}
    if not plan['enabled']:
        return plan
    try:
        gateway, target, scope = _scope(desired)
        checked_dns_target(scope, driver.read_dhcp_dns())
        interface, rows = _read(driver, gateway)
        plan['interface'] = interface
        entries = _entries(driver, rows)
        plan['evidence'] = {'gateway': gateway, 'interface': interface, 'scope': scope, 'entries': entries}
        effective_ids = {row['.id'] for row in _effective(rows, interface)}
        for row in rows:
            pending = row['.id'] in effective_ids and row['advertise-dns'] != 'no'
            overridden = row['interface'] == 'all' and row['.id'] not in effective_ids and not _flag(row, 'disabled')
            plan['items'].append({
                'id': row['.id'], 'interface': interface, 'source_interface': row['interface'],
                'advertise_dns': row['advertise-dns'], 'dns_servers': _dns_servers(row),
                'status': 'pending' if pending else 'synced',
                'detail': ('发布时关闭该 LAN 的 IPv6 DNS 下发，保留 IPv6 地址和默认网关通告。'
                           if pending else ('该 LAN 使用专用通告，本次保留这条共享设置。' if overridden
                                            else '此条目未下发 IPv6 DNS，无需调整。'))})
        plan['fix_command'] = _override_command(rows, interface)
        _constraints(rows, interface)
        pending = [row for row in rows if row['interface'] == interface
                   and not _flag(row, 'disabled') and row['advertise-dns'] != 'no']
        after = copy.deepcopy(rows)
        for row in after:
            if row in pending:
                row['advertise-dns'] = 'no'
        plan['check'] = {'gateway': gateway, 'interface': interface, 'scope': copy.deepcopy(scope),
                         'entries': _entries(driver, after)}
        plan['status'] = 'pending' if pending else 'synced'
        plan['message'] = (f'发布时关闭 {interface} 的 IPv6 DNS 下发，客户端继续使用 DHCP 提供的 OxiDNS。'
                           if pending else '该 LAN 未下发 IPv6 DNS，无需调整。')
        for row in pending:
            plan['ops'].append({
                'kind': 'ipv6_dns_update', 'id': row['.id'], 'interface': interface,
                'source_interface': row['interface'], 'target_dns': target, 'gateway': gateway,
                'scope': copy.deepcopy(scope), 'values': {'advertise-dns': 'no'},
                'expected_rows': copy.deepcopy(entries), 'expected': driver._ownership_snapshot(ND, row),
                'check': copy.deepcopy(plan['check']), 'summary': f'关闭 {interface} 的 IPv6 DNS 下发'})
    except RouterError as error:
        plan.update(status='blocked', message=str(error), blockers=[str(error)], ops=[], check={})
        for item in plan['items']:
            item.update(status='blocked', detail=str(error))
    return plan


def report(plan):
    return {key: copy.deepcopy(plan[key]) for key in (
        'enabled', 'status', 'interface', 'target_dns', 'message', 'items', 'blockers', 'warnings', 'fix_command')}


def apply(driver, op):
    gateway, _, scope = _scope({'dns_server': op.get('target_dns'), 'proxy_gateway': op.get('gateway'),
                               'ipv6_dns_scope': op.get('scope')})
    verify_dns(scope, driver.read_dhcp_dns())
    interface, rows = _read(driver, gateway)
    _constraints(rows, interface)
    if interface != op.get('interface') or _entries(driver, rows) != op.get('expected_rows'):
        raise RouterError('LAN 或 IPv6 通告设置在预览后发生变化，请重新预览')
    selected = [row for row in rows if row['.id'] == op.get('id') and row['interface'] == interface]
    if (len(selected) != 1 or _flag(selected[0], 'disabled')
            or selected[0]['advertise-dns'] not in ('yes', 'self')
            or op.get('values') != {'advertise-dns': 'no'}
            or driver._ownership_snapshot(ND, selected[0]) != op.get('expected')):
        raise RouterError('IPv6 DNS 下发的目标或修改内容已变化，请重新预览')
    return driver._transaction([{'method': 'PATCH', 'path': ND, 'id': selected[0]['.id'],
                                 'values': {'advertise-dns': 'no'}, 'expected': op['expected']}])


def verify(driver, check):
    if not check:
        return
    verify_dns(check['scope'], driver.read_dhcp_dns())
    interface, rows = _read(driver, check['gateway'])
    _constraints(rows, interface)
    if interface != check['interface'] or _entries(driver, rows) != check['entries']:
        raise RouterError('回读校验失败：IPv6 DNS 下发未按计划关闭，或 LAN 的其他通告设置发生变化')
    if any(row['advertise-dns'] != 'no' for row in _effective(rows, interface)):
        raise RouterError('回读校验失败：该 LAN 仍在下发 IPv6 DNS')
