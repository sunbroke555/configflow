"""Post-publication verification must preserve scope and evidence boundaries."""
from copy import deepcopy
from concurrent.futures import Future
import threading

import pytest

from backend.agents import ipv6_verification as verify
from backend.agents import ipv6_readiness as bridge
from backend.agents.oxidns_monitor import MonitorError
from backend.test_ipv6_readiness import environment  # noqa: F401
from backend.test_agent_upgrade_auth import authenticated_app  # noqa: F401


class InlineExecutor:
    def submit(self, fn, *args):
        future = Future()
        try:
            future.set_result(fn(*args))
        except Exception as error:
            future.set_exception(error)
        return future


def complete_runtime():
    return {'success': True, 'checks': [{'id': ident, 'label': ident, 'status': 'ok', 'message': '已取得证据'}
                                      for ident in ('deployed_config', 'runtime_core', 'runtime_ipv6',
                                                    'runtime_tun', 'proxy_http', 'node_ipv6_transport',
                                                    'dns_a', 'dns_aaaa')]}


@pytest.fixture
def verification(environment, monkeypatch):
    client, manager, agent, headers, path, router, calls, fail = environment
    monkeypatch.setattr(verify, '_executor', InlineExecutor())
    monkeypatch.setattr(bridge, '_inspect', lambda *a, **k: {'checks': [], 'routers': []})
    responses = []
    def request(target, operation='', payload=None):
        calls.append((operation, payload))
        if responses:
            return responses.pop(0)
        return complete_runtime()
    monkeypatch.setattr(bridge, '_request', request)
    return client, manager, agent, headers, path, calls, responses


def publish(manager, agent, ident='publication-one', status='succeeded'):
    _, revision = manager.repository.get_publish_snapshot('v6')
    manager.begin_deployment(agent['id'], ident)
    manager.record_deployment(agent['id'], ident, {'status': status, 'success': status == 'succeeded',
                                                  'config_revision': revision, 'config_version': 'a' * 64})
    return ident, revision


def snapshot(monkeypatch, config, ident):
    from backend.agents import rule_sync_store
    monkeypatch.setattr(rule_sync_store, 'load', lambda *a, **k: {
        'source': deepcopy(config), 'manifest': {'deployment_id': ident,
                                               'files': [{'role': 'config', 'sha256': 'a' * 64}]}})


def test_manual_verification_persists_separate_results_without_claiming_client_path(verification):
    client, manager, agent, headers, path, calls, _ = verification
    before, revision = manager.repository.get_publish_snapshot('v6')
    response = client.post(path + '/verify', headers=headers, json={'expected_config_revision': revision})
    assert response.status_code == 200, response.get_json()
    report = response.get_json()['verification']
    assert report['status'] == 'warning' and report['scope'] == 'mihomo_runtime'
    assert next(c for c in report['checks'] if c['id'] == 'runtime_expected_config')['status'] == 'unknown'
    assert next(c for c in report['checks'] if c['id'] == 'client_not_configured')['status'] == 'unknown'
    after, current_revision = manager.repository.get_publish_snapshot('v6')
    # Device reports live in the repository too, but must not change generated
    # configuration or its revision.
    assert current_revision == revision
    assert after['mihomo'] == before['mihomo']
    assert after['proxy_groups'] == before['proxy_groups']
    assert after['rule_configs'] == before['rule_configs']
    saved = client.get(path + '/verify', headers=headers).get_json()['verification']
    assert saved == report
    assert [c[0] for c in calls] == ['verify']


def test_verification_rejects_external_targets_and_stale_manual_revision(verification):
    client, _, _, headers, path, calls, _ = verification
    for body in ({'test_url': 'http://other.test'}, {'host': '::1'}, {'expected_config_revision': -1}):
        assert client.post(path + '/verify', headers=headers, json=body).status_code in (400, 409)
    assert not calls
    assert client.post(path + '/verify', json={}).status_code == 401
    assert client.get(path + '/verify', headers={**headers, 'X-ConfigFlow-Profile': 'other'}).status_code == 409


