"""Read-only advice for state filters on the single-LAN proxy path.

This is not a firewall simulator. Exclude a rule only when its selectors prove
it cannot match this LAN path; unknown matchers remain possible. Never feed
this optional observation into release operations, checks or fingerprints.
"""
import ipaddress

from backend.routers.base import RouterError
from backend.routers.report import _networks
from backend.routers.routeros_nat import RouteEgressIndex, _selector_matches, _truthy

FILTER = '/ip/firewall/filter'
METADATA = {'.id', 'chain', 'action', 'comment', 'disabled', 'invalid', 'dynamic',
            'bytes', 'packets', 'log', 'log-prefix', 'reject-with'}
SCOPE = {'src-address', 'dst-address', 'src-address-list', 'dst-address-list',
         'in-interface', 'out-interface', 'in-interface-list', 'out-interface-list',
         'src-address-type', 'dst-address-type'}


def _live(row):
    return row.get('chain') == 'forward' and not any(_truthy(row.get(key)) for key in ('disabled', 'invalid'))


def _states(row):
    text = str(row.get('connection-state') or '').strip()
    return {part.strip() for part in text.lstrip('!').split(',') if part.strip()}, text.startswith('!')


def _matches_state(row, state):
    values, negate = _states(row)
    return not values or ((state not in values) if negate else (state in values))


def _established_accept(row):
    return (row.get('action') == 'accept' and row.get('connection-state')
            and _matches_state(row, 'established') and not _matches_state(row, 'invalid')
            and not _matches_state(row, 'new'))


def _remaining_drop(row):
    # A DNS/port-specific drop after established/related is not a default-deny
    # state firewall. Explicit invalid drops are handled independently.
    allowed = METADATA | SCOPE | {'connection-state'}
    if str(row.get('protocol') or '').lower() in {'tcp', '6', 'all'}:
        allowed.add('protocol')
    return _matches_state(row, 'invalid') and not any(value for key, value in row.items() if key not in allowed)


def _candidates(rows):
    established = False
    for row in rows:
        if not _live(row):
            continue
        established |= bool(_established_accept(row))
        values, negate = _states(row)
        if row.get('action') in {'drop', 'reject'} and (
                ('invalid' in values and not negate) or (established and _remaining_drop(row))):
            return True
    return False


def _overlap(values, networks, negate=False):
    if not networks:
        return True  # No destination selection is available to exclude a rule.
    parsed = _networks(values)
    if not parsed:
        return not values or negate  # Known empty lists cannot match positively.
    if negate:
        return not all(any(target.subnet_of(net) for net in parsed) for target in networks)
    return any(target.overlaps(net) for target in networks for net in parsed)


def _address_match(value, networks):
    text = str(value or '').strip()
    if not text:
        return True
    negate = text.startswith('!')
    literal = text[1:] if negate else text
    # Unknown syntax is not proof that a rule misses LAN traffic.
    return not _networks([literal]) or _overlap([literal], networks, negate)


def _list_match(value, networks, address_sets):
    text = str(value or '').strip()
    if not text:
        return True
    negate = text.startswith('!')
    name = text[1:] if negate else text
    if name not in address_sets:
        return True  # Do not download potentially enormous unrelated lists.
    values = address_sets[name]
    if not values:
        return negate
    if not _networks(values):
        return True
    return _overlap(values, networks, negate)


def _sources(gateway, context):
    address = ipaddress.IPv4Address(gateway)
    networks = [network for row in context.get('routes') or []
                if _truthy(row.get('connect')) and _truthy(row.get('active'))
                for network in _networks([row.get('dst-address') or '']) if address in network]
    network = max(networks, key=lambda net: net.prefixlen) if networks else ipaddress.IPv4Network(f'{gateway}/24', strict=False)
    host = ipaddress.IPv4Network(f'{gateway}/32')
    return list(network.address_exclude(host)) if network != host else []


def _possible(row, interface, context, sources, destinations, address_sets):
    if row.get('src-address-type') == 'local' or row.get('dst-address-type') == 'local':
        return False  # Router-originated/local traffic does not use forward.
    for direction, networks in (('src', sources), ('dst', destinations)):
        if not _address_match(row.get(direction + '-address'), networks):
            return False
        if not _list_match(row.get(direction + '-address-list'), networks, address_sets):
            return False
    for direction in ('in', 'out'):
        for suffix, is_list in (('interface', False), ('interface-list', True)):
            if _selector_matches(row.get(f'{direction}-{suffix}'), interface, context, is_list) is False:
                return False
    return True


