"""Conservatively assess whether a RouterOS proxy path needs source-NAT bypass.

This is deliberately not a firewall simulator. Only a proven egress mismatch
excludes a potentially translating rule; other packet matchers remain possible.
"""
import ipaddress
from bisect import bisect_left, bisect_right
from typing import Any, Dict, Optional, Tuple

from backend.routers.base import RouteEntry, StaticRouteIndex


NON_TRANSLATING = {'accept', 'log', 'passthrough', 'return'}
TRANSLATING = {'masquerade', 'src-nat', 'netmap', 'same', 'endpoint-independent-nat'}
# RouterOS documents these as packet/connection metadata, logging, header or
# control-flow actions, not gateway selection. Unknown future actions are unsafe
# evidence. A jump is handled separately after checking its target chain exists.
# https://help.mikrotik.com/docs/spaces/ROS/pages/48660587/Mangle
# https://help.mikrotik.com/docs/spaces/ROS/pages/250708064/Common+Firewall+Matchers+and+Actions
NON_ROUTING_MANGLE = {
    'accept', 'log', 'passthrough', 'return', 'add-dst-to-address-list', 'add-src-to-address-list',
    'change-dscp', 'change-mss', 'change-ttl', 'clear-df', 'fasttrack-connection',
    'mark-connection', 'mark-packet', 'set-priority', 'sniff-pc', 'sniff-tzsp', 'strip-ipv4-options',
}


def _truthy(value: Any) -> bool:
    return value is True or str(value).lower() in ('true', 'yes', '1')


def _enabled(row: Dict[str, Any]) -> bool:
    return not _truthy(row.get('disabled'))


def _network(row: Dict[str, Any]):
    try:
        return ipaddress.IPv4Network(str(row.get('dst-address') or ''), strict=False)
    except ValueError:
        return None


def _interface(value: Any) -> Optional[str]:
    """Accept a single interface name, not an address, list or recursive gateway."""
    value = str(value or '').strip()
    if not value or any(character in value for character in (',', ';', '%', '/', '@')):
        return None
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    return None


class RouteEgressIndex:
    """Parsed NAT route evidence scoped to a single, unchanged device read."""
    def __init__(self, context):
        self.error = ''
        self.main = []
        self.raw = {}
        self._static = None
        self._gateways = {}
        self.nat_selectors = {}
        rows = context.get('routes') if isinstance(context, dict) else None
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            self.error = '无法读取路由出口信息'
            return
        for row in rows:
            if (row.get('routing-table') or 'main') != 'main':
                continue
            network = _network(row)
            if network is None:
                if _enabled(row):
                    self.error = '主路由表包含无法识别的目的地址'
                continue
            self.main.append((row, network))
        self.exact = {network for _, network in self.main}

    def select(self, cidr, gateway):
        if self._static is None:
            entries = []
            for index, (row, network) in enumerate(self.main):
                active = _truthy(row['active']) if 'active' in row else None
                if active is None and 'inactive' in row:
                    active = not _truthy(row['inactive'])
                if _truthy(row.get('inactive')) or _truthy(row.get('invalid')):
                    active = False
                try:
                    distance = int(row['distance'])
                except (KeyError, TypeError, ValueError):
                    distance = None
                key = str(index)
                entries.append(RouteEntry(key, str(network), str(row.get('gateway') or ''), _enabled(row),
                                          active=active, distance=distance))
                self.raw[key] = row
            self._static = StaticRouteIndex(entries)
        return self._static.select(cidr, gateway)

    def gateway_egress(self, next_hop):
        if next_hop not in self._gateways:
            self._gateways[next_hop] = _gateway_egress(next_hop, self.main)
        return self._gateways[next_hop]


