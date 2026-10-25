"""Drift report: compare the desired side-gateway state with what the router has.

Statuses per desired item:
- ``synced``     managed by ConfigFlow and correct
- ``adoptable``  correct but created manually; P1 adopts it (tags it) instead of recreating
- ``conflict``   configured routes differ or effective forwarding uses another gateway
- ``missing``    not present
- ``not_required`` ROS forwarding cannot match any potential source-NAT rule
- ``unknown``    ROS source-NAT requirement cannot be safely determined
Managed objects that are no longer desired are reported as ``extra``.
"""
import ipaddress
from typing import Any, Dict, Iterable, List, Set, Tuple

from backend.routers.base import (
    POLICY_AUTO_COMMENT, POLICY_MANUAL_COMMENT, RouterState, as_dict, is_managed, policy_route, policy_route_ok,
    StaticRouteIndex,
)
from backend.routers.dhcp_dns import build_dns_report
from backend.routers.routeros_nat import assess_bypass, RouteEgressIndex


def _networks(values: Iterable[str]) -> List[ipaddress.IPv4Network]:
    """Parse CIDRs, single IPs and ``a-b`` ranges (iKuai style) into IPv4 networks."""
    result = []
    for value in values:
        text = str(value).strip()
        try:
            if '-' in text:
                start, end = (ipaddress.IPv4Address(part.strip()) for part in text.split('-', 1))
                result.extend(ipaddress.summarize_address_range(start, end))
            else:
                network = ipaddress.ip_network(text, strict=False)
                if isinstance(network, ipaddress.IPv4Network):
                    result.append(network)
        except ValueError:
            continue
    return result


class SupernetIndex:
    """Answers ``target.subnet_of(network)`` against many networks at once.

    A network containing ``target`` is one of its supernets (at most 33 for
    IPv4), so a lookup hashes those instead of scanning every network: large
    rule sets made the pairwise scan quadratic.  Each network may carry owner
    positions, e.g. the rule it came from.
    """

    def __init__(self, networks: Iterable[ipaddress.IPv4Network] = ()):
        self._owners: Dict[Tuple[int, int, int], Set[int]] = {}
        self._prefixlens: Dict[int, List[int]] = {}
        self.add(networks)

    def add(self, networks: Iterable[ipaddress.IPv4Network], owner: int = 0) -> None:
        for network in networks:
            key = (network.version, int(network.network_address), network.prefixlen)
            self._owners.setdefault(key, set()).add(owner)
            lengths = self._prefixlens.setdefault(network.version, [])
            if network.prefixlen not in lengths:
                lengths.append(network.prefixlen)
                lengths.sort()

    def _supernet_keys(self, target: ipaddress.IPv4Network):
        address, width = int(target.network_address), target.max_prefixlen
        for prefixlen in self._prefixlens.get(target.version, ()):
            if prefixlen > target.prefixlen:
                break
            mask = ((1 << width) - 1) ^ ((1 << (width - prefixlen)) - 1)
            yield target.version, address & mask, prefixlen

    def owners(self, target: ipaddress.IPv4Network) -> Set[int]:
        """Owners of every indexed network that contains ``target``."""
        found: Set[int] = set()
        for key in self._supernet_keys(target):
            found.update(self._owners.get(key, ()))
        return found

    def covers(self, target: ipaddress.IPv4Network) -> bool:
        return any(key in self._owners for key in self._supernet_keys(target))