def _label(row, index):
    text = ' '.join(str(row.get('comment') or '').split())[:100]
    identity = ' '.join(str(row.get('.id') or f'第 {index + 1} 条').split())[:40]
    return f'「{text}」（ID {identity}）' if text else f'（ID {identity}）'


def _covers_invalid(row, interface, context, sources, destinations, address_sets):
    """Only proved complete coverage can shadow later invalid-state rules."""
    if not _matches_state(row, 'invalid') or any(
            value for key, value in row.items() if key not in METADATA | SCOPE | {'connection-state'}):
        return False
    for direction, networks in (('src', sources), ('dst', destinations)):
        for suffix in ('address', 'address-list'):
            text = str(row.get(f'{direction}-{suffix}') or '').strip()
            if not text:
                continue
            negate = text.startswith('!')
            key = text[1:] if negate else text
            values = address_sets.get(key) if suffix == 'address-list' else [key]
            if values is None or not networks:
                return False
            parsed = _networks(values)
            if values and not parsed:
                return False
            if negate:
                if any(net.overlaps(target) for net in parsed for target in networks):
                    return False
            elif not all(any(target.subnet_of(net) for net in parsed) for target in networks):
                return False
    if any(row.get(key) for key in ('src-address-type', 'dst-address-type')):
        return False
    return all(_selector_matches(row.get(f'{direction}-{suffix}'), interface, context, is_list) is True
               for direction in ('in', 'out') for suffix, is_list in (('interface', False), ('interface-list', True)))


def warnings_for(desired, state, driver):
    if driver is None or not callable(getattr(driver, '_list', None)):
        return []
    try:
        # Policy previews already read the full filter; reuse exactly that
        # snapshot without changing policy evidence or request payloads.
        live = state.ros_policy if state.ros_policy is not None else state.dns_guard
        rows = live.get(FILTER) if live is not None else None
        if rows is None:
            rows = getattr(driver, '_forward_filter_rows', None)
        if rows is None:
            rows = driver._list(FILTER)
        if not _candidates(rows):
            return []
        context = dict(state.nat_context or {})
        gateway = desired['proxy_gateway']
        sources = _sources(gateway, context)
        interface, _ = RouteEgressIndex(context).gateway_egress(ipaddress.IPv4Address(gateway))
        if interface and any(row.get(key) for row in rows for key in ('in-interface-list', 'out-interface-list')):
            for key, path in (('interface_lists', '/interface/list'), ('interface_members', '/interface/list/member')):
                if context.get(key) is None:
                    try:
                        context[key] = driver._list(path)
                    except RouterError:
                        pass  # Unknown scope remains possible, never a blocker.
    except (RouterError, ValueError, KeyError):
        return []  # Optional advice must not introduce a publication dependency.
    destinations = _networks([item['cidr'] for item in desired.get('proxy_cidrs') or []]
                             + [item['cidr'] for item in desired.get('ros_policy', {}).get('cidrs') or []])
    warnings, established = [], None
    for index, row in enumerate(rows):
        if not _live(row) or not _possible(row, interface, context, sources, destinations, state.address_sets):
            continue
        if _established_accept(row):
            established = (row, index)
        values, negate = _states(row)
        if row.get('action') in {'drop', 'reject'}:
            explicit = 'invalid' in values and not negate
            if explicit or (established and _remaining_drop(row)):
                reason = (f'{row["action"]} connection-state={row["connection-state"]}' if explicit
                          else f'在放行已建立连接的规则 {_label(*established)} 后 {row["action"]} 其余流量')
                warnings.append(f'RouterOS forward 规则 {_label(row, index)}：{reason}，可能作用于局域网代理转发。'
                                '当前未开启自动配置回流，主路由只能看到去程，状态过滤可能丢弃或拒绝后续包。'
                                '同类 drop 规则的 TCP 实测中，每个新连接约 6 秒后才通（syn-sent 超时），'
                                '短超时请求会失败。建议开启「自动配置回流」，让回包先经过主路由。'
                                '本提示不会自动开启回流，也不阻止发布。')
        # Nothing below an unconditional terminal rule can affect this path.
        if row.get('action') in {'accept', 'drop', 'reject'} and _covers_invalid(
                row, interface, context, sources, destinations, state.address_sets):
            break
    return warnings
