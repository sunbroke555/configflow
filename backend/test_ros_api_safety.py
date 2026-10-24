"""Router API locking, manifest ownership and deletion safety regressions."""
import copy
from contextlib import contextmanager
import multiprocessing

import pytest

from backend.common.config import get_repository
from backend.routers.base import RouterError
from backend.routes import routers as routes_module
from backend.test_agent_upgrade_auth import authenticated_app  # noqa: F401
from backend.test_ros_policy import GATEWAY, ROUTER, TARGET, MemoryROS


@pytest.fixture
def router_api(authenticated_app, monkeypatch):
    client, _, agents, admin, _ = authenticated_app
    driver = MemoryROS()
    constructed, closed = [], []

    def construct(*args, **kwargs):
        constructed.append((args, kwargs))
        return driver

    monkeypatch.setitem(routes_module.DRIVERS, 'routeros', construct)
    monkeypatch.setattr(driver, 'close', lambda: closed.append(True))
    monkeypatch.setattr(routes_module, '_agent', lambda agent_id: {'host': GATEWAY})
    # Baseline receipt persistence is orthogonal to router configuration locking.
    monkeypatch.setattr(routes_module, '_capture_sync', lambda *args: None)

    def compute(config, gateway, selected):
        entries = [{'cidr': '198.18.0.0/16', 'sources': ['fake-ip']}]
        if 'telegram' in selected:
            entries.append({'cidr': TARGET, 'sources': ['Telegram']})
        return {'fake_ip_range': '198.18.0.0/16', 'proxy_gateway': gateway,
                'proxy_cidrs': entries, 'warnings': []}

    monkeypatch.setattr(routes_module, 'compute_desired', compute)
    monkeypatch.setattr('backend.routers.desired.compute_desired', compute)
    monkeypatch.setattr('backend.common.oxidns_device_policy.read_policies', lambda settings, config: {
        'editable': True, 'policies': [{'ip': '192.168.0.101', 'mode': 'smart'}]})
    created = client.post('/api/routers', headers=admin, json={**ROUTER, 'name': 'ROS',
        'username': 'admin', 'password': 'pw', 'proxy_gateway_agent_id': agents[0]['id']})
    assert created.status_code == 200, created.get_json()
    router_id = created.get_json()['router']['id']
    return client, admin, f'/api/routers/{router_id}', driver, constructed, closed


def snapshot():
    return get_repository().get_compat_config('default')


def preview(client, admin, path):
    response = client.post(path + '/plan', headers=admin)
    assert response.status_code == 200, response.get_json()
    plan = response.get_json()['plan']
    assert not plan['blockers'], plan
    return plan


def publish(client, admin, path):
    plan = preview(client, admin, path)
    response = client.post(path + '/apply', headers=admin, json={'fingerprint': plan['fingerprint']})
    assert response.status_code == 200, response.get_json()
    return plan


def change_at_lock(monkeypatch, mutate):
    original = routes_module.router_operation_lock

    @contextmanager
    def lock(router):
        # A separate repository transaction does not invalidate the waiting
        # request's compatibility cache (unlike update_config_transaction).
        get_repository().update_profile_transaction('default', mutate)
        with original(router):
            yield

    monkeypatch.setattr(routes_module, 'router_operation_lock', lock)


@pytest.mark.parametrize('change', ['settings', 'deleted'])
def test_apply_blocks_repository_replacement_while_waiting_before_driver_creation(router_api, monkeypatch, change):
    client, admin, path, driver, constructed, closed = router_api
    plan = preview(client, admin, path)
    before = snapshot()
    constructed.clear()
    closed.clear()

    def mutate(config):
        if change == 'deleted':
            config['routers']['items'] = []
        else:
            config['routers']['items'][0]['endpoint'] = 'http://192.168.0.2'

    change_at_lock(monkeypatch, mutate)
    response = client.post(path + '/apply', headers=admin, json={'fingerprint': plan['fingerprint']})
    assert response.status_code == 409, response.get_json()
    assert not constructed and not closed and not driver.writes
    assert snapshot() == before


@pytest.mark.parametrize('method', ['put', 'delete', 'policy', 'route-plan', 'route-apply'])
def test_locked_router_callers_reject_changed_settings(router_api, monkeypatch, method):
    client, admin, path, driver, constructed, closed = router_api
    payload = {'action': 'add', 'values': {'cidr': '203.0.113.0/24', 'gateway': GATEWAY,
                                         'enabled': True, 'comment': 'manual'}}
    if method == 'route-apply':
        response = client.post(path + '/routes/plan', headers=admin, json=payload)
        assert response.status_code == 200, response.get_json()
        payload['fingerprint'] = response.get_json()['plan']['fingerprint']
    constructed.clear()
    closed.clear()
    change_at_lock(monkeypatch, lambda config: config['routers']['items'][0].update(name='concurrent'))
    if method == 'put':
        response = client.put(path, headers=admin, json={'name': 'stale'})
    elif method == 'delete':
        response = client.delete(path, headers=admin)
    elif method == 'policy':
        response = client.get(path + '/policy', headers=admin)
    else:
        response = client.post(path + '/routes/' + method.removeprefix('route-'), headers=admin, json=payload)
    assert response.status_code == 409, response.get_json()
    assert snapshot()['routers']['items'][0]['name'] == 'concurrent'
    assert not constructed and not closed and not driver.writes


