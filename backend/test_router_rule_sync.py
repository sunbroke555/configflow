"""Remote-source updates never broaden ownership or publish unrelated drafts."""
import copy
from pathlib import Path

import pytest

from backend.common import config as config_module
from backend.common.config_repository import ProfileRepository
from backend.routers.base import RouterError
from backend.routers import rule_sync, rule_sync_store as store
from backend.routers.rule_sync_rules import classify, check_updates, load_sources, loader
from backend.routers.plan import build_plan
from backend.routers.release import execute
from backend.test_ros_policy import MemoryROS, GATEWAY, ADDRESSES, MANGLE, ROUTES, NAT, TABLES

OLD = 'IP-CIDR,1.1.1.0/24\nDOMAIN-SUFFIX,example.com'
NEW = 'IP-CIDR,8.8.8.0/24\nDOMAIN-SUFFIX,new.example.com'


@pytest.mark.parametrize('content', [OLD, 'payload:\n  - IP-CIDR,1.1.1.0/24\n  - DOMAIN,example.com',
                                     '1.1.1.0/24\nexample.com'])
def test_mixed_sources_only_extract_public_ipv4(content):
    parsed = classify(content, 'classical')
    assert parsed['ipv4'] == ['1.1.1.0/24']
    assert parsed['domain_count'] == 1 and parsed['invalid_count'] == 0


@pytest.mark.parametrize('content,behavior', [('', 'classical'), ('# empty', 'classical'),
    ('<html>Bad gateway</html>', 'classical'), ('["1.1.1.1"]', 'classical'),
    ('1.1.1.1\nexample.com', 'ipcidr'), ('DOMAIN-SUFFIX,example.com', 'ipcidr'),
    ('1.1.1.0/bad', 'ipcidr'), ('payload:\n - [bad]', 'classical'), ('payload: bad', 'classical'),
    ('IP-CIDR,::1/128', 'classical'), ('IP-CIDR6,1.1.1.0/24', 'classical'),
    ('please log in', 'classical'), ('payload: [unclosed', 'classical')])
def test_invalid_downloads_never_become_empty_routing_intent(content, behavior):
    with pytest.raises(RouterError): classify(content, behavior)


def test_each_selected_source_has_its_own_empty_guard():
    old = {'a': {**classify(OLD), 'name': 'A', 'source_type': 'url'},
           'b': {**classify(NEW), 'name': 'B', 'source_type': 'url'}}
    current = copy.deepcopy(old)
    current['a'].update(classify('DOMAIN,only.example.com'))
    with pytest.raises(RouterError, match='A.*变为 0'):
        check_updates(old, current)


