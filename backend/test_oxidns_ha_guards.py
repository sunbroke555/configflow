"""Exercise HA mutation guards and response envelopes through authenticated HTTP."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
import requests

from backend.agents import oxidns_ha_publish as publications
from backend.agents import oxidns_rule_update
from backend.test_agent_upgrade_auth import authenticated_app  # noqa: F401


@pytest.fixture(autouse=True)
def no_background_reconciliation(monkeypatch):
    monkeypatch.setattr(publications, 'start_loop', lambda _: None)


@pytest.fixture
def ha_http(authenticated_app, monkeypatch):
    client, manager, _, admin, _ = authenticated_app
    agents = [manager.register_agent({
        'name': name, 'host': host, 'service_type': 'oxidns',
        'deployment_method': 'shell', 'version': '1.8.45-go',
    }) for name, host in (('dns-main', '192.0.2.20'), ('dns-backup', '192.0.2.21'))]
    agents = [manager.get_agent_by_id(agent['id']) for agent in agents]
    # Scope the network tripwire to the native rule bridge. Other tests may
    # still be draining unrelated background requests in this process.
    monkeypatch.setattr(oxidns_rule_update, 'requests', SimpleNamespace(
        Session=lambda: pytest.fail('unexpected native Agent operation'),
        RequestException=requests.RequestException))
    return client, manager, agents, admin


def seed(manager, agents, *, enabled=True, status=None, kind='rules'):
    primary, secondary = agents
    children = {item['id']: 'child-' + str(index) for index, item in enumerate(agents)}
    job = {'id': 'http-pair', 'owner_agent_id': primary['id'],
           'owner_deployment_id': children[primary['id']], 'children': children,
           'kind': kind, 'status': status, 'phase': 'preparing' if status == 'preparing' else 'applying',
           'message': 'pair update', 'active_agent_id': secondary['id'],
           'members': [{'agent_id': item['id'], 'deployment_id': children[item['id']],
                        'name': item['name'], 'status': 'waiting'} for item in agents],
           'private_source': 'must-not-be-public'}

    def update(items):
        for item in items:
            if item['id'] not in children:
                continue
            if enabled:
                item['oxidns_ha'] = {'enabled': True, 'group_id': 'http-pair',
                    'peer_id': secondary['id'] if item['id'] == primary['id'] else primary['id'],
                    'role': 'primary' if item['id'] == primary['id'] else 'secondary',
                    'vip': '192.0.2.53', 'vrid': 53}
                item['oxidns_ha_state'] = {'status': 'succeeded'}
            if status is not None:
                item[publications.FIELD] = deepcopy(job)
                item['deployments'] = {children[item['id']]: {
                    'deployment_id': children[item['id']], 'status': 'succeeded',
                    'ha_publication': publications.summary(job)}}
    manager._update_agents(update)
    return job


@pytest.mark.parametrize('status', ['preparing', 'applying', 'unknown', 'ready'])
def test_oxidns_ha_initial_sync_blocks_native_rule_mutations_on_both_members(ha_http, status):
    client, manager, agents, admin = ha_http
    seed(manager, agents, enabled=False, status=status, kind='enable')
    for agent in agents:
        response = client.post('/api/agents/' + agent['id'] + '/oxidns/rules/update',
                               json={}, headers=admin)
        assert response.status_code == 409
        assert response.get_json()['success'] is False
        assert response.get_json()['code'] == 'SERVICE_BUSY'
        assert response.headers['Cache-Control'] == 'no-store'
        assert agent['token'] not in response.get_data(as_text=True)


@pytest.mark.parametrize('method,status,task_status,code', [
    ('GET', None, 'idle', 200),
    ('GET', 'applying', 'running', 202),
    ('POST', 'applying', 'running', 202),
    ('GET', 'succeeded', 'succeeded', 200),
    ('GET', 'failed', 'failed', 200),
])
def test_oxidns_ha_rules_http_preserves_success_envelope(ha_http, monkeypatch,
                                                      method, status, task_status, code):
    client, manager, agents, admin = ha_http
    job = seed(manager, agents, status=status)
    # The durable task was already reserved; HTTP polls it without executing
    # reconciliation here, which is covered by the full pair publication tests.
    monkeypatch.setattr(publications, 'advance', lambda *a: None)
    for agent in agents:
        response = client.open('/api/agents/' + agent['id'] + '/oxidns/rules/update',
                               method=method, headers=admin, **({'json': {}} if method == 'POST' else {}))
        assert response.status_code == code
        body = response.get_json()
        assert body['success'] is True
        assert body['task']['status'] == task_status
        assert response.headers['Cache-Control'] == 'no-store'
        if status is not None:
            assert body['ha_publication']['id'] == job['id']
        assert 'private_source' not in response.get_data(as_text=True)
        assert agent['token'] not in response.get_data(as_text=True)


@pytest.mark.parametrize('force', [False, True])
def test_oxidns_ha_disable_clears_current_publication_but_retains_history(ha_http, monkeypatch, force):
    client, manager, agents, admin = ha_http
    job = seed(manager, agents, status='succeeded', kind='enable')
    removed = []

    def remove(agent):
        removed.append(agent['id'])
        if force and agent['id'] == agents[1]['id']:
            raise requests.ConnectionError('offline')
        return {'removed': True}

    monkeypatch.setattr(manager, 'oxidns_ha_remove', remove)
    response = client.delete('/api/agents/' + agents[0]['id'] + '/oxidns-ha' + ('?force=1' if force else ''),
                             headers=admin)
    assert response.status_code == 200
    assert response.get_json()['success'] is True
    assert removed == [agents[1]['id'], agents[0]['id']]
    for agent in agents:
        stored = manager.get_agent_by_id(agent['id'])
        assert publications.FIELD not in stored
        assert 'oxidns_ha' not in stored and 'oxidns_ha_state' not in stored
        child = stored['deployments'][job['children'][agent['id']]]
        assert child['ha_publication']['id'] == job['id']
        response = client.get('/api/agents/' + agent['id'] + '/oxidns-ha', headers=admin)
        assert response.status_code == 200
        assert response.get_json() == {'success': True, 'config': None}
