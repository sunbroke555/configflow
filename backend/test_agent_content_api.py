"""Exercise push -> persisted baseline -> non-blocking public status end to end."""
from copy import deepcopy

import pytest
from flask import Flask

from backend.agents.content_sync import ContentSyncChecker
from backend.agents.manager import AgentManager
from backend.routes import agents, register_blueprints
from backend.test_agent_content_preparation import content_env  # noqa: F401


class DeferredExecutor:
    def __init__(self):
        self.jobs = []

    def submit(self, callback, *args):
        self.jobs.append((callback, args))

    def run(self):
        callback, args = self.jobs.pop(0)
        callback(*args)


@pytest.fixture
def api_env(content_env, monkeypatch):
    config, remote, fetches, repository = content_env
    manager = AgentManager(repository)
    monkeypatch.setattr(agents, 'get_agent_manager', lambda: manager)
    monkeypatch.setattr(agents, 'get_config', lambda *_: deepcopy(config))
    monkeypatch.setattr(repository, 'get_compat_config', lambda *_: deepcopy(config))
    # Real pushes may commit provider caches; this fixture captures rather than writes them.
    monkeypatch.setattr('backend.utils.provider_delivery.commit_provider_bundle', lambda *_: None)
    executor = DeferredExecutor()
    checker = ContentSyncChecker(executor=executor)
    monkeypatch.setattr('backend.agents.content_sync._checker', checker)
    replies = {'status': 'succeeded'}

    def transport(agent, method, path, **kwargs):
        if path == 'capabilities':
            return {'deployment_protocols': [1], 'service_type': agent['service_type'], 'config_path': 'installed.yaml'}
        assert method == 'post' and path == 'deployments'
        return {'success': True, 'status': replies['status'], 'config_version': 'agent-rewritten-hash'}

    monkeypatch.setattr(manager, '_deployment_request', transport)
    app = Flask(__name__)
    register_blueprints(app)
    registered = {service: manager.register_agent({'name': service, 'host': service + '.test',
                  'service_type': service})['id'] for service in ('mihomo', 'mosdns')}
    return app.test_client(), manager, config, registered, executor, replies, fetches


def push(client, agent_id, deployment_id='first'):
    return client.post(f'/api/agents/{agent_id}/push-config', json={
        'deployment_id': deployment_id, 'base_url': 'https://private-origin.test/prefix'})


def listing(client):
    response = client.get('/api/agents', base_url='http://another-ui-origin.test')
    assert response.status_code == 200
    return {agent['service_type']: agent for agent in response.json}


def test_both_services_compare_full_contents_and_hide_private_baselines(api_env):
    client, manager, config, ids, executor, _replies, fetches = api_env
    for service, agent_id in ids.items():
        response = push(client, agent_id)
        assert response.status_code == 200
        assert 'prepared_content' not in response.json
        assert manager.get_agent_by_id(agent_id)['applied_content']['base_url'] == 'https://private-origin.test/prefix'
    before_fetches = list(fetches)
    initial = listing(client)
    assert all(item['content_sync']['status'] == 'checking' for item in initial.values())
    assert fetches == before_fetches  # GET did not generate or fetch anything inline.
    assert len(executor.jobs) == 2
    for item in initial.values():
        assert 'token' not in item and 'applied_content' not in item
        assert 'prepared_content' not in item['latest_deployment']
        assert 'prepared_content' not in item['deployments']['first']
    executor.run()
    executor.run()
    assert all(item['content_sync']['status'] == 'synced' for item in listing(client).values())
    # The reported bug: management settings and revision increases alter neither output.
    config['domain_discovery'] = {'auto_probe': True, 'auto_apply': False, 'region_check_enabled': False}
    config['_revision'] = 999
    listing(client)
    executor.run()
    executor.run()
    assert all(item['content_sync']['status'] == 'synced' for item in listing(client).values())
    config['mosdns']['remote_dns'] = 'udp://192.0.2.200:53'
    listing(client)
    executor.run()
    executor.run()
    updated = listing(client)
    assert updated['mihomo']['content_sync']['status'] == 'synced'
    assert updated['mosdns']['content_sync']['status'] == 'changed'
    # Repeated list requests use the completed cache rather than re-rendering bundles.
    assert not executor.jobs
    for agent_id in ids.values():
        detail = client.get(f'/api/agents/{agent_id}').json
        assert 'applied_content' not in detail
        task = client.get(f'/api/agents/{agent_id}/deployments/first').json
        assert 'prepared_content' not in task


