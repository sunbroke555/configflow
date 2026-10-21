"""Common types for upstream router drivers.

Drivers probe the device and return the objects relevant to side-gateway
deployments, normalized to a vendor-neutral shape so the drift report does not
need to know router specifics. Writable drivers apply release operations (see
``backend.routers.plan``) one at a time and return the operation that undoes it.
"""
import ipaddress
from bisect import bisect_left, bisect_right
from heapq import heappop, heappush
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Prefix used by generated deployments. Unmarked objects are only adopted
# during a release or changed through an explicitly confirmed manual edit.
MANAGED_PREFIX = 'cf'


class RouterError(Exception):
    """A router request failed; the message is safe to show to users."""


class RouterConflictError(RouterError):
    """The route changed or cannot be edited; a fresh preview is required."""


class RouterRollbackError(RouterError):
    """A driver could not fully compensate its partially applied operation."""


@dataclass
class RouterInfo:
    vendor: str
    version: str
    model: str = ''
    capabilities: List[str] = field(default_factory=list)


@dataclass
class RouteEntry:
    id: str
    dst: str            # CIDR, e.g. 198.18.0.0/16
    gateway: str
    enabled: bool
    comment: str = ''
    table: str = 'main'
    # None means this driver/API response did not supply runtime evidence.
    active: Optional[bool] = None
    distance: Optional[int] = None


@dataclass
class StaticRouteSelection:
    """Forwarding evidence and the minimal correct route ownership proof.

    ``candidates`` must ALL be owned to verify a deployment. It never includes
    a wrong aggregate merely because correct more-specific routes shadow it.
    ``selected`` includes every potentially forwarding route for NAT evidence.
    """
    candidates: List[RouteEntry] = field(default_factory=list)
    selected: List[RouteEntry] = field(default_factory=list)
    covered: bool = False
    conflict: bool = False


def _preferred_routes(routes: List[RouteEntry]) -> List[RouteEntry]:
    live = [route for route in routes if route.active is not False]
    if not live:
        return []
    active = [route for route in live if route.active is True]
    distance = min((route.distance for route in active or live if route.distance is not None),
                   default=None)
    # Active alternatives remain possible even when preference contradicts
    # runtime evidence. Inactive equal/lower-distance siblings are likewise
    # ambiguous; an inactive more-specific prefix alone cannot forward.
    return [route for route in routes
            if route.active is True
            or (route.distance is not None and distance is not None and route.distance <= distance)
            or (route.active is not False and (route.distance is None or distance is None))]


class StaticRouteIndex:
    """One readback's parsed routes; callers discard it after their batch.

    Sorted network starts restrict each lookup to possible subnets. Neither
    this index nor its selections are cached across device reads or writes.
    """
    def __init__(self, routes: List[RouteEntry]):
        self.exact = {}
        self.groups = {}
        for route in routes:
            if route.table != 'main':
                continue
            self.exact.setdefault(route.dst, []).append(route)
            if not route.enabled:
                continue
            try:
                network = ipaddress.ip_network(route.dst, strict=False)
            except ValueError:
                continue
            self.groups.setdefault(network, []).append(route)
        self.preferred = {network: _preferred_routes(group) for network, group in self.groups.items()}
        self.networks = {
            version: sorted((network for network in self.groups if network.version == version),
                            key=lambda network: (int(network.network_address), network.prefixlen))
            for version in (4, 6)}
        self.starts = {version: [int(network.network_address) for network in networks]
                       for version, networks in self.networks.items()}

    def select(self, cidr: str, gateway: str) -> StaticRouteSelection:
        target = ipaddress.ip_network(cidr, strict=False)
        starts = self.starts[target.version]
        left = bisect_left(starts, int(target.network_address))
        right = bisect_right(starts, int(target.broadcast_address))
        networks = [network for network in self.networks[target.version][left:right]
                    if network.prefixlen >= target.prefixlen]
        groups = {network: self.groups[network] for network in networks}
        exact = groups.get(target, [])
        result = StaticRouteSelection()
        if not any(route.active is not None or route.distance is not None
                   for group in groups.values() for route in group):
            good = [route for route in exact if gateway and route.gateway == gateway]
            if good:
                result.candidates = [next((route for route in good if is_managed(route.comment)), good[0])]
                result.selected = good
                result.covered = True
            return result

        preferred = {network: self.preferred[network] for network in networks if self.preferred[network]}
        boundaries = {int(target.network_address), int(target.broadcast_address) + 1}
        intervals = []
        for network in preferred:
            start, end = int(network.network_address), int(network.broadcast_address)
            boundaries.update((start, end + 1))
            intervals.append((start, end, network))
        selected_groups = {}
        result.covered = True
        active, cursor = [], 0
        for start in sorted(boundaries)[:-1]:
            while cursor < len(intervals) and intervals[cursor][0] <= start:
                _, end, network = intervals[cursor]
                heappush(active, (-network.prefixlen, end, cursor, network))
                cursor += 1
            while active and active[0][1] < start:
                heappop(active)
            if not active:
                result.covered = False
                continue
            network = active[0][3]
            selected_groups[network] = preferred[network]
        result.selected = [route for group in selected_groups.values() for route in group]
        result.conflict = any(not gateway or route.gateway != gateway for route in result.selected)
        if result.covered and not result.conflict:
            # Own the effective exact anchor, or the full more-specific cover.
            # A wrong aggregate shadowed by correct subnets is never adopted.
            anchors = preferred.get(target, [])
            ownership = [anchors] if anchors and all(route.gateway == gateway for route in anchors) else selected_groups.values()
            for group in ownership:
                live = [route for route in group if route.active is True]
                if not live:
                    live = [route for route in group if route.active is not False]
                result.candidates.append(next((route for route in live if is_managed(route.comment)), live[0]))
        return result


