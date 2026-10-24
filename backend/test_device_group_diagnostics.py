from copy import deepcopy
import json

import pytest

from backend.common import device_group_diagnostics as service
from backend.routers.base import RouterState
from backend.test_device_group_activity import Manager, agent


def config():
    return {'profile_id': 'office', 'agents': [agent('core', service_type='mihomo', host='192.168.60.2')],
            'device_groups': {'version': 1, 'groups': [
                {'id': 'dg_aaaaaa', 'name': '直连设备', 'mode': 'direct',
                 'members': ['192.168.60.0/24', '192.168.70.0/24']},
                {'id': 'default', 'name': '默认组', 'members': []}]},
            'routers': {'items': [{'id': 'router', 'type': 'routeros', 'endpoint': 'http://192.168.60.1',
                                   'username': 'read-only', 'proxy_gateway_agent_id': 'core'}]}}


class Driver:
    def __init__(self, rows):
        self.state = RouterState(nat_context={'rules': rows, 'routes': []})
        self.calls = []

    def read_state(self):
        self.calls.append('read_state')
        return self.state

    def close(self):
        self.calls.append('close')


@pytest.fixture(autouse=True)
def no_diagnostics_cache():
    service.clear_diagnostics_cache()
    yield
    service.clear_diagnostics_cache()


def cached_check(value, *, driver=None, manager=None, loader=None):
    manager = manager or Manager(*value['agents'])
    driver = driver or Driver([{'.id': '*a', 'chain': 'srcnat', 'action': 'accept',
                                'dst-address': '198.18.0.0/16'}])
    factory = lambda *_: driver
    def read(current=None, **options):
        return service.diagnostics(value if current is None else current, manager, check=True,
            driver_factory=factory, config_loader=loader or (lambda: value), **options)
    return read, driver, manager


def test_initial_state_never_contacts_a_router():
    value = config()
    result = service.diagnostics(value, Manager(*value['agents']),
                                 driver_factory=lambda *_: pytest.fail('initial GET contacted router'))
    assert result['diagnostics'][0]['state'] == 'not_checked'
    assert result['diagnostics'][0]['preserves_source_ip'] is None


def test_actual_ordered_nat_proves_only_the_effective_group_paths_without_writes():
    value = config()
    before = deepcopy(value)
    driver = Driver([{'.id': '*a', 'chain': 'srcnat', 'action': 'accept', 'dst-address': '198.18.0.0/16'},
                     {'.id': '*b', 'chain': 'srcnat', 'action': 'masquerade'}])
    result = service.diagnostics(value, Manager(*value['agents']), check=True, driver_factory=lambda *_: driver)
    row = result['diagnostics'][0]
    assert row['state'] == 'confirmed' and row['preserves_source_ip'] is True
    assert row['checked_at']
    assert driver.calls == ['read_state', 'close']
    assert value == before


def test_gateway_subnet_bypass_does_not_false_confirm_a_group_in_another_subnet():
    value = config()
    driver = Driver([{'.id': '*a', 'chain': 'srcnat', 'action': 'accept',
                      'src-address': '192.168.60.0/24', 'dst-address': '198.18.0.0/16'},
                     {'.id': '*b', 'chain': 'srcnat', 'action': 'masquerade'}])
    row = service.diagnostics(value, Manager(*value['agents']), check=True,
                              driver_factory=lambda *_: driver)['diagnostics'][0]
    # The old /24 assessment looks covered, while the actual group is not.
    assert row['checks'][0]['status'] == 'adoptable'
    assert row['checks'][0]['preserves_source_ip'] is False
    assert row['state'] == 'uncertain' and row['preserves_source_ip'] is None


def test_selected_destinations_are_checked_as_well_as_fake_ip(monkeypatch):
    value = config()
    value['routers']['items'][0]['ros_policy_rule_sets'] = ['telegram']
    def desired(_config, gateway, selected):
        assert gateway == '192.168.60.2' and selected == ['telegram']
        return {'proxy_cidrs': [{'cidr': '198.18.0.0/16'}, {'cidr': '203.0.113.0/24'}]}
    monkeypatch.setattr(service, 'compute_desired', desired)
    driver = Driver([{'.id': '*a', 'chain': 'srcnat', 'action': 'accept', 'dst-address': '198.18.0.0/16'},
                     {'.id': '*b', 'chain': 'srcnat', 'action': 'masquerade'}])
    row = service.diagnostics(value, Manager(*value['agents']), check=True,
                              driver_factory=lambda *_: driver)['diagnostics'][0]
    assert row['state'] == 'uncertain'
    assert [check['preserves_source_ip'] for check in row['checks']] == [True, False]


