"""Conservatively assess whether a RouterOS proxy path needs source-NAT bypass.

This is deliberately not a firewall simulator. Only a proven egress mismatch
excludes a potentially translating rule; other packet matchers remain possible.
"""
import ipaddress
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


def preserves_sources(rows, sources, destinations, interface, context):
    """Prove no source translation for every source/destination pair, in order.

    Only literal address and proven egress selectors can discharge coverage.
    Other matchers never establish an unconditional exemption. An unsupported
    translating/jump rule remains a possible match and prevents the proof.
    """
    pending = [(source, destination) for source in sources for destination in destinations]
    fields = {'.id', 'chain', 'action', 'disabled', 'comment', 'log', 'log-prefix',
              'bytes', 'packets', 'dynamic', 'invalid', 'src-address', 'dst-address',
              'out-interface'}
    for row in rows:
        if not pending:
            return True
        if not _enabled(row) or row.get('chain') not in ('srcnat', None):
            continue
        egress_match = _selector_matches(row.get('out-interface'), interface, context, False)
        list_match = _selector_matches(row.get('out-interface-list'), interface, context, True)
        if egress_match is False or list_match is False:
            continue
        try:
            source = ipaddress.IPv4Network(row.get('src-address') or '0.0.0.0/0', strict=False)
            destination = ipaddress.IPv4Network(row.get('dst-address') or '0.0.0.0/0', strict=False)
        except (ValueError, TypeError):
            source = destination = None
        affected = [(src, dst) for src, dst in pending
                    if source is None or (src.overlaps(source) and dst.overlaps(destination))]
        if not affected:
            continue
        action = row.get('action')
        if action in ('log', 'passthrough'):
            continue
        if action != 'accept':
            return False
        if (source is None or not row.get('chain') or set(row) - fields
                or _truthy(row.get('invalid')) or _truthy(row.get('dynamic'))
                or egress_match is not True):
            continue
        remainder = []
        for src, dst in pending:
            if not src.overlaps(source) or not dst.overlaps(destination):
                remainder.append((src, dst))
                continue
            src_hit = src if src.subnet_of(source) else source
            dst_hit = dst if dst.subnet_of(destination) else destination
            remainder.extend((part, dst) for part in src.address_exclude(src_hit))
            remainder.extend((src_hit, part) for part in dst.address_exclude(dst_hit))
        pending = remainder
    # Uncovered packets are safe only if no possibly matching translation or
    # unsupported control flow was encountered on their path through the chain.
    return True
