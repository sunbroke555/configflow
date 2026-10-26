"""Indexed CIDR coverage must match the pairwise ``subnet_of`` scans it replaced.

The ``legacy_*`` functions are verbatim copies of the pre-index code; random
pools mix duplicates, subnets, supernets and sibling halves (which collapse
into a parent that no single source equals).
"""
import ipaddress
import random

import pytest

from backend.routers import desired as desired_module
from backend.routers import plan as plan_module
from backend.routers import release as release_module
from backend.routers import report as report_module
from backend.routers.base import (
    as_dict, is_managed, NatBypassEntry, POLICY_AUTO_COMMENT, POLICY_MANUAL_COMMENT, policy_route, policy_route_ok,
    PolicyRouteEntry, RouteEntry, RouterError, RouterState,
)
from backend.routers.ikuai_groups import canon
from backend.routers.plan import _policy_op
from backend.routers.report import _nat_items, _policy_route_items, SupernetIndex

GATEWAY = '192.168.1.4'
# Public /8s only, so compute_desired keeps every generated network.
BASES = tuple(range(1, 10)) + tuple(range(11, 100))
SIZES = [1000, 3000]


# ---- verbatim pre-index implementations -------------------------------------------------

_parsed = {}


def _networks(values):
    """``report._networks`` memoized for the legacy scans only: it is pure, and
    reparsing per CIDR would make the 3000-network comparison take ~20 s."""
    key = tuple(values)
    if key not in _parsed:
        _parsed[key] = report_module._networks(values)
    return _parsed[key]


def legacy_merge(sources):
    collapsed = list(ipaddress.collapse_addresses(sources))
    proxy_cidrs = []
    for network in collapsed:
        names = sorted({name for original, labels in sources.items() if original.subnet_of(network) for name in labels})
        proxy_cidrs.append({'cidr': str(network), 'sources': names})
    proxy_cidrs.sort(key=lambda entry: ipaddress.IPv4Network(entry['cidr']))
    return proxy_cidrs


def legacy_nat_items(desired, state):
    rules = [rule for rule in state.nat_bypass if rule.enabled]
    items = []
    for entry in desired['proxy_cidrs']:
        target = ipaddress.IPv4Network(entry['cidr'])
        covering = [rule for rule in rules if any(target.subnet_of(net) for net in _networks(rule.dst))]
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


def legacy_covered(cidr, state):
    target = ipaddress.IPv4Network(cidr)
    return any(rule.enabled and is_managed(rule.comment) and any(target.subnet_of(net) for net in _networks(rule.dst))
               for rule in state.nat_bypass)


def legacy_verify_nat(checks, state):
    uncovered = [cidr for cidr in checks['nat_bypass'] if not legacy_covered(cidr, state)]
    if uncovered:
        raise RouterError(f'回读校验失败：{", ".join(uncovered)} 没有被受管 NAT 免转换规则覆盖')