@pytest.mark.parametrize('status', ['ready', 'failed', 'rolled_back', 'checking'])
def test_unconfirmed_publications_never_start_network_verification(verification, status):
    client, manager, agent, headers, path, calls, _ = verification
    ident, _ = publish(manager, agent, status=status)
    response = client.post(path + '/verify', headers=headers, json={'deployment_id': ident})
    assert response.status_code == 409
    assert not calls


def test_auto_verification_uses_exact_published_snapshot_and_is_idempotent(verification, monkeypatch):
    client, manager, agent, headers, path, calls, _ = verification
    original, _ = manager.repository.get_publish_snapshot('v6')
    ident, revision = publish(manager, agent)
    snapshot(monkeypatch, original, ident)
    manager.repository.save_profile('v6', {'mihomo': {'custom_config': 'ipv6: false'}})
    inspected = []
    monkeypatch.setattr(bridge, '_inspect', lambda m, a, p, config: inspected.append(config) or {'checks': [], 'routers': []})
    body = {'deployment_id': ident, 'expected_config_revision': revision}
    first = client.post(path + '/verify', headers=headers, json=body)
    assert first.status_code == 200, first.get_json()
    second = client.post(path + '/verify', headers=headers, json=body)
    assert second.get_json()['verification']['run_id'] == first.get_json()['verification']['run_id']
    assert len(calls) == 1 and inspected == [original]
    assert calls[0][1]['expected_config_sha256'] == 'a' * 64
    assert first.get_json()['verification']['config_revision'] == revision


def test_changed_target_invalidates_completed_results(verification, monkeypatch):
    client, manager, agent, headers, path, _, _ = verification
    def changed(*a, **k):
        manager.update_agent(agent['id'], {'host': '192.168.0.9'})
        return {'checks': [{'id': 'http', 'label': 'HTTP', 'status': 'ok', 'message': 'ok'}]}
    monkeypatch.setattr(bridge, '_request', changed)
    response = client.post(path + '/verify', headers=headers, json={})
    report = response.get_json()['verification']
    assert report['status'] == 'warning'
    assert [c['id'] for c in report['checks']] == ['result_stale']


def test_scheduler_runs_enrolled_success_even_without_frontend(verification, monkeypatch):
    _, manager, agent, _, _, calls, _ = verification
    config, _ = manager.repository.get_publish_snapshot('v6')
    ident, _ = publish(manager, agent)
    snapshot(monkeypatch, config, ident)
    verify.tick(manager)
    assert not calls  # Never enroll all historical success records on upgrade.
    verify.enroll(manager, agent['id'], ident, config)
    verify.tick(manager)
    verify.tick(manager)
    assert [c[0] for c in calls] == ['verify']
    assert manager.get_agent_by_id(agent['id'])['ipv6_verifications'][ident]['status'] == 'ok'


def test_scheduler_does_not_run_failed_or_manual_rollback(verification):
    _, manager, agent, _, _, calls, _ = verification
    config, _ = manager.repository.get_publish_snapshot('v6')
    ident, _ = publish(manager, agent, status='failed')
    verify.enroll(manager, agent['id'], ident, config)
    verify.tick(manager)
    assert not calls


def add_client(manager, *, profile='v6', host='192.168.0.4'):
    return manager.register_agent({'name': 'Probe', 'host': host, 'port': 27648, 'service_type': 'mosdns',
                                   'deployment_method': 'shell', 'version': '1.8.50-go', 'profile_id': profile})


