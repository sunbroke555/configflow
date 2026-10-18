from copy import deepcopy

import pytest
from flask import Flask

from backend.common import config as config_module
from backend.common.agent_manager import init_agent_manager
from backend.common.config_repository import ProfileRepository
from backend.routes import register_blueprints


@pytest.fixture
def env(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    repository.create_profile({'id': 'outside', 'name': '外面'})
    repository.create_profile({'id': 'home', 'name': '家里'})
    monkeypatch.setattr(config_module, '_repository', repository)
    manager = init_agent_manager()
    agents = []
    for index, service in enumerate(('mihomo', 'oxidns')):
        registered = manager.register_agent({'name': f'外面 {service}', 'host': f'192.0.2.{index + 1}',
                                             'profile_id': 'outside', 'service_type': service})
        agents.append(manager.get_agent_by_id(registered['id']))
    manager.register_agent({'name': 'unrelated', 'host': '192.0.2.9', 'profile_id': 'home'})
    app = Flask(__name__)
    register_blueprints(app)
    return repository, manager, agents, app.test_client()


def rebind(client, agents, **extra):
    return client.post('/api/profiles/outside/agents/rebind', json={
        'target_profile_id': 'home', 'agent_ids': [agent['id'] for agent in agents], **extra})


def test_lists_safe_agent_identities_and_names_in_delete_error(env):
    repository, manager, agents, client = env
    manager.update_heartbeat(agents[0]['id'], {'config_version': 'sensitive-history'})
    bindings = client.get('/api/profiles/outside/agents').json
    assert bindings['profile']['name'] == '外面'
    assert {agent['id'] for agent in bindings['agents']} == {agent['id'] for agent in agents}
    assert bindings['agents'][0]['host'] == '192.0.2.1'
    assert bindings['agents'][1]['service_type'] == 'oxidns'
    assert all(set(agent) == {'id', 'name', 'host', 'port', 'service_type'} for agent in bindings['agents'])
    profiles = client.get('/api/profiles').json
    assert next(profile for profile in profiles if profile['id'] == 'outside')['bound_agents'] == bindings['agents']
    response = client.delete('/api/profiles/outside')
    assert response.status_code == 409
    assert all(agent['name'] in response.json['message'] for agent in agents)
    assert repository.get_profile('outside')


def test_rebind_is_explicit_and_preserves_agent_contents_and_profile_settings(env, monkeypatch):
    repository, manager, agents, client = env
    repository.save_profile('outside', {'mihomo': {'custom_config': 'port: 12345'}})
    repository.update_system_transaction(lambda system: system['agents'][0].update(
        deployments={'old': {'status': 'succeeded', 'profile_id': 'outside'}},
        applied_content={'profile_id': 'outside', 'fingerprint': 'applied'}, config_version='deployed'))
    before = repository.export_all()
    requests = []
    monkeypatch.setattr('backend.agents.manager.requests.post', lambda *a, **kw: requests.append((a, kw)))
    response = rebind(client, agents)
    assert response.status_code == 200
    assert response.json['from_profile']['name'] == '外面'
    assert response.json['to_profile']['name'] == '家里'
    assert {agent['id'] for agent in response.json['agents']} == {agent['id'] for agent in agents}
    after = repository.export_all()
    assert after['profiles'] == before['profiles']
    assert after['shared'] == before['shared']
    for old, new in zip(before['system']['agents'], after['system']['agents']):
        expected = deepcopy(old)
        if old['profile_id'] == 'outside':
            expected.update(profile_id='home', updated_at=new['updated_at'])
        assert new == expected
    assert requests == []
    assert rebind(client, agents).status_code == 409  # ambiguous retries cannot move a different set
    assert client.delete('/api/profiles/outside').status_code == 204
    assert all(agent['profile_id'] == 'home' for agent in manager.get_all_agents())


@pytest.mark.parametrize('field,status', [
    ('deployments', 'preparing'), ('deployments', 'uploading'), ('deployments', 'checking'),
    ('deployments', 'ready'), ('deployments', 'rollback_failed'),
    ('latest_upgrade', 'uploading'), ('latest_upgrade', 'rollback_failed'),
])
def test_one_busy_agent_blocks_the_entire_batch(env, field, status):
    repository, _, agents, client = env
    state = {'status': status}
    repository.update_system_transaction(lambda system: system['agents'][1].update(
        {field: {'busy': state} if field == 'deployments' else state}))
    before = repository.export_all()
    response = rebind(client, agents)
    assert response.status_code == 409
    assert agents[1]['name'] in response.json['message']
    assert repository.export_all() == before


@pytest.mark.parametrize('change', ['added', 'moved', 'removed'])
def test_changed_binding_set_requires_review_before_mutating(env, change):
    repository, manager, agents, client = env
    if change == 'added':
        manager.register_agent({'name': 'newly bound', 'host': '192.0.2.3', 'profile_id': 'outside'})
    elif change == 'moved':
        manager.update_agent(agents[1]['id'], {'profile_id': 'home'})
    else:
        manager.delete_agent(agents[1]['id'])
    before = repository.export_all()
    response = rebind(client, agents)
    assert response.status_code == 409
    assert '绑定已变化' in response.json['message']
    assert repository.export_all() == before


@pytest.mark.parametrize('payload,status', [
    ({'target_profile_id': 'outside'}, 400), ({'target_profile_id': None}, 400),
    ({'target_profile_id': 'missing'}, 404), ({'agent_ids': []}, 400),
    ({'agent_ids': 'all'}, 400), ({'agent_ids': [None]}, 400),
    ({'agent_ids': ['one', 'one']}, 400),
])
def test_invalid_rebind_does_not_change_anything(env, payload, status):
    repository, _, agents, client = env
    before = repository.export_all()
    assert rebind(client, agents, **payload).status_code == status
    assert repository.export_all() == before


def test_rechecks_busy_state_inside_the_write_transaction(env, monkeypatch):
    repository, manager, agents, client = env
    original = repository.update_system_transaction

    def raced(updater):
        monkeypatch.setattr(repository, 'update_system_transaction', original)
        original(lambda system: system['agents'][1].update(deployments={'new': {'status': 'preparing'}}))
        return original(updater)

    monkeypatch.setattr(repository, 'update_system_transaction', raced)
    assert rebind(client, agents).status_code == 409
    assert all(manager.get_agent_by_id(agent['id'])['profile_id'] == 'outside' for agent in agents)


def test_missing_profile_and_invalid_json_are_rejected(env):
    _, _, _, client = env
    assert client.get('/api/profiles/missing/agents').status_code == 404
    assert client.post('/api/profiles/outside/agents/rebind', json=['wrong']).status_code == 400


def test_default_profile_rebinding_does_not_implicitly_switch_default(env):
    repository, manager, agents, client = env
    for agent in agents:
        manager.update_agent(agent['id'], {'profile_id': 'default'})
    response = client.post('/api/profiles/default/agents/rebind', json={
        'target_profile_id': 'home', 'agent_ids': [agent['id'] for agent in agents]})
    assert response.status_code == 200
    assert repository.default_profile_id() == 'default'
    assert client.delete('/api/profiles/default').status_code == 409
    assert client.delete('/api/profiles/default?new_default=home').status_code == 204