def legacy_policy_route_items(desired, state):
    gateway = desired.get('proxy_gateway') or ''
    desired_cidrs = {entry['cidr'] for entry in desired['proxy_cidrs']}
    auto = policy_route(state, POLICY_AUTO_COMMENT)
    covered = _networks(auto.dst) if auto and policy_route_ok(auto, gateway) else []
    items = []
    for entry in desired['proxy_cidrs']:
        target = ipaddress.IPv4Network(entry['cidr'])
        matches = [route for route in state.routes if route.dst == entry['cidr'] and route.enabled]
        bad = [route for route in matches if not (gateway and route.gateway == gateway)]
        if bad:
            status, detail = 'conflict', f'同网段路由下一跳为 {bad[0].gateway}，期望下一跳 {gateway or "未知"}'
        elif matches:
            status, detail = 'adoptable', '静态路由会让 mihomo 自身的流量回环，将迁移为排除 mihomo 的策略路由'
        elif any(target.subnet_of(network) for network in covered):
            status, detail = 'synced', ''
        else:
            status, detail = 'missing', ''
        items.append({'cidr': entry['cidr'], 'sources': entry['sources'], 'status': status, 'detail': detail,
                      'route_ids': [route.id for route in matches]})
    extra = [{'cidr': route.dst, 'gateway': route.gateway, 'route_id': route.id, 'comment': route.comment}
             for route in state.routes if is_managed(route.comment) and route.dst not in desired_cidrs]
    if auto:
        # A rule's entries are compared as addresses (an IP group may read 1.1.1.1/32 back as 1.1.1.1).
        wanted = {canon(cidr) for cidr in desired_cidrs}
        extra += [{'cidr': cidr, 'gateway': auto.gateway, 'route_id': f'policy:{auto.id}', 'comment': auto.comment}
                  for cidr in auto.dst if canon(cidr) not in wanted]
    unmanaged_other = [{'cidr': route.dst, 'gateway': route.gateway, 'comment': route.comment, 'enabled': route.enabled,
                        'migrate': route.enabled}
                       for route in state.routes
                       if gateway and route.gateway == gateway and not is_managed(route.comment)
                       and route.dst not in desired_cidrs]
    manual = policy_route(state, POLICY_MANUAL_COMMENT)
    return {'items': items, 'extra': extra, 'unmanaged_other': unmanaged_other, 'mode': 'policy',
            'policy': {'auto': as_dict(auto) if auto else None, 'manual': as_dict(manual) if manual else None}}


def legacy_policy_route_ops(report, state, gateway, warnings):
    ops, skipped = [], set()
    for item in report['routes']['items']:
        if item['status'] == 'conflict':
            skipped.add(item['cidr'])
            warnings.append(f'{item["cidr"]}：{item["detail"]}。本次跳过该网段（路由、NAT、回流都不处理），请先手动处理')
    target = [item['cidr'] for item in report['routes']['items'] if item['cidr'] not in skipped]
    covered = _networks(target)
    auto = policy_route(state, POLICY_AUTO_COMMENT)
    checks = []
    if target:
        op = _policy_op(auto, gateway, target, POLICY_AUTO_COMMENT)
        ops.extend([op] if op else [])
        checks.append({'comment': POLICY_AUTO_COMMENT, 'gateway': gateway, 'dst': target})
    elif auto is not None:
        ops.append({'kind': 'policy_route_delete', 'id': auto.id,
                    'summary': f'删除不再需要的策略路由「{POLICY_AUTO_COMMENT}」'})

    def inside(cidr):
        return any(network.subnet_of(outer) for network in _networks([cidr]) for outer in covered)

    migrate = [item['cidr'] for item in report['routes']['unmanaged_other'] if item.get('migrate') and not inside(item['cidr'])]
    manual = policy_route(state, POLICY_MANUAL_COMMENT)
    if migrate or (manual is not None and not policy_route_ok(manual, gateway)):
        current = list(manual.dst) if manual else []
        dst = current + [cidr for cidr in migrate if cidr not in current]
        op = _policy_op(manual, gateway, dst, POLICY_MANUAL_COMMENT)
        ops.extend([op] if op else [])
        checks.append({'comment': POLICY_MANUAL_COMMENT, 'gateway': gateway, 'dst': dst})

    migrated = [route for route in state.routes
                if route.enabled and route.gateway == gateway and (route.dst in migrate or inside(route.dst))]
    for route in migrated:
        ops.append({'kind': 'route_delete', 'id': route.id, 'cidr': route.dst,
                    'summary': f'删除静态路由 {route.dst} → {gateway}（{route.comment or "无备注"}），'
                               '改由策略路由转发，避免 mihomo 自身流量回环'})
    if migrated:
        warnings.insert(0, f'将删除 {len(migrated)} 条指向 {gateway} 的静态路由，网段迁移到 iKuai「端口分流」的'
                           f'「{POLICY_AUTO_COMMENT}」「{POLICY_MANUAL_COMMENT}」规则，转发效果不变，'
                           f'但 mihomo 自身的流量不再回环：'
                           + '、'.join(f'{route.dst}（{route.comment or "无备注"}）' for route in migrated))
    deleting = {op['id'] for op in ops if op['kind'] == 'route_delete'}
    for extra in report['routes']['extra']:
        if extra['route_id'].startswith('policy:') or extra['route_id'] in deleting:
            continue
        ops.append({'kind': 'route_delete', 'id': extra['route_id'], 'cidr': extra['cidr'],
                    'summary': f'删除不再需要的受管路由 {extra["cidr"]} → {extra["gateway"]}'})
    return ops, skipped, checks