def _policy_route_items(desired: Dict[str, Any], state: RouterState) -> Dict[str, Any]:
    """Proxy CIDRs routed by a policy rule that excludes the gateway as a source.

    Every enabled static route to the gateway is loop-prone: it is migrated into
    a policy rule and then deleted, including hand-made ones (kept in their own
    add-only rule, ``cf-mihomo-manual``).
    """
    gateway = desired.get('proxy_gateway') or ''
    desired_cidrs = {entry['cidr'] for entry in desired['proxy_cidrs']}
    auto = policy_route(state, POLICY_AUTO_COMMENT)
    covered = SupernetIndex(_networks(auto.dst) if auto and policy_route_ok(auto, gateway) else [])
    # A disabled static route forwards nothing, so the policy rule decides: leave it be.
    enabled_by_dst: Dict[str, List[Any]] = {}
    for route in state.routes:
        if route.enabled:
            enabled_by_dst.setdefault(route.dst, []).append(route)
    items = []
    for entry in desired['proxy_cidrs']:
        target = ipaddress.IPv4Network(entry['cidr'])
        matches = enabled_by_dst.get(entry['cidr'], [])
        bad = [route for route in matches if not (gateway and route.gateway == gateway)]
        if bad:
            status, detail = 'conflict', f'同网段路由下一跳为 {bad[0].gateway}，期望下一跳 {gateway or "未知"}'
        elif matches:
            status, detail = 'adoptable', '静态路由会让 mihomo 自身的流量回环，将迁移为排除 mihomo 的策略路由'
        elif covered.covers(target):
            status, detail = 'synced', ''
        else:
            status, detail = 'missing', ''
        items.append({'cidr': entry['cidr'], 'sources': entry['sources'], 'status': status, 'detail': detail,
                      'route_ids': [route.id for route in matches]})
    extra = [{'cidr': route.dst, 'gateway': route.gateway, 'route_id': route.id, 'comment': route.comment}
             for route in state.routes if is_managed(route.comment) and route.dst not in desired_cidrs]
    if auto:
        extra += [{'cidr': cidr, 'gateway': auto.gateway, 'route_id': f'policy:{auto.id}', 'comment': auto.comment}
                  for cidr in auto.dst if cidr not in desired_cidrs]
    unmanaged_other = [{'cidr': route.dst, 'gateway': route.gateway, 'comment': route.comment, 'enabled': route.enabled,
                        'migrate': route.enabled}
                       for route in state.routes
                       if gateway and route.gateway == gateway and not is_managed(route.comment)
                       and route.dst not in desired_cidrs]
    manual = policy_route(state, POLICY_MANUAL_COMMENT)
    return {'items': items, 'extra': extra, 'unmanaged_other': unmanaged_other, 'mode': 'policy',
            'policy': {'auto': as_dict(auto) if auto else None, 'manual': as_dict(manual) if manual else None}}


def _route_items(desired: Dict[str, Any], state: RouterState) -> Dict[str, Any]:
    if state.policy_routes is not None:
        return _policy_route_items(desired, state)
    gateway = desired.get('proxy_gateway') or ''
    desired_cidrs = {entry['cidr'] for entry in desired['proxy_cidrs']}
    items = []
    required_route_ids = set()
    route_index = StaticRouteIndex(state.routes)
    for entry in desired['proxy_cidrs']:
        matches = route_index.exact.get(entry['cidr'], [])
        selection = route_index.select(entry['cidr'], gateway)
        good = selection.candidates
        required_route_ids.update(route.id for route in good)
        if good and all(is_managed(route.comment) for route in good):
            status, detail = 'synced', ''
        elif good:
            status, detail = 'adoptable', '已有手工路由提供完整有效覆盖，将直接接管'
        elif matches:
            route = matches[0]
            reason = '已停用' if not route.enabled else f'下一跳为 {route.gateway}'
            if any(route.enabled and route.gateway == gateway for route in matches):
                reason = '未成为有效主路由，或存在可优先/等价转发到其他下一跳的路由'
            status, detail = 'conflict', f'同网段路由{reason}，期望下一跳 {gateway or "未知"}'
        elif selection.conflict:
            status, detail = 'conflict', f'更细网段存在可转发到其他下一跳的路由，期望下一跳 {gateway or "未知"}'
        else:
            status, detail = 'missing', ''
        items.append({'cidr': entry['cidr'], 'sources': entry['sources'], 'status': status, 'detail': detail,
                      'route_ids': [route.id for route in matches]})
    extra = [{'cidr': route.dst, 'gateway': route.gateway, 'route_id': route.id, 'comment': route.comment}
             for route in state.routes
             if route.table == 'main' and is_managed(route.comment) and route.dst not in desired_cidrs
             and route.id not in required_route_ids
             # Policy ownership is established by its manifest, not its name.
             and not route.comment.startswith('cf-ros-policy-')]
    # Manual routes to the proxy gateway that ConfigFlow does not derive (e.g. public DNS
    # addresses routed for TUN DNS hijack) are informational, never touched.
    unmanaged_other = [{'cidr': route.dst, 'gateway': route.gateway, 'comment': route.comment, 'enabled': route.enabled}
                       for route in state.routes
                       if route.table == 'main' and gateway and route.gateway == gateway and not is_managed(route.comment)
                       and route.dst not in desired_cidrs]
    return {'items': items, 'extra': extra, 'unmanaged_other': unmanaged_other}


