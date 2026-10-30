"""Conservative IPv6 inspection and additive Fake-IP routing for RouterOS.

Existing routes, RA, DNS and firewall rules stay under their current owner.
Only an unambiguous missing main-table Fake-IPv6 route is automatically added.
"""
import hashlib
import ipaddress
import json
import secrets
from urllib.parse import quote, unquote

from backend.routers.base import RouterError
from backend.routers.ipv6_policy import POLICY_TABLES, policy_checks

ROUTES = '/ipv6/route'
READINESS_TABLES = {
    'routes': ROUTES, 'addresses': '/ipv6/address', 'nd': '/ipv6/nd',
    'dhcp': '/ipv6/dhcp-client', 'nat': '/ipv6/firewall/nat',
    'filter': '/ipv6/firewall/filter', 'ipv4_addresses': '/ip/address',
}
TABLES = {**READINESS_TABLES, **POLICY_TABLES}


def enabled(row):
    return str(row.get('disabled', 'false')).lower() not in ('true', 'yes', '1')


def yes(value):
    return value is True or str(value).lower() in ('true', 'yes', '1')


def check(key, label, status, message, details=None):
    return {'id': key, 'label': label, 'status': status, 'message': message,
            **({'details': details} if details else {})}


def _network(value):
    try:
        return ipaddress.ip_network(value, strict=False)
    except (ValueError, TypeError):
        return None


def _lan(snapshot, host):
    try:
        address = ipaddress.IPv4Address(host)
    except ValueError:
        return None
    interfaces = set()
    for row in snapshot.get('ipv4_addresses', []):
        network = _network(row.get('address'))
        if network and network.version == 4 and address in network and enabled(row):
            interfaces.add(row.get('actual-interface') or row.get('interface'))
    return next(iter(interfaces)) if len(interfaces) == 1 else None


def _gateway(host, snapshot, lan, existing_routes=()):
    """Keep a proven existing next hop; prefer scoped LLA for a new route."""
    if not lan:
        return None
    networks = [_network(row.get('address')) for row in snapshot.get('addresses', [])
                if enabled(row) and row.get('interface') == lan]
    ula, link = set(), set()
    # Agent addresses belong only to its detected upstream interface (the
    # interfaces inventory also contains other NICs, so never use it here).
    for row in host.get('addresses', []):
        if row.get('interface') and row['interface'] != host.get('interface'):
            continue
        try:
            address = ipaddress.IPv6Address(str(row['address']).split('/')[0])
        except (ValueError, KeyError, TypeError):
            continue
        if any(yes(row.get(key)) for key in ('temporary', 'deprecated', 'tentative', 'dad_failed', 'invalid')):
            continue
        if address.is_link_local:
            link.add(str(address) + '%' + lan)
        elif row.get('usable') is True and address in ipaddress.IPv6Network('fc00::/7') and any(
                network and network.version == 6 and address in network for network in networks):
            ula.add(str(address) + '%' + lan)
    # A valid existing ULA route should not need replacing solely because the
    # creation preference changed. Its resolved ROS interface must still match.
    existing = {candidate for candidate in link | ula for row in existing_routes
                if row.get('routing-table', 'main') == 'main' and enabled(row) and yes(row.get('active'))
                and not any(yes(row.get(key)) for key in ('blackhole', 'unreachable', 'prohibit', 'invalid', 'dynamic', 'connect'))
                and _same_gateway(row, candidate)}
    if len(existing) == 1:
        return next(iter(existing))
    candidates = link if link else ula
    return next(iter(candidates)) if len(candidates) == 1 else None


def _same_gateway(row, gateway):
    immediate = str(row.get('immediate-gw', ''))
    # Prefer the resolved interface: an unscoped ULA can resolve through an
    # unexpected interface even when its address text matches our next hop.
    if '%' in immediate:
        return immediate == gateway
    return str(row.get('gateway', '')) == gateway