@pytest.fixture
def environment(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    monkeypatch.setattr(config_module, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(config_module, '_repository', repository)
    config_module.reset_config_context()
    router = {'id': 'ros', 'name': 'ROS', 'type': 'routeros', 'endpoint': 'http://192.168.0.1',
              'username': 'test', 'password': 'secret', 'verify_tls': True, 'proxy_gateway_agent_id': 'gw',
              'ip_rule_sets': [], 'ros_policy_rule_sets': ['remote'], 'manage_return_routing': False,
              'dns_agent_id': '', 'auto_rule_sync': True, 'rule_sync_interval': 60}
    repository.update_shared_transaction(lambda doc: doc['rule_library'].append(
        {'id': 'lib', 'name': 'Remote', 'source_type': 'url', 'url': 'https://example.test/ips',
         'behavior': 'classical', 'enabled': True}))
    def seed(config):
        config['routers'] = {'items': [router]}
        config['rule_configs'] = [{'id': 'remote', 'itemType': 'ruleset', 'library_rule_id': 'lib',
                                   'enabled': True, 'policy': 'PROXY'}]
        config['oxidns']['device_policies'] = [{'ip': '192.168.0.101', 'mode': 'smart'}]
    repository.update_profile_transaction('default', seed)
    repository.write_shared_text('rules/Remote.list', OLD)
    driver = MemoryROS()
    monkeypatch.setattr('backend.routes.routers._agent', lambda agent_id: {'id': 'gw', 'host': GATEWAY} if agent_id else None)
    monkeypatch.setattr('backend.routes.routers._driver', lambda router: driver)
    monkeypatch.setattr('backend.common.oxidns_device_policy.read_policies',
                        lambda settings, config: {'editable': True, 'policies': settings['device_policies']})
    yield repository, driver
    config_module.reset_config_context()


def publish(environment):
    from backend.routes.routers import _inspect
    repository, driver = environment
    config = rule_sync.config_for('default')
    router = config['routers']['items'][0]
    _, desired, state, report = _inspect(config, router, driver)
    plan = build_plan(router, desired, state, report, report['return_routing'], driver)
    assert not plan['blockers'], plan['blockers']
    result = execute(plan, driver, None)
    assert result['ok'], result
    rule_sync._save_manifest('default', router, driver)
    rule_sync.capture_manual('default', config, router, driver, desired)
    receipt = store.read('default', 'ros')
    assert receipt.get('baseline'), receipt
    driver.writes.clear()
    return receipt


def test_policy_remote_change_updates_only_destinations_and_remains_withdrawable(environment):
    repo, driver = environment
    publish(environment)
    kept = {path: copy.deepcopy(driver.rows[path]) for path in (MANGLE, NAT, TABLES, ROUTES)}
    # A different manually maintained list must survive both refresh and withdrawal.
    driver.rows[ADDRESSES].append({'.id': '*foreign', 'list': 'Manual', 'address': '9.9.9.9'})
    result = rule_sync.sync('default', 'ros', lambda *_: NEW)
    assert result['status'] == 'synced', result
    assert (result['added'], result['removed']) == (1, 1)
    assert all(path.startswith(ADDRESSES) for _, path, _ in driver.writes)
    assert all(driver.rows[path] == rows for path, rows in kept.items())
    assert result['rules'][0]['version'] == result['rules'][0]['synced_version']
    from backend.utils.rule_utils import rule_cache_filename
    assert (repo.shared_rules_dir() / rule_cache_filename({'id': 'lib'})).read_text() == NEW
    repo.update_profile_transaction('default', lambda doc: doc['routers']['items'][0].update(ros_policy_rule_sets=[]))
    from backend.routes.routers import _inspect
    config = rule_sync.config_for('default'); router = config['routers']['items'][0]
    _, desired, state, report = _inspect(config, router, driver)
    plan = build_plan(router, desired, state, report, report['return_routing'], driver)
    assert not plan['blockers'], plan['blockers']
    assert execute(plan, driver, None)['ok']
    assert driver.rows[ADDRESSES] == [{'.id': '*foreign', 'list': 'Manual', 'address': '9.9.9.9'}]


def test_static_remote_refresh_does_not_delete_unenrolled_cf_routes(environment):
    repo, driver = environment
    repo.update_profile_transaction('default', lambda doc: doc['routers']['items'][0].update(
        ros_policy_rule_sets=[], ip_rule_sets=['remote']))
    publish(environment)
    driver.rows[ROUTES].append({'.id': '*manual', 'dst-address': '9.9.9.9/32', 'gateway': GATEWAY,
        'comment': 'cf-hand-created', 'routing-table': 'main', 'disabled': 'false', 'active': 'true',
        'immediate-gw': GATEWAY + '%bridge'})
    result = rule_sync.sync('default', 'ros', lambda *_: NEW)
    assert result['status'] == 'synced', result
    assert (result['added'], result['removed']) == (1, 1)
    assert any(row['.id'] == '*manual' for row in driver.rows[ROUTES])
    assert all(path.startswith(ROUTES) for _, path, _ in driver.writes)


def test_domain_only_change_and_unchanged_round_do_not_write_or_change_config_revision(environment):
    repo, driver = environment
    publish(environment)
    revision = repo.config_revision('default')
    for text in (OLD, OLD.replace('example.com', 'other.example.com')):
        result = rule_sync.sync('default', 'ros', lambda *_: text)
        assert result['status'] == 'synced', result
        assert result['added'] == result['removed'] == 0
        assert driver.writes == []
        assert repo.config_revision('default') == revision


@pytest.mark.parametrize('mode', ['empty', 'domain_only', 'bad', 'download', 'gateway', 'source', 'selection', 'external'])
def test_invalid_or_unpublished_changes_do_not_write_ros(environment, mode):
    repo, driver = environment
    publish(environment)
    if mode == 'gateway':
        repo.update_profile_transaction('default', lambda doc: doc['routers']['items'][0].update(proxy_gateway_agent_id='changed'))
    if mode == 'source':
        repo.update_profile_transaction('default', lambda doc: doc['oxidns'].update(device_policies=[]))
    if mode == 'selection':
        repo.update_profile_transaction('default', lambda doc: doc['routers']['items'][0].update(ros_policy_rule_sets=[]))
    if mode == 'external':
        driver.rows[MANGLE][-1]['comment'] = 'changed in WinBox'
    def fetch(*_):
        if mode == 'download': raise RouterError('download failed')
        return {'empty': '', 'domain_only': 'DOMAIN,example.com', 'bad': '<html>error</html>'}.get(mode, NEW)
    result = rule_sync.sync('default', 'ros', fetch)
    assert result['status'] in ('failed', 'paused'), result
    assert not driver.writes
    assert (repo.shared_rules_dir() / 'Remote.list').read_text() == OLD


def test_failure_rolls_back_destinations_and_preserves_remapped_ownership(environment):
    repo, driver = environment
    publish(environment)
    old = copy.deepcopy(driver.rows[ADDRESSES])
    driver.fail = ('DELETE', ADDRESSES, driver.counts['DELETE', ADDRESSES] + 1)
    result = rule_sync.sync('default', 'ros', lambda *_: NEW)
    assert result['status'] == 'paused', result
    assert driver.rows[ADDRESSES] == old
    saved = repo.get_profile('default')['routers']['items'][0]['ros_policy_state']
    assert saved == store.read('default', 'ros')['baseline']['policy']
    assert (repo.shared_rules_dir() / 'Remote.list').read_text() == OLD


def test_no_sync_without_manual_baseline_or_when_disabled(environment):
    repo, driver = environment
    with pytest.raises(RouterError, match='成功发布'):
        rule_sync.sync('default', 'ros', lambda *_: NEW)
    publish(environment)
    repo.update_profile_transaction('default', lambda doc: doc['routers']['items'][0].update(auto_rule_sync=False))
    with pytest.raises(RouterError, match='开启'):
        rule_sync.sync('default', 'ros', lambda *_: NEW)
    assert not driver.writes


def test_failure_after_delete_remaps_ids_and_next_withdrawal_still_cleans(environment, monkeypatch):
    repo, driver = environment
    publish(environment)
    original = copy.deepcopy(driver.ros_policy_manifest)
    from backend.routers import release as release_module
    verify = release_module._verify
    def fail_readback(stage, *args):
        if stage == 'routes': raise RouterError('injected readback failure')
        return verify(stage, *args)
    monkeypatch.setattr(release_module, '_verify', fail_readback)
    result = rule_sync.sync('default', 'ros', lambda *_: NEW)
    assert result['status'] == 'paused'
    saved = repo.get_profile('default')['routers']['items'][0]['ros_policy_state']
    assert saved['spec'] == original['spec']
    assert saved == store.read('default', 'ros')['baseline']['policy']
    assert saved['resources'] != original['resources']  # restored entries have new IDs
    assert not any(row.get('address') == '8.8.8.0/24' for row in driver.rows[ADDRESSES])
    monkeypatch.setattr(release_module, '_verify', verify)
    repo.update_profile_transaction('default', lambda doc: doc['routers']['items'][0].update(ros_policy_rule_sets=[]))
    from backend.routes.routers import _inspect
    config = rule_sync.config_for('default'); router = config['routers']['items'][0]
    _, desired, state, report = _inspect(config, router, driver)
    plan = build_plan(router, desired, state, report, report['return_routing'], driver)
    assert execute(plan, driver, None)['ok']
    assert not driver.rows[ADDRESSES]


def test_disable_or_change_draft_during_download_never_writes(environment):
    repo, driver = environment
    publish(environment)
    def fetch(*_):
        repo.update_profile_transaction('default', lambda doc: doc['routers']['items'][0].update(auto_rule_sync=False))
        return NEW
    result = rule_sync.sync('default', 'ros', fetch)
    assert result['status'] == 'paused' and not driver.writes
    assert repo.get_profile('default')['routers']['items'][0]['auto_rule_sync'] is False


def test_foreign_reference_is_not_overwritten_or_deleted(environment):
    repo, driver = environment
    publish(environment)
    target = driver.rows[MANGLE][-1]['dst-address-list']
    foreign = {'.id': '*foreign', 'chain': 'forward', 'action': 'accept', 'dst-address-list': target}
    driver.rows[MANGLE].append(foreign)
    result = rule_sync.sync('default', 'ros', lambda *_: NEW)
    assert result['status'] == 'paused' and not driver.writes
    assert driver.rows[MANGLE][-1] == foreign


def test_download_failure_retries_but_interrupted_write_requires_manual_check(environment):
    repo, driver = environment
    publish(environment)
    def fail(*_): raise rule_sync.DownloadError('network unavailable')
    assert rule_sync.sync('default', 'ros', fail)['status'] == 'failed'
    assert rule_sync.sync('default', 'ros', lambda *_: NEW)['status'] == 'synced'
    receipt = store.read('default', 'ros'); receipt['status'] = 'running'
    store.write('default', 'ros', receipt)
    driver.writes.clear()
    with pytest.raises(RouterError): rule_sync.sync('default', 'ros', lambda *_: OLD)
    assert store.read('default', 'ros')['status'] == 'paused' and not driver.writes


def test_scheduler_skips_disabled_and_not_due_routers(environment):
    repo, driver = environment
    publish(environment)
    calls = []
    def fetch(*_): calls.append(True); return NEW
    rule_sync.run_round(fetch)
    assert len(calls) == 1
    driver.writes.clear()
    rule_sync.run_round(fetch)
    assert len(calls) == 1 and not driver.writes
    repo.update_profile_transaction('default', lambda doc: doc['routers']['items'][0].update(auto_rule_sync=False))
    rule_sync.run_round(fetch)
    assert len(calls) == 1


def test_shared_remote_download_is_reused_without_persistent_memory_cache(monkeypatch):
    from backend.utils import rule_fetch
    calls = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def raise_for_status(self): pass
        def iter_content(self, size): yield NEW.encode()
    monkeypatch.setattr(rule_fetch, 'request_rule', lambda *a, **k: calls.append(k) or Response())
    downloads = rule_sync.Downloads()
    source = {'url': 'https://example.test/rules'}
    assert downloads(source, {}) == downloads(source, {}) == NEW
    assert len(calls) == 1 and calls[0]['stream'] is True
    assert downloads(source, {'system_config': {'rule_fetch_proxy': 'different'}}) == NEW
    assert len(calls) == 2


def test_shared_ip_survives_removal_from_one_source(environment):
    repo, driver = environment
    repo.update_shared_transaction(lambda doc: doc['rule_library'].append(
        {'id': 'second', 'name': 'Second', 'source_type': 'url', 'url': 'https://example.test/second',
         'behavior': 'classical', 'enabled': True}))
    def select(config):
        config['rule_configs'].append({'id': 'second', 'itemType': 'ruleset', 'library_rule_id': 'second', 'enabled': True})
        config['routers']['items'][0]['ros_policy_rule_sets'].append('second')
    repo.update_profile_transaction('default', select)
    repo.write_shared_text('rules/Second.list', OLD)
    publish(environment)
    result = rule_sync.sync('default', 'ros', lambda source, _: NEW if source['id'] == 'lib' else OLD)
    assert result['status'] == 'synced', result
    assert (result['added'], result['removed']) == (1, 0)
    assert any(row['address'] == '1.1.1.0/24' for row in driver.rows[ADDRESSES])


def test_second_source_going_empty_cannot_hide_behind_first_source(environment):
    repo, driver = environment
    repo.update_shared_transaction(lambda doc: doc['rule_library'].append(
        {'id': 'second', 'name': 'Second', 'source_type': 'url', 'url': 'https://example.test/second',
         'behavior': 'classical', 'enabled': True}))
    def select(config):
        config['rule_configs'].append({'id': 'second', 'itemType': 'ruleset', 'library_rule_id': 'second', 'enabled': True})
        config['routers']['items'][0]['ros_policy_rule_sets'].append('second')
    repo.update_profile_transaction('default', select)
    repo.write_shared_text('rules/Second.list', OLD)
    publish(environment)
    result = rule_sync.sync('default', 'ros', lambda source, _: NEW if source['id'] == 'lib' else 'DOMAIN,example.com')
    assert result['status'] == 'paused' and 'Second' in result['message']
    assert not driver.writes
    assert (repo.shared_rules_dir() / 'Remote.list').read_text() == OLD


def test_api_defaults_validation_authentication_and_receipt_privacy(authenticated_app, monkeypatch):
    client, _, _, admin, _ = authenticated_app
    from backend.common.config import get_repository
    payload = {'name': 'ROS', 'type': 'routeros', 'endpoint': 'http://192.0.2.1', 'username': 'admin', 'password': 'private'}
    response = client.post('/api/routers', headers=admin, json=payload)
    assert response.status_code == 200
    router = response.get_json()['router']; path = '/api/routers/' + router['id'] + '/rule-sync'
    assert router['auto_rule_sync'] is False
    initial = client.get(path, headers=admin).get_json()
    assert initial['settings'] == {'auto_rule_sync': False, 'rule_sync_interval': 60}
    for method in ('get', 'put', 'post'):
        assert getattr(client, method)(path).status_code == 401
    for body in ({'auto_rule_sync': 'true', 'rule_sync_interval': 60},
                 {'auto_rule_sync': True, 'rule_sync_interval': 1},
                 {'auto_rule_sync': True, 'rule_sync_interval': True},
                 {'auto_rule_sync': True, 'rule_sync_interval': 60, 'endpoint': 'elsewhere'}):
        assert client.put(path, headers=admin, json=body).status_code == 400
    response = client.put(path, headers=admin, json={'auto_rule_sync': True, 'rule_sync_interval': 15})
    assert response.status_code == 200
    assert client.post(path, headers=admin).status_code == 409
    store.write('default', router['id'], {'status': 'synced', 'baseline': {'control': 'not-public', 'static_resources': [{'secret': 'not-public'}], 'rules': {}}})
    response = client.get(path, headers=admin)
    assert b'not-public' not in response.data and b'private' not in response.data
    other = get_repository().create_profile({'id': 'other', 'name': 'Other'})
    assert client.get('/api/profiles/other/routers/' + router['id'] + '/rule-sync', headers=admin).status_code == 404


from backend.test_agent_upgrade_auth import authenticated_app  # noqa: E402, F401


def test_host_address_normalization_does_not_cause_repeated_writes(environment):
    repo, driver = environment
    host = 'IP-CIDR,1.1.1.1/32\nDOMAIN,example.com'
    repo.write_shared_text('rules/Remote.list', host)
    publish(environment)
    assert any(row['address'] == '1.1.1.1' for row in driver.rows[ADDRESSES])
    result = rule_sync.sync('default', 'ros', lambda *_: host)
    assert result['status'] == 'synced' and not driver.writes
    result = rule_sync.sync('default', 'ros', lambda *_: host.replace('example.com', 'other.example.com'))
    assert result['status'] == 'synced' and not driver.writes


def test_existing_cf_route_is_not_silently_enrolled_by_an_automatic_update(environment):
    repo, driver = environment
    repo.update_profile_transaction('default', lambda doc: doc['routers']['items'][0].update(
        ros_policy_rule_sets=[], ip_rule_sets=['remote']))
    publish(environment)
    foreign = {'.id': '*foreign', 'dst-address': '8.8.8.0/24', 'gateway': GATEWAY,
               'routing-table': 'main', 'active': 'true', 'disabled': 'false', 'comment': 'cf-manual',
               'immediate-gw': GATEWAY + '%bridge'}
    driver.rows[ROUTES].append(foreign)
    result = rule_sync.sync('default', 'ros', lambda *_: NEW)
    assert result['status'] == 'paused' and not driver.writes
    assert not any(item['id'] == '*foreign' for item in store.read('default', 'ros')['baseline']['static_resources'])


def test_synced_receipt_preserves_dependency_order_for_later_withdrawal(environment):
    repo, driver = environment
    publish(environment)
    assert rule_sync.sync('default', 'ros', lambda *_: NEW)['status'] == 'synced'
    resources = driver.ros_policy_manifest['resources']
    assert max(i for i, item in enumerate(resources) if item['path'] == ADDRESSES) < min(
        i for i, item in enumerate(resources) if item['path'] == MANGLE)


def test_api_background_sync_and_concurrent_manual_write_are_serialized(authenticated_app, environment, monkeypatch):
    import time
    import threading
    client, _, _, admin, _ = authenticated_app
    repo, driver = environment
    publish(environment)
    entered, finish = threading.Event(), threading.Event()
    def fetch(*_):
        entered.set()
        assert finish.wait(3)
        return NEW
    monkeypatch.setattr(rule_sync, 'Downloads', lambda: fetch)
    path = '/api/routers/ros'
    response = client.post(path + '/rule-sync', headers=admin)
    assert response.status_code == 202, response.get_json()
    assert entered.wait(3)
    try:
        response = client.post(path + '/apply', headers=admin, json={'fingerprint': 'test'})
        assert response.status_code == 409 and '正在发布或同步' in response.get_json()['message']
        response = client.post(path + '/routes/apply', headers=admin, json={})
        assert response.status_code == 409
        assert client.post(path + '/rule-sync', headers=admin).status_code == 409
    finally:
        finish.set()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        response = client.get(path + '/rule-sync', headers=admin).get_json()
        if response['sync']['status'] not in ('running', 'queued'):
            break
        time.sleep(.01)
    assert response['sync']['status'] == 'synced', response
    assert response['sync']['added'] == response['sync']['removed'] == 1
    while time.monotonic() < deadline:
        if rule_sync._worker.acquire(blocking=False):
            rule_sync._worker.release()
            break
        time.sleep(.01)
    else:
        pytest.fail('background worker did not finish')


def test_large_input_guard_and_duplicate_cache_names(environment, monkeypatch):
    monkeypatch.setattr('backend.routers.rule_sync_rules.MAX_IPS', 1)
    with pytest.raises(RouterError, match='20000'):
        classify('1.1.1.1\n8.8.8.8', 'ipcidr')
    repo, driver = environment
    repo.update_shared_transaction(lambda doc: doc['rule_library'].append({
        'id': 'collision', 'name': 'Remote.', 'source_type': 'url', 'url': 'https://example.test/other'}))
    from backend.utils.rule_utils import rule_cache_filename
    repo.write_shared_text('rules/' + rule_cache_filename({'id': 'lib'}), OLD)
    contents, _ = load_sources(rule_sync.config_for('default'), repo.get_profile('default')['routers']['items'][0])
    assert contents == {'remote': OLD}
    assert not driver.writes
