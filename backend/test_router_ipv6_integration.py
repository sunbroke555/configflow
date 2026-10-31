"""IPv6 route inspection and rule sync keep profile and publication boundaries."""
import copy
from types import SimpleNamespace

import pytest
import yaml

from backend.agents import ipv6_readiness
from backend.agents.oxidns_monitor import MonitorError
from backend.routers import ipv6_fakeip, rule_sync, rule_sync_store
from backend.routers.base import RouterState
from backend.routes import routers as routes_module
from backend.test_router_ipv6_manifest_safety import IPV6_MANIFEST
from backend.test_router_rule_sync import NEW, OLD, environment, publish  # noqa: F401


DOCUMENT = {
    'ipv6': True,
    'dns': {'enable': True, 'ipv6': True, 'enhanced-mode': 'fake-ip', 'fake-ip-range6': 'fc00::1/64'},
    'tun': {'enable': True},
}


def config_for(document=None, profile='live'):
    return {'profile_id': profile, 'mihomo': {'custom_config': yaml.safe_dump(
        DOCUMENT if document is None else document)}}


@pytest.fixture
def inspection(monkeypatch):
    agent = {'id': 'gw', 'host': '192.168.0.3', 'service_type': 'mihomo',
             'profile_id': 'live', 'enabled': True}
    router = {'id': 'ros', 'type': 'routeros', 'proxy_gateway_agent_id': 'gw'}
    state = RouterState()
    driver = SimpleNamespace(read_state=lambda: state)
    calls = {'host': [], 'plan': []}
    host = {'success': True, 'addresses': []}
    monkeypatch.setattr(routes_module, '_agent', lambda agent_id: agent if agent_id else None)
    monkeypatch.setattr(routes_module, 'compute_desired', lambda *args, **kwargs: {})
    monkeypatch.setattr(routes_module, '_dns_settings', lambda *_: {'dns_error': ''})
    monkeypatch.setattr(routes_module, '_return_cleanup', lambda *_: None)
    monkeypatch.setattr(routes_module, 'build_report', lambda *_: {})
    monkeypatch.setattr('backend.common.profile_context.resolve_profile_id', lambda: 'live')

    def request(item):
        calls['host'].append(copy.deepcopy(item))
        return host

    def inspect(config, item, gateway, report, remote):
        calls['plan'].append({'agent': gateway, 'host': report, 'previous': item.get('ipv6_fakeip_state')})
        return {'enabled': ipv6_fakeip.settings(config)['enabled'], 'ops': [], 'blockers': []}

    monkeypatch.setattr(ipv6_readiness, '_request', request)
    monkeypatch.setattr(ipv6_fakeip, 'inspect', inspect)
    return router, agent, driver, calls, host


def test_ipv4_only_inspection_never_calls_ipv6_agent_or_reads_ipv6_state(inspection):
    router, _, driver, calls, _ = inspection
    _, _, state, _ = routes_module._inspect_state(config_for({}), router, driver)
    assert calls == {'host': [], 'plan': []}
    assert state.ipv6_fakeip_plan is None
    assert not hasattr(driver, 'ipv6_fakeip_manifest')


def test_enabled_ipv6_inspection_reads_the_linked_mihomo_host(inspection):
    router, agent, driver, calls, host = inspection
    _, _, state, _ = routes_module._inspect_state(config_for(), router, driver)
    assert calls['host'] == [agent]
    assert calls['plan'] == [{'agent': agent, 'host': host, 'previous': None}]
    assert state.ipv6_fakeip_plan['enabled'] is True
    assert driver.ipv6_fakeip_manifest is None


def test_background_inspection_uses_explicit_nondefault_profile(inspection, monkeypatch):
    router, agent, driver, calls, _ = inspection
    agent['profile_id'] = 'second'
    # Background sync has no request profile, so the resolver falls back to default.
    monkeypatch.setattr('backend.common.profile_context.resolve_profile_id', lambda: 'default')
    routes_module._inspect_state(config_for(profile='second'), router, driver)
    assert calls['host'] == [agent]
    assert 'error' not in calls['plan'][0]['host']


@pytest.mark.parametrize('change', [{'profile_id': 'other'}, {'service_type': 'oxidns'}, {'enabled': False}])
def test_invalid_gateway_binding_does_not_contact_agent(inspection, change):
    router, agent, driver, calls, _ = inspection
    agent.update(change)
    routes_module._inspect_state(config_for(), router, driver)
    assert calls['host'] == []
    assert calls['plan'][0]['host']['error']


