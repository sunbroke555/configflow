"""Read-only, three-valued RouterOS packet-path diagnostics.

This is deliberately not a RouterOS emulator. Only documented simple matchers
are interpreted. Unknown packet fields, dynamic rule side effects and policy
routing never become a claim that a real connection succeeds.
"""
import ipaddress


POLICY_TABLES = {
    'raw': '/ipv6/firewall/raw', 'mangle': '/ipv6/firewall/mangle',
    'address_lists': '/ipv6/firewall/address-list',
    'ipv4_routes': '/ip/route', 'ipv4_filter': '/ip/firewall/filter',
    'ipv4_nat': '/ip/firewall/nat', 'ipv4_raw': '/ip/firewall/raw',
    'ipv4_mangle': '/ip/firewall/mangle', 'ipv4_address_lists': '/ip/firewall/address-list',
    'interface_lists': '/interface/list', 'interface_members': '/interface/list/member',
    'interfaces': '/interface', 'routing_rules': '/routing/rule',
}
_META = {'.id', 'chain', 'action', 'disabled', 'invalid', 'dynamic', 'comment',
         'log', 'log-prefix', 'bytes', 'packets', 'jump-target', 'reject-with',
         'to-addresses', 'to-ports', 'random', 'fully-random', 'persistent', 'hw-offload'}
_PROTOCOL = {'tcp': '6', 'udp': '17', 'icmp': '1', 'icmpv6': '58', 'ipv6-icmp': '58', 'icmp6': '58'}


def _yes(value):
    return value is True or str(value).lower() in ('true', 'yes', '1')


def _enabled(row):
    return not _yes(row.get('disabled')) and not _yes(row.get('invalid'))


def _key(family, table):
    return ('ipv4_' if family == 4 else '') + table


def _network(value):
    try:
        return ipaddress.ip_network(str(value).split('%')[0], strict=False)
    except (ValueError, TypeError):
        return None


def _not(value):
    return None if value is None else not value


def _or(values):
    values = list(values)
    return True if True in values else None if None in values else False


def _and(values):
    values = list(values)
    return False if False in values else None if None in values else True


def _address_match(actual, expected):
    """A configured Fake-IP prefix is assessed as a whole, not a made-up IP."""
    source = _network(actual)
    if source is None:
        return None
    expected = str(expected)
    negated, expected = expected.startswith('!'), expected.lstrip('!')
    target = _network(expected)
    if target is not None:
        result = False if source.version != target.version or not source.overlaps(target) else True if source.subnet_of(target) else None
    elif '-' in expected:
        try:
            low, high = (ipaddress.ip_address(part) for part in expected.split('-', 1))
            if low.version != source.version or high.version != source.version or int(low) > int(high):
                result = False
            elif int(source.network_address) >= int(low) and int(source.broadcast_address) <= int(high):
                result = True
            elif int(source.broadcast_address) < int(low) or int(source.network_address) > int(high):
                result = False
            else:
                result = None
        except ValueError:
            result = None
    else:
        # Unresolved DNS names in an address-list are not empty address sets.
        result = None
    return _not(result) if negated else result


def _number_match(actual, expected):
    if actual is None:
        return None
    text = str(expected)
    negated, text = text.startswith('!'), text.lstrip('!')
    try:
        actual = int(actual)
        found = False
        for item in text.split(','):
            bounds = item.split('-', 1)
            low, high = int(bounds[0]), int(bounds[-1])
            found = found or low <= actual <= high
        return not found if negated else found
    except (ValueError, TypeError):
        return None


def _string_match(actual, expected, protocol=False):
    if actual is None:
        return None
    text = str(expected)
    negated, text = text.startswith('!'), text.lstrip('!')
    normalize = (lambda item: _PROTOCOL.get(str(item).lower(), str(item).lower())) if protocol else str
    result = normalize(actual) in [normalize(item.strip()) for item in text.split(',')]
    return not result if negated else result


