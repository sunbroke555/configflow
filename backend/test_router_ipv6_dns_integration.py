"""Same-LAN RA DNS is bound to DHCP publication, preview evidence and rollback."""
import copy
from urllib.parse import urlsplit

import pytest

from backend.routers import ipv6_dns, release, rule_sync, rule_sync_store
from backend.routers.base import RouterError, RouterState
from backend.routers.plan import build_plan
from backend.routers.routeros import (
    DHCP_NETWORKS, DHCP_SERVERS, DHCP_LEASES, DHCP_OPTIONS, DHCP_OPTION_SETS,
    DHCP_MATCHERS, RouterOSDriver,
)
from backend.routes import routers as api
from backend.test_router_dns_guard import RouterOS, Response
from backend.test_router_rule_sync import environment, publish  # noqa: F401

ND = '/ipv6/nd'
ADDRESSES = '/ip/address'
DNS = '10.0.0.2'
GATEWAY = '10.0.0.4'
ROUTER = {'id': 'ros', 'type': 'routeros', 'endpoint': 'https://10.0.0.1',
          'proxy_gateway_agent_id': 'gateway', 'dns_agent_id': 'dns'}
DESIRED = {'fake_ip_range': '198.18.0.0/16', 'proxy_gateway': GATEWAY, 'warnings': [],
           'proxy_cidrs': [{'cidr': '198.18.0.0/16', 'sources': ['fake-ip']}]}
CONFIG = {'mihomo': {'custom_config': '{}'}}


class DNSRouter(RouterOS):
    def __init__(self):
        super().__init__(nats=[])
        self.rows[ADDRESSES] = [{'.id': '*A', 'address': '10.0.0.1/24', 'interface': 'bridge',
                                 'disabled': 'false', 'invalid': 'false'}]
        self.rows[ND] = [
            {'.id': '*D', 'interface': 'bridge', 'advertise-dns': 'yes',
             'dns-servers': '2001:db8::53', 'ra-lifetime': '30m', 'ra-interval': '3m20s-10m',
             'managed-address-configuration': 'false', 'other-configuration': 'false',
             'advertise-mac-address': 'true', 'disabled': 'false', 'invalid': 'false', 'dynamic': 'false'},
            {'.id': '*E', 'interface': 'other-lan', 'advertise-dns': 'yes', 'dns-servers': '2001:db8:1::53',
             'disabled': 'false', 'invalid': 'false', 'dynamic': 'false'},
        ]
        self.rows[DHCP_NETWORKS] = [
            {'.id': '*F', 'address': '10.0.0.0/24', 'gateway': '10.0.0.1',
             'dns-server': '1.1.1.1', 'dns-none': 'false'},
            {'.id': '*10', 'address': '10.1.0.0/24', 'gateway': '10.1.0.1', 'dns-server': '9.9.9.9'},
        ]
        self.rows[DHCP_SERVERS] = [{'.id': '*11', 'name': 'lan', 'interface': 'bridge', 'disabled': 'false'}]
        for path in (DHCP_LEASES, DHCP_OPTIONS, DHCP_OPTION_SETS, DHCP_MATCHERS):
            self.rows[path] = []

    def request(self, method, url, json=None, **kwargs):
        result = super().request(method, url, json=json, **kwargs)
        if method == 'PUT' and urlsplit(url).path == '/rest/ip/route' and result.status_code == 201:
            self.rows['/ip/route'][-1].update(active='true', **{'immediate-gw': GATEWAY + '%bridge'})
            result = Response(self.rows['/ip/route'][-1], 201)
        return result


@pytest.fixture
def inspection(monkeypatch):
    session = DNSRouter()
    driver = RouterOSDriver(ROUTER['endpoint'], 'admin', 'pw')
    driver.session = session
    agents = {'gateway': {'id': 'gateway', 'host': GATEWAY, 'service_type': 'mihomo'},
              'dns': {'id': 'dns', 'host': DNS, 'service_type': 'oxidns', 'status': 'online'}}
    monkeypatch.setattr(api, '_agent', lambda agent_id: agents.get(agent_id))
    monkeypatch.setattr(api, 'compute_desired', lambda *_args, **_kwargs: copy.deepcopy(DESIRED))
    monkeypatch.setattr(api, '_return_cleanup', lambda *_: None)
    return session, driver, agents


