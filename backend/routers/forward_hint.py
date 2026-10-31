"""Read-only hint: the RouterOS forward chain drops the same-port path to Mihomo.

Static routes (and policy routes) to a Mihomo on the same LAN send a client's
packets into the router's LAN port and straight back out of it. Mihomo answers
the client directly, so RouterOS sees only the first packet of each connection;
the rest are ``invalid``. A ``drop invalid`` ahead of any LAN-to-LAN accept
(RouterOS's default firewall has one, for IPv4 and for IPv6) then can stall a
new connection for seconds or until it times out. Redirects only ease it:
RouterOS rate-limits them per client, and forwarding Linux clients ignore them
(VM151, RouterOS 7.25).

The check simulates one LAN-in, LAN-out, invalid-state TCP packet through the
forward chain with the ``ipv6_policy`` rule evaluator (it handles IPv4 and
IPv6), and on a definite drop suggests one accept placed before the rule that
first takes the packet. ConfigFlow never writes the firewall: this changes no
release operation, check, evidence or fingerprint.
"""
import re
from urllib.parse import quote

from backend.routers.base import RouterError
from backend.routers.ipv6_policy import _enabled, _rule_match, evaluate_chain

FAMILIES = {
    4: {'filter': '/ip/firewall/filter', 'lists': '/ip/firewall/address-list', 'command': '/ip firewall filter',
        'filter_key': 'ipv4_filter', 'lists_key': 'ipv4_address_lists'},
    6: {'filter': '/ipv6/firewall/filter', 'lists': '/ipv6/firewall/address-list', 'command': '/ipv6 firewall filter',
        'filter_key': 'filter', 'lists_key': 'address_lists'},
}
COMMENT = 'LAN to Mihomo via router'
# Plain LAN traffic to a LAN host is neither decapsulated from nor sent into an
# IPsec policy; without this the default "accept in/out ipsec policy" rules
# ahead of IPv4 "drop invalid" would leave every IPv4 result unknown.
NO_IPSEC = {'in': 'none', 'out': 'none'}


def _quote(value):
    """A RouterOS CLI value: bare when safe, otherwise a quoted string."""
    text = str(value)
    if re.fullmatch(r'[A-Za-z0-9._\-]+', text):
        return text
    return '"' + re.sub(r'(["\\$])', r'\\\1', text) + '"'


def packet(lan, cidr):
    return {'in-interface': lan, 'out-interface': lan, 'dst-address': cidr, 'protocol': 'tcp',
            'connection-state': 'invalid', 'ipsec-policy': dict(NO_IPSEC)}


def _snapshot(driver, family, rows):
    paths = FAMILIES[family]
    snapshot = {paths['filter_key']: rows, 'interface_lists': driver._list('/interface/list'),
                'interface_members': driver._list('/interface/list/member'),
                'interfaces': driver._list('/interface'), paths['lists_key']: []}
    # Forward and the custom chains it may jump to; never input/output lists.
    names = sorted({str(row[field]).lstrip('!') for row in rows if row.get('chain') not in ('input', 'output')
                    for field in ('src-address-list', 'dst-address-list') if row.get(field)})
    if family == 4:
        # Scoped and reused within one preview, like every other IPv4 list read.
        from backend.routers import ros_address_lists
        for members in ros_address_lists.read(driver, names).values():
            snapshot[paths['lists_key']].extend(members)
    else:
        for name in names:
            snapshot[paths['lists_key']].extend(driver._list(paths['lists'] + '?list=' + quote(name, safe='')))
    return snapshot


def _first_taker(snapshot, family, test):
    """The forward rule that first takes this packet, whatever its comment says.

    Mirrors ``evaluate_chain``: rules that cannot match, log and passthrough are
    passed over; the first one that matches or may match decides (directly or
    through its jump), so an accept placed right before it always applies.
    Dynamic rules cannot be a ``place-before`` target, so the next one is used.
    """
    for row in snapshot[FAMILIES[family]['filter_key']]:
        if row.get('chain') != 'forward' or not _enabled(row):
            continue
        if str(row.get('action', 'accept')) in ('log', 'passthrough'):
            continue
        if _rule_match(snapshot, family, row, test) is False:
            continue
        if str(row.get('dynamic', '')).lower() in ('true', 'yes') or not row.get('.id'):
            continue
        return row
    return None