def _egress(cidr: str, gateway: str, context: Dict[str, Any], *,
            policy_routing: bool = False, route_index=None) -> Tuple[Optional[str], str]:
    """Resolve static destinations or the next hop of a verified policy table.

    Policy callers validate Mangle priority and their table separately. Its
    gateway uses @main, so only the gateway lookup depends on the main table;
    main-table routes to the traffic's destination do not select its egress.
    """
    try:
        target = ipaddress.IPv4Network(cidr, strict=False)
        next_hop = ipaddress.IPv4Address(gateway)
    except ValueError:
        return None, '代理网段或旁路下一跳不是有效 IPv4 地址'

    for key, label in (('routing_rules', '策略路由'), ('mangle', '路由标记'), ('vrfs', 'VRF')):
        rows = context.get(key)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            return None, f'无法读取 {label}，不能确定旁路出口'
    if any(_enabled(row) for row in context['routing_rules']):
        return None, '存在启用的策略路由，不能仅凭主路由表确定旁路出口'
    # The driver reads the entire mangle collection, including custom chains.
    # Never treat an absent or unrecognized action as proof of no policy route.
    mangle_chains = {row['chain'] for row in context['mangle']
                     if isinstance(row.get('chain'), str) and row['chain']}
    for row in context['mangle']:
        if not _enabled(row):
            continue
        action = row.get('action')
        if not isinstance(action, str) or not action:
            return None, '启用的 mangle 规则缺少有效动作，不能确定旁路出口'
        if action in ('mark-routing', 'route'):
            return None, '存在启用的路由标记或路由动作，不能确定旁路出口'
        if action == 'jump':
            jump_target = row.get('jump-target')
            if not isinstance(jump_target, str) or jump_target not in mangle_chains:
                return None, '无法确认 mangle 跳转目标链的完整规则，不能确定旁路出口'
        elif action not in NON_ROUTING_MANGLE:
            return None, '存在无法识别的 mangle 动作，不能确定旁路出口'
    if any(_enabled(row) and row.get('name') != 'main' for row in context['vrfs']):
        return None, '存在非 main VRF，不能确定旁路出口'

    index = route_index if route_index is not None else RouteEgressIndex(context)
    if index.error:
        return None, index.error
    if policy_routing:
        return index.gateway_egress(next_hop)

    selection = index.select(cidr, str(next_hop))
    if selection.conflict:
        return None, '代理网段存在有效的其他下一跳，不能确定整个网段使用同一旁路出口'
    interfaces = set()
    for entry in selection.selected:
        row = index.raw[entry.id]
        if (entry.active is not True
                or any(_truthy(row.get(key)) for key in ('blackhole', 'invalid', 'ecmp'))):
            return None, '代理路由未生效、无效、属于黑洞路由或包含多个等价出口'
        immediate = str(row.get('immediate-gw') or '')
        parts = immediate.split('%')
        if len(parts) != 2:
            return None, '无法从代理路由确认唯一的实际出口'
        try:
            ipaddress.IPv4Address(parts[0])
        except ValueError:
            return None, '代理路由的实际下一跳无法识别'
        interface = _interface(parts[1])
        if not interface:
            return None, '代理路由包含多个或无法识别的实际出口'
        interfaces.add(interface)

    if not selection.covered:
        if target in index.exact:
            return None, '代理路由未生效，无法确认整个网段的实际出口'
        interface, error = index.gateway_egress(next_hop)
        if interface is None:
            return None, error
        interfaces.add(interface)
    if len(interfaces) != 1:
        return None, '代理网段的有效路由使用不同或无法确认的实际出口'
    return next(iter(interfaces)), ''


def _gateway_egress(next_hop, main):
    # The desired route has not been created. Infer its egress only from one
    # active directly connected route to the gateway, never from a default or
    # recursive route. Competing more-specific routes make this ambiguous.
    covering = [(row, network) for row, network in main
                if _enabled(row) and next_hop in network]
    connected = [(row, network) for row, network in covering
                 if _truthy(row.get('connect')) and _truthy(row.get('active'))
                 and not any(_truthy(row.get(key)) for key in ('blackhole', 'invalid', 'ecmp'))]
    if not connected:
        return None, '代理路由尚未创建，且无法找到旁路下一跳的有效直连路由'
    prefix = max(network.prefixlen for _, network in connected)
    selected = [(row, network) for row, network in covering if network.prefixlen >= prefix]
    if len(selected) != 1 or selected[0] not in connected:
        return None, '旁路下一跳存在多条或更细的路由，无法可靠推断新增路由的出口'
    row = selected[0][0]
    interface = _interface(row.get('gateway'))
    immediate = _interface(row.get('immediate-gw')) if row.get('immediate-gw') else interface
    if not interface or immediate != interface:
        return None, '旁路下一跳的直连路由未给出一致的接口出口'
    return interface, ''


