"""DHCP DNS selection, safe network scope, preview freshness and staged rollback."""
import copy

import pytest

from backend.routers.base import DhcpDnsEntry, DhcpDnsState, RouteEntry, RouterDriver, RouterError, RouterState
from backend.routers.dhcp_dns import build_dns_report, dns_agent_settings
from backend.routers.plan import build_plan
from backend.routers.release import execute
from backend.routers.report import build_report
from backend.test_agent_upgrade_auth import authenticated_app  # noqa: F401


DNS = '192.168.0.2'
GATEWAY = '192.168.0.3'
DESIRED = {'proxy_gateway': GATEWAY, 'proxy_cidrs': [{'cidr': '198.18.0.0/16', 'sources': ['fake-ip']}],
           'dns_agent_id': 'dns-agent', 'dns_server': DNS, 'dns_error': '', 'warnings': []}
ROUTER = {'type': 'routeros', 'endpoint': 'http://192.168.0.1', 'dns_agent_id': 'dns-agent'}
DNS_AGENT = {'id': 'dns-agent', 'host': DNS, 'service_type': 'oxidns', 'status': 'online'}


def entry(id='*1', network='192.168.0.0/24', dns=None, **kwargs):
    return DhcpDnsEntry(id, network, ['1.1.1.1', '8.8.8.8'] if dns is None else dns,
                       attributes={'address': network, 'gateway': '192.168.0.1', 'dns-server': '1.1.1.1,8.8.8.8'},
                       **kwargs)


class MemoryDriver(RouterDriver):
    supports_write = True

    def __init__(self):
        self.dhcp = DhcpDnsState([entry(), entry('*2', '10.20.0.0/24')], {'options': []})
        self.routes = [RouteEntry('*r', '198.18.0.0/16', GATEWAY, True, 'cf-fake-ip')]
        self.writes = []
        self.dhcp_reads = 0
        self.read_error = ''
        self.corrupt_dns = False
        self.fail_restore = False
        self.route_side_effect = None

    def read_state(self):
        return RouterState(routes=copy.deepcopy(self.routes), nat_context={'rules': []})

    def read_dhcp_dns(self):
        self.dhcp_reads += 1
        if self.read_error:
            raise RouterError(self.read_error)
        return copy.deepcopy(self.dhcp)

    def apply(self, op):
        self.writes.append(copy.deepcopy(op))
        if op['kind'] == 'dhcp_dns_update':
            target = next(item for item in self.dhcp.entries if item.id == op['id'])
            previous = copy.deepcopy(target.dns)
            target.dns = ['9.9.9.9'] if self.corrupt_dns else list(op['dns'])
            return {'kind': 'test_dns_restore', 'id': op['id'], 'dns': previous}
        if op['kind'] == 'test_dns_restore':
            if self.fail_restore:
                raise RouterError('恢复失败')
            next(item for item in self.dhcp.entries if item.id == op['id']).dns = list(op['dns'])
            return {}
        if op['kind'] == 'route_add':
            self.routes.append(RouteEntry('*new', op['cidr'], op['gateway'], True, op['comment']))
            if self.route_side_effect:
                self.route_side_effect(self)
            return {'kind': 'test_route_restore', 'id': '*new'}
        if op['kind'] == 'test_route_restore':
            self.routes[:] = [route for route in self.routes if route.id != op['id']]
            return {}
        raise AssertionError(op)


def plan_for(driver, router=ROUTER, desired=DESIRED):
    state = driver.read_state()
    if router.get('dns_agent_id'):
        state.dhcp_dns = driver.read_dhcp_dns()
    return build_plan(router, desired, state, build_report(desired, state), {'status': 'disabled'}, driver)


def test_dns_only_plan_limits_scope_and_keeps_existing_gateway_and_secondary_network():
    driver = MemoryDriver()
    before = copy.deepcopy(driver.dhcp)
    plan = plan_for(driver)
    assert plan['blockers'] == []
    assert [stage['key'] for stage in plan['stages']] == ['return_routing', 'routes', 'nat_bypass', 'dhcp_dns']
    assert all(not stage['ops'] for stage in plan['stages'][:-1])
    [op] = plan['stages'][-1]['ops']
    assert op['id'] == '*1' and op['dns'] == [DNS]
    assert '1.1.1.1, 8.8.8.8 → ' + DNS in op['summary']
    assert driver.writes == []
    result = execute(plan, driver, None)
    assert result['ok'], result
    assert driver.dhcp.entries[0].dns == [DNS]
    assert driver.dhcp.entries[0].attributes == before.entries[0].attributes
    assert driver.dhcp.entries[1] == before.entries[1]
    assert [op['kind'] for op in driver.writes] == ['dhcp_dns_update']


@pytest.mark.parametrize('entries', [[], [entry(network='10.0.0.0/24')],
    [entry(), entry('*2', '192.168.0.0/25')], [entry(network='invalid')],
    [entry(enabled=False)], [entry(blockers=['DHCP option 6 覆盖 DNS'])]])