def preview(inspection, router=None):
    _, driver, _ = inspection
    router = ROUTER if router is None else router
    _, desired, state, report = api._inspect_state(CONFIG, router, driver)
    return state, report, build_plan(router, desired, state, report, report['return_routing'], driver)


def test_selected_oxidns_exposes_checklist_and_disables_only_same_lan_dns_after_dhcp(inspection):
    session, driver, _ = inspection
    before = copy.deepcopy(session.rows)
    state, report, plan = preview(inspection)
    assert report['ipv6_dns']['status'] == 'pending'
    assert report['summary']['pending'] == 2 and not report['in_sync']
    assert not plan['blockers'], plan['blockers']
    keys = [stage['key'] for stage in plan['stages']]
    assert keys.index('ipv6_dns') == keys.index('dhcp_dns') + 1
    result = release.execute(plan, driver, None)
    assert result['ok'], result
    assert [path for _, path, _ in session.writes] == [DHCP_NETWORKS, ND]
    assert session.rows[ND][0] == {**before[ND][0], 'advertise-dns': 'no'}
    assert session.rows[ND][1] == before[ND][1]
    assert session.rows[DHCP_NETWORKS][0] == {**before[DHCP_NETWORKS][0], 'dns-server': DNS}
    assert session.rows[DHCP_NETWORKS][1] == before[DHCP_NETWORKS][1]
    assert session.rows[ADDRESSES] == before[ADDRESSES]
    assert state.ipv6_dns_plan['check'] == plan['checks']['ipv6_dns']
    _, after_report, after_plan = preview(inspection)
    assert after_report['ipv6_dns']['status'] == 'synced' and after_report['in_sync']
    assert after_plan['empty'] and not after_plan['blockers']


@pytest.mark.parametrize('case', ['unselected', 'invalid-agent', 'ambiguous-dhcp', 'ikuai'])
def test_unrelated_or_unproven_dns_target_never_reads_ipv6_paths(inspection, monkeypatch, case):
    session, _, agents = inspection
    router = dict(ROUTER)
    if case == 'unselected':
        router['dns_agent_id'] = ''
    elif case == 'invalid-agent':
        agents['dns']['status'] = 'offline'
    elif case == 'ambiguous-dhcp':
        session.rows[DHCP_NETWORKS].append({'.id': '*12', 'address': '10.0.0.0/25', 'dns-server': DNS})
    else:
        router['type'] = 'ikuai'
    calls = []
    monkeypatch.setattr(ipv6_dns, 'inspect', lambda *_: calls.append(True))
    _, _, state, report = api._inspect_state(CONFIG, router, inspection[1])
    assert calls == [] and state.ipv6_dns_plan is None and 'ipv6_dns' not in report
    assert not any(path.startswith('/ipv6/') for path in session.reads)


@pytest.mark.parametrize('change', ['dns', 'lan', 'nd-config', 'nd-enabled'])
def test_preview_fingerprint_binds_selected_dns_lan_and_full_nd_configuration(inspection, change):
    session, _, agents = inspection
    first = preview(inspection)[2]
    if change == 'dns':
        agents['dns']['host'] = '10.0.0.5'
    elif change == 'lan':
        session.rows[ADDRESSES][0]['interface'] = 'new-bridge'
        session.rows[ND][0]['interface'] = 'new-bridge'
    elif change == 'nd-config':
        session.rows[ND][0]['ra-lifetime'] = '20m'
    else:
        session.rows[ND][0]['disabled'] = 'true'
    assert preview(inspection)[2]['fingerprint'] != first['fingerprint']