def _interface_member(snapshot, interface, name, visited=()):
    if interface is None:
        return None
    if not interface:
        return False
    if name == 'all':
        return True
    if name == 'none':
        return False
    if name in ('dynamic', 'static'):
        if 'interfaces' in snapshot.get('_errors', ()) or 'interfaces' not in snapshot:
            return None
        item = next((r for r in snapshot['interfaces'] if r.get('name') == interface), None)
        if item is None:
            return None
        return _yes(item.get('dynamic')) == (name == 'dynamic')
    if name in visited or any(k in snapshot.get('_errors', ()) or k not in snapshot for k in ('interface_lists', 'interface_members')):
        return None
    definitions = [r for r in snapshot['interface_lists'] if r.get('name') == name]
    if len(definitions) != 1:
        return None
    # RouterOS applies includes, excludes, then explicit members. Thus an
    # explicit member wins even when an included list was excluded.
    if any(_enabled(r) and r.get('list') == name and r.get('interface') == interface for r in snapshot['interface_members']):
        return True
    row = definitions[0]
    includes = [s for s in str(row.get('include', '')).split(',') if s]
    excludes = [s for s in str(row.get('exclude', '')).split(',') if s]
    included = _or(_interface_member(snapshot, interface, n, (*visited, name)) for n in includes)
    excluded = _or(_interface_member(snapshot, interface, n, (*visited, name)) for n in excludes)
    return _and((included, _not(excluded)))


def _list_match(snapshot, family, actual, name):
    negated, name = str(name).startswith('!'), str(name).lstrip('!')
    key = _key(family, 'address_lists')
    if key not in snapshot or key in snapshot.get('_errors', ()) or actual is None:
        return None
    result = _or(_address_match(actual, row.get('address')) for row in snapshot[key]
                 if _enabled(row) and row.get('list') == name)
    return _not(result) if negated else result


def _rule_match(snapshot, family, row, packet):
    results = []
    for key, value in row.items():
        if key in _META or value in ('', None):
            continue
        actual = packet.get(key)
        if key in ('src-address', 'dst-address'):
            result = _address_match(actual, value)
        elif key in ('src-address-list', 'dst-address-list'):
            result = _list_match(snapshot, family, packet.get(key.replace('-list', '')), value)
        elif key in ('in-interface-list', 'out-interface-list'):
            name = str(value)
            result = _interface_member(snapshot, packet.get(key.replace('-list', '')), name.lstrip('!'))
            if name.startswith('!'):
                result = _not(result)
        elif key in ('in-interface', 'out-interface', 'connection-state', 'connection-nat-state'):
            result = _string_match(actual, value)
        elif key == 'protocol':
            result = _string_match(actual, value, True)
        elif key in ('src-port', 'dst-port'):
            result = _number_match(actual, value)
        elif key == 'port':
            result = _or((_number_match(packet.get('src-port'), value), _number_match(packet.get('dst-port'), value)))
        elif key == 'icmp-options':
            parts = str(value).split(':', 1)
            result = _and((_number_match(packet.get('icmp-type'), parts[0]),
                           _number_match(packet.get('icmp-code'), parts[1]) if len(parts) == 2 else True))
        elif key == 'tcp-flags' and packet.get('tcp-flags') is not None:
            flags = set(packet['tcp-flags'])
            result = all((flag[1:] not in flags) if flag.startswith('!') else (flag in flags) for flag in str(value).split(','))
        elif key == 'ipsec-policy' and packet.get('ipsec-policy') is not None:
            # "in,ipsec" / "out,none": only a packet that states its IPsec use is judged.
            direction, _, policy = str(value).partition(',')
            actual = packet['ipsec-policy'].get(direction)
            result = None if actual is None or policy not in ('ipsec', 'none') else actual == policy
        elif key == 'hop-limit' and packet.get('hop-limit') is not None:
            try:
                op, limit = str(value).split(':', 1)
                result = {'equal': packet[key] == int(limit), 'less-than': packet[key] < int(limit), 'greater-than': packet[key] > int(limit)}.get(op)
            except (ValueError, TypeError):
                result = None
        else:
            # Includes marks, ipsec, time/limits, layer7, address-type, nth,
            # fragment and any future RouterOS matcher we do not understand.
            result = None
        results.append(result)
    return _and(results)