def check(driver, family, lan, targets, rows=None):
    """``{'status', 'command', 'rule'}`` for the same-port path to ``targets``.

    status: ``drop`` (definite, with a command), ``ok`` (not dropped, or not
    provable either way: unknown matchers are not daily warnings) or ``unknown``
    (the firewall could not be read). Never raises RouterError.
    """
    targets = [target for target in targets if target]
    if not lan or not targets:
        return {'status': 'unknown', 'command': '', 'rule': None}
    try:
        if rows is None:
            rows = driver._list(FAMILIES[family]['filter'])
        snapshot = _snapshot(driver, family, rows)
    except RouterError:
        return {'status': 'unknown', 'command': '', 'rule': None}
    # Without a destination selector anywhere, every target meets the same rules.
    if not any(row.get(field) for row in rows if _enabled(row)
               for field in ('dst-address', 'dst-address-list')):
        targets = targets[:1]
    for target in targets:
        test = packet(lan, target)
        if evaluate_chain(snapshot, family, 'filter', 'forward', test)['decision'] != 'drop':
            continue
        rule = _first_taker(snapshot, family, test)
        place = f' place-before={rule[".id"]}' if rule else ''
        command = (f'{FAMILIES[family]["command"]} add chain=forward action=accept in-interface={_quote(lan)} '
                   f'out-interface={_quote(lan)} comment="{COMMENT}"{place}')
        return {'status': 'drop', 'command': command, 'rule': rule}
    return {'status': 'ok', 'command': '', 'rule': None}


def drop_warning(family):
    """One explanation for both families; only the names differ.

    Measured on VM151 (RouterOS 7.25): without the accept every new IPv4
    connection stalled until the 5 s client timeout. For IPv6 with a link-local
    next hop, RouterOS redirected a client that opened connections one at a
    time, so those barely stalled; several at once, or a client that ignores
    redirects, stalled or timed out. So no fixed delay, and no reliance on
    redirects.
    """
    redirect = 'ICMPv6' if family == 6 else 'ICMP'
    covers = '一条命令同时覆盖 IPv6 Fake-IP 和 IPv6 规则集路由；' if family == 6 else ''
    return (f'IPv{family} 防火墙会丢弃同口转发的后续包：设备经 ROS 同一 LAN 口转给 Mihomo，回包不经过 ROS，'
            'ROS 把后续包判为 invalid，被 forward 链丢弃（如出厂的「drop invalid」），新连接可能卡几秒甚至超时。'
            f'ROS 的 {redirect} 重定向只能部分缓解：有数量限制，同时打开多个连接时仍会卡，开了转发的 Linux 设备还会忽略，'
            f'不能依赖。执行下方命令放行同口转发后，连接即恢复正常；{covers}ConfigFlow 不修改防火墙，路由照常发布。')


IPV4_DROP = drop_warning(4)
IPV6_DROP = drop_warning(6)
IPV4_UNKNOWN = '未能确认 IPv4 防火墙是否会丢弃转给 Mihomo 的同口转发：'


def ipv4(router, desired, state, report, driver):
    """The IPv4 same-port hint for the report's proxy routes, or None.

    RouterOS only, and only while this router has (or is about to publish)
    IPv4 routes to Mihomo. With managed return routing the replies come back
    through the router, the path is symmetric and nothing is invalid.
    """
    import ipaddress
    from backend.routers.routeros_nat import RouteEgressIndex
    if router.get('type') != 'routeros' or router.get('manage_return_routing') is True or driver is None:
        return None
    targets = [item['cidr'] for item in (report.get('routes') or {}).get('items') or []
               if item.get('status') in ('synced', 'adoptable', 'missing')]
    policy = report.get('policy_routing')
    if policy and policy.get('enabled') and not policy.get('blockers'):
        targets += [item['cidr'] for item in policy.get('items') or []]
    fake = desired.get('fake_ip_range')
    targets = sorted(dict.fromkeys(targets), key=lambda cidr: cidr != fake)  # Fake-IP first
    if not targets:
        return None
    try:
        lan, _ = RouteEgressIndex(state.nat_context or {}).gateway_egress(
            ipaddress.IPv4Address(desired.get('proxy_gateway') or ''))
    except ValueError:
        return None
    if not lan:
        return {'status': 'unknown', 'fix_command': '',
                'warning': IPV4_UNKNOWN + '无法确定 Mihomo 所在的 LAN 口。'}
    # Policy and DNS guard previews already read the whole filter: reuse it.
    live = state.ros_policy if state.ros_policy is not None else state.dns_guard
    rows = (live or {}).get(FAMILIES[4]['filter'])
    try:
        result = check(driver, 4, lan, targets, rows)
    except Exception:  # noqa: BLE001 - an optional hint must never break a preview or rule sync
        result = {'status': 'unknown'}
    if result['status'] == 'drop':
        return {'status': 'drop', 'warning': IPV4_DROP, 'fix_command': result['command']}
    if result['status'] == 'unknown':
        return {'status': 'unknown', 'fix_command': '', 'warning': IPV4_UNKNOWN + '无法读取 RouterOS IPv4 防火墙。'}
    return None