def _route_evidence(rows):
    fields = ('.id', 'dst-address', 'gateway', 'immediate-gw', 'routing-table', 'disabled',
              'active', 'blackhole', 'unreachable', 'prohibit', 'comment')
    # RouterOS may return an unchanged route set in a different order. Unlike
    # firewall rules, this read order is not the route selection order.
    return sorted(({key: row.get(key) for key in fields} for row in rows),
                  key=lambda row: json.dumps(row, sort_keys=True, ensure_ascii=False))


class _SnapshotReads:
    """Serve ``ipv6_fakeip.hairpin`` the rows this inspection already read.

    The same-port forward hint then costs no further RouterOS request; a table
    that failed to read stays unreadable, so the hint reports it as unknown.
    """
    PATHS = {'/ipv6/firewall/filter': 'filter', '/interface/list': 'interface_lists',
             '/interface/list/member': 'interface_members', '/interface': 'interfaces'}

    def __init__(self, snapshot, errors):
        self.snapshot, self.errors = snapshot, errors

    def _list(self, path):
        base, marker, name = path.partition('?list=')
        key = 'address_lists' if marker and base == '/ipv6/firewall/address-list' else self.PATHS.get(path)
        if key is None or key in self.errors or key not in self.snapshot:
            raise RouterError('未读取 ' + path)
        rows = self.snapshot[key]
        return [row for row in rows if row.get('list') == unquote(name)] if marker else rows


def _forward_hint(snapshot, errors, lan, fake, configuration):
    """#452's same-port forward hint for this router, from the rows already read.

    Clients reach Mihomo through the router's own LAN port for IPv6 Fake-IP and
    for the rule-set routes (published while Mihomo uses IPv6). Without Fake-IP
    a public destination stands in for those rule-set networks. Only reported
    when there is something to do: a drop to fix (with the command) or rows
    that could not be read. Advisory, so the readiness summary does not count it.
    """
    if not lan or configuration.get('ipv6') is not True:
        return None
    from backend.routers.ipv6_fakeip import hairpin
    warning, command = hairpin(_SnapshotReads(snapshot, errors), lan, fake or '2000::/3')
    if not warning:
        return None
    return {**check('forward_hairpin', 'IPv6 同口转发', 'warning' if command else 'unknown', warning),
            **({'command': command} if command else {})}