def test_ambiguous_unsupported_or_missing_network_blocks_every_write(entries):
    driver = MemoryDriver()
    driver.dhcp.entries = entries
    plan = plan_for(driver)
    assert plan['blockers']
    assert not execute(plan, driver, None)['ok']
    assert driver.writes == []


@pytest.mark.parametrize('agent', [None, {**DNS_AGENT, 'status': 'offline'},
    {**DNS_AGENT, 'service_type': 'mihomo'}, {**DNS_AGENT, 'host': 'dns.example'},
    {**DNS_AGENT, 'host': '::1'}, {**DNS_AGENT, 'host': '127.0.0.1'}, {**DNS_AGENT, 'host': '0.0.0.0'},
    {**DNS_AGENT, 'host': True}, {**DNS_AGENT, 'host': 3232235522}])
def test_invalid_selected_dns_agent_is_blocking(agent):
    settings = dns_agent_settings('dns-agent', agent)
    assert settings['dns_error'] and not settings['dns_server']
    report = build_dns_report({**DESIRED, **settings}, None)
    assert report['status'] == 'unknown' and report['blockers']


def test_unselected_dns_does_not_read_or_write_and_deselection_keeps_prior_dns():
    driver = MemoryDriver()
    driver.dhcp.entries[0].dns = [DNS]
    driver.read_error = 'DHCP access must not be requested'
    before = copy.deepcopy(driver.dhcp)
    plan = plan_for(driver, {**ROUTER, 'dns_agent_id': ''}, {**DESIRED, 'dns_agent_id': ''})
    assert not plan['blockers'] and plan['empty']
    assert plan['stages'][-1]['enabled'] is False
    assert 'dhcp_dns' not in plan['checks']
    assert execute(plan, driver, None)['ok']
    assert driver.dhcp_reads == 0 and driver.writes == [] and driver.dhcp == before


def test_unknown_and_pending_dns_affect_overall_sync_summary():
    driver = MemoryDriver()
    state = driver.read_state()
    state.dhcp_dns = driver.read_dhcp_dns()
    report = build_report(DESIRED, state)
    assert report['dhcp_dns']['status'] == 'pending'
    assert report['summary']['pending'] == 1 and not report['in_sync']
    state.dhcp_dns.entries[0].dns = [DNS]
    assert build_report(DESIRED, state)['in_sync']
    state.dhcp_dns = None
    report = build_report(DESIRED, state)
    assert report['summary']['unknown'] == 1 and not report['in_sync']


def test_unset_vendor_dns_slot_is_pending_even_if_known_dns_matches_target():
    driver = MemoryDriver()
    driver.dhcp.entries[0].dns = [DNS]
    driver.dhcp.entries[0].dns_explicit = False
    state = driver.read_state()
    state.dhcp_dns = driver.read_dhcp_dns()
    report = build_report(DESIRED, state)
    assert report['dhcp_dns']['status'] == 'pending' and not report['in_sync']
    assert '未指定' in report['dhcp_dns']['items'][0]['detail']
    plan = plan_for(driver)
    assert plan['stages'][-1]['ops']
    # The fake only updates the normalized DNS list, leaving the implicit slot;
    # verification must reject that incomplete write despite matching addresses.
    result = execute(plan, driver, None)
    assert not result['ok'] and result['failed_stage'] == 'dhcp_dns'


def test_dns_publication_blocks_on_proxy_route_conflict():
    driver = MemoryDriver()
    driver.routes[0].gateway = '192.168.0.99'
    plan = plan_for(driver)
    assert any('代理路由冲突' in blocker for blocker in plan['blockers'])
    assert not execute(plan, driver, None)['ok'] and driver.writes == []


@pytest.mark.parametrize('fail_restore', [False, True])
def test_failed_dns_readback_restores_dns_then_previous_route_stages(fail_restore):
    driver = MemoryDriver()
    driver.routes.clear()
    driver.corrupt_dns = True
    driver.fail_restore = fail_restore
    before = copy.deepcopy(driver.dhcp)
    result = execute(plan_for(driver), driver, None)
    assert not result['ok'] and result['failed_stage'] == 'dhcp_dns'
    assert [item['stage'] for item in result['rollback']] == ['dhcp_dns', 'routes']
    assert result['rollback'][0]['status'] == ('failed' if fail_restore else 'ok')
    assert driver.routes == []
    if not fail_restore:
        assert driver.dhcp == before


@pytest.mark.parametrize('change', ['dns', 'scope', 'override'])
def test_synced_dns_is_rechecked_after_other_stages(change):
    driver = MemoryDriver()
    driver.routes.clear()
    driver.dhcp.entries[0].dns = [DNS]
    def drift(current):
        if change == 'dns':
            current.dhcp.entries[0].dns = ['9.9.9.9']
        elif change == 'scope':
            current.dhcp.entries.append(entry('*3', '192.168.0.0/25'))
        else:
            current.dhcp.entries[0].blockers.append('新增 option 6')
    driver.route_side_effect = drift
    plan = plan_for(driver)
    assert not plan['stages'][-1]['ops']
    result = execute(plan, driver, None)
    assert not result['ok'] and result['failed_stage'] == 'dhcp_dns'
    assert driver.routes == []
    assert all(op['kind'] != 'dhcp_dns_update' for op in driver.writes)


