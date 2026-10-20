"""Only trusted prepared bundles and confirmed activation establish a baseline."""
from copy import deepcopy

import pytest

from backend.agents.deployment_bundle import deployment_content_digests, deployment_content_fingerprint
from backend.agents.manager import AgentManager
from backend.common.config_repository import ProfileRepository
from backend.test_agent_deployments import env, minimal_bundle, response


def remote(monkeypatch, *, final_hash='f' * 64, upload_extra=None, final_extra=None, config_path='config.yaml'):
    final = {'success': True, 'status': 'succeeded', 'config_version': final_hash, **(final_extra or {})}

    def get(url, **_kwargs):
        if url.endswith('/capabilities'):
            return response({'success': True, 'deployment_protocols': [1], 'service_type': 'mihomo',
                             'config_path': config_path})
        return response(deepcopy(final))

    monkeypatch.setattr('backend.agents.manager.requests.get', get)
    monkeypatch.setattr('backend.agents.manager.requests.post', lambda *_args, **_kwargs: response(
        {'success': True, 'status': 'ready', **(upload_extra or {})}, 202))
    return final


def publish(manager, agent, config, deployment_id):
    bundle = minimal_bundle(agent, config, deployment_id)
    result = manager.publish_deployment(agent['id'], bundle, activate=False, config_revision=17,
                                        base_url='https://config.test')
    assert result['status'] == 'ready'
    return bundle


def complete(manager, agent, deployment_id):
    result = manager.get_deployment(agent['id'], deployment_id)
    assert result['status'] == 'succeeded'
    return manager.get_agent_by_id(agent['id'])


def test_ready_does_not_promote_and_success_uses_agent_final_config_hash(env, monkeypatch):
    _, manager, agent, config = env
    remote(monkeypatch, config_path='installed.conf')
    bundle = publish(manager, agent, config, 'content-ready')
    waiting = manager.get_agent_by_id(agent['id'])
    assert 'applied_content' not in waiting
    assert waiting['config_version'] == '0'
    prepared = waiting['deployments']['content-ready']['prepared_content']
    assert prepared == {
        'fingerprint': deployment_content_fingerprint(bundle.manifest), 'profile_id': 'default',
        'service_type': 'mihomo', 'config_path': 'installed.conf', 'base_url': 'https://config.test',
        'files': deployment_content_digests(bundle.manifest),
    }
    applied = complete(manager, agent, 'content-ready')
    assert applied['config_version'] == 'f' * 64
    assert applied['config_version'] != bundle.config_version
    assert applied['applied_content'] == {**prepared, 'config_version': 'f' * 64,
                                         'deployment_id': 'content-ready'}
    assert applied['config_revision'] == 17


@pytest.mark.parametrize('status', ['failed', 'rolled_back', 'rollback_failed'])
def test_failed_or_rolled_back_attempt_preserves_previous_baseline(env, monkeypatch, status):
    _, manager, agent, config = env
    final = remote(monkeypatch)
    publish(manager, agent, config, 'previous-success')
    previous = complete(manager, agent, 'previous-success')
    baseline = deepcopy(previous['applied_content'])
    publish(manager, agent, config, 'next-attempt')
    final.update(status=status, success=False, config_version='unapplied-version')
    assert manager.get_deployment(agent['id'], 'next-attempt')['status'] == status
    current = manager.get_agent_by_id(agent['id'])
    assert current['applied_content'] == baseline
    assert current['config_version'] == previous['config_version']
    assert current['pushed_at'] == previous['pushed_at']


def test_agent_response_cannot_replace_prepared_or_applied_content(env, monkeypatch):
    _, manager, agent, config = env
    forged = {'fingerprint': 'v1:' + '0' * 64, 'profile_id': 'attacker', 'service_type': 'mosdns',
              'deployment_id': 'other', 'config_version': 'forged-version', 'config_path': 'other.yaml'}
    malicious = {'prepared_content': forged, 'applied_content': forged}
    remote(monkeypatch, upload_extra=malicious, final_extra=malicious)
    bundle = publish(manager, agent, config, 'trusted-content')
    applied = complete(manager, agent, 'trusted-content')
    assert applied['applied_content']['fingerprint'] == deployment_content_fingerprint(bundle.manifest)
    assert applied['applied_content']['profile_id'] == 'default'
    assert applied['applied_content']['service_type'] == 'mihomo'
    assert applied['applied_content']['deployment_id'] == 'trusted-content'
    assert applied['applied_content']['config_version'] == 'f' * 64
    assert 'applied_content' not in applied['deployments']['trusted-content']
    assert applied['deployments']['trusted-content']['prepared_content']['fingerprint'] != forged['fingerprint']