def static_route_selection(routes: List[RouteEntry], cidr: str, gateway: str) -> StaticRouteSelection:
    """Single lookup; batch callers reuse a StaticRouteIndex for their snapshot."""
    return StaticRouteIndex(routes).select(cidr, gateway)


def static_route_candidates(routes: List[RouteEntry], cidr: str, gateway: str) -> List[RouteEntry]:
    """Return the complete ownership proof, empty for incomplete/unsafe coverage."""
    return static_route_selection(routes, cidr, gateway).candidates


@dataclass
class PolicyRouteEntry:
    """A source-aware forwarding rule (iKuai 端口分流 with a next-hop gateway).

    Static routes match destinations only, so the gateway's own traffic to a
    proxy CIDR would be routed straight back to it. A policy route can exclude
    the gateway as a source and avoid that loop.
    """
    id: str
    dst: List[str]
    gateway: str        # next hop; empty when the rule targets a WAN line
    enabled: bool
    comment: str = ''
    src: List[str] = field(default_factory=list)
    src_invert: bool = False
    dst_invert: bool = False
    protocol: str = ''
    priority: Optional[int] = None


@dataclass
class NatBypassEntry:
    """A rule that exempts traffic from source NAT (iKuai 'filter', RouterOS srcnat accept)."""
    id: str
    dst: List[str]      # destination CIDRs/ranges; empty means any
    src: List[str]
    enabled: bool
    comment: str = ''
    # Vendor matchers used to preserve source scopes and detect preview drift.
    attributes: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DhcpDnsEntry:
    id: str
    network: str
    dns: List[str]
    enabled: bool = True
    attributes: Dict[str, Any] = field(default_factory=dict)
    blockers: List[str] = field(default_factory=list)
    # False when a vendor's empty DNS slot could inherit an unlisted server.
    dns_explicit: bool = True


@dataclass
class DhcpDnsState:
    entries: List[DhcpDnsEntry] = field(default_factory=list)
    evidence: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RouterState:
    routes: List[RouteEntry] = field(default_factory=list)
    nat_bypass: List[NatBypassEntry] = field(default_factory=list)
    # Named address sets (iKuai route objects / RouterOS address-lists) -> CIDRs.
    address_sets: Dict[str, List[str]] = field(default_factory=dict)
    # ROS forwarding/NAT evidence. None keeps legacy drivers' coverage checks;
    # an empty rules list explicitly means there are no source-NAT rules.
    nat_context: Optional[Dict[str, Any]] = None
    dhcp_dns: Optional[DhcpDnsState] = None
    # None: the vendor routes proxy CIDRs with static routes. A list (possibly
    # empty) means proxy CIDRs are routed by policy rules excluding the gateway.
    policy_routes: Optional[List[PolicyRouteEntry]] = None
    # A list (possibly empty): the router supports policy rules but routes by
    # static routes, and these rules of ours are withdrawn (retire_policy_routes).
    retired_policy_routes: Optional[List[PolicyRouteEntry]] = None
    # Routes the router is actually forwarding with (iKuai 当前路由表). None when
    # the vendor cannot report them; used to find kernel routes without config.
    active_routes: Optional[List[RouteEntry]] = None
    ros_policy: Optional[Dict[str, Any]] = None
    ros_policy_plan: Optional[Dict[str, Any]] = None
    # RouterOS DNS guard: live rows by REST path, and the plan built from them.
    dns_guard: Optional[Dict[str, Any]] = None
    dns_guard_plan: Optional[Dict[str, Any]] = None