def _list_contains(name: str, interface: str, context: Dict[str, Any]) -> Optional[bool]:
    if name == 'all':
        return True
    if name == 'none':
        return False
    if name in ('static', 'dynamic'):
        return None
    definitions, members = context.get('interface_lists'), context.get('interface_members')
    if not isinstance(definitions, list) or not isinstance(members, list):
        return None
    if any(not isinstance(row, dict) for row in definitions + members):
        return None
    if any(_enabled(row) and (not row.get('list') or not row.get('interface')) for row in members):
        return None
    matching = [row for row in definitions if row.get('name') == name and _enabled(row)]
    if len(matching) != 1 or matching[0].get('include') or matching[0].get('exclude'):
        return None
    if any(_enabled(row) and row.get('list') == name
           and (row.get('interface') == 'all' or str(row.get('interface') or '').startswith('all-'))
           for row in members):
        return None
    return any(_enabled(row) and row.get('list') == name and row.get('interface') == interface
               for row in members)


def _selector_matches(value: Any, interface: str, context: Dict[str, Any], is_list: bool) -> Optional[bool]:
    text = str(value or '').strip()
    if not text:
        return True
    negate = text.startswith('!')
    name = text[1:] if negate else text
    if not name or name.startswith('!'):
        return None
    if interface is None and name not in ('all', 'none'):
        return None
    if is_list:
        result = _list_contains(name, interface, context)
    elif name == 'all':
        result = True
    elif name.startswith('all-'):
        # Legacy firewall interface groups (all-ethernet, all-ppp, etc.)
        # are not literal interface names; interface type evidence is absent.
        result = None
    else:
        result = name == interface
    return not result if negate and result is not None else result


def assess_nat(cidr: str, gateway: str, state, *, policy_routing: bool = False, route_index=None) -> Dict[str, str]:
    """Return ``not_required``, ``missing`` or ``unknown`` for one proxy CIDR.

    ``missing`` means a source-NAT rule may match: source, destination, protocol
    and other matchers are intentionally not simulated. ``not_required`` is
    reserved for an absence of possible translation or proven egress mismatch.
    """
    context = getattr(state, 'nat_context', None)
    if not isinstance(context, dict):
        return {'status': 'unknown', 'detail': '缺少 RouterOS NAT 判断依据，无法确认是否需要免 NAT'}
    rows = context.get('rules')
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return {'status': 'unknown', 'detail': '无法读取完整 NAT 规则，无法确认是否需要免 NAT'}
    if any(_enabled(row) and not row.get('chain') for row in rows):
        return {'status': 'unknown', 'detail': '启用的 NAT 规则缺少链信息，无法排除源地址转换'}
    candidates = [row for row in rows if _enabled(row) and row.get('chain') == 'srcnat'
                  and row.get('action') not in NON_TRANSLATING]
    if not candidates:
        return {'status': 'not_required', 'detail': '没有启用的潜在源 NAT 转换规则，无需额外免 NAT'}

    interface, egress_error = None, None
    unknown, possible = [], []
    for row in candidates:
        restricted = bool(row.get('out-interface') or row.get('out-interface-list'))
        if restricted:
            if egress_error is None:
                interface, egress_error = _egress(cidr, gateway, context, policy_routing=policy_routing,
                                                  route_index=route_index)
            if interface is None:
                unknown.append(egress_error)
                continue
            matches = [_selector_matches(row.get('out-interface'), interface, context, False),
                       _selector_matches(row.get('out-interface-list'), interface, context, True)]
            # Matchers on one rule are conjunctive. A definite mismatch excludes
            # that rule even when the other selector is unsupported.
            if False in matches:
                continue
            if None in matches:
                unknown.append('NAT 出口条件包含无法解析的接口分组或列表，无法排除源地址转换')
                continue
        if row.get('action') not in TRANSLATING:
            unknown.append('存在可能匹配的 NAT 跳转或未知动作，无法确认是否发生源地址转换')
        else:
            possible.append(row)

    if unknown:
        return {'status': 'unknown', 'detail': unknown[0]}
    if possible:
        return {'status': 'missing', 'detail': '存在可能匹配旁路流量的源 NAT 转换规则，需要免 NAT 保护'}
    return {'status': 'not_required',
            'detail': f'旁路出口为 {interface}，与启用的潜在源 NAT 规则均不匹配，无需额外免 NAT'}