def test_disabled_route_withdrawal_does_not_require_the_agent_online(inspection, monkeypatch):
    router, _, driver, calls, _ = inspection
    router['ipv6_fakeip_state'] = copy.deepcopy(IPV6_MANIFEST)
    monkeypatch.setattr(routes_module, '_agent', lambda *_: None)
    _, _, state, _ = routes_module._inspect_state(config_for({}), router, driver)
    assert calls['host'] == []
    assert calls['plan'] == [{'agent': None, 'host': {}, 'previous': IPV6_MANIFEST}]
    assert state.ipv6_fakeip_plan['enabled'] is False
    assert driver.ipv6_fakeip_manifest == IPV6_MANIFEST


def test_agent_network_error_becomes_report_evidence(inspection, monkeypatch):
    router, _, driver, calls, _ = inspection

    def offline(*_):
        raise MonitorError('AGENT_UNREACHABLE', 502, '设备离线')

    monkeypatch.setattr(ipv6_readiness, '_request', offline)
    routes_module._inspect_state(config_for(), router, driver)
    assert '设备离线' in calls['plan'][0]['host']['error']


def save_manifest(repository, value):
    router = repository.get_profile('default')['routers']['items'][0]
    with rule_sync_store.job_lock('default', router['id'], router) as acquired:
        assert acquired
        repository.save_router_publication('default', router, ipv6_fakeip=value)


def save_document(repository, document):
    repository.update_profile_transaction('default', lambda config: config['mihomo'].update(
        custom_config=yaml.safe_dump(document)))


@pytest.mark.parametrize('transition', ['create', 'replace', 'withdraw'])
def test_capture_after_publication_uses_the_saved_ipv6_manifest_without_mutating_preview(environment, transition):
    repository, driver = environment
    initial = publish(environment)
    previous = copy.deepcopy(IPV6_MANIFEST) if transition != 'create' else None
    save_manifest(repository, previous)
    save_document(repository, {} if transition == 'withdraw' else DOCUMENT)
    config = rule_sync.config_for('default')
    router = config['routers']['items'][0]
    before = copy.deepcopy(config)
    new = None if transition == 'withdraw' else copy.deepcopy(IPV6_MANIFEST)
    if new:
        new['resources'][0]['id'] = '*new'
    save_manifest(repository, new)
    driver.ipv6_fakeip_manifest = copy.deepcopy(new)
    routes_module._capture_sync(config, router, driver, initial['baseline']['desired'])
    receipt = rule_sync_store.read('default', router['id'])
    current = rule_sync.config_for('default')
    assert receipt['status'] == 'synced', receipt
    assert receipt['baseline']['control'] == rule_sync.controls(current, current['routers']['items'][0])
    assert config == before  # The snapshot that was previewed remains unchanged.


def test_capture_still_rejects_settings_changed_during_publication(environment):
    repository, driver = environment
    initial = publish(environment)
    save_document(repository, DOCUMENT)
    config = rule_sync.config_for('default')
    router = config['routers']['items'][0]
    save_manifest(repository, IPV6_MANIFEST)
    driver.ipv6_fakeip_manifest = copy.deepcopy(IPV6_MANIFEST)
    changed = copy.deepcopy(DOCUMENT)
    changed['dns']['fake-ip-range6'] = 'fd01::1/64'
    save_document(repository, changed)
    routes_module._capture_sync(config, router, driver, initial['baseline']['desired'])
    receipt = rule_sync_store.read('default', router['id'])
    assert receipt['status'] == 'waiting', receipt
    assert '设置有变化' in receipt['message']
    assert 'baseline' not in receipt


def test_switching_ipv6_mode_keeps_the_fake6_control_so_auto_sync_carries_on():
    """dns.ipv6 picks the IPv6 mode only; the route stays the same in both, as does the sync baseline."""
    router = {'id': 'ros', 'type': 'routeros', 'ipv6_fakeip_state': IPV6_MANIFEST}
    nodes = copy.deepcopy(DOCUMENT)
    nodes['dns']['ipv6'] = False
    assert ipv6_fakeip.settings(config_for(nodes)) == ipv6_fakeip.settings(config_for())
    assert ipv6_fakeip.relevant(config_for(nodes), router)
    # Without TUN only the mode that hands out the addresses cannot be routed.
    nodes['tun']['enable'] = False
    assert not ipv6_fakeip.settings(config_for(nodes))['error']
    nodes['dns']['ipv6'] = True
    assert ipv6_fakeip.settings(config_for(nodes))['error']