def test_settings_require_explicit_independent_capable_client(verification, monkeypatch):
    client, manager, agent, headers, path, _, _ = verification
    probe = add_client(manager)
    monkeypatch.setattr(verify, '_capabilities', lambda *a: {'platform': 'linux', 'ipv6_client_probe_protocols': [1]})
    for ident in [agent['id'], 'missing', add_client(manager, profile='other', host='192.168.0.5')['id']]:
        response = client.put(path + '/verification-settings', headers=headers,
                              json={'client_agent_id': ident, 'test_url': ''})
        assert response.status_code == 409
    for url in ['file:///etc/passwd', 'https://user:password@example.test', 'https://example.test/#fragment']:
        assert client.put(path + '/verification-settings', headers=headers,
                          json={'client_agent_id': '', 'test_url': url}).status_code == 400
    response = client.put(path + '/verification-settings', headers=headers,
                          json={'client_agent_id': probe['id'], 'test_url': 'http://test.example/health'})
    assert response.status_code == 200, response.get_json()
    assert response.get_json()['settings']['client_agent_id'] == probe['id']
    assert [a['id'] for a in response.get_json()['candidates']] == [probe['id']]


def test_client_http_success_needs_matching_mihomo_flow_and_ipv6_evidence(verification, monkeypatch):
    _, manager, agent, _, _, _, _ = verification
    probe = add_client(manager)
    source = manager.get_agent_by_id(agent['id'])
    monkeypatch.setattr(verify, '_capabilities', lambda *a: {'platform': 'linux', 'ipv6_client_probe_protocols': [1]})
    def request(target, operation, payload):
        if operation == 'verify-connections':
            return {'matches': [{'source_ip': '192.168.0.4', 'source_port': 5555, 'host': 'site.test', 'chains': ['node']}],
                    'checks': []}
        return {'checks': [{'id': 'http', 'label': '网站访问', 'status': 'ok', 'message': '200'}],
                'target_host': 'site.test', 'requests': [{'source_ip': '192.168.0.4', 'source_port': 5556, 'status': 'ok'}]}
    monkeypatch.setattr(bridge, '_request', request)
    checks = verify._client_checks(manager, source, {'mihomo': {'custom_config': 'ipv6: true'}},
                                   {'client_agent_id': probe['id']}, 'https://site.test/', 'test-run')
    assert next(c for c in checks if c['id'] == 'client_mihomo_path')['status'] == 'unknown'
    assert next(c for c in checks if c['id'] == 'client_ipv6_transport')['status'] == 'unknown'


def test_runtime_failure_keeps_publication_success_separate(verification):
    client, manager, agent, headers, path, _, responses = verification
    responses.append({'checks': [{'id': 'http', 'label': '网站访问', 'status': 'error', 'message': '连接失败'}]})
    response = client.post(path + '/verify', headers=headers, json={})
    assert response.get_json()['verification']['status'] == 'error'
    assert not manager.get_agent_by_id(agent['id']).get('latest_deployment')


@pytest.mark.parametrize('when', ['during', 'after'])
def test_saved_manual_configuration_change_invalidates_evidence(verification, monkeypatch, when):
    client, manager, _, headers, path, _, _ = verification
    def change():
        manager.repository.save_profile('v6', {'mihomo': {'custom_config': 'ipv6: false'}})
    if when == 'during':
        def request(*a, **k):
            change()
            return complete_runtime()
        monkeypatch.setattr(bridge, '_request', request)
    response = client.post(path + '/verify', headers=headers, json={})
    if when == 'after':
        change()
        response = client.get(path + '/verify', headers=headers)
    report = response.get_json()['verification']
    assert report['stale'] and report['status'] == 'warning'
    assert '已保存配置已变化' in report['checks'][0]['details']