def _nat_items(desired: Dict[str, Any], state: RouterState) -> Dict[str, Any]:
    rules = [rule for rule in state.nat_bypass if rule.enabled]
    route_index = RouteEgressIndex(state.nat_context)
    cover_index = None
    items = []
    for entry in desired['proxy_cidrs']:
        if state.nat_context is not None:
            assessment = assess_bypass(entry['cidr'], desired.get('proxy_gateway') or '', state,
                                       route_index=route_index)
            status, detail = assessment['status'], assessment['detail']
            rule_ids = assessment['rule_ids']
        else:
            target = ipaddress.IPv4Network(entry['cidr'])
            if cover_index is None:
                # Parse each rule's destinations once, not once per CIDR. RouterOS
                # (nat_context set) proves coverage with its raw selectors instead.
                cover_index = SupernetIndex()
                for position, rule in enumerate(rules):
                    cover_index.add(_networks(rule.dst), position)
            covering = [rules[position] for position in sorted(cover_index.owners(target))]
            if any(is_managed(rule.comment) for rule in covering):
                status, detail = 'synced', ''
                rule_ids = [rule.id for rule in covering]
            elif covering:
                status, detail = 'adoptable', f'已被手工规则「{covering[0].comment or covering[0].id}」覆盖，将直接接管'
                rule_ids = [rule.id for rule in covering]
            else:
                status, detail = 'missing', '未发现覆盖该网段的 NAT 免转换规则，需结合源 NAT 配置确认是否影响客户端 IP'
                rule_ids = []
        items.append({'cidr': entry['cidr'], 'status': status, 'detail': detail, 'rule_ids': rule_ids})
    disabled = [{'rule_id': rule.id, 'comment': rule.comment, 'dst': rule.dst}
                for rule in state.nat_bypass if not rule.enabled]
    return {'items': items, 'disabled_rules': disabled}


def build_report(desired: Dict[str, Any], state: RouterState) -> Dict[str, Any]:
    routes = _route_items(desired, state)
    if 'ros_policy' in desired:
        from backend.routers.ros_policy import build, ROUTES
        state.ros_policy_plan = build(desired, state)
        check = state.ros_policy_plan.get('check') or {}
        migrating = {item['id'] for item in check.get('migrations', []) if item['path'] == ROUTES}
        # Migration owns exact IDs, including superseded aggregates. All other
        # obsolete managed main routes remain in ordinary static cleanup.
        routes['extra'] = [item for item in routes['extra'] if item['route_id'] not in migrating]
    # Policy rules being withdrawn because the router routes by static routes again.
    routes['retired_policy'] = [{'cidr': cidr, 'gateway': entry.gateway, 'comment': entry.comment,
                                 'restore': entry.comment == POLICY_MANUAL_COMMENT}
                                for entry in state.retired_policy_routes or [] for cidr in entry.dst]
    nat = _nat_items(desired, state)
    dns = build_dns_report(desired, state.dhcp_dns)
    summary: Dict[str, int] = {}
    for section in (routes['items'], nat['items']):
        for item in section:
            summary[item['status']] = summary.get(item['status'], 0) + 1
    summary['extra'] = len(routes['extra'])
    if dns['status'] != 'disabled':
        summary[dns['status']] = summary.get(dns['status'], 0) + 1
    in_sync = all(item['status'] in {'synced', 'not_required'}
                  for item in routes['items'] + nat['items']) and not routes['extra']
    in_sync = in_sync and not any(item.get('migrate') for item in routes['unmanaged_other'])
    in_sync = in_sync and not routes['retired_policy']
    in_sync = in_sync and dns['status'] in {'disabled', 'synced'}
    result = {'desired': {key: value for key, value in desired.items() if key != 'ros_policy'},
              'routes': routes, 'nat_bypass': nat, 'dhcp_dns': dns, 'summary': summary, 'in_sync': in_sync}
    if state.ros_policy_plan is not None:
        policy = state.ros_policy_plan
        result['policy_routing'] = {key: policy[key] for key in ('items', 'blockers', 'warnings')}
        result['policy_routing']['sources'] = desired['ros_policy']['sources']
        result['policy_routing']['enabled'] = bool(desired['ros_policy']['selected'])
        for item in policy['items']:
            summary[item['status']] = summary.get(item['status'], 0) + 1
        if policy['blockers']:
            summary['unknown'] = summary.get('unknown', 0) + len(policy['blockers'])
        if policy['ops'] and not policy['items']:
            summary['pending'] = summary.get('pending', 0) + 1
        result['in_sync'] = in_sync and not policy['ops'] and not policy['blockers']
    if 'dns_guard' in desired:
        from backend.routers.dns_guard import build as build_guard, report as guard_report
        state.dns_guard_plan = build_guard(desired, state)
        guard = result['dns_guard'] = guard_report(state.dns_guard_plan)
        if guard['status'] != 'disabled':
            summary[guard['status']] = summary.get(guard['status'], 0) + 1
        result['in_sync'] = (result['in_sync'] and guard['status'] in {'disabled', 'synced'}
                             and not guard['blockers'] and not state.dns_guard_plan['ops'])
    result['desired'].pop('dns_guard', None)
    if state.ikuai_groups is not None:
        from backend.routers.ikuai_groups import report_section
        groups = result['ikuai_groups'] = report_section(state)
        # Rules still listing CIDRs themselves, or groups nothing uses: a release is due.
        due = len(groups['migrations']) + len(groups['orphans'])
        if due:
            summary['pending'] = summary.get('pending', 0) + due
        result['in_sync'] = result['in_sync'] and not due
    return result