# ---- random inputs ----------------------------------------------------------------------

def random_networks(rng, count):
    """Networks with duplicates, nested subnets/supernets and sibling halves."""
    pool = []
    while len(pool) < count:
        roll = rng.random()
        if pool and roll < 0.15:
            pool.append(rng.choice(pool))
        elif pool and roll < 0.35:
            parent = rng.choice(pool)
            if parent.prefixlen < 32:
                prefix = rng.randint(parent.prefixlen + 1, min(32, parent.prefixlen + 8))
                offset = rng.randrange(1 << (prefix - parent.prefixlen)) << (32 - prefix)
                pool.append(ipaddress.IPv4Network((int(parent.network_address) + offset, prefix)))
        elif pool and roll < 0.45:
            child = rng.choice(pool)
            if child.prefixlen > 12:
                pool.append(child.supernet(new_prefix=rng.randint(max(12, child.prefixlen - 4), child.prefixlen - 1)))
        elif pool and roll < 0.6:
            network = rng.choice(pool)
            if network.prefixlen > 8:
                sibling = int(network.network_address) ^ (1 << (32 - network.prefixlen))
                pool.append(ipaddress.IPv4Network((sibling, network.prefixlen)))
        else:
            prefix = rng.choice((16, 20, 22, 24, 24, 24, 28, 32))
            address = (rng.choice(BASES) << 24) | rng.getrandbits(24)
            pool.append(ipaddress.IPv4Network((address, prefix), strict=False))
    return pool


def dst_value(rng, network):
    """A rule destination as iKuai writes it: CIDR, bare IP, range, or junk."""
    roll = rng.random()
    if roll < 0.1:
        return str(network.network_address)
    if roll < 0.2:
        start = rng.randint(int(network.network_address), int(network.broadcast_address))
        end = rng.randint(start, int(network.broadcast_address))
        return f'{ipaddress.IPv4Address(start)}-{ipaddress.IPv4Address(end)}'
    if roll < 0.22:
        return rng.choice(('bogus', '2001:db8::/32', '9.9.9.9-1.1.1.1', ''))
    return str(network)