def test_missing_nat_evidence_and_read_failure_do_not_claim_nat_is_safe():
    value = config()
    driver = Driver([])
    driver.state.nat_context = None
    row = service.diagnostics(value, Manager(*value['agents']), check=True,
                              driver_factory=lambda *_: driver)['diagnostics'][0]
    assert row['state'] == 'uncertain' and row['preserves_source_ip'] is None
    def failed(_):
        raise RuntimeError('private router-password')
    result = service.diagnostics(value, Manager(*value['agents']), check=True, driver_factory=failed)
    assert result['diagnostics'][0]['state'] == 'failed'
    assert 'router-password' not in json.dumps(result)


def test_binding_change_during_read_invalidates_confirmation():
    value = config()
    manager = Manager(*value['agents'])
    driver = Driver([])
    original = driver.read_state
    def changed():
        manager.agents['core']['profile_id'] = 'elsewhere'
        return original()
    driver.read_state = changed
    row = service.diagnostics(value, manager, check=True, driver_factory=lambda *_: driver)['diagnostics'][0]
    assert row['state'] == 'uncertain' and row['preserves_source_ip'] is None


def test_changed_router_snapshot_invalidates_old_read():
    value = config()
    fresh = deepcopy(value)
    fresh['routers']['items'][0]['endpoint'] = 'http://192.0.2.1'
    row = service.diagnostics(value, Manager(*value['agents']), check=True,
                              driver_factory=lambda *_: Driver([]), config_loader=lambda: fresh)['diagnostics'][0]
    assert row['state'] == 'uncertain' and row['preserves_source_ip'] is None


