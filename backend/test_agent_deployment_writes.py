"""Status polls and finished history do not rewrite or bloat the configuration document."""
from copy import deepcopy

from backend.test_agent_deployments import env  # noqa: F401


def count_writes(repository, monkeypatch):
    writes = []
    original = repository.update_system_transaction
    monkeypatch.setattr(repository, 'update_system_transaction',
                        lambda updater: writes.append(1) or original(updater))
    return writes


def submitted(manager, agent_id, deployment_id):
    manager.begin_deployment(agent_id, deployment_id)
    manager.record_deployment(agent_id, deployment_id, {'status': 'uploading', 'submitted': True, 'success': True})
    manager.finish_deployment_preparation(agent_id, deployment_id)


def test_repeated_status_poll_does_not_rewrite_configuration(env, monkeypatch):
    repository, manager, agent, _ = env
    submitted(manager, agent['id'], 'polled')
    reply = {'success': True, 'status': 'activating', 'message': '正在启动', 'updated_at': '2026-10-25T01:00:00Z'}
    monkeypatch.setattr(manager, '_deployment_request', lambda *_a, **_k: dict(reply))
    first = manager.get_deployment(agent['id'], 'polled')
    stored = deepcopy(repository.get_system()['agents'])
    writes = count_writes(repository, monkeypatch)
    for _ in range(3):
        assert manager.get_deployment(agent['id'], 'polled') == first
    # Before, each 2-second poll rewrote the whole document to move updated_at.
    assert writes == [] and repository.get_system()['agents'] == stored
    reply.update(status='checking', message='正在检查', updated_at='2026-10-25T01:00:05Z')
    moved = manager.get_deployment(agent['id'], 'polled')
    assert writes == [1] and moved['status'] == 'checking' and moved['updated_at'] > first['updated_at']
    assert [event['status'] for event in moved['events']][-2:] == ['activating', 'checking']
    reply.update(status='succeeded', message='', config_version='confirmed')
    assert manager.get_deployment(agent['id'], 'polled')['status'] == 'succeeded'
    assert manager.get_agent_by_id(agent['id'])['config_version'] == 'confirmed'


def test_superseded_history_keeps_fingerprint_but_not_per_file_digests(env):
    repository, manager, agent, _ = env
    prepared = {'fingerprint': 'v1:' + '0' * 64, 'profile_id': 'default', 'service_type': 'mihomo',
                'config_path': 'config.yaml', 'base_url': '', 'files': {'config.yaml': 'a' * 64, 'r.list': 'b' * 64}}

    def install(deployment_id):
        submitted(manager, agent['id'], deployment_id)
        repository.update_system_transaction(lambda system: next(
            item for item in system['agents'] if item['id'] == agent['id'])['deployments'][deployment_id]
            .update(prepared_content=deepcopy(prepared)))
        manager.record_deployment(agent['id'], deployment_id, {'status': 'succeeded', 'config_version': deployment_id})

    install('first')
    stored = manager.get_agent_by_id(agent['id'])
    assert stored['applied_content']['files'] == prepared['files']
    assert stored['deployments']['first']['prepared_content']['files'] == prepared['files']
    manager.begin_deployment(agent['id'], 'second')
    stored = manager.get_agent_by_id(agent['id'])
    # The latest task and applied baseline keep the digests that explain drift.
    assert 'files' not in stored['deployments']['first']['prepared_content']
    assert stored['deployments']['first']['prepared_content']['fingerprint'] == prepared['fingerprint']
    assert stored['applied_content']['files'] == prepared['files']