def targets_from(rng, pool, count):
    """Desired CIDRs: pool members plus collapsed aggregates (never equal to one source)."""
    chosen = rng.sample(pool, min(count, len(pool)))
    collapsed = list(ipaddress.collapse_addresses(rng.sample(pool, len(pool) // 4)))
    cidrs = list(dict.fromkeys(str(network) for network in chosen + collapsed[:count // 10]))
    rng.shuffle(cidrs)
    return [{'cidr': cidr, 'sources': ['x']} for cidr in cidrs]


def nat_state(rng, pool):
    rules = []
    for index in range(8):
        members = rng.sample(pool, rng.randint(0, len(pool) // 5))
        rules.append(NatBypassEntry(
            id=f'n{index}', dst=[dst_value(rng, network) for network in members], src=[],
            enabled=rng.random() < 0.8, comment=rng.choice(('cf-mihomo', 'cf', 'manual', '', 'cf:old'))))
    return RouterState(nat_bypass=rules)


def policy_state(rng, pool, desired, auto_ok):
    routes = []
    desired_cidrs = [entry['cidr'] for entry in desired['proxy_cidrs']]
    for index in range(len(pool) // 2):
        dst = rng.choice(desired_cidrs) if rng.random() < 0.2 else str(rng.choice(pool))
        routes.append(RouteEntry(id=f'r{index}', dst=dst, enabled=rng.random() < 0.85,
                                 gateway=GATEWAY if rng.random() < 0.8 else '192.168.1.9',
                                 comment=rng.choice(('cf', 'cf-tg', 'manual', ''))))
    auto_dst = [dst_value(rng, network) for network in rng.sample(pool, len(pool) // 2)]
    auto = PolicyRouteEntry(id='p1', dst=auto_dst, gateway=GATEWAY, enabled=True, comment=POLICY_AUTO_COMMENT,
                            src=[GATEWAY], src_invert=True, protocol='any', priority=0 if auto_ok else 5)
    manual = PolicyRouteEntry(id='p2', dst=[str(network) for network in rng.sample(pool, 20)], gateway=GATEWAY,
                              enabled=True, comment=POLICY_MANUAL_COMMENT, src=[GATEWAY], src_invert=True,
                              protocol='any', priority=0)
    return RouterState(routes=routes, policy_routes=[auto, manual])


def outcome(call):
    try:
        call()
    except RouterError as exc:
        return str(exc)
    return None


# ---- differential tests -----------------------------------------------------------------

@pytest.mark.parametrize('size', SIZES)
def test_merge_sources_matches_pairwise_scan(size):
    rng = random.Random(size)
    sources = {ipaddress.IPv4Network('198.18.0.0/16'): ['fake-ip']}
    for network in random_networks(rng, size):
        sources.setdefault(network, []).extend(rng.sample('ABCDEFG', rng.randint(1, 2)))
    assert desired_module.merge_sources(sources) == legacy_merge(sources)


@pytest.mark.parametrize('size', SIZES)
def test_compute_desired_matches_pairwise_scan(size, monkeypatch):
    rng = random.Random(size + 1)
    pool = random_networks(rng, size)
    names = ['Telegram', 'Netflix', 'Google', 'Extra']
    texts = {name: '\n'.join(f'IP-CIDR,{network}' for network in rng.sample(pool, len(pool) // 2)) for name in names}
    config = {'rule_configs': [{'id': name, 'itemType': 'ruleset', 'name': name, 'policy': 'Proxy'} for name in names]}
    selected = names + ['gone']

    def compute():
        return desired_module.compute_desired(config, GATEWAY, selected,
                                              content_loader=lambda item, _library: texts[item['name']])

    current = compute()
    monkeypatch.setattr(desired_module, 'merge_sources', legacy_merge)
    assert current == compute()
    assert len(current['proxy_cidrs']) > 50


@pytest.mark.parametrize('size', SIZES)
def test_nat_coverage_matches_pairwise_scan(size):
    rng = random.Random(size + 2)
    pool = random_networks(rng, size)
    state = nat_state(rng, pool)
    desired = {'proxy_gateway': GATEWAY, 'proxy_cidrs': targets_from(rng, pool, size // 3)}
    current = _nat_items(desired, state)
    assert current == legacy_nat_items(desired, state)
    assert {item['status'] for item in current['items']} == {'synced', 'adoptable', 'missing'}

    checks = {'gateway': GATEWAY, 'nat_bypass': [entry['cidr'] for entry in desired['proxy_cidrs']]}
    expected = outcome(lambda: legacy_verify_nat(checks, state))
    assert expected and outcome(lambda: release_module.verify_nat(checks, state)) == expected
    synced = {'gateway': GATEWAY, 'nat_bypass': [item['cidr'] for item in current['items'] if item['status'] == 'synced']}
    assert outcome(lambda: release_module.verify_nat(synced, state)) is None
    assert outcome(lambda: legacy_verify_nat(synced, state)) is None


@pytest.mark.parametrize('size', SIZES)
@pytest.mark.parametrize('auto_ok', [True, False])
def test_policy_routes_match_pairwise_scan(size, auto_ok):
    rng = random.Random(size + 3)
    pool = random_networks(rng, size)
    desired = {'proxy_gateway': GATEWAY, 'proxy_cidrs': targets_from(rng, pool, size // 3)}
    state = policy_state(rng, pool, desired, auto_ok)
    items = _policy_route_items(desired, state)
    assert items == legacy_policy_route_items(desired, state)
    statuses = {item['status'] for item in items['items']}
    assert statuses >= ({'conflict', 'adoptable', 'synced', 'missing'} if auto_ok else {'conflict', 'missing'})

    report = {'routes': items}
    new_warnings, old_warnings = ['seed'], ['seed']
    current = plan_module._policy_route_ops(report, state, GATEWAY, new_warnings)
    assert current == legacy_policy_route_ops(report, state, GATEWAY, old_warnings)
    assert new_warnings == old_warnings
    migrated = [op for op in current[0] if op['kind'] == 'route_delete' and '避免' in op['summary']]
    assert migrated and len(current[2]) == 2


@pytest.mark.parametrize('version', [4, 6])
def test_supernet_index_matches_subnet_of(version):
    rng = random.Random(version)
    width = 32 if version == 4 else 128
    make = ipaddress.IPv4Network if version == 4 else ipaddress.IPv6Network
    base = rng.getrandbits(width)
    networks = []
    for _ in range(400):
        prefix = rng.randint(width - 24, width)
        networks.append(make((base ^ rng.getrandbits(24), prefix), strict=False))
    index = SupernetIndex(networks[:200])
    for target in networks:
        assert index.covers(target) == any(target.subnet_of(network) for network in networks[:200])
    assert not SupernetIndex().covers(networks[0])


# ---- work bounds: each fails on the pairwise implementation -----------------------------

def _forbid_subnet_of(monkeypatch):
    def forbidden(self, other):
        pytest.fail('coverage must use the supernet index, not pairwise subnet_of')
    monkeypatch.setattr(ipaddress.IPv4Network, 'subnet_of', forbidden)


def test_compute_desired_does_not_scan_pairs(monkeypatch):
    rng = random.Random(7)
    text = '\n'.join(f'IP-CIDR,{network}' for network in random_networks(rng, 200))
    config = {'rule_configs': [{'id': 'r', 'itemType': 'ruleset', 'name': 'r', 'policy': 'Proxy'}]}
    _forbid_subnet_of(monkeypatch)
    result = desired_module.compute_desired(config, GATEWAY, ['r'], content_loader=lambda *_: text)
    assert len(result['proxy_cidrs']) > 50


def test_nat_rule_destinations_are_parsed_once(monkeypatch):
    rng = random.Random(8)
    pool = random_networks(rng, 300)
    state = nat_state(rng, pool)
    desired = {'proxy_gateway': GATEWAY, 'proxy_cidrs': targets_from(rng, pool, 100)}
    calls = []
    original = report_module._networks

    def counted(values):
        calls.append(values)
        return original(values)

    monkeypatch.setattr(report_module, '_networks', counted)
    monkeypatch.setattr(release_module, '_networks', counted)
    _nat_items(desired, state)
    assert len(calls) == sum(rule.enabled for rule in state.nat_bypass)
    calls.clear()
    checks = {'gateway': GATEWAY, 'nat_bypass': [entry['cidr'] for entry in desired['proxy_cidrs']]}
    outcome(lambda: release_module.verify_nat(checks, state))
    assert len(calls) == sum(rule.enabled and is_managed(rule.comment) for rule in state.nat_bypass)


def test_policy_coverage_does_not_scan_pairs(monkeypatch):
    rng = random.Random(9)
    pool = random_networks(rng, 300)
    desired = {'proxy_gateway': GATEWAY, 'proxy_cidrs': targets_from(rng, pool, 100)}
    state = policy_state(rng, pool, desired, True)
    _forbid_subnet_of(monkeypatch)
    report = {'routes': _policy_route_items(desired, state)}
    plan_module._policy_route_ops(report, state, GATEWAY, [])
