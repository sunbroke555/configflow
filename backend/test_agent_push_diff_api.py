"""Push -> saved bundle -> /push-diff end to end, sharing the list's background comparison."""
from copy import deepcopy
import json

import pytest
from flask import Flask

from backend.agents.content_sync import ContentSyncChecker
from backend.agents.manager import AgentManager
from backend.routes import agents, register_blueprints
from backend.test_agent_content_preparation import content_env  # noqa: F401

CONTROLLER_SECRET = 'controller-secret-value-1234'
OLD_PASSWORD = 'manual-node-password-old'
NEW_PASSWORD = 'manual-node-password-new'


class DeferredExecutor:
    def __init__(self):
        self.jobs = []

    def submit(self, callback, *args):
        self.jobs.append((callback, args))

    def run_all(self):
        while self.jobs:
            callback, args = self.jobs.pop(0)
            callback(*args)


@pytest.fixture
def diff_env(content_env, monkeypatch):
    config, remote, _fetches, repository = content_env
    config['nodes'] = [{'id': 'manual', 'name': 'Manual SS', 'type': 'ss', 'server': 'manual.example.com', 'port': 8388,
                        'enabled': True, 'params': {'cipher': 'aes-128-gcm', 'password': OLD_PASSWORD}}]
    config['proxy_groups'][0]['manual_nodes'] = ['manual']
    manager = AgentManager(repository)
    monkeypatch.setattr(agents, 'get_agent_manager', lambda: manager)
    monkeypatch.setattr(agents, 'get_config', lambda *_: deepcopy(config))
    monkeypatch.setattr(repository, 'get_compat_config', lambda *_: deepcopy(config))
    monkeypatch.setattr('backend.utils.provider_delivery.commit_provider_bundle', lambda *_: None)
    executor = DeferredExecutor()
    monkeypatch.setattr('backend.agents.content_sync._checker', ContentSyncChecker(executor=executor))

    def transport(agent, method, path, **kwargs):
        if path == 'capabilities':
            return {'deployment_protocols': [1], 'service_type': agent['service_type'], 'config_path': 'config.yaml'}
        assert method == 'post' and path == 'deployments'
        # A real Agent reports the installed main file's short digest.
        return {'success': True, 'status': 'succeeded', 'config_version': '0a1b2c3d'}

    monkeypatch.setattr(manager, '_deployment_request', transport)
    app = Flask(__name__)
    register_blueprints(app)
    ids = {service: manager.register_agent({'name': service, 'host': service + '.test', 'service_type': service})['id']
           for service in ('mihomo', 'mosdns')}
    return app.test_client(), manager, config, remote, repository, ids, executor


def push(client, agent_id, deployment_id='first'):
    response = client.post(f'/api/agents/{agent_id}/push-config', json={
        'deployment_id': deployment_id, 'base_url': 'https://config.test'})
    assert response.status_code == 200, response.json
    return response


def diff(client, agent_id):
    response = client.get(f'/api/agents/{agent_id}/push-diff')
    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'no-store'
    return response.json


def settle(client, executor, agent_id):
    first = diff(client, agent_id)
    assert first['status'] == 'checking'
    executor.run_all()
    return diff(client, agent_id)


def expire_subscription_cache(repository, config):
    from backend.utils.subscription_refresh import _download_cache_path
    cache_path = _download_cache_path(config['subscriptions'][0]['url'])
    downloaded = repository.read_shared_json(cache_path)
    downloaded['fetched_at'] = '2000-01-01T00:00:00+00:00'
    repository.write_shared_json(cache_path, downloaded)


def test_unpushed_and_legacy_agents_explain_the_missing_baseline(diff_env):
    client, manager, _config, _remote, _repository, ids, executor = diff_env
    assert diff(client, ids['mihomo']) == {'status': 'never-pushed', 'service_type': 'mihomo'}
    manager.repository.update_system_transaction(lambda system: next(
        item for item in system['agents'] if item['id'] == ids['mosdns']).update(config_version='abcd1234'))
    assert diff(client, ids['mosdns']) == {'status': 'unknown', 'reason': 'baseline_missing', 'service_type': 'mosdns'}
    assert not executor.jobs
    assert client.get('/api/agents/missing/push-diff').status_code == 404