def test_unproven_global_nd_entry_skips_only_ipv6_dns_and_marks_report(inspection):
    session, driver, _ = inspection
    session.rows[ND][0]['interface'] = 'all'
    before = copy.deepcopy(session.rows[ND])
    _, report, plan = preview(inspection)
    assert report['ipv6_dns']['status'] == 'blocked' and report['ipv6_dns']['fix_command']
    assert report['summary']['blocked'] == 1 and not report['in_sync']
    assert not plan['blockers'], plan['blockers']
    assert any(item.startswith('IPv6 DNS 下发本次跳过') for item in plan['warnings'])
    stage = next(stage for stage in plan['stages'] if stage['key'] == 'ipv6_dns')
    assert stage['skipped'] and not stage['ops']
    result = release.execute(plan, driver, None)
    assert result['ok'], result
    assert [path for _, path, _ in session.writes] == [DHCP_NETWORKS]
    assert session.rows[ND] == before
    assert {'stage': 'ipv6_dns', 'status': 'warning'}.items() <= next(
        entry for entry in result['log'] if entry['stage'] == 'ipv6_dns').items()


def test_failed_nd_write_restores_preceding_dhcp_change(inspection):
    session, driver, _ = inspection
    before = copy.deepcopy(session.rows)
    plan = preview(inspection)[2]
    session.fail.add(('PATCH', ND, 1))
    result = release.execute(plan, driver, None)
    assert not result['ok'] and result['failed_stage'] == 'ipv6_dns'
    assert [item['stage'] for item in result['rollback']] == ['dhcp_dns']
    assert all(item['status'] == 'ok' for item in result['rollback'])
    assert session.rows == before


def test_failed_ipv6_readback_restores_nd_dhcp_and_earlier_routes(inspection, monkeypatch):
    session, driver, _ = inspection
    session.rows['/ip/route'].pop()
    before = copy.deepcopy(session.rows)
    plan = preview(inspection)[2]

    def fail_readback(*_):
        raise RouterError('模拟 IPv6 DNS 回读失败')

    monkeypatch.setattr(ipv6_dns, 'verify', fail_readback)
    result = release.execute(plan, driver, None)
    assert not result['ok'] and result['failed_stage'] == 'ipv6_dns'
    assert [item['stage'] for item in result['rollback']] == ['ipv6_dns', 'dhcp_dns', 'routes']
    assert all(item['status'] == 'ok' for item in result['rollback'])
    assert session.rows == before


def test_dhcp_is_rechecked_immediately_before_disabling_nd_dns(inspection, monkeypatch):
    session, driver, _ = inspection
    before = copy.deepcopy(session.rows)
    plan = preview(inspection)[2]
    real_verify = release._verify
    dhcp_verified = []

    def verify(stage, *args, **kwargs):
        if stage == 'dhcp_dns':
            dhcp_verified.append(True)
            if len(dhcp_verified) == 2:
                raise RouterError('DHCP DNS 刚发生变化')
        return real_verify(stage, *args, **kwargs)

    monkeypatch.setattr(release, '_verify', verify)
    result = release.execute(plan, driver, None)
    assert len(dhcp_verified) == 2
    assert not result['ok'] and result['failed_stage'] == 'ipv6_dns'
    assert all(path != ND for _, path, _ in session.writes)
    assert [item['stage'] for item in result['rollback']] == ['dhcp_dns']
    assert session.rows == before


def test_synced_ipv6_stage_is_read_back_even_when_no_write_is_planned(inspection, monkeypatch):
    session, driver, _ = inspection
    session.rows[ND][0]['advertise-dns'] = 'no'
    session.rows[DHCP_NETWORKS][0]['dns-server'] = DNS
    plan = preview(inspection)[2]
    assert plan['empty']
    calls = []
    verify = ipv6_dns.verify

    def record(remote, check):
        calls.append(check)
        return verify(remote, check)

    monkeypatch.setattr(ipv6_dns, 'verify', record)
    assert release.execute(plan, driver, None)['ok']
    assert calls == [plan['checks']['ipv6_dns']] and session.writes == []