@pytest.mark.parametrize('field,value', [('ipv6', False), ('dns.enable', False),
    ('dns.enhanced-mode', 'redir-host'), ('dns.fake-ip-range6', 'fd01::1/64'), ('tun.enable', False)])
def test_changed_fake6_control_pauses_auto_sync_before_any_download_or_write(environment, field, value):
    repository, driver = environment
    initial = publish(environment)
    save_document(repository, DOCUMENT)
    save_manifest(repository, IPV6_MANIFEST)
    config = rule_sync.config_for('default')
    driver.ipv6_fakeip_manifest = copy.deepcopy(IPV6_MANIFEST)
    routes_module._capture_sync(config, config['routers']['items'][0], driver, initial['baseline']['desired'])
    assert rule_sync_store.read('default', 'ros')['status'] == 'synced'
    changed = copy.deepcopy(DOCUMENT)
    if '.' in field:
        parent, key = field.split('.')
        changed[parent][key] = value
    else:
        changed[field] = value
    save_document(repository, changed)
    downloads = []
    result = rule_sync.sync('default', 'ros', lambda *_: downloads.append(True))
    assert result['status'] == 'paused', result
    assert '尚未发布' in result['message']
    assert downloads == [] and driver.writes == []


@pytest.mark.parametrize('pending', [{'blockers': ['无法确认设备'], 'ops': []},
                                    {'blockers': [], 'ops': [{'kind': 'ipv6_fakeip_replace'}]}])
def test_auto_sync_stops_before_marking_writes_started_when_ipv6_needs_attention(environment, monkeypatch, pending):
    _, driver = environment
    initial = publish(environment)
    desired = initial['baseline']['desired']
    monkeypatch.setattr(routes_module, '_inspect', lambda *_args, **_kwargs: (
        None, desired, RouterState(ipv6_fakeip_plan=pending), {'return_routing': {}}))
    monkeypatch.setattr('backend.routers.plan.build_plan', lambda *_: {
        'checks': {'routes': desired['proxy_cidrs']}})
    saves = []
    monkeypatch.setattr(rule_sync, '_save_manifest', lambda *_: saves.append(True))
    result = rule_sync.sync('default', 'ros', lambda *_: 'IP-CIDR,8.8.8.0/24')
    assert result['status'] == 'paused', result
    assert 'IPv6 Fake-IP' in result['message']
    assert saves == [] and driver.writes == []



def enroll_ipv6_sync(environment, monkeypatch, ownership, mode):
    repository, driver = environment
    if mode == 'static':
        repository.update_profile_transaction('default', lambda config: config['routers']['items'][0].update(
            ros_policy_rule_sets=[], ip_rule_sets=['remote']))
    initial = publish(environment)
    save_document(repository, DOCUMENT)
    row = {'.id': '*V6', 'dst-address': 'fc00::/64', 'gateway': 'fdda::3%bridge',
           'routing-table': 'main', 'distance': '1', 'disabled': 'false', 'active': 'true',
           'dynamic': 'false', 'comment': 'cf-fakeip6:ros' if ownership == 'owned' else 'my IPv6 route'}
    driver.rows[ipv6_fakeip.ROUTES] = [row]
    driver.rows[ipv6_fakeip.ADDRESSES] = [
        {'.id': '*A6', 'address': 'fdda::1/64', 'interface': 'bridge', 'disabled': 'false'}]
    driver.rows[ipv6_fakeip.IPV4_ADDRESSES] = [
        {'.id': '*A4', 'address': '192.168.0.1/24', 'interface': 'bridge', 'disabled': 'false'}]
    # Read by the same-port firewall hint; an empty filter has nothing to report.
    for path in (ipv6_fakeip.FILTER, ipv6_fakeip.ADDRESS_LISTS, '/interface', '/interface/list', '/interface/list/member'):
        driver.rows.setdefault(path, [])
    manifest = {'version': 1, 'spec': {'cidr': 'fc00::/64', 'gateway': 'fdda::3%bridge'},
                'resources': [{'path': ipv6_fakeip.ROUTES, 'id': '*V6', 'config': ipv6_fakeip.stable(row)}]}
    if ownership != 'owned':
        manifest = None
    save_manifest(repository, manifest)
    driver.ipv6_fakeip_manifest = copy.deepcopy(manifest)
    monkeypatch.setattr(routes_module, '_agent', lambda agent_id: {
        'id': 'gw', 'host': '192.168.0.3', 'profile_id': 'default',
        'service_type': 'mihomo', 'enabled': True} if agent_id else None)
    monkeypatch.setattr(ipv6_readiness, '_request', lambda *_: {
        'success': True, 'addresses': [{'address': 'fdda::3', 'usable': True}]})
    config = rule_sync.config_for('default')
    routes_module._capture_sync(config, config['routers']['items'][0], driver, initial['baseline']['desired'])
    receipt = rule_sync_store.read('default', 'ros')
    assert receipt['status'] == 'synced', receipt
    return row, manifest