def _address_hits(row, field, target, context, cache):
    """Intersect a packet range with both literal and list selectors (AND)."""
    try:
        literal = ipaddress.IPv4Network(row.get(field) or '0.0.0.0/0', strict=False)
    except (ValueError, TypeError):
        return None
    if not target.overlaps(literal):
        return []
    target = target if target.subnet_of(literal) else literal
    name = row.get(field + '-list')
    if not name:
        return [target]
    lists = context.get('address_sets') or {}
    if not isinstance(name, str) or name.startswith('!') or name not in lists:
        return None
    if name not in cache:
        try:
            networks = []
            for value in lists[name]:
                if '-' in value:
                    first, last = value.split('-', 1)
                    networks.extend(ipaddress.summarize_address_range(ipaddress.IPv4Address(first), ipaddress.IPv4Address(last)))
                else:
                    networks.append(ipaddress.IPv4Network(value, strict=False))
            networks = list(ipaddress.collapse_addresses(networks))
            cache[name] = (networks, [int(net.network_address) for net in networks],
                           [int(net.broadcast_address) for net in networks])
        except (ValueError, TypeError):
            cache[name] = None
    if cache[name] is None:
        return None
    networks, starts, ends = cache[name]
    left, right = bisect_left(ends, int(target.network_address)), bisect_right(starts, int(target.broadcast_address))
    return [target if target.subnet_of(net) else net for net in networks[left:right]]


def preserves_sources(rows, sources, destinations, interface, context, *, accepted_ids=None, selector_cache=None):
    """Prove no source translation for every source/destination pair, in order.

    Literal addresses, address lists and proven egress selectors can discharge
    coverage. Other matchers cannot establish an unconditional exemption.
    """
    pending = [(source, destination) for source in sources for destination in destinations]
    cache = selector_cache if selector_cache is not None else {}
    fields = {'.id', 'chain', 'action', 'disabled', 'comment', 'log', 'log-prefix',
              'bytes', 'packets', 'dynamic', 'invalid', 'src-address', 'dst-address',
              'src-address-list', 'dst-address-list', 'out-interface', 'out-interface-list'}
    for row in rows:
        if not pending:
            return True
        if not _enabled(row) or row.get('chain') not in ('srcnat', None):
            continue
        egress_match = _selector_matches(row.get('out-interface'), interface, context, False)
        list_match = _selector_matches(row.get('out-interface-list'), interface, context, True)
        if egress_match is False or list_match is False:
            continue
        affected = []
        for src, dst in pending:
            sources_hit = _address_hits(row, 'src-address', src, context, cache)
            destinations_hit = _address_hits(row, 'dst-address', dst, context, cache)
            if sources_hit != [] and destinations_hit != []:
                affected.append((src, dst, sources_hit, destinations_hit))
        if not affected:
            continue
        action = row.get('action')
        if action in ('log', 'passthrough'):
            continue
        if action != 'accept':
            return False
        if (not row.get('chain') or set(row) - fields or _truthy(row.get('invalid'))
                or _truthy(row.get('dynamic')) or egress_match is not True or list_match is not True):
            continue
        before = pending
        pending = list(pending)
        for src, dst, sources_hit, destinations_hit in affected:
            if sources_hit is None or destinations_hit is None:
                continue
            pieces = [(src, dst)]
            for source in sources_hit:
                for destination in destinations_hit:
                    remainder = []
                    for a, b in pieces:
                        if not a.overlaps(source) or not b.overlaps(destination):
                            remainder.append((a, b))
                            continue
                        hit_a = a if a.subnet_of(source) else source
                        hit_b = b if b.subnet_of(destination) else destination
                        remainder.extend((part, b) for part in a.address_exclude(hit_a))
                        remainder.extend((hit_a, part) for part in b.address_exclude(hit_b))
                    pieces = remainder
            pending.remove((src, dst))
            pending.extend(pieces)
        if accepted_ids is not None and pending != before:
            accepted_ids.append(str(row.get('.id', '')))
    return True


def client_source_networks(gateway):
    """The same existing /24 host scope used when creating a new exemption."""
    address = ipaddress.IPv4Address(gateway)
    network = ipaddress.IPv4Network(f'{gateway}/24', strict=False)
    result = []
    if address > network.network_address + 1:
        result.extend(ipaddress.summarize_address_range(network.network_address + 1, address - 1))
    if address < network.broadcast_address - 1:
        result.extend(ipaddress.summarize_address_range(address + 1, network.broadcast_address - 1))
    return result