def _evidence(row, index, table, chain):
    identity = row.get('.id') or f'位置 {index + 1}'
    comment = str(row.get('comment') or '').replace('\n', ' ')[:180]
    return f'{table}/{chain} 第 {index + 1} 条 {identity}' + (f'（{comment}）' if comment else '') + f'：{row.get("action", "accept")}'


def evaluate_chain(snapshot, family, table, chain, packet):
    """Evaluate ordered rules; unknown matches branch instead of inventing data."""
    key = _key(family, table)
    if key not in snapshot or key in snapshot.get('_errors', ()):
        return {'decision': 'unknown', 'evidence': [f'{key} 未完整读取']}
    rows = [(index, row) for index, row in enumerate(snapshot[key]) if _enabled(row)]
    budget = [0]

    def walk(current, offset=0, stack=(), trail=()):
        budget[0] += 1
        if budget[0] > 256 or len(stack) > 12:
            return {'decision': 'unknown', 'evidence': [*trail, '规则分支或跳转过多，停止静态推断']}
        candidates = [(i, r) for i, r in rows if i >= offset and r.get('chain') == current]
        if not candidates:
            if stack:
                parent, resume = stack[-1]
                return walk(parent, resume, stack[:-1], trail)
            return {'decision': 'allow', 'evidence': [*trail, f'{table}/{current} 未命中终止规则，按内建链默认放行' if table != 'nat' else f'nat/{current} 未命中转换，保持地址']}
        index, row = candidates[0]
        matches = _rule_match(snapshot, family, row, packet)
        action = str(row.get('action', 'accept'))
        if matches is False or action in ('log', 'passthrough'):
            return walk(current, index + 1, stack, trail)
        evidence = _evidence(row, index, table, current)

        def apply():
            next_trail = (*trail, evidence)
            if action in ('accept', 'drop', 'reject'):
                return {'decision': 'allow' if action == 'accept' else 'drop', 'evidence': list(next_trail)}
            if action == 'return':
                if stack:
                    parent, resume = stack[-1]
                    return walk(parent, resume, stack[:-1], next_trail)
                return {'decision': 'allow', 'evidence': [*next_trail, '返回内建链默认处理']}
            if action == 'jump':
                target = row.get('jump-target')
                if not target or target == current or target in [entry[0] for entry in stack] or not any(r.get('chain') == target for _, r in rows):
                    return {'decision': 'unknown', 'evidence': [*next_trail, '跳转目标缺失、为空或形成循环']}
                return walk(target, 0, (*stack, (current, index + 1)), next_trail)
            if table == 'nat' and action in ('masquerade', 'src-nat', 'dst-nat', 'netmap', 'redirect'):
                return {'decision': 'translate', 'evidence': list(next_trail), 'translation': action}
            return {'decision': 'unknown', 'evidence': [*next_trail, '动作会改变连接/标记或超出当前静态检查范围']}

        matched = apply()
        if matches is True:
            return matched
        skipped = walk(current, index + 1, stack, trail)
        # Safe acceptance can be proven when both an unknown match and its
        # non-match branch terminate in the same allow/drop decision.
        if matched['decision'] == skipped['decision'] and matched['decision'] in ('allow', 'drop'):
            return {'decision': matched['decision'], 'evidence': list(dict.fromkeys([*matched['evidence'], *skipped['evidence'], '部分条件未知，但匹配与不匹配两条路径结论一致']))}
        return {'decision': 'unknown', 'evidence': [*trail, evidence, '缺少报文信息或包含未支持的条件，匹配与不匹配可能得到不同结果']}

    return walk(chain)