@pytest.mark.parametrize('clear_selection', [False, True])
def test_delete_active_policy_preserves_manifest_config_and_device(router_api, clear_selection):
    client, admin, path, driver, constructed, closed = router_api
    publish(client, admin, path)
    if clear_selection:
        response = client.put(path, headers=admin, json={'ros_policy_rule_sets': []})
        assert response.status_code == 200, response.get_json()
    before = snapshot()
    assert before['routers']['items'][0]['ros_policy_state']['resources']
    rows, writes = copy.deepcopy(driver.rows), copy.deepcopy(driver.writes)
    constructed.clear()
    closed.clear()
    response = client.delete(path, headers=admin)
    assert response.status_code == 409, response.get_json()
    assert snapshot() == before
    assert driver.rows == rows and driver.writes == writes
    assert not constructed and not closed


def test_delete_after_published_withdrawal_only_removes_record(router_api):
    client, admin, path, driver, constructed, closed = router_api
    publish(client, admin, path)
    response = client.put(path, headers=admin, json={'ros_policy_rule_sets': []})
    assert response.status_code == 200, response.get_json()
    publish(client, admin, path)
    assert not snapshot()['routers']['items'][0].get('ros_policy_state')
    rows, writes = copy.deepcopy(driver.rows), copy.deepcopy(driver.writes)
    constructed.clear()
    closed.clear()
    response = client.delete(path, headers=admin)
    assert response.status_code == 200, response.get_json()
    assert snapshot()['routers']['items'] == []
    assert driver.rows == rows and driver.writes == writes
    assert not constructed and not closed


def test_apply_refreshes_config_after_slow_execution_before_saving_manifest(router_api, monkeypatch):
    client, admin, path, driver, _, _ = router_api
    original = routes_module.execute

    def execute(*args):
        result = original(*args)
        get_repository().update_profile_transaction('default',
            lambda config: config['routers']['items'][0].update(name='concurrent'))
        return result

    monkeypatch.setattr(routes_module, 'execute', execute)
    publish(client, admin, path)
    stored = snapshot()['routers']['items'][0]
    assert stored['name'] == 'concurrent'
    assert stored['ros_policy_state'] == driver.ros_policy_manifest
    assert stored['last_release']['ok']


def test_apply_closes_driver_on_inspection_failure(router_api, monkeypatch):
    client, admin, path, driver, constructed, closed = router_api
    plan = preview(client, admin, path)
    constructed.clear()
    closed.clear()

    def fail(*args):
        raise RouterError('read failed')

    monkeypatch.setattr(routes_module, '_inspect', fail)
    response = client.post(path + '/apply', headers=admin, json={'fingerprint': plan['fingerprint']})
    assert response.status_code == 502, response.get_json()
    assert len(constructed) == len(closed) == 1
    assert not driver.writes


def _hold_connection_job_lock(data_dir, connection, ready, release):
    from backend.common import config as config_module
    from backend.routers.rule_sync_store import job_lock

    config_module.DATA_DIR = data_dir
    # Different profile/id proves exclusion is keyed by the device connection.
    with job_lock('other-profile', 'other-router', connection) as acquired:
        ready.send(acquired)
        if acquired:
            if not release.wait(30):
                raise RuntimeError('Timed out waiting to release test job lock')
    ready.close()


@pytest.mark.parametrize('published', [False, True])
@pytest.mark.parametrize('operation', ['delete', 'put', 'policy-settings', 'rule-sync', 'probe'])
def test_router_mutations_exclude_cross_process_publication(router_api, published, operation):
    from backend.common import config as config_module
    from backend.common.device_notes import policy_names_revision
    from backend.routers.policy_workspace import FIELDS, draft_revision

    client, admin, path, driver, constructed, closed = router_api
    if published:
        publish(client, admin, path)
    plan = preview(client, admin, path)
    before = snapshot()
    router = before['routers']['items'][0]
    rows, writes = copy.deepcopy(driver.rows), copy.deepcopy(driver.writes)
    constructed.clear()
    closed.clear()
    context = multiprocessing.get_context('spawn')
    receiver, sender = context.Pipe(duplex=False)
    release = context.Event()
    process = context.Process(target=_hold_connection_job_lock,
                              args=(config_module.DATA_DIR, router, sender, release))
    process.start()
    sender.close()
    try:
        assert receiver.poll(20), 'Child did not acquire the publication lock'
        assert receiver.recv() is True
        if operation == 'delete':
            response = client.delete(path, headers=admin)
        elif operation == 'put':
            response = client.put(path, headers=admin, json={'name': 'changed'})
        elif operation == 'policy-settings':
            response = client.put(path + '/policy-settings', headers=admin, json={
                'revision': draft_revision(before, router), 'names_revision': policy_names_revision(before),
                'router': {field: router[field] for field in FIELDS},
                'policies': [{'ip': '192.168.0.101', 'mode': 'smart'}],
                'base_url': '',
            })
        elif operation == 'rule-sync':
            response = client.put(path + '/rule-sync', headers=admin, json={
                'auto_rule_sync': True, 'rule_sync_interval': 120,
            })
        else:
            response = client.post(path + '/probe', headers=admin)
        assert response.status_code == 409, response.get_json()
        assert snapshot() == before
        assert driver.rows == rows and driver.writes == writes
        assert not constructed and not closed
        # Read-only settings remain available while publication holds the lock.
        for endpoint in ('policy-settings', 'rule-sync'):
            response = client.get(path + '/' + endpoint, headers=admin)
            assert response.status_code == 200, response.get_json()
    finally:
        release.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(10)
        receiver.close()
    assert process.exitcode == 0
    response = client.post(path + '/apply', headers=admin, json={'fingerprint': plan['fingerprint']})
    assert response.status_code == 200, response.get_json()
    stored = snapshot()['routers']['items'][0]
    assert stored['ros_policy_state'] == driver.ros_policy_manifest
    assert stored['ros_policy_state']['resources']
    assert stored['last_release']['ok']