def test_synced_after_push_then_real_changes_are_listed_and_redacted(diff_env):
    client, manager, config, remote, repository, ids, executor = diff_env
    agent_id = ids['mihomo']
    push(client, agent_id)
    synced = settle(client, executor, agent_id)
    assert synced['status'] == 'synced' and synced['baseline']['pushed_at']
    # The list's status reuses the same finished comparison.
    listed = {item['id']: item for item in client.get('/api/agents').json}
    assert listed[agent_id]['content_sync']['status'] == 'synced' and not executor.jobs

    config['mihomo']['custom_config'] = ('mode: rule\nlog-level: info\ndns: {enable: false}\n'
                                         f'secret: {CONTROLLER_SECRET}\nhosts: {{printer.lan: 192.168.1.30}}\n')
    config['rule_library'][0]['content'] = '+.first.test\n+.second.test\n+.third.test'
    config['nodes'][0]['params']['password'] = NEW_PASSWORD
    remote['provider'] = 'proxies: [{name: Remote, type: http, server: first.test, port: 8080}, ' \
                         '{name: Added, type: http, server: added.test, port: 8081}]'
    expire_subscription_cache(repository, config)
    result = settle(client, executor, agent_id)
    assert result['status'] == 'changed' and result['content'] == 'full' and result['checked_at']
    files = {item['path']: item for item in result['files']}
    assert files['providers/Feed.yaml']['summary'] == {'kind': 'nodes', 'before': 1, 'after': 2, 'added': 1,
                                                       'removed': 0, 'modified': 0}
    rule = next(item for item in result['files'] if item['role'] == 'rule')
    assert rule['name'] == 'Selected' and rule['summary'] == {'kind': 'rules', 'before': 1, 'after': 3,
                                                              'added': 2, 'removed': 0}
    items = {item['key']: item for item in result['config']['items']}
    assert items['hosts.printer.lan']['new'] == '192.168.1.30'
    assert items['secret'] == {'key': 'secret', 'change': 'added', 'masked': True}
    node = next(entry for entry in items['proxies']['list']['entries'] if entry['text'] == 'Manual SS')
    assert node['fields'] == [{'key': 'password', 'change': 'modified', 'masked': True}]
    body = json.dumps(result, ensure_ascii=False)
    for secret in (CONTROLLER_SECRET, OLD_PASSWORD, NEW_PASSWORD, 'fixture-rule-token'):
        assert secret not in body
    assert '[REDACTED]' not in body  # nothing needed the response hook's whole-string fallback
    raw = result['config']['raw']['text']
    assert '+  printer.lan: 192.168.1.30' in raw and 'secret: <已隐藏 ' in raw
    # The change list is cached with the status: polling again does not rebuild it.
    assert diff(client, agent_id) == result and not executor.jobs
    listed = {item['id']: item for item in client.get('/api/agents').json}
    assert listed[agent_id]['content_sync']['status'] == 'changed' and not executor.jobs

    push(client, agent_id, 'second')
    assert settle(client, executor, agent_id)['status'] == 'synced'


def test_list_check_and_change_list_share_one_worker(diff_env):
    client, _manager, config, _remote, _repository, ids, executor = diff_env
    agent_id = ids['mihomo']
    push(client, agent_id)
    config['rule_library'][0]['content'] = '+.first.test\n+.other.test'
    assert client.get('/api/agents').json  # the list starts a worker
    assert len(executor.jobs) == 1
    # Asking for the change list while it waits joins that worker instead of queueing another.
    assert diff(client, agent_id)['status'] == 'checking' and len(executor.jobs) == 1
    executor.run_all()
    assert diff(client, agent_id)['counts']['modified'] == 1 and not executor.jobs
    listed = {item['id']: item for item in client.get('/api/agents').json}
    assert listed[agent_id]['content_sync']['status'] == 'changed' and not executor.jobs


def test_mosdns_without_saved_bundle_lists_files_by_digest(diff_env):
    client, _manager, config, _remote, _repository, ids, executor = diff_env
    agent_id = ids['mosdns']
    push(client, agent_id)
    config['mosdns']['remote_dns'] = 'udp://192.0.2.200:53'
    result = settle(client, executor, agent_id)
    assert result['status'] == 'changed'
    assert result['content'] == 'digests' and result['content_reason'] == 'service_without_bundle'
    assert [item['path'] for item in result['files']] == ['config.yaml']
    assert result['config']['status'] == 'unavailable'


def test_pending_deployment_and_failed_preparation_are_reported(diff_env, monkeypatch):
    client, manager, config, _remote, _repository, ids, executor = diff_env
    agent_id = ids['mihomo']
    push(client, agent_id)
    config['mihomo']['custom_config'] = 'mode: rule\nlog-level: debug\n'

    def broken(*_args, **_kwargs):
        raise ValueError(f'secret {CONTROLLER_SECRET} in an error')

    monkeypatch.setattr(agents, '_prepare_content_bundle', broken)
    failed = settle(client, executor, agent_id)
    assert failed == {'status': 'unknown', 'reason': 'check_failed', 'service_type': 'mihomo',
                      'baseline': failed['baseline']}
    manager.begin_deployment(agent_id, 'running')
    try:
        assert diff(client, agent_id)['reason'] == 'deployment_pending'
    finally:
        manager.finish_deployment_preparation(agent_id, 'running')