def test_unprepared_success_cannot_establish_baseline_from_agent_fields(env):
    _, manager, agent, _ = env
    ident = 'unprepared'
    assert manager.begin_deployment(agent['id'], ident)['success']
    forged = {'fingerprint': 'v1:' + '0' * 64, 'profile_id': 'default', 'service_type': 'mihomo'}
    try:
        manager.record_deployment(agent['id'], ident, {'status': 'succeeded', 'success': True,
            'config_version': 'f' * 64, 'prepared_content': forged, 'applied_content': forged})
        updated = manager.get_agent_by_id(agent['id'])
        assert 'applied_content' not in updated
        assert 'prepared_content' not in updated['deployments'][ident]
    finally:
        manager.finish_deployment_preparation(agent['id'], ident)


def test_authenticated_reregistration_preserves_confirmed_baseline(env, monkeypatch):
    _, manager, agent, config = env
    remote(monkeypatch)
    publish(manager, agent, config, 'before-restart')
    previous = complete(manager, agent, 'before-restart')
    result = manager.register_agent({'name': agent['name'], 'host': agent['host'], 'port': agent['port'],
                                    'service_type': 'mihomo', 'version': '1.6.0-go'}, existing_token=agent['token'])
    assert result['id'] == agent['id'] and result['is_new'] is False
    updated = manager.get_agent_by_id(agent['id'])
    for field in ('applied_content', 'config_revision', 'config_version', 'pushed_at'):
        assert updated[field] == previous[field]


def test_late_old_success_does_not_replace_new_successful_baseline(env, monkeypatch):
    _, manager, agent, config = env
    final = remote(monkeypatch, final_hash='a' * 64)
    publish(manager, agent, config, 'older-success')
    complete(manager, agent, 'older-success')
    final['config_version'] = 'b' * 64
    publish(manager, agent, config, 'newer-success')
    newest = complete(manager, agent, 'newer-success')
    manager.record_deployment(agent['id'], 'older-success', {'success': True, 'status': 'succeeded',
                                                           'config_version': 'a' * 64})
    current = manager.get_agent_by_id(agent['id'])
    for field in ('applied_content', 'config_revision', 'config_version', 'pushed_at'):
        assert current[field] == newest[field]
    assert current['latest_deployment']['deployment_id'] == 'newer-success'


def test_preparation_and_applied_baseline_survive_manager_and_repository_restart(env, monkeypatch):
    repository, manager, agent, config = env
    remote(monkeypatch)
    bundle = publish(manager, agent, config, 'persistent-content')
    restarted = AgentManager(ProfileRepository(repository.data_dir))
    pending = restarted.get_agent_by_id(agent['id'])
    assert 'applied_content' not in pending
    assert pending['deployments']['persistent-content']['prepared_content']['fingerprint'] == deployment_content_fingerprint(bundle.manifest)
    applied = complete(restarted, agent, 'persistent-content')
    again = AgentManager(ProfileRepository(repository.data_dir)).get_agent_by_id(agent['id'])
    assert again['applied_content'] == applied['applied_content']
    assert again['config_version'] == 'f' * 64


def test_binding_change_blocks_late_old_success_from_promoting_baseline(env, monkeypatch):
    repository, manager, agent, config = env
    remote(monkeypatch)
    publish(manager, agent, config, 'old-profile')
    previous = complete(manager, agent, 'old-profile')
    repository.create_profile({'id': 'new-profile', 'name': 'New'})
    assert manager.update_agent(agent['id'], {'profile_id': 'new-profile'})
    manager.record_deployment(agent['id'], 'old-profile', {'success': True, 'status': 'succeeded',
                                                         'config_version': 'unexpected-hash'})
    current = manager.get_agent_by_id(agent['id'])
    assert current['applied_content'] == previous['applied_content']
    assert current['config_version'] == previous['config_version']