@pytest.mark.parametrize('mode', ['static', 'policy'])
@pytest.mark.parametrize('ownership', ['owned', 'borrowed'])
def test_ipv6_change_during_auto_sync_rolls_back_ipv4_and_preserves_outside_ipv6_edit(
        environment, monkeypatch, mode, ownership):
    from backend.test_ros_policy import ADDRESSES, MANGLE, NAT, ROUTES, TABLES

    repository, driver = environment
    row, manifest = enroll_ipv6_sync(environment, monkeypatch, ownership, mode)
    paths = (ADDRESSES, MANGLE, NAT, ROUTES, TABLES)
    before = {path: copy.deepcopy(driver.rows[path]) for path in paths}
    request = driver._request
    outside_edit = []

    def change_while_writing(method, path, values=None):
        written = len(driver.writes)
        result = request(method, path, values)
        # Policy members are added through a POST /execute console batch; the
        # memory driver records its actual PUTs without calling _request again.
        # Trigger after either a REST PUT or the batch has committed a member.
        if not outside_edit and any(action == 'PUT' and target in (ADDRESSES, ROUTES)
                                    for action, target, _ in driver.writes[written:]):
            row['gateway'] = 'fdda::9%bridge'
            outside_edit.append(True)
        return result

    monkeypatch.setattr(driver, '_request', change_while_writing)
    result = rule_sync.sync('default', 'ros', lambda *_: NEW)
    assert outside_edit
    assert result['status'] == 'paused', result
    assert 'IPv6 Fake-IP' in result['message']

    def configurations(rows):
        # Restore can assign new IDs; behavior and ownership are what must recover.
        return sorted((repr(sorted((key, value) for key, value in item.items() if key != '.id'))
                       for item in rows))

    assert all(configurations(driver.rows[path]) == configurations(before[path]) for path in paths)
    assert driver.rows[ipv6_fakeip.ROUTES] == [{**row, 'gateway': 'fdda::9%bridge'}]
    assert driver.ipv6_fakeip_manifest == manifest
    assert repository.get_profile('default')['routers']['items'][0].get('ipv6_fakeip_state') == manifest
    assert driver.writes and all(not path.startswith('/ipv6/') for _, path, _ in driver.writes)
    assert (repository.shared_rules_dir() / 'Remote.list').read_text() == OLD


@pytest.mark.parametrize('ownership', ['owned', 'borrowed'])
def test_unchanged_ipv6_is_verified_without_writing_it_during_auto_sync(environment, monkeypatch, ownership):
    _, driver = environment
    row, manifest = enroll_ipv6_sync(environment, monkeypatch, ownership, 'static')
    before = copy.deepcopy(row)
    verified = []
    verify = ipv6_fakeip.verify

    def record(remote, check):
        verified.append(copy.deepcopy(check))
        return verify(remote, check)

    monkeypatch.setattr(ipv6_fakeip, 'verify', record)
    result = rule_sync.sync('default', 'ros', lambda *_: NEW)
    assert result['status'] == 'synced', result
    assert len(verified) == 1 and verified[0]['spec']['cidr'] == 'fc00::/64'
    assert driver.rows[ipv6_fakeip.ROUTES] == [before]
    assert driver.ipv6_fakeip_manifest == manifest
    assert all(not path.startswith('/ipv6/') for _, path, _ in driver.writes)