@pytest.mark.parametrize('when', ['during', 'after'])
def test_client_identity_is_bound_for_entire_report_lifetime(verification, monkeypatch, when):
    client, manager, agent, headers, path, _, _ = verification
    probe = add_client(manager)
    def set_client(agents):
        next(a for a in agents if a['id'] == agent['id'])['ipv6_verification_settings'] = {
            'client_agent_id': probe['id'], 'test_url': ''}
    manager._update_agents(set_client)
    def client_checks(*a, **k):
        if when == 'during':
            manager.update_agent(probe['id'], {'host': '192.168.0.24'})
        return [verify._check('client_ros_path', '客户端经过 ROS', 'ok', '已确认')]
    monkeypatch.setattr(verify, '_client_checks', client_checks)
    response = client.post(path + '/verify', headers=headers, json={})
    if when == 'after':
        manager.update_agent(probe['id'], {'host': '192.168.0.24'})
        response = client.get(path + '/verify', headers=headers)
    report = response.get_json()['verification']
    assert report['stale'] and report['status'] == 'warning'
    assert '检测客户端已变化' in report['checks'][0]['details']


@pytest.mark.parametrize('change', [{'enabled': False}, {'gateway_installation_id': 'replacement'}])
def test_identity_tracks_disable_and_reinstallation(change):
    original = {'id': 'one', 'host': '192.0.2.3', 'enabled': True, 'gateway_installation_id': 'first'}
    assert verify._identity(original) != verify._identity({**original, **change})


def test_new_publication_invalidates_completed_report(verification, monkeypatch):
    client, manager, agent, headers, path, _, _ = verification
    config, _ = manager.repository.get_publish_snapshot('v6')
    ident, _ = publish(manager, agent)
    snapshot(monkeypatch, config, ident)
    first = client.post(path + '/verify', headers=headers, json={'deployment_id': ident}).get_json()['verification']
    assert first['status'] == 'ok'
    manager.begin_deployment(agent['id'], 'replacement')
    report = client.get(path + '/verify', headers=headers).get_json()['verification']
    assert report['status'] == 'warning' and report['stale']
    assert '设备发布版本已变化' in report['checks'][0]['details']


def test_publication_failure_can_be_retried_explicitly_but_not_each_scheduler_tick(verification, monkeypatch):
    client, manager, agent, headers, path, calls, responses = verification
    config, _ = manager.repository.get_publish_snapshot('v6')
    ident, _ = publish(manager, agent)
    snapshot(monkeypatch, config, ident)
    verify.enroll(manager, agent['id'], ident, config)
    responses.append({'checks': [{'id': 'proxy_http', 'status': 'error', 'message': 'temporary failure'}]})
    verify.tick(manager)
    first = manager.get_agent_by_id(agent['id'])['ipv6_verifications'][ident]
    verify.tick(manager)
    assert first['status'] == 'error' and len(calls) == 1
    response = client.post(path + '/verify', headers=headers, json={'deployment_id': ident})
    second = response.get_json()['verification']
    assert second['status'] == 'ok' and second['run_id'] != first['run_id']
    assert len(calls) == 2


def test_old_result_cannot_bypass_expected_revision_validation(verification, monkeypatch):
    client, manager, agent, headers, path, calls, _ = verification
    config, _ = manager.repository.get_publish_snapshot('v6')
    ident, revision = publish(manager, agent)
    snapshot(monkeypatch, config, ident)
    client.post(path + '/verify', headers=headers, json={'deployment_id': ident})
    response = client.post(path + '/verify', headers=headers,
                           json={'deployment_id': ident, 'expected_config_revision': revision + 1})
    assert response.status_code == 409 and len(calls) == 1


@pytest.mark.parametrize('response', [{}, {'checks': None}, {'checks': 'ok'},
                                     {'checks': [{'id': 'proxy_http', 'status': 'ok', 'details': None}]},
                                     {'checks': [{'id': 'proxy_http', 'status': 'ok', 'details': 'text'}]}])
def test_incomplete_agent_reply_cannot_report_success(verification, monkeypatch, response):
    client, manager, agent, headers, path, _, responses = verification
    config, _ = manager.repository.get_publish_snapshot('v6')
    ident, _ = publish(manager, agent)
    snapshot(monkeypatch, config, ident)
    responses.append(response)
    report = client.post(path + '/verify', headers=headers, json={'deployment_id': ident}).get_json()['verification']
    assert report['status'] == 'warning'
    assert next(c for c in report['checks'] if c['id'] == 'runtime_evidence')['status'] == 'unknown'