def assess_bypass(cidr, gateway, state, *, route_index=None):
    """Read the actual ordered chain before claiming an exemption is effective.

    An accept with unsupported matchers cannot prove all intended clients are
    protected. Keep it intact and report the existing 'unknown' state instead
    of silently broadening it or treating its destination alone as coverage.
    """
    from backend.routers.base import is_managed

    context = {**(state.nat_context or {}), 'address_sets': getattr(state, 'address_sets', {})}
    assessment = assess_nat(cidr, gateway, state, route_index=route_index)
    if assessment['status'] == 'not_required':
        return {**assessment, 'rule_ids': []}
    rows = context.get('rules') if isinstance(context, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return {**assessment, 'rule_ids': []}
    interface = None
    if any(row.get('out-interface') or row.get('out-interface-list') for row in rows):
        interface, _ = _egress(cidr, gateway, context, route_index=route_index)
    try:
        sources = client_source_networks(gateway)
        destination = ipaddress.IPv4Network(cidr)
    except ValueError:
        return {'status': 'unknown', 'detail': '无法确认 NAT 免转换的客户端范围', 'rule_ids': []}
    accepted_ids = []
    cache = route_index.nat_selectors if route_index is not None else {}
    if preserves_sources(rows, sources, [destination], interface, context, accepted_ids=accepted_ids, selector_cache=cache):
        accepted = [row for row in rows if str(row.get('.id', '')) in accepted_ids]
        if not accepted:
            return {'status': 'not_required', 'detail': '客户端流量不会命中源 NAT 转换规则，无需额外免 NAT', 'rule_ids': []}
        managed = all(is_managed(row.get('comment')) for row in accepted)
        return {'status': 'synced' if managed else 'adoptable', 'rule_ids': accepted_ids,
                'detail': '' if managed else '现有免转换规则可保护客户端流量，将直接接管'}
    retiring = {item['id'] for op in (getattr(state, 'ros_policy_plan', None) or {}).get('ops', [])
                for item in op.get('owned', []) if item['path'] == '/ip/firewall/nat'}
    # ConfigFlow's list rule, intact and first, is maintained rather than
    # preserved: a stale source list (the gateway moved) or a member narrower
    # than this CIDR makes it missing, and the plan rewrites the members.
    own = _list_rule(rows, route_index)
    if own is not None and (not own['out_interface'] or own['out_interface'] == interface):
        retiring = retiring | {own['rule'].get('.id')}
    # An existing exemption with any potentially overlapping destination must
    # be preserved. Do not compensate for its restrictions with a broader rule.
    for row in rows:
        if (not _enabled(row) or row.get('chain') != 'srcnat' or row.get('action') != 'accept'
                or row.get('.id') in retiring):
            continue
        # A literal target combined with a narrower list is still an existing
        # restriction on this target; cloning must not silently discard it.
        selectors = {key: value for key, value in row.items() if key != 'dst-address-list'} if row.get('dst-address') else row
        if _address_hits(selectors, 'dst-address', destination, context, cache) != []:
            return {'status': 'unknown', 'rule_ids': [],
                    'detail': '已有免转换规则的顺序或来源、地址列表、接口、协议等条件不能保证客户端流量均免转换；请先检查现有规则'}
    return {**assessment, 'rule_ids': []}


def _list_rule(rows, route_index):
    """The valid ConfigFlow list rule of this read, once per route index."""
    from backend.routers.routeros_nat_list import inspect_rule
    cached = getattr(route_index, 'nat_list_rule', False) if route_index is not None else False
    if cached is not False and cached[0] is rows:
        return cached[1]
    found = inspect_rule(rows)
    own = found if found['rule'] is not None and not found['problem'] else None
    if route_index is not None:
        route_index.nat_list_rule = (rows, own)
    return own


def planned_bypass_conflicts(operations, state, gateway):
    """Check expanded copies without changing or broadening their matchers."""
    import copy
    from types import SimpleNamespace

    expanding = [op for op in operations if op['kind'] == 'nat_update' and op.get('add_dst')]
    if not expanding:
        return []
    context = {**state.nat_context, 'rules': copy.deepcopy(state.nat_context['rules'])}
    rows = context['rules']
    targets = set()
    for op in expanding:
        original = next(row for row in rows if str(row.get('.id')) == op['id'])
        for cidr in op['add_dst']:
            clone = {**original, '.id': f'preview:{op["id"]}:{cidr}',
                     'dst-address': cidr, 'comment': op['comment']}
            rows.insert(rows.index(original), clone)
            targets.add(cidr)
    virtual = SimpleNamespace(nat_context=context, address_sets=state.address_sets)
    index = RouteEgressIndex(context)
    return [f'{cidr}：扩充后仍无法确认 NAT 免转换覆盖，已保留原规则的限制，请先检查现有规则'
            for cidr in sorted(targets)
            if assess_bypass(cidr, gateway, virtual, route_index=index)['status'] not in ('synced', 'adoptable', 'not_required')]