class RouterDriver:
    vendor = ''
    # False: router stages are exported as a script for the user to import.
    supports_write = False

    def probe(self) -> RouterInfo:
        raise NotImplementedError

    def read_state(self) -> RouterState:
        raise NotImplementedError

    def read_dhcp_dns(self) -> DhcpDnsState:
        """Read DHCP DNS only when the user selected a DNS Agent."""
        raise RouterError(f'{self.vendor} 不支持 DHCP DNS 下发')

    def apply(self, op: Dict[str, Any]) -> Dict[str, Any]:
        """Apply one release operation; return the operation that reverts it."""
        raise RouterError(f'{self.vendor} 不支持直接写入')

    def render_script(self, ops: List[Dict[str, Any]]) -> str:
        return ''

    def close(self) -> None:
        pass


def is_managed(comment: Optional[str]) -> bool:
    text = (comment or '').strip().lower()
    return text == MANAGED_PREFIX or text.startswith(MANAGED_PREFIX + ':') or text.startswith(MANAGED_PREFIX + '-')


def managed_comment(comment: Optional[str]) -> str:
    """Tag a comment as managed, keeping what the user wrote: ``tg`` -> ``cf-tg``.

    ``-`` rather than ``:``: router UIs restrict comment characters and every
    existing comment proves ``-`` is accepted.
    """
    text = (comment or '').strip()
    return text if is_managed(text) else (f'{MANAGED_PREFIX}-{text}' if text else MANAGED_PREFIX)


# Policy routes for the proxy gateway: derived CIDRs are fully managed; CIDRs
# migrated from hand-made static routes are only ever added, never removed.
POLICY_AUTO_COMMENT = f'{MANAGED_PREFIX}-mihomo'
POLICY_MANUAL_COMMENT = f'{MANAGED_PREFIX}-mihomo-manual'
# Hand-made CIDRs restored from POLICY_MANUAL_COMMENT stay hand-made: no cf marker.
RESTORED_COMMENT = 'mihomo-manual'
# Highest 端口分流 priority, so no other 分流 rule (multi-WAN, protocol) can send
# proxy traffic out a WAN line instead of to the gateway.
POLICY_PRIORITY = 0


def policy_route_ok(entry: 'PolicyRouteEntry', gateway: str) -> bool:
    """Enabled, any protocol, every source except the gateway, next hop the gateway."""
    return (entry.enabled and bool(gateway) and entry.gateway == gateway and entry.src == [gateway]
            and entry.src_invert and not entry.dst_invert and entry.protocol == 'any'
            and entry.priority == POLICY_PRIORITY)


def policy_route(state: 'RouterState', comment: str) -> Optional['PolicyRouteEntry']:
    """The first policy route carrying ``comment``; duplicates are left alone."""
    return next((entry for entry in state.policy_routes or [] if entry.comment == comment), None)


def retire_policy_routes(state: 'RouterState') -> None:
    """Route proxy CIDRs by static routes and withdraw our policy rules.

    Policy rules need managed return routing: with NAT bypass, mihomo answers
    clients directly, and iKuai stops forwarding a 端口分流 connection whose
    replies it never sees (verified on 4.0.314). Static routes tolerate that.
    """
    if state.policy_routes is None:
        return
    state.retired_policy_routes = [entry for entry in state.policy_routes
                                   if entry.comment in (POLICY_AUTO_COMMENT, POLICY_MANUAL_COMMENT)]
    state.policy_routes = None


def stale_routes(state: 'RouterState', gateway: str) -> List['RouteEntry']:
    """Active routes to the gateway that no configured static route explains.

    iKuai can keep a kernel route after its static route is edited or deleted.
    Static routes take precedence over 端口分流, so such a leftover silently
    brings back the loop the policy rules exist to prevent.
    """
    if not gateway or state.active_routes is None:
        return []
    configured = {route.dst for route in state.routes if route.enabled and route.gateway == gateway}
    return [route for route in state.active_routes if route.gateway == gateway and route.dst not in configured]


def stale_route_warning(routes: List['RouteEntry'], gateway: str) -> str:
    return (f'路由器当前路由表中有 {len(routes)} 条指向 {gateway} 的路由不在静态路由配置里'
            f'（{", ".join(route.dst for route in routes)}）：这是修改或删除静态路由后残留的内核路由，'
            f'优先于分流规则，会让 {gateway} 自身的流量回环。界面和接口都无法删除，需要重启路由器清除。')


def as_dict(value: Any) -> Any:
    from dataclasses import asdict, is_dataclass
    return asdict(value) if is_dataclass(value) else value
