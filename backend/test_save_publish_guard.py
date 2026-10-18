"""Settings saves may publish only the committed profile version and target type."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from flask import Flask

from backend.agents.manager import AgentManager
from backend.common import config as config_store
from backend.common.config_repository import ProfileRepository
from backend.routes import register_blueprints


@pytest.fixture
def publish_app(tmp_path, monkeypatch):
    previous = config_store._repository
    repository = ProfileRepository(tmp_path)
    repository.create_profile({'id': 'office', 'name': 'Office'})
    repository.save_shared({'rule_library': []})
    config_store.set_repository(repository)
    manager = AgentManager(repository)
    monkeypatch.setattr('backend.routes.agents.get_agent_manager', lambda: manager)
    monkeypatch.setattr('backend.agents.manager.requests.post',
                        lambda *_a, **_k: pytest.fail('Unexpected publication'))
    app = Flask(__name__)
    app.config['TESTING'] = True
    register_blueprints(app)
    yield app.test_client(), repository, manager
    config_store.set_repository(previous)


def register(manager, service='mihomo', profile='office'):
    return manager.register_agent({'name': service, 'host': 'agent.test', 'port': 8080,
                                   'service_type': service, 'profile_id': profile})['id']


def save(client, service='mihomo'):
    if service == 'oxidns':
        path, value = '/api/oxidns/settings', {'worker_threads': 2}
    elif service == 'mosdns':
        path, value = '/api/mosdns/settings', {'custom_config': 'plugins: []',
                                             'local_dns': '223.5.5.5', 'log_file': 'new.log'}
    else:
        path, value = f'/api/custom-config/{service}', {'config': 'mode: rule'}
    result = client.post(path + '?profile=office', json=value)
    assert result.status_code == 200, result.json
    return result.json['publish_guard']


def stub_preparation(monkeypatch, manager, seen):
    def prepare(agent, config, **kwargs):
        seen.append(deepcopy(config))
        bundle = SimpleNamespace(manifest={'files': [], 'profile_id': config['profile_id'],
                                          'service_type': agent['service_type']})
        return {'bundle': bundle, 'config_content': 'prepared', 'prepared_providers': [],
                'provider_downloads': [], 'ruleset_downloads': [], 'custom_files': []}
    monkeypatch.setattr('backend.routes.agents._prepare_transactional_content', prepare)
    monkeypatch.setattr(manager, 'push_config_to_agent', lambda *_a, **_k: {
        'success': True, 'status': 'succeeded', 'deployment_id': 'saved-draft'})


@pytest.mark.parametrize(('path', 'payload', 'service'), [
    ('/api/custom-config/mihomo', {'config': 'mode: rule'}, 'mihomo'),
    ('/api/custom-config/mosdns', {'config': 'plugins: []'}, 'mosdns'),
    ('/api/custom-config/surge', {'config': '[General]'}, 'surge'),
    ('/api/custom-config/surge', {'smart_groups': []}, 'surge'),
    ('/api/oxidns/settings', {'worker_threads': 2}, 'oxidns'),
    ('/api/mosdns/cache-settings', {'cache_size': 100}, 'mosdns'),
    ('/api/mosdns/settings', {'local_dns': '223.5.5.5', 'log_file': 'new.log'}, 'mosdns'),
])
def test_save_returns_exact_profile_type_and_committed_revision(publish_app, path, payload, service):
    client, repository, _ = publish_app
    default = repository.get_profile('default')
    result = client.post(path + '?profile=office', json=payload)
    assert result.status_code == 200, result.json
    assert result.json['success'] is True
    assert result.json['publish_guard'] == {
        'expected_profile_id': 'office', 'expected_service_type': service,
        'expected_config_revision': repository.config_revision('office')}
    assert repository.get_profile('default') == default


def test_save_receipt_is_not_relabelled_by_a_write_after_commit(publish_app, monkeypatch):
    client, repository, _ = publish_app
    original = repository.save_profile
    committed = repository.config_revision('office') + 1
    def concurrent_save(*args, **kwargs):
        result = original(*args, **kwargs)
        original('office', {'mihomo': {'custom_config': 'mode: direct'}})
        return result
    monkeypatch.setattr(repository, 'save_profile', concurrent_save)
    guard = save(client)
    assert guard['expected_config_revision'] == committed
    assert repository.config_revision('office') == committed + 1


def test_invalid_mihomo_yaml_still_cannot_be_saved(publish_app):
    client, repository, _ = publish_app
    before = repository.get_profile('office')
    result = client.post('/api/custom-config/mihomo?profile=office', json={'config': 'dns: ['})
    assert result.status_code == 400
    assert 'publish_guard' not in result.json
    assert repository.get_profile('office') == before


@pytest.mark.parametrize('kind', ['profile', 'shared', 'binding', 'type', 'wrong_profile', 'wrong_type'])
def test_changed_draft_or_target_is_rejected_before_preparation(publish_app, monkeypatch, kind):
    client, repository, manager = publish_app
    agent_id = register(manager)
    guard = save(client)
    if kind == 'profile':
        repository.save_profile('office', {'mihomo': {'custom_config': 'mode: direct'}})
    elif kind == 'shared':
        repository.save_shared({'rule_library': []})
    elif kind == 'binding':
        manager.update_agent(agent_id, {'profile_id': 'default'})
    elif kind == 'type':
        manager.update_agent(agent_id, {'service_type': 'mosdns'})
    elif kind == 'wrong_profile':
        guard['expected_profile_id'] = 'default'
    else:
        guard['expected_service_type'] = 'mosdns'
    monkeypatch.setattr('backend.routes.agents._prepare_transactional_content',
                        lambda *_a, **_k: pytest.fail('Stale draft was prepared'))
    result = client.post(f'/api/agents/{agent_id}/push-config', json=guard)
    assert result.status_code == 409
    assert '草稿已保存' in result.json['message']
    assert not manager.get_agent_by_id(agent_id).get('deployments')


@pytest.mark.parametrize('patch', [
    {'expected_profile_id': None}, {'expected_service_type': None},
    {'expected_config_revision': None}, {'expected_config_revision': True},
    {'expected_config_revision': -1}, {'expected_config_revision': '1'},
])
def test_incomplete_or_invalid_guard_is_rejected(publish_app, patch):
    client, _, manager = publish_app
    agent_id = register(manager)
    guard = save(client)
    guard.update(patch)
    assert client.post(f'/api/agents/{agent_id}/push-config', json=guard).status_code == 400


@pytest.mark.parametrize('service', ['mihomo', 'mosdns', 'oxidns'])
def test_matching_guard_uses_existing_transactional_preparation(publish_app, monkeypatch, service):
    client, _, manager = publish_app
    agent_id = register(manager, service)
    guard = save(client, service)
    seen = []
    stub_preparation(monkeypatch, manager, seen)
    result = client.post(f'/api/agents/{agent_id}/push-config',
                         json={**guard, 'deployment_id': 'saved-draft'})
    assert result.status_code == 200, result.json
    assert result.json['status'] == 'succeeded'
    assert seen[0]['profile_id'] == 'office'
    if service == 'mosdns':
        assert seen[0]['mosdns']['custom_config'] == 'plugins: []'
        assert seen[0]['mosdns']['local_dns'] == '223.5.5.5'
        assert seen[0]['mosdns']['log_file'] == 'new.log'


def test_whole_mosdns_save_guard_rejects_a_changed_draft_before_publication(publish_app, monkeypatch):
    client, repository, manager = publish_app
    agent_id = register(manager, 'mosdns')
    guard = save(client, 'mosdns')
    repository.update_profile_transaction('office', lambda profile: profile['mosdns'].update(log_file='later.log'))
    monkeypatch.setattr('backend.routes.agents._prepare_transactional_content',
                        lambda *_a, **_k: pytest.fail('Stale MosDNS draft was prepared'))
    result = client.post(f'/api/agents/{agent_id}/push-config', json=guard)
    assert result.status_code == 409
    assert not manager.get_agent_by_id(agent_id).get('deployments')


def test_guard_checks_the_same_snapshot_that_is_generated(publish_app, monkeypatch):
    client, repository, manager = publish_app
    agent_id = register(manager)
    guard = save(client)
    original = repository.get_publish_snapshot
    def change_after_snapshot(profile_id):
        snapshot = original(profile_id)
        repository.save_profile(profile_id, {'mihomo': {'custom_config': 'mode: direct'}})
        return snapshot
    monkeypatch.setattr(repository, 'get_publish_snapshot', change_after_snapshot)
    monkeypatch.setattr('backend.routes.agents.get_config',
                        lambda *_a, **_k: pytest.fail('Guarded push must capture an atomic snapshot'))
    seen = []
    stub_preparation(monkeypatch, manager, seen)
    result = client.post(f'/api/agents/{agent_id}/push-config', json=guard)
    assert result.status_code == 200, result.json
    assert seen[0]['mihomo']['custom_config'] == 'mode: rule'
    assert repository.get_profile('office')['mihomo']['custom_config'] == 'mode: direct'


def test_binding_race_during_reservation_does_not_prepare_or_upload(publish_app, monkeypatch):
    client, _, manager = publish_app
    agent_id = register(manager)
    guard = save(client)
    begin = manager.begin_deployment
    def rebind_before_reserving(*args):
        manager.update_agent(agent_id, {'profile_id': 'default'})
        return begin(*args)
    monkeypatch.setattr(manager, 'begin_deployment', rebind_before_reserving)
    monkeypatch.setattr('backend.routes.agents._prepare_transactional_content',
                        lambda *_a, **_k: pytest.fail('Rebound target was prepared'))
    result = client.post(f'/api/agents/{agent_id}/push-config', json=guard)
    assert result.status_code == 409
    assert manager.get_agent_by_id(agent_id)['latest_deployment']['status'] == 'failed'


@pytest.mark.parametrize('pin_config', [False, True])
def test_legacy_oxidns_device_policy_guard_remains_compatible(publish_app, monkeypatch, pin_config):
    from backend.common.oxidns_device_policy import revision
    client, repository, manager = publish_app
    agent_id = register(manager, 'oxidns')
    receipt = save(client, 'oxidns')
    guard = {'expected_profile_id': 'office',
             'expected_oxidns_revision': revision(repository.get_compat_config('office')['oxidns'])}
    seen = []
    stub_preparation(monkeypatch, manager, seen)
    path = f'/api/agents/{agent_id}/push-config'
    if pin_config:
        guard['expected_config_revision'] = receipt['expected_config_revision']
        for stale in (receipt['expected_config_revision'] + 1, True, -1, '1'):
            assert client.post(path, json={**guard, 'expected_config_revision': stale}).status_code == 409
    assert client.post(path, json={**guard, 'expected_oxidns_revision': 'stale'}).status_code == 409
    assert client.post(path, json=guard).status_code == 200
    assert len(seen) == 1


def test_missing_service_type_does_not_bypass_saved_draft_guard(publish_app):
    client, _, manager = publish_app
    agent_id = register(manager)
    guard = save(client)
    guard.pop('expected_service_type')
    assert client.post(f'/api/agents/{agent_id}/push-config', json=guard).status_code == 400


def test_surge_rechecks_binding_after_generation_before_sending(publish_app, monkeypatch):
    client, _, manager = publish_app
    agent_id = register(manager, 'surge')
    guard = save(client, 'surge')
    def generate(*_args, **_kwargs):
        manager.update_agent(agent_id, {'profile_id': 'default'})
        return '[General]'
    monkeypatch.setattr('backend.routes.agents.generate_surge_config', generate)
    result = client.post(f'/api/agents/{agent_id}/push-config', json=guard)
    assert result.status_code == 409
    assert '设备绑定已改变' in result.json['message']


@pytest.mark.parametrize('service', ['mosdns', 'surge'])
def test_changed_service_after_preparation_cannot_fall_back_to_legacy_publish(publish_app, monkeypatch, service):
    from backend.agents.deployment_bundle import build_deployment_bundle
    client, repository, manager = publish_app
    agent_id = register(manager)
    guard = save(client)
    def prepare(agent, config, **kwargs):
        bundle = build_deployment_bundle(agent, config, 'mode: rule',
                                         deployment_id=kwargs['deployment_id'])
        manager.update_agent(agent_id, {'service_type': service})
        return {'bundle': bundle, 'config_content': 'mode: rule', 'prepared_providers': [],
                'provider_downloads': [], 'ruleset_downloads': [], 'custom_files': []}
    monkeypatch.setattr('backend.routes.agents._prepare_transactional_content', prepare)
    monkeypatch.setattr(manager, '_deployment_request',
                        lambda *_a, **_k: pytest.fail('Mismatched service was contacted'))
    result = client.post(f'/api/agents/{agent_id}/push-config', json=guard)
    assert result.json['status'] == 'failed'
    assert 'service type changed' in result.json['message']
    assert manager.get_agent_by_id(agent_id)['latest_deployment']['status'] == 'failed'


def test_surge_uses_existing_push_and_restart_path_with_matching_guard(publish_app, monkeypatch):
    client, _, manager = publish_app
    agent_id = register(manager, 'surge')
    guard = save(client, 'surge')
    sent = []
    monkeypatch.setattr('backend.routes.agents.generate_surge_config', lambda *_a, **_k: '[General]')
    def push(*args, **kwargs):
        sent.append((args, kwargs))
        return {'success': True}
    monkeypatch.setattr(manager, 'push_config_to_agent', push)
    monkeypatch.setattr(manager, 'restart_agent_service', lambda target: {'success': target == agent_id})
    result = client.post(f'/api/agents/{agent_id}/push-config', json=guard)
    assert result.status_code == 200
    assert result.json['restart']['success'] is True
    assert sent[0][0] == (agent_id, '[General]')
    assert sent[0][1]['expected_profile_id'] == 'office'
    assert sent[0][1]['expected_service_type'] == 'surge'


def test_incomplete_oxidns_draft_is_saved_but_still_fails_publish_validation(publish_app):
    client, _, manager = publish_app
    agent_id = register(manager, 'oxidns')
    saved = client.post('/api/oxidns/settings?profile=office', json={
        'config_mode': 'native', 'native_config': 'plugins:\n  - tag: dns\n    type: forward\n    args: {}\n'})
    assert saved.status_code == 200, saved.json
    result = client.post(f'/api/agents/{agent_id}/push-config', json=saved.json['publish_guard'])
    assert result.status_code == 400, result.json
    assert result.json['status'] == 'failed'
    assert result.json.get('issues')


def test_ready_candidate_from_previous_service_cannot_be_queried_or_activated(publish_app, monkeypatch):
    _, _, manager = publish_app
    agent_id = register(manager)
    manager.begin_deployment(agent_id, 'old-service')
    manager.record_deployment(agent_id, 'old-service', {'status': 'ready', 'submitted': True})
    manager.finish_deployment_preparation(agent_id, 'old-service')
    before = manager.get_agent_by_id(agent_id)['deployments']['old-service']
    manager.update_agent(agent_id, {'service_type': 'mosdns'})
    monkeypatch.setattr(manager, '_deployment_request',
                        lambda *_a, **_k: pytest.fail('Previous software candidate must not contact Agent'))
    assert manager.get_deployment(agent_id, 'old-service')['http_status'] == 409
    assert manager.activate_deployment(agent_id, 'old-service')['http_status'] == 409
    assert manager.get_agent_by_id(agent_id)['deployments']['old-service'] == before


@pytest.mark.parametrize('status', ['succeeded', 'failed', 'rolled_back'])
def test_previous_service_terminal_history_stays_readable(publish_app, monkeypatch, status):
    _, _, manager = publish_app
    agent_id = register(manager)
    manager.begin_deployment(agent_id, 'old-service')
    manager.record_deployment(agent_id, 'old-service', {'status': status, 'submitted': True})
    manager.finish_deployment_preparation(agent_id, 'old-service')
    manager.update_agent(agent_id, {'service_type': 'mosdns'})
    monkeypatch.setattr(manager, '_deployment_request',
                        lambda *_a, **_k: pytest.fail('Completed history must not contact Agent'))
    result = manager.get_deployment(agent_id, 'old-service')
    assert result['status'] == status
    assert not result.get('http_status')
    assert manager.activate_deployment(agent_id, 'old-service')['http_status'] == 409


def test_activation_rechecks_service_after_status_request(publish_app, monkeypatch):
    _, _, manager = publish_app
    agent_id = register(manager)
    manager.begin_deployment(agent_id, 'old-service')
    manager.record_deployment(agent_id, 'old-service', {'status': 'ready', 'submitted': True})
    manager.finish_deployment_preparation(agent_id, 'old-service')
    requests = []
    def query_then_rebind(agent, method, path, **kwargs):
        requests.append(method)
        assert method == 'get', 'Activation was sent after the software changed'
        manager.update_agent(agent_id, {'service_type': 'mosdns'})
        return {'status': 'ready', 'success': True}
    monkeypatch.setattr(manager, '_deployment_request', query_then_rebind)
    result = manager.activate_deployment(agent_id, 'old-service')
    assert result['http_status'] == 409
    assert requests == ['get']