def _route_interface(snapshot, family, destination):
    key = 'routes' if family == 6 else 'ipv4_routes'
    target = _network(destination)
    if target is None or key not in snapshot or key in snapshot.get('_errors', ()):
        return None, '目的地址或路由表不完整'
    matches, partial = [], []
    for row in snapshot[key]:
        network = _network(row.get('dst-address'))
        if not _enabled(row) or not _yes(row.get('active')) or row.get('routing-table', 'main') != 'main' or network is None or network.version != family:
            continue
        if target.subnet_of(network):
            matches.append((network.prefixlen, row))
        elif target.overlaps(network):
            partial.append(network.prefixlen)
    if not matches:
        return None, '主路由表没有覆盖目的地址的生效路由'
    best = max(length for length, _ in matches)
    if any(length > best for length in partial):
        return None, '目的网段内存在更具体路由，不能用一条路径代表整个网段'
    rows = [row for length, row in matches if length == best]
    interfaces = set()
    for row in rows:
        if any(_yes(row.get(k)) for k in ('blackhole', 'unreachable', 'prohibit')):
            return None, '目的地址命中丢弃或不可达路由'
        gateway = str(row.get('immediate-gw') or row.get('gateway') or '')
        if ',' in gateway or not gateway:
            return None, '无法确认唯一路由出口'
        if '%' in gateway:
            interface = gateway.rsplit('%', 1)[1]
        elif _network(gateway) is None:
            interface = gateway
        else:
            return None, '下一跳未解析出出口网卡'
        interfaces.add(interface)
    return (next(iter(interfaces)), '按当前主路由表选择出口') if len(interfaces) == 1 else (None, '存在多个不同出口')


def _path(snapshot, family, chain, packet, nat=True):
    evidence = []
    nat_decision = None
    before = 'output' if chain == 'output' else 'prerouting'
    stages = [('raw', before), ('mangle', before)]
    if chain != 'output' and packet.get('connection-state') not in ('established', 'related', 'untracked'):
        stages.append(('nat', 'dstnat'))
    stages.extend([('mangle', chain), ('filter', chain)])
    if chain != 'input':
        stages.append(('mangle', 'postrouting'))
    if nat and chain != 'input' and packet.get('connection-state') == 'new':
        stages.append(('nat', 'srcnat'))
    seen = set()
    for table, current in stages:
        if (table, current) in seen:
            continue
        seen.add((table, current))
        result = evaluate_chain(snapshot, family, table, current, packet)
        evidence += result['evidence']
        if table == 'nat' and current == 'srcnat':
            nat_decision = result['decision']
        if result['decision'] != 'allow':
            if result['decision'] == 'translate' and current != 'srcnat':
                return {'decision': 'unknown', 'nat_decision': 'unknown', 'evidence': [*evidence, '目的地址被转换，后续路径需要实流验证']}
            return {**result, 'evidence': evidence, 'nat_decision': nat_decision or 'unknown'}
    return {'decision': 'allow', 'evidence': evidence, 'nat_decision': nat_decision}


def _policy_route_uncertain(snapshot, family, packet):
    if 'routing_rules' not in snapshot or 'routing_rules' in snapshot.get('_errors', ()):
        return True
    for row in snapshot['routing_rules']:
        if not _enabled(row):
            continue
        conditions = {key: value for key, value in row.items() if key in ('src-address', 'dst-address', 'interface')}
        if 'interface' in conditions:
            conditions['in-interface'] = conditions.pop('interface')
        # Only a main-table lookup with no extra condition is harmless here.
        if row.get('table') == 'main' and row.get('action', 'lookup') in ('lookup', 'lookup-only-in-table'):
            continue
        if _rule_match(snapshot, family, conditions, packet) is not False:
            return True
    return False


def _fake_gateway_matches(snapshot, family, target, agent, host):
    """A LAN exit alone does not prove that Fake-IP reaches this Mihomo."""
    candidates = []
    for row in snapshot.get('routes' if family == 6 else 'ipv4_routes', []):
        network = _network(row.get('dst-address'))
        if network and network.version == family and target.subnet_of(network) and _enabled(row) and _yes(row.get('active')) and row.get('routing-table', 'main') == 'main':
            candidates.append((network.prefixlen, row))
    if not candidates:
        return False
    known = {str(_network(agent.get('host')).network_address)} if family == 4 and _network(agent.get('host')) else set()
    if family == 6:
        known = {str(value.network_address) for item in host.get('addresses', [])
                 if (value := _network(item.get('address'))) and value.version == 6 and value.prefixlen == 128
                 and not any(item.get(k) for k in ('deprecated', 'tentative', 'dad_failed', 'temporary'))}
    best = max(length for length, _ in candidates)
    return all(str(row.get('immediate-gw') or row.get('gateway') or '').split('%', 1)[0] in known
               for length, row in candidates if length == best)