def test_completed_cache_preserves_check_time_returns_copies_and_uses_opaque_keys(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(service, 'monotonic', lambda: now[0])
    value = config()
    value['routers']['items'][0]['password'] = 'private-router-password'
    read, driver, _ = cached_check(value)
    first = read()
    checked_at = first['diagnostics'][0]['checked_at']
    first['diagnostics'][0]['message'] = 'caller mutation'
    first['diagnostics'][0]['checks'][0]['cidr'] = 'caller mutation'
    now[0] = 109.0
    second = read()
    assert second['diagnostics'][0]['checked_at'] == checked_at
    assert second['diagnostics'][0]['state'] == 'confirmed'
    assert second['diagnostics'][0]['checks'][0]['cidr'] == '198.18.0.0/16'
    assert second['diagnostics'][0]['message'] != 'caller mutation'
    assert driver.calls == ['read_state', 'close']
    assert all(len(key) == 64 and all(char in '0123456789abcdef' for char in key) for key in service._CACHE)
    assert 'private-router-password' not in json.dumps(list(service._CACHE))
    assert 'private-token' not in json.dumps(second)


def test_cache_expires_after_ten_seconds_and_hits_do_not_extend_the_evidence(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(service, 'monotonic', lambda: now[0])
    read, driver, _ = cached_check(config())
    read()
    now[0] = 109.999
    read()
    assert driver.calls.count('read_state') == 1
    now[0] = 110.0
    read()
    assert driver.calls.count('read_state') == 2


@pytest.mark.parametrize('change', ['members', 'sources', 'password', 'username', 'endpoint', 'tls',
                                    'nat_manifest', 'dns_guard', 'agent_token', 'agent_port', 'agent_host'])
def test_configuration_changes_require_new_router_evidence(change):
    value = config()
    read, driver, manager = cached_check(value)
    assert read()['diagnostics'][0]['state'] == 'confirmed'
    router = value['routers']['items'][0]
    if change == 'members':
        value['device_groups']['groups'][0]['members'] = ['192.168.60.0/25']
    elif change == 'sources':
        value['policy_routing_sources'] = {'include': ['192.168.60.0/24'], 'exclude_groups': []}
    elif change == 'nat_manifest':
        router['ros_policy_state'] = {'generation': 'new-evidence'}
    elif change == 'dns_guard':
        router['dns_guard'] = {'enabled': True}
    elif change.startswith('agent_'):
        field = change.removeprefix('agent_')
        updated = {'token': 'new-private-token', 'port': 18081, 'host': '192.168.60.3'}[field]
        value['agents'][0][field] = updated
        manager.agents['core'][field] = updated
    else:
        field = {'tls': 'verify_tls'}.get(change, change)
        router[field] = {'password': 'changed-private-password', 'username': 'different-user',
                         'endpoint': 'http://192.168.60.254', 'tls': False}[change]
    assert read()['diagnostics'][0]['state'] == 'confirmed'
    assert driver.calls.count('read_state') == 2


def test_cached_evidence_rechecks_live_agent_binding_before_returning():
    value = config()
    read, driver, manager = cached_check(value)
    assert read()['diagnostics'][0]['state'] == 'confirmed'
    manager.agents['core']['profile_id'] = 'elsewhere'
    row = read()['diagnostics'][0]
    assert row['state'] == 'uncertain' and row['preserves_source_ip'] is None
    assert '绑定已变化' in row['message']
    assert 'checks' not in row
    assert driver.calls.count('read_state') == 1


@pytest.mark.parametrize('change', ['members', 'sources', 'password', 'agent_binding'])
def test_cached_evidence_rechecks_loader_even_when_the_caller_keeps_an_old_snapshot(change):
    value = config()
    fresh = deepcopy(value)
    read, driver, _ = cached_check(value, loader=lambda: fresh)
    assert read()['diagnostics'][0]['state'] == 'confirmed'
    if change == 'members':
        fresh['device_groups']['groups'][0]['members'] = ['192.168.90.0/24']
    elif change == 'sources':
        fresh['policy_routing_sources'] = {'include': ['192.168.60.0/24'], 'exclude_groups': []}
    elif change == 'password':
        fresh['routers']['items'][0]['password'] = 'new-private-password'
    else:
        fresh['agents'][0]['profile_id'] = 'elsewhere'
    row = read()['diagnostics'][0]
    assert row['state'] == 'uncertain' and row['preserves_source_ip'] is None
    assert '配置已变化' in row['message']
    assert driver.calls.count('read_state') == 1


def test_local_rule_contents_refresh_the_cached_destination_scope(monkeypatch):
    destinations = ['198.18.0.0/16']
    monkeypatch.setattr(service, 'compute_desired', lambda *_: {
        'proxy_cidrs': [{'cidr': cidr} for cidr in destinations]})
    driver = Driver([{'.id': '*a', 'chain': 'srcnat', 'action': 'accept', 'dst-address': '198.18.0.0/16'},
                     {'.id': '*b', 'chain': 'srcnat', 'action': 'masquerade'}])
    read, driver, _ = cached_check(config(), driver=driver)
    assert read()['diagnostics'][0]['state'] == 'confirmed'
    destinations.append('203.0.113.0/24')
    row = read()['diagnostics'][0]
    assert row['state'] == 'uncertain'
    assert [check['preserves_source_ip'] for check in row['checks']] == [True, False]
    assert driver.calls.count('read_state') == 2


def test_unavailable_fresh_configuration_does_not_return_cached_confirmation():
    value = config()
    unavailable = [False]
    def loader():
        if unavailable[0]:
            raise RuntimeError('private-loader-details')
        return value
    read, driver, _ = cached_check(value, loader=loader)
    assert read()['diagnostics'][0]['state'] == 'confirmed'
    unavailable[0] = True
    result = read()
    row = result['diagnostics'][0]
    assert row['state'] == 'uncertain' and row['preserves_source_ip'] is None
    assert '无法核对' in row['message'] and 'checks' not in row
    assert 'private-loader-details' not in json.dumps(result)
    assert driver.calls.count('read_state') == 1


def test_changed_inputs_during_router_read_are_never_cached():
    value = config()
    fresh = deepcopy(value)
    driver = Driver([{'.id': '*a', 'chain': 'srcnat', 'action': 'accept'}])
    original = driver.read_state
    def changed():
        fresh['device_groups']['groups'][0]['members'] = ['192.168.90.0/24']
        return original()
    driver.read_state = changed
    read, _, _ = cached_check(value, driver=driver, loader=lambda: fresh)
    row = read()['diagnostics'][0]
    assert row['state'] == 'uncertain' and row['preserves_source_ip'] is None
    assert '配置已变化' in row['message']
    assert not service._CACHE


def test_failed_reads_and_slow_expired_evidence_are_not_cached(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(service, 'monotonic', lambda: now[0])
    value = config()
    driver = Driver([])
    def failed():
        raise RuntimeError('private-router-password')
    driver.read_state = failed
    read, _, _ = cached_check(value, driver=driver)
    assert read()['diagnostics'][0]['state'] == 'failed'
    assert not service._CACHE
    def slow():
        now[0] += service.CACHE_SECONDS
        return driver.state
    driver.read_state = slow
    assert read()['diagnostics'][0]['state'] == 'confirmed'
    assert not service._CACHE


def test_initial_reads_and_calls_without_a_fresh_loader_do_not_reuse_checked_evidence():
    value = config()
    read, driver, manager = cached_check(value)
    assert read()['diagnostics'][0]['state'] == 'confirmed'
    row = service.diagnostics(value, manager, config_loader=lambda: value)['diagnostics'][0]
    assert row['state'] == 'not_checked'
    factory = lambda *_: driver
    service.diagnostics(value, manager, check=True, driver_factory=factory)
    service.diagnostics(value, manager, check=True, driver_factory=factory)
    assert driver.calls.count('read_state') == 3


def test_cache_is_bounded_to_sixty_four_completed_responses(monkeypatch):
    monkeypatch.setattr(service, 'monotonic', lambda: 100.0)
    value = config()
    read, driver, _ = cached_check(value)
    for revision in range(service.MAX_CACHE_ENTRIES + 1):
        value['_revision'] = revision
        assert read()['diagnostics'][0]['state'] == 'confirmed'
    assert len(service._CACHE) == 64
    value['_revision'] = 0
    read()
    assert driver.calls.count('read_state') == 66