def test_legacy_baseline_does_not_guess_from_revisions_or_fetch(api_env):
    client, manager, _config, ids, executor, _replies, _fetches = api_env
    manager.repository.update_system_transaction(lambda system: system['agents'][0].update(
        config_version='old-hash', config_revision=50))
    result = listing(client)
    assert result['mihomo']['content_sync'] == {'status': 'unknown', 'reason': 'baseline_missing'}
    assert result['mosdns']['content_sync'] == {'status': 'never-pushed'}
    assert not executor.jobs


def test_ready_does_not_create_baseline_and_successful_repush_clears_changes(api_env):
    client, manager, config, ids, executor, replies, _fetches = api_env
    agent_id = ids['mosdns']
    replies['status'] = 'ready'
    assert push(client, agent_id).status_code == 202
    assert 'applied_content' not in manager.get_agent_by_id(agent_id)
    assert listing(client)['mosdns']['content_sync']['status'] == 'unknown'
    manager.record_deployment(agent_id, 'first', {'status': 'succeeded', 'config_version': 'agent-rewritten-hash'})
    listing(client)
    executor.run()
    assert listing(client)['mosdns']['content_sync']['status'] == 'synced'
    config['mosdns']['remote_dns'] = 'udp://192.0.2.200:53'
    listing(client)
    executor.run()
    assert listing(client)['mosdns']['content_sync']['status'] == 'changed'
    replies['status'] = 'succeeded'
    assert push(client, agent_id, 'second').status_code == 200
    listing(client)
    executor.run()
    assert listing(client)['mosdns']['content_sync']['status'] == 'synced'


def test_missing_profile_is_unknown_without_breaking_other_agents(api_env, monkeypatch):
    from backend.common.config_repository import ProfileRepositoryError
    client, manager, config, ids, executor, _replies, _fetches = api_env
    stale_agents = manager.get_all_agents()
    stale_agents[0]['profile_id'] = 'missing'
    monkeypatch.setattr(manager, 'get_all_agents', lambda: stale_agents)

    def snapshot(profile_id):
        if profile_id == 'missing':
            raise ProfileRepositoryError('unavailable')
        return deepcopy(config)

    monkeypatch.setattr(manager.repository, 'get_compat_config', snapshot)
    result = listing(client)
    assert result['mihomo']['content_sync'] == {'status': 'unknown', 'reason': 'profile_unavailable'}
    assert result['mosdns']['content_sync']['status'] == 'never-pushed'
    assert not executor.jobs


def test_summary_listing_skips_content_comparison_and_hides_private_baselines(api_env, monkeypatch):
    client, _manager, _config, ids, executor, _replies, _fetches = api_env
    for agent_id in ids.values():
        assert push(client, agent_id).status_code == 200

    def no_comparison(*_args, **_kwargs):
        pytest.fail('A summary listing must not load profiles or compare contents')
    monkeypatch.setattr(agents, '_agent_with_content_sync', no_comparison)
    response = client.get('/api/agents?summary=1')
    assert response.status_code == 200
    listed = {agent['service_type']: agent for agent in response.json}
    assert set(listed) == {'mihomo', 'mosdns'}
    for agent in listed.values():
        assert {'id', 'name', 'status', 'service_type'} <= set(agent)
        assert not {'content_sync', 'has_update', 'token', 'applied_content'} & set(agent)
    assert executor.jobs == []


@pytest.mark.parametrize('change', ['agent_field', 'remote_source'])
def test_first_check_after_activation_sees_inputs_changed_since_preparation(api_env, content_env, change):
    """Preparation and activation can be far apart: a ready release waits for
    its confirmation. The first drift check after success compares with the
    inputs current then, not with the preparation's result."""
    client, manager, config, ids, executor, replies, fetches = api_env
    _config, remote, _fetches, repository = content_env
    agent_id = ids['mihomo']
    replies['status'] = 'ready'
    assert push(client, agent_id).status_code == 202
    assert fetches  # The preparation rendered the remote subscription.
    if change == 'agent_field':
        manager.set_domain_discovery(agent_id, True)  # Adds the region probe entry.
    else:
        # The subscription's source interval ends with new upstream nodes.
        from backend.utils.subscription_refresh import _download_cache_path
        remote['provider'] = remote['provider'].replace('first.test', 'second.test')
        cache_path = _download_cache_path(config['subscriptions'][0]['url'])
        downloaded = repository.read_shared_json(cache_path)
        downloaded['fetched_at'] = '2000-01-01T00:00:00+00:00'
        repository.write_shared_json(cache_path, downloaded)
    manager.record_deployment(agent_id, 'first', {'status': 'succeeded', 'config_version': 'agent-rewritten-hash'})
    assert listing(client)['mihomo']['content_sync'] == {'status': 'checking'}
    executor.run()
    assert listing(client)['mihomo']['content_sync']['status'] == 'changed'