def inspect(driver, router, agent, host, configuration, dns_target=None, policy_context=None):
    snapshot, errors = {}, {}
    for key, path in TABLES.items():
        if key in ('address_lists', 'ipv4_address_lists'):
            continue
        try:
            snapshot[key] = driver._list(path)
        except RouterError:
            errors[key] = '读取失败，请检查 RouterOS IPv6 支持与账号权限'
    # Only read address lists referenced by the inspected rules. GeoIP lists
    # can be large and are irrelevant when no rule in this snapshot uses them.
    for prefix in ('', 'ipv4_'):
        key = prefix + 'address_lists'
        names = {str(row[field]).lstrip('!') for kind in ('filter', 'nat', 'raw', 'mangle')
                 for row in snapshot.get(prefix + kind, []) for field in ('src-address-list', 'dst-address-list')
                 if row.get(field)}
        snapshot[key] = []
        try:
            for name in sorted(names):
                snapshot[key].extend(driver._list(TABLES[key] + '?list=' + quote(name, safe='')))
        except RouterError:
            errors[key] = '引用的地址列表未完整读取，无法确认规则匹配'
    try:
        snapshot['dns'] = driver._get('/ip/dns')
    except RouterError:
        errors['dns'] = '无法读取 ROS DNS'
    checks = [check('read_' + key, key, 'unknown', message) for key, message in errors.items()]
    lan = _lan(snapshot, agent.get('host', ''))
    fake = configuration.get('fake_ipv6_range') if configuration.get('fake_ipv6_enabled') else None
    existing = [row for row in snapshot.get('routes', [])
                if fake and _network(row.get('dst-address')) == _network(fake)]
    gateway = _gateway(host, snapshot, lan, existing)
    defaults = [r for r in snapshot.get('routes', []) if r.get('dst-address') == '::/0'
                and r.get('routing-table', 'main') == 'main' and enabled(r) and yes(r.get('active'))
                and not any(yes(r.get(k)) for k in ('blackhole', 'unreachable', 'prohibit'))]
    checks.append(check('wan', 'IPv6 外网路由', 'ok' if defaults else 'warning',
                        '已有生效的 IPv6 默认路由；出口连接另行验证' if defaults else '未确认生效的 IPv6 默认路由'))
    nd = [r for r in snapshot.get('nd', []) if enabled(r) and r.get('interface') in (lan, 'all')]
    checks.append(check('ra', 'LAN 路由通告', 'ok' if lan and nd else 'warning',
                        f'{lan} 已启用 RA；主机是否收到以 Agent 检查为准' if lan and nd else '未确认 LAN 的 RA 设置，请核对主机地址与默认路由'))
    peers = [r for r in snapshot.get('dhcp', []) if enabled(r) and yes(r.get('use-peer-dns'))]
    dns = snapshot.get('dns') or {}
    dns_servers = str(dns.get('servers') or '').split(',')
    dns_details = []
    if peers:
        dns_details.append('DHCPv6 正在接受上游 DNS；此项影响 ROS 的 DNS 来源，不决定 IPv6 是否能联网。')
    if any(yes(r.get('advertise-dns')) for r in nd):
        dns_details.append('LAN RA 会广播 DNS，请确认客户端收到的是预期解析器。')
    if dns.get('dynamic-servers'):
        dns_details.append('ROS 存在动态 DNS。')
    if dns_target and dns_servers != [dns_target]:
        dns_details.append('ROS DNS 与所选 OxiDNS 不完全一致；先核对查询路径，再调整。')
    checks.append(check('dns', 'DNS 路径', 'warning' if dns_details else 'ok',
                        '需要核对 DNS 来源，保留现有设置' if dns_details else '未发现需要自动更改 DNS 的依据', dns_details))
    checks.extend(policy_checks(snapshot, errors, agent, host, configuration, lan, policy_context))
    hint = _forward_hint(snapshot, errors, lan, fake, configuration)
    if hint:
        checks.append(hint)
    plan = None
    if fake:
        target = ipaddress.IPv6Network(fake)
        rows = [r for r in snapshot.get('routes', []) if r.get('routing-table', 'main') == 'main'
                and _network(r.get('dst-address')) == target]
        conflicts = [r for r in snapshot.get('routes', []) if enabled(r) and r.get('routing-table', 'main') == 'main'
                     and (network := _network(r.get('dst-address'))) and network.version == 6
                     and network != target and network.prefixlen >= target.prefixlen and network.overlaps(target)]
        overlaps_lan = any(network and network.version == 6 and network.overlaps(target)
                           for network in (_network(r.get('address')) for r in snapshot.get('addresses', [])))
        if not configuration.get('tun_enabled'):
            checks.append(check('fake_ipv6', 'IPv6 Fake-IP 接收', 'error', '配置会返回 IPv6 Fake-IP，但尚未开启 TUN，现有路由不能代替接收能力'))
        elif overlaps_lan:
            checks.append(check('fake_ipv6', 'IPv6 Fake-IP 路由', 'error', 'IPv6 Fake-IP 与路由器现有地址网段重叠，请先调整网段'))
        elif (rows and gateway and len(rows) == 1 and enabled(rows[0]) and yes(rows[0].get('active'))
              and _same_gateway(rows[0], gateway) and not conflicts
              and not any(yes(rows[0].get(key)) for key in ('blackhole', 'unreachable', 'prohibit', 'invalid', 'dynamic', 'connect'))):
            checks.append(check('fake_ipv6', 'IPv6 Fake-IP 路由', 'ok', f'{fake} 已指向这台 Mihomo，沿用现有路由'))
        elif rows or conflicts or overlaps_lan:
            checks.append(check('fake_ipv6', 'IPv6 Fake-IP 路由', 'error', '已有路由未能确认匹配，或 Fake-IP 与现有网段冲突；请核对，自动配置不会覆盖'))
        elif any(key in READINESS_TABLES or key == 'dns' for key in errors) or not gateway:
            checks.append(check('fake_ipv6', 'IPv6 Fake-IP 路由', 'warning', '缺少对应路由，但未确认唯一、稳定的同网段下一跳，不能自动添加'))
        else:
            plan = {'dst-address': fake, 'gateway': gateway, 'routing-table': 'main', 'disabled': 'false', 'distance': '1'}
            checks.append(check('fake_ipv6', 'IPv6 Fake-IP 路由', 'warning', f'缺少 {fake} → {gateway}；请到上级路由差异报告预览并发布。'))
    else:
        checks.append(check('fake_ipv6', 'IPv6 Fake-IP 路由', 'ok', '当前保存配置未启用 IPv6 Fake-IP，本次不新增相关路由'))
    dependencies = {'wan': ('routes',), 'ra': ('nd', 'ipv4_addresses'), 'dns': ('dns', 'dhcp', 'nd')}
    for item in checks:
        if any(key in errors for key in dependencies.get(item['id'], ())):
            item.update(status='unknown', message='相关 RouterOS 配置未完整读取，暂时无法判断')
    evidence = {'router': router, 'routes': _route_evidence(snapshot.get('routes', [])),
                'lan': lan, 'gateway': gateway, 'plan': plan, 'errors': sorted(errors)}
    fingerprint = hashlib.sha256(json.dumps(evidence, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {'id': router['id'], 'name': router.get('name') or router['id'], 'checks': checks,
            'status': 'error' if any(c['status'] == 'error' for c in checks) else
                      'warning' if any(c['status'] in ('warning', 'unknown') for c in checks) else 'ok',
            'can_prepare': False, 'route_publication_required': plan is not None, 'plan': plan, 'fingerprint': fingerprint}


def add_missing_route(driver, plan):
    """Return an undo receipt; never edit/adopt an existing route by comment alone."""
    before = [r for r in driver._list(ROUTES) if _network(r.get('dst-address')) == _network(plan['dst-address'])
              and r.get('routing-table', 'main') == 'main']
    if before:
        raise RouterError('IPv6 路由已变化，请重新检查后再补齐')
    values = {**plan, 'comment': 'cf-ipv6-readiness:' + secrets.token_hex(12)}
    try:
        driver._request('PUT', ROUTES, values)
        rows = [r for r in driver._list(ROUTES) if r.get('comment') == values['comment']]
        if len(rows) != 1 or not _owned(rows[0], values) or not yes(rows[0].get('active')):
            raise RouterError('新增 IPv6 路由未能通过读回验证')
        return {'id': rows[0]['.id'], 'values': values}
    except Exception as error:
        # A lost write reply may still have created the row. The per-attempt
        # unpredictable marker and exact fields are required before removal.
        try:
            rows = [r for r in driver._list(ROUTES) if r.get('comment') == values['comment']]
            if len(rows) == 1 and _owned(rows[0], values):
                undo_route(driver, {'id': rows[0]['.id'], 'values': values})
        except RouterError:
            raise RouterError('IPv6 路由添加结果或撤销结果未能确认，请刷新路由器检查，勿重复添加') from None
        raise RouterError(str(error)) from None


def _owned(row, values):
    return all(str(row.get(k, 'main' if k == 'routing-table' else '')) == str(v)
               for k, v in values.items() if k != 'disabled') and enabled(row)


def undo_route(driver, receipt):
    row = next((r for r in driver._list(ROUTES) if r.get('.id') == receipt['id']), None)
    if row is None:
        return
    if not _owned(row, receipt['values']):
        raise RouterError('新增路由已被其他操作修改，未自动删除，请人工核对')
    # RouterOS 7.25 REST accepts *80000002 but does not decode %2A80000002
    # as a resource ID. Keep only the literal star unescaped; other path
    # delimiters remain encoded. Verified against the isolated CHR lab.
    driver._request('DELETE', ROUTES + '/' + quote(receipt['id'], safe='*'))