def _report(key, label, decision, message, details):
    status = {'allow': 'ok', 'drop': 'error', 'translate': 'warning', 'unknown': 'unknown'}[decision]
    return {'id': key, 'label': label, 'status': status, 'decision': decision,
            'message': message, 'details': list(dict.fromkeys(details))[:40]}


def policy_checks(snapshot, errors, agent, host, configuration, lan, context=None):
    """Report named traffic classes and only supplied node/client endpoints.

    ``context`` may contain node_targets (IPv6 literals and TCP ports), clients
    (real/saved address values) and fake_ipv4_range. No DNS or network writes.
    """
    context = context or {}
    snapshot = {**snapshot, '_errors': set(errors)}
    checks, traffic_results, nat_results = [], [], []
    scope = '仅检查当前规则快照和所列报文条件；不代表代理握手、桥防火墙、连接跟踪实流或公网已经连通。'

    def flow(key, label, family, packet, chain='forward', conditional=False, source_candidate=False, source_note=''):
        details = [scope, '；'.join(f'{k}={v}' for k, v in packet.items() if v is not None)]
        if source_note:
            details.append(source_note)
        if chain != 'input' and _policy_route_uncertain(snapshot, family, packet):
            result = {'decision': 'unknown', 'evidence': ['存在适用的策略路由或未完整读取路由规则，无法确认主表路径']}
        else:
            result = _path(snapshot, family, chain, packet)
        decision = result['decision']
        messages = {'allow': '按列出的报文条件，当前已支持的规则路径允许通过；仍需实际连接验证',
                    'drop': '按列出的报文条件命中阻断规则，请查看规则证据',
                    'translate': '按列出的报文条件会改写来源地址；是否需要保留需结合回程验证',
                    'unknown': '现有信息不足或规则超出静态判断范围，需要实际连接验证'}
        report = _report(key, label, decision, messages[decision], [*details, *result['evidence']])
        if conditional and report['status'] == 'error':
            report['status'] = 'warning'
            report['message'] = '此类报文会被阻断；是否需要放行取决于当前上网方式，请查看规则证据'
        if source_candidate and report['status'] == 'error':
            report['status'] = 'warning'
            report['message'] = '此源地址候选的报文条件会被阻断；实际连接选择哪个源地址仍需实测'
        checks.append(report)
        if not conditional:
            traffic_results.append('unknown' if source_candidate and decision == 'drop' else decision)
            if packet.get('connection-state') == 'new':
                nat_results.append(result.get('nat_decision') or 'unknown')
        return result

    # Control-plane classes have protocol-defined addresses/ports rather than
    # invented node endpoints. Unknown peer/local addresses stay unknown.
    wan_interfaces = set()
    for row in snapshot.get('routes', []):
        if row.get('dst-address') == '::/0' and _enabled(row) and _yes(row.get('active')):
            interface, _ = _route_interface(snapshot, 6, '::/0')
            if interface:
                wan_interfaces.add(interface)
    wan = next(iter(wan_interfaces)) if len(wan_interfaces) == 1 else None
    controls = [
        ('ra_input', 'ROS 接收上游多播 RA', {'protocol': 'icmpv6', 'icmp-type': 134, 'icmp-code': 0, 'hop-limit': 255, 'src-address': 'fe80::/10', 'dst-address': 'ff02::1', 'in-interface': wan}),
        ('dhcpv6_input', 'ROS 接收 DHCPv6 回复', {'protocol': 'udp', 'src-port': 547, 'dst-port': 546, 'in-interface': wan}),
        ('rs_input', 'ROS 接收 LAN 路由请求', {'protocol': 'icmpv6', 'icmp-type': 133, 'icmp-code': 0, 'hop-limit': 255, 'in-interface': lan}),
        ('nd_input', 'ROS 接收 LAN 邻居请求', {'protocol': 'icmpv6', 'icmp-type': 135, 'icmp-code': 0, 'hop-limit': 255, 'in-interface': lan}),
        ('na_input', 'ROS 接收 LAN 邻居通告', {'protocol': 'icmpv6', 'icmp-type': 136, 'icmp-code': 0, 'hop-limit': 255, 'in-interface': lan}),
    ]
    for icmp_type, title in ((1, '不可达'), (2, '路径 MTU'), (3, '超时'), (4, '参数错误')):
        controls.append((f'icmpv6_{icmp_type}_input', f'ROS 接收相关 IPv6 {title}报文',
                         {'protocol': 'icmpv6', 'icmp-type': icmp_type, 'icmp-code': 0 if icmp_type == 2 else None,
                          'connection-state': 'related', 'in-interface': wan}))
    for key, label, packet in controls:
        flow(key, label, 6, {**packet, 'out-interface': ''}, 'input', True)

    addresses = []
    for row in host.get('addresses', []):
        address = _network(row.get('address'))
        if address and address.version == 6 and address.prefixlen == 128 and row.get('usable') is True and not any(row.get(k) for k in ('deprecated', 'tentative', 'dad_failed')):
            addresses.append(str(address.network_address))
    targets = []
    for item in context.get('node_targets', []):
        address = _network(item.get('address')) if isinstance(item, dict) else None
        port = item.get('port') if isinstance(item, dict) else None
        if address and address.version == 6 and address.prefixlen == 128 and type(port) is int and 1 <= port <= 65535:
            targets.append((str(address.network_address), port))
    if len(addresses) > 3 or len(targets) > 3:
        checks.append(_report('node_policy_scope', '节点规则检查范围', 'unknown', '本次最多检查 3 个已保存节点及 3 个主机源地址候选，其余组合尚未检查', [scope]))
        traffic_results.append('unknown')
        nat_results.append('unknown')
    if not addresses or not targets or not lan:
        checks.append(_report('node_policy', 'Mihomo IPv6 节点规则路径', 'unknown', '缺少明确的主机源地址、节点 IPv6 地址或 LAN 网卡，未虚构测试连接', [scope]))
        traffic_results.append('unknown')
        nat_results.append('unknown')
    for index, (target, port) in enumerate(targets[:3]):
        interface, reason = _route_interface(snapshot, 6, target)
        for source_index, source in enumerate(addresses[:3]):
            packet = {'src-address': source, 'dst-address': target, 'protocol': 'tcp', 'dst-port': port,
                      'connection-state': 'new', 'tcp-flags': ['syn'], 'in-interface': lan, 'out-interface': interface}
            key = f'node_{index}_{source_index}'
            if not interface:
                checks.append(_report(key, 'Mihomo IPv6 节点出口', 'unknown', reason, [scope]))
                traffic_results.append('unknown')
                nat_results.append('unknown')
                continue
            outgoing = flow(key, 'Mihomo IPv6 节点出口（源地址候选）', 6, packet, source_candidate=True)
            if outgoing['decision'] == 'translate':
                checks.append(_report(key + '_return', 'IPv6 节点返回路径', 'unknown', '去程会改写源地址，返回路径需读取实际连接跟踪或实测确认', outgoing['evidence']))
            else:
                flow(key + '_return', 'IPv6 节点返回路径', 6,
                     {'src-address': target, 'dst-address': source, 'protocol': 'tcp', 'src-port': port,
                      'connection-state': 'established', 'in-interface': interface, 'out-interface': lan}, source_candidate=True)
                flow(key + '_mtu', 'Mihomo 接收相关 IPv6 路径 MTU 报文', 6,
                     {'dst-address': source, 'protocol': 'icmpv6', 'icmp-type': 2, 'icmp-code': 0,
                      'connection-state': 'related', 'in-interface': interface, 'out-interface': lan}, source_candidate=True)

    clients = []
    for item in context.get('clients', []):
        address = _network(item.get('address')) if isinstance(item, dict) else None
        if address and address.prefixlen == address.max_prefixlen:
            clients.append((address, item.get('source_candidate') is True, str(item.get('source_note') or '')))
    for family, fake in ((6, configuration.get('fake_ipv6_range') if configuration.get('fake_ipv6_enabled') else None),
                         (4, context.get('fake_ipv4_range'))):
        network = _network(fake)
        if not network or network.version != family:
            continue
        family_clients = [item for item in clients if item[0].version == family]
        if not family_clients:
            checks.append(_report(f'fake{family}_policy', f'客户端 IPv{family} Fake-IP 规则路径', 'unknown', '未提供明确客户端地址，无法确认来源匹配、NAT 免转换及返回路径', [scope, f'仅已知配置中的目标网段 {network}，未验证客户端 DNS 实际答案']))
            traffic_results.append('unknown')
            nat_results.append('unknown')
            continue
        if len(family_clients) > 3:
            checks.append(_report(f'fake{family}_policy_scope', f'IPv{family} 客户端检查范围', 'unknown', '本次最多检查 3 个明确客户端地址，其余客户端尚未检查', [scope]))
            traffic_results.append('unknown')
            nat_results.append('unknown')
        interface, reason = _route_interface(snapshot, family, str(network))
        for index, (client, candidate, note) in enumerate(family_clients[:3]):
            client_interface, client_reason = _route_interface(snapshot, family, str(client))
            if not interface or not client_interface or interface != lan or not _fake_gateway_matches(snapshot, family, network, agent, host):
                message = reason if not interface else client_reason if not client_interface else 'Fake-IP 路由的出口或下一跳未指向这台 Mihomo，尚未确认分流路径'
                checks.append(_report(f'fake{family}_{index}', f'客户端 IPv{family} Fake-IP 规则路径' + ('（地址候选）' if candidate else ''),
                                      'unknown', message, [scope, note] if note else [scope]))
                traffic_results.append('unknown')
                nat_results.append('unknown')
                continue
            packet = {'src-address': str(client.network_address), 'dst-address': str(network), 'protocol': 'tcp',
                      'connection-state': 'new', 'tcp-flags': ['syn'], 'in-interface': client_interface, 'out-interface': interface}
            outgoing = flow(f'fake{family}_{index}', f'客户端 IPv{family} Fake-IP 去程与 NAT' + ('（地址候选）' if candidate else ''),
                            family, packet, source_candidate=candidate, source_note=note)
            # A same-LAN proxy can return directly to the client, bypassing ROS;
            # no static router snapshot can prove the host return-routing rules.
            returned = {'src-address': str(network), 'dst-address': str(client.network_address), 'protocol': 'tcp',
                        'connection-state': 'established', 'in-interface': interface, 'out-interface': client_interface}
            flow(f'fake{family}_{index}_return', f'IPv{family} Fake-IP 返回 ROS 时的规则' + ('（地址候选）' if candidate else ''),
                 family, returned, conditional=True, source_candidate=candidate, source_note=note)
            checks.append(_report(f'fake{family}_{index}_return_routing', f'IPv{family} Fake-IP 实际回程', 'unknown',
                                  '返回包是否经过 ROS、是否被源地址转换及连接跟踪识别，仍需 Mihomo 主机回流检查或客户端实测',
                                  [scope, '上述返回规则按 established 条件评估，不证明真实连接已经处于该状态', *outgoing['evidence'][-2:]]))

    def summary(key, label, values):
        decision = 'drop' if 'drop' in values else 'unknown' if 'unknown' in values or not values else 'translate' if 'translate' in values else 'allow'
        messages = {'drop': '至少一条已列出的路径命中阻断，详见逐条规则证据',
                    'unknown': '部分路径缺少目标或存在无法静态判断的规则，不能据此确认联网正常',
                    'translate': '已列出的路径包含来源地址转换，需结合实际回程判断',
                    'allow': '已列出的有限报文条件通过静态规则检查，实际连接仍需单独验证'}
        return _report(key, label, decision, messages[decision], [scope])

    return [summary('firewall', '防火墙路径检查', traffic_results), summary('nat', 'NAT 与免转换检查', nat_results), *checks]