def test_failed_submit_persists_retryable_failure_and_releases_queue_slot(verification, monkeypatch):
    _, manager, agent, _, _, _, _ = verification
    class BrokenExecutor:
        def submit(self, *args):
            raise RuntimeError('executor stopped')
    monkeypatch.setattr(verify, '_executor', BrokenExecutor())
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(verify, '_slots', slots)
    with pytest.raises(RuntimeError, match='executor stopped'):
        verify.start(manager, agent['id'], 'v6', {})
    assert manager.get_agent_by_id(agent['id'])['ipv6_verifications']['manual']['status'] == 'error'
    assert slots.acquire(blocking=False)
    slots.release()
    monkeypatch.setattr(verify, '_executor', InlineExecutor())
    report = verify.start(manager, agent['id'], 'v6', {})['verification']
    assert report['status'] == 'warning'
    assert all(c['id'] != 'interrupted' for c in report['checks'])


def test_worker_failure_is_persisted_without_stuck_running_state(verification, monkeypatch):
    _, manager, agent, _, _, _, _ = verification
    def broken(*args):
        raise RuntimeError('worker failed')
    monkeypatch.setattr(verify, '_execute', broken)
    report = verify.start(manager, agent['id'], 'v6', {})['verification']
    assert report['status'] == 'error' and report['checks'][0]['id'] == 'interrupted'


def test_interrupted_run_expires_and_late_worker_cannot_replace_retry(verification, monkeypatch):
    _, manager, agent, _, _, _, _ = verification
    pending = []
    class DeferredExecutor:
        def submit(self, fn):
            pending.append(fn)
    monkeypatch.setattr(verify, '_executor', DeferredExecutor())
    first = verify.start(manager, agent['id'], 'v6', {})['verification']
    def age(agents):
        next(a for a in agents if a['id'] == agent['id'])['ipv6_verifications']['manual']['started_epoch'] = 0
    manager._update_agents(age)
    verify.tick(manager)
    assert manager.get_agent_by_id(agent['id'])['ipv6_verifications']['manual']['status'] == 'error'
    monkeypatch.setattr(verify, '_executor', InlineExecutor())
    second = verify.start(manager, agent['id'], 'v6', {})['verification']
    assert second['run_id'] != first['run_id']
    pending[0]()
    assert verify.result(manager, agent['id'], 'v6')['verification'] == second


@pytest.mark.parametrize('epoch', [None, 'broken', float('nan'), float('inf'), 0])
def test_invalid_or_expired_scheduler_timestamp_is_not_stuck_running(epoch):
    assert verify._expired({'status': 'running', 'started_epoch': epoch})


def test_request_id_mismatch_cannot_confirm_client_path(verification, monkeypatch):
    _, manager, agent, _, _, _, _ = verification
    probe = add_client(manager)
    monkeypatch.setattr(verify, '_capabilities', lambda *a: {'platform': 'linux', 'ipv6_client_probe_protocols': [1]})
    monkeypatch.setattr(bridge, '_request', lambda *a: {'request_id': 'old-run', 'checks': []})
    checks = verify._client_checks(manager, manager.get_agent_by_id(agent['id']), {},
                                   {'client_agent_id': probe['id']}, 'https://site.test', 'new-run')
    assert next(c for c in checks if c['id'] == 'client_request_identity')['status'] == 'unknown'
    assert all(c['status'] == 'unknown' for c in checks)


def test_socket_check_retains_its_limited_scope():
    checks = verify._safe_checks({'checks': [{'id': 'node_ipv6_transport', 'status': 'ok',
                                            'scope': 'mihomo_ipv6_socket', 'details': None}]})
    assert checks[0]['scope'] == 'mihomo_ipv6_socket'


