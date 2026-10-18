"""Batch route checks stay bounded and never reuse evidence across readbacks."""
import copy
import ipaddress
from dataclasses import asdict

import pytest

from backend.routers import base, routeros_nat
from backend.routers.base import RouteEntry, RouterState, StaticRouteIndex
from backend.routers.release import ReleaseError, _verify
from backend.routers.report import _route_items, build_report

GATEWAY = '192.168.0.3'


def batch(count=1000):
    destinations = [str(ipaddress.IPv4Network((int(ipaddress.IPv4Address('40.0.0.0')) + i * 256, 24)))
                    for i in range(count)]
    desired = {'proxy_gateway': GATEWAY, 'proxy_cidrs': [
        {'cidr': cidr, 'sources': ['rules']} for cidr in destinations]}
    state = RouterState(routes=[RouteEntry(str(i), cidr, GATEWAY, True, 'cf', active=True, distance=1)
                                for i, cidr in enumerate(destinations)])
    return desired, state


def test_report_parses_routes_once_per_batch(monkeypatch):
    desired, state = batch()
    parse = base.ipaddress.ip_network
    calls = []

    def counted(value, *args, **kwargs):
        calls.append(value)
        return parse(value, *args, **kwargs)

    monkeypatch.setattr(base.ipaddress, 'ip_network', counted)
    report = _route_items(desired, state)
    assert all(item['status'] == 'synced' for item in report['items'])
    assert len(calls) <= 2 * len(state.routes)


def test_nat_report_reuses_route_evidence_without_mutating_fingerprint(monkeypatch):
    desired, state = batch()
    state.nat_context = {
        'routes': [{'.id': row.id, 'dst-address': row.dst, 'gateway': row.gateway,
                    'active': 'true', 'immediate-gw': GATEWAY + '%bridge'} for row in state.routes],
        'rules': [{'chain': 'srcnat', 'action': 'masquerade', 'out-interface': 'wan'}],
        'routing_rules': [], 'mangle': [], 'vrfs': []}
    original = copy.deepcopy(asdict(state))
    parse, calls = routeros_nat._network, []

    def counted(row):
        calls.append(row['dst-address'])
        return parse(row)

    monkeypatch.setattr(routeros_nat, '_network', counted)
    report = build_report(desired, state)
    assert all(item['status'] == 'not_required' for item in report['nat_bypass']['items'])
    assert len(calls) == len(state.routes)
    assert asdict(state) == original


def test_missing_routes_share_one_gateway_lookup_within_the_read(monkeypatch):
    desired, state = batch(200)
    state.routes = []
    state.nat_context = {
        'routes': [{'dst-address': '192.168.0.0/24', 'gateway': 'bridge',
                    'active': 'true', 'connect': 'true'}],
        'rules': [{'chain': 'srcnat', 'action': 'masquerade', 'out-interface': 'wan'}],
        'routing_rules': [], 'mangle': [], 'vrfs': []}
    lookup, calls = routeros_nat._gateway_egress, []

    def counted(address, routes):
        calls.append(address)
        return lookup(address, routes)

    monkeypatch.setattr(routeros_nat, '_gateway_egress', counted)
    report = build_report(desired, state)
    assert all(item['status'] == 'not_required' for item in report['nat_bypass']['items'])
    assert len(calls) == 1


@pytest.mark.parametrize('cidr', ['40.0.0.0/24', '2001:db8::/120'])
def test_longest_prefix_sweep_covers_aggregate_but_detects_nested_wrong_gateway(cidr):
    target = ipaddress.ip_network(cidr)
    children = list(target.subnets(prefixlen_diff=4))
    routes = [RouteEntry('aggregate', cidr, '192.168.0.9', True, 'manual', active=True)]
    routes += [RouteEntry(str(i), str(net), GATEWAY, True, 'cf', active=True)
               for i, net in enumerate(children)]
    index = StaticRouteIndex(routes)
    selection = index.select(cidr, GATEWAY)
    assert selection.covered and not selection.conflict
    assert {row.id for row in selection.candidates} == {str(i) for i in range(len(children))}
    routes.append(RouteEntry('wrong-host', str(children[4].network_address), '192.168.0.9',
                             True, 'manual', active=True))
    changed = StaticRouteIndex(routes).select(cidr, GATEWAY)
    assert changed.covered and changed.conflict and not changed.candidates


def test_readback_and_nat_rebuild_after_live_route_changes():
    from backend.test_routeros_nat import ACTIVE_PROXY_ROUTE, CIDR, DESIRED, nat_session
    from backend.test_routeros_release import driver_for, ROUTES
    session = nat_session()
    driver = driver_for(session)
    before = build_report(DESIRED, driver.read_state())
    assert before['in_sync']
    checks = {'routes': [{'cidr': CIDR, 'gateway': GATEWAY}], 'removed_routes': []}
    _verify('routes', checks, driver, None)
    session.rows[ROUTES][0]['gateway'] = '192.168.0.99'
    after = build_report(DESIRED, driver.read_state())
    assert after['routes']['items'][0]['status'] == 'conflict'
    assert after['nat_bypass']['items'][0]['status'] == 'unknown'
    with pytest.raises(ReleaseError):
        _verify('routes', checks, driver, None)
    session.rows[ROUTES][0] = copy.deepcopy(ACTIVE_PROXY_ROUTE)
    assert build_report(DESIRED, driver.read_state())['in_sync']