@pytest.mark.parametrize('pending', [{'blockers': [], 'ops': [{'kind': 'ipv6_dns_update'}]}])
def test_auto_sync_pauses_before_writes_when_ra_dns_needs_attention(environment, monkeypatch, pending):
    _, driver = environment
    initial = publish(environment)
    desired = initial['baseline']['desired']
    monkeypatch.setattr(api, '_inspect', lambda *_args, **_kwargs: (
        None, desired, RouterState(ipv6_dns_plan=pending), {'return_routing': {}}))
    monkeypatch.setattr('backend.routers.plan.build_plan', lambda *_: {
        'checks': {'routes': desired['proxy_cidrs']}})
    saves = []
    monkeypatch.setattr(rule_sync, '_save_manifest', lambda *_: saves.append(True))
    result = rule_sync.sync('default', 'ros', lambda *_: 'IP-CIDR,8.8.8.0/24')
    assert result['status'] == 'paused' and 'IPv6 DNS 下发' in result['message']
    assert saves == [] and driver.writes == []


def test_auto_sync_does_not_pause_for_ra_dns_it_would_skip(environment, monkeypatch):
    _, driver = environment
    initial = publish(environment)
    desired = initial['baseline']['desired']
    monkeypatch.setattr(api, '_inspect', lambda *_args, **_kwargs: (
        None, desired, RouterState(ipv6_dns_plan={'blockers': ['LAN 无法确认'], 'ops': []}), {'return_routing': {}}))
    monkeypatch.setattr('backend.routers.plan.build_plan', lambda *_: {
        'checks': {'routes': desired['proxy_cidrs']}})
    result = rule_sync.sync('default', 'ros', lambda *_: 'IP-CIDR,8.8.8.0/24')
    assert 'IPv6 DNS 下发' not in (result.get('message') or '')


def test_auto_sync_rechecks_ra_dns_and_rolls_back_ip_delta_on_drift(environment, monkeypatch):
    _, driver = environment
    publish(environment)
    real_plan = build_plan
    calls = []
    before = copy.deepcopy(driver.rows)

    def with_ipv6_check(*args, **kwargs):
        plan = real_plan(*args, **kwargs)
        plan['checks']['ipv6_dns'] = {'test': True}
        return plan

    def drift(remote, check):
        calls.append(check)
        raise RouterError('IPv6 DNS 通告在同步期间发生变化')

    monkeypatch.setattr('backend.routers.plan.build_plan', with_ipv6_check)
    monkeypatch.setattr(ipv6_dns, 'verify', drift)
    result = rule_sync.sync('default', 'ros', lambda *_: 'IP-CIDR,8.8.8.0/24')
    assert result['status'] == 'paused' and calls == [{'test': True}]
    # Restored rows may receive new IDs; their configurations must be unchanged.
    clean = lambda rows: sorted(({key: value for key, value in row.items() if key != '.id'} for row in rows),
                                key=lambda row: repr(sorted(row.items())))
    assert {path: clean(rows) for path, rows in driver.rows.items()} == {
        path: clean(rows) for path, rows in before.items()}
    assert rule_sync_store.read('default', 'ros')['status'] == 'paused'


@pytest.mark.parametrize('field,value', [('invalid', 'true'), ('dynamic', 'true'), ('disabled', 'unknown')])
def test_last_transaction_read_rejects_nd_state_changes_before_patch(inspection, monkeypatch, field, value):
    session, driver, _ = inspection
    original_dns = copy.deepcopy(session.rows[DHCP_NETWORKS])
    plan = preview(inspection)[2]
    transaction = driver._transaction

    def changed(changes):
        if changes[0]['path'] == ND:
            session.rows[ND][0][field] = value
        return transaction(changes)

    monkeypatch.setattr(driver, '_transaction', changed)
    result = release.execute(plan, driver, None)
    assert not result['ok'] and result['failed_stage'] == 'ipv6_dns'
    assert all(path != ND for _, path, _ in session.writes)
    assert session.rows[ND][0]['advertise-dns'] == 'yes'
    assert session.rows[ND][0][field] == value  # Preserve the external change.
    assert session.rows[DHCP_NETWORKS] == original_dns