def test_scheduler_persists_missing_snapshot_once_and_manual_retry_recovers(verification, monkeypatch):
    from backend.agents import rule_sync_store
    client, manager, agent, headers, path, calls, _ = verification
    config, _ = manager.repository.get_publish_snapshot('v6')
    ident, _ = publish(manager, agent)
    verify.enroll(manager, agent['id'], ident, config)
    loaded = []
    monkeypatch.setattr(rule_sync_store, 'load', lambda *a, **k: loaded.append(True))
    verify.tick(manager)
    first = verify.result(manager, agent['id'], 'v6')['verification']
    assert first['status'] == 'error' and first['error_code'] == 'SNAPSHOT_UNAVAILABLE'
    assert first['deployment_id'] == ident and not first.get('stale')
    verify.tick(manager)
    assert len(loaded) == 1 and not calls
    snapshot(monkeypatch, config, ident)
    report = client.post(path + '/verify', headers=headers, json={'deployment_id': ident}).get_json()['verification']
    assert report['status'] == 'ok' and report['run_id'] != first['run_id']


@pytest.mark.parametrize('code', ['BUSY', 'DEPLOYMENT_CHANGED', 'AGENT_CHANGED'])
def test_scheduler_does_not_freeze_temporary_or_changed_target(verification, monkeypatch, code):
    _, manager, agent, _, _, _, _ = verification
    config, _ = manager.repository.get_publish_snapshot('v6')
    ident, _ = publish(manager, agent)
    verify.enroll(manager, agent['id'], ident, config)
    attempts = []
    def unavailable(*a, **k):
        attempts.append(True)
        raise MonitorError(code, 409, '暂不能开始')
    monkeypatch.setattr(verify, 'start', unavailable)
    verify.tick(manager)
    verify.tick(manager)
    assert len(attempts) == 2
    assert not manager.get_agent_by_id(agent['id']).get('ipv6_verifications')


@pytest.mark.parametrize('manual', [False, True])
def test_verification_uses_confirmed_adapted_file_digest_not_uploaded_yaml(verification, monkeypatch, manual):
    client, manager, agent, headers, path, calls, _ = verification
    config, _ = manager.repository.get_publish_snapshot('v6')
    ident, _ = publish(manager, agent)
    snapshot(monkeypatch, config, ident)  # Original uploaded YAML has digest a.
    def adapted(agents):
        current = next(a for a in agents if a['id'] == agent['id'])
        current['config_version'] = 'b' * 64
        current['deployments'][ident]['config_version'] = 'b' * 64
        current['latest_deployment']['config_version'] = 'b' * 64
        current['applied_content'] = {'deployment_id': ident}
    manager._update_agents(adapted)
    response = client.post(path + '/verify', headers=headers, json={} if manual else {'deployment_id': ident})
    assert response.status_code == 200, response.get_json()
    assert response.get_json()['verification']['status'] == 'ok'
    assert calls[0][1]['expected_config_sha256'] == 'b' * 64


@pytest.mark.parametrize('digest', ['a' * 8, '', None, 'g' * 64])
def test_invalid_final_digest_cannot_fall_back_to_upload_digest(verification, monkeypatch, digest):
    client, manager, agent, headers, path, calls, _ = verification
    config, _ = manager.repository.get_publish_snapshot('v6')
    ident, _ = publish(manager, agent)
    snapshot(monkeypatch, config, ident)
    def unconfirmed(agents):
        current = next(a for a in agents if a['id'] == agent['id'])
        current['deployments'][ident]['config_version'] = digest
        current['latest_deployment']['config_version'] = digest
    manager._update_agents(unconfirmed)
    report = client.post(path + '/verify', headers=headers, json={'deployment_id': ident}).get_json()['verification']
    assert report['status'] == 'warning'
    assert 'expected_config_sha256' not in calls[0][1]
    assert next(c for c in report['checks'] if c['id'] == 'runtime_expected_config')['status'] == 'unknown'