def test_pending_dns_scope_drift_after_routes_is_blocked_before_dns_write():
    driver = MemoryDriver()
    driver.routes.clear()
    driver.route_side_effect = lambda current: current.dhcp.entries.append(entry('*3', '192.168.0.0/25'))
    result = execute(plan_for(driver), driver, None)
    assert not result['ok'] and result['failed_stage'] == 'dhcp_dns'
    assert driver.routes == []
    assert all(op['kind'] != 'dhcp_dns_update' for op in driver.writes)


def test_pending_dns_rechecks_even_previously_synced_routes_before_write():
    driver = MemoryDriver()
    plan = plan_for(driver)
    driver.routes[0].gateway = '192.168.0.99'
    result = execute(plan, driver, None)
    assert not result['ok'] and result['failed_stage'] == 'dhcp_dns'
    assert driver.writes == []


@pytest.mark.parametrize('change', ['scope', 'override', 'field', 'selection'])
def test_noop_dns_preview_tracks_all_semantic_evidence(change):
    driver = MemoryDriver()
    driver.dhcp.entries[0].dns = [DNS]
    first = plan_for(driver)
    router, desired = dict(ROUTER), dict(DESIRED)
    if change == 'scope':
        driver.dhcp.entries[0].network = '192.168.0.0/25'
    elif change == 'override':
        driver.dhcp.evidence['options'].append({'name': 'boot', 'code': 66, 'value': 'server'})
    elif change == 'field':
        driver.dhcp.entries[0].attributes['gateway'] = '192.168.0.9'
    else:
        router['dns_agent_id'] = desired['dns_agent_id'] = 'another-agent-same-host'
    second = plan_for(driver, router, desired)
    assert first['empty'] and second['empty']
    assert first['fingerprint'] != second['fingerprint']


def test_api_inspection_only_reads_selected_dns_and_rejects_stale_target(authenticated_app, monkeypatch):  # noqa: F811
    client, _, agents, admin, _ = authenticated_app
    from backend.routes import routers as api
    driver = MemoryDriver()
    # This API case emulates RouterOS with a known LAN and no RA service.
    # Selecting OxiDNS now checks that LAN before it advertises DHCP DNS.
    nd_state = {'/ip/address': [{'.id': '*A', 'address': '192.168.0.1/24', 'interface': 'bridge'}],
                '/ipv6/nd': []}
    monkeypatch.setattr(driver, '_list', lambda path: copy.deepcopy(nd_state[path]), raising=False)
    dns_agent = dict(DNS_AGENT)
    monkeypatch.setattr(api, '_agent', lambda id: dns_agent if id == 'dns-agent' else
                        {**agents[0], 'host': GATEWAY})
    monkeypatch.setitem(api.DRIVERS, 'routeros', lambda *args, **kwargs: driver)
    monkeypatch.setattr(api, 'compute_desired', lambda *args, **kwargs:
                        {key: value for key, value in DESIRED.items() if not key.startswith('dns_')})
    response = client.post('/api/routers', headers=admin, json={
        'name': 'ROS', 'type': 'routeros', 'endpoint': ROUTER['endpoint'], 'username': 'admin',
        'password': 'pw', 'proxy_gateway_agent_id': agents[0]['id']})
    assert response.status_code == 200, response.get_json()
    path = '/api/routers/' + response.get_json()['router']['id']
    assert client.get(path + '/report', headers=admin).get_json()['report']['dhcp_dns']['status'] == 'disabled'
    assert driver.dhcp_reads == 0
    saved = client.put(path, headers=admin, json={'dns_agent_id': 'dns-agent'})
    assert saved.status_code == 200
    plan = client.post(path + '/plan', headers=admin).get_json()['plan']
    assert plan['blockers'] == [] and not plan['empty'] and driver.writes == []
    dns_agent['host'] = '192.168.0.4'
    rejected = client.post(path + '/apply', headers=admin, json={'fingerprint': plan['fingerprint']})
    assert rejected.status_code == 409 and driver.writes == []
    updated = client.post(path + '/plan', headers=admin).get_json()['plan']
    applied = client.post(path + '/apply', headers=admin, json={'fingerprint': updated['fingerprint']})
    assert applied.status_code == 200, applied.get_json()
    assert driver.dhcp.entries[0].dns == ['192.168.0.4']
    driver.read_error = '权限不足'
    report = client.get(path + '/report', headers=admin).get_json()['report']
    assert report['dhcp_dns']['status'] == 'unknown' and '权限不足' in report['dhcp_dns']['message']
    assert client.post(path + '/plan', headers=admin).get_json()['plan']['blockers']
