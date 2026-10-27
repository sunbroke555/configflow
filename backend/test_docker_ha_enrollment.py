"""The guided Docker installer never needs an administrator or global key."""
import concurrent.futures
import json
import time

import pytest

from backend.agents import docker_ha_enrollment as enrollment
from backend.test_agent_upgrade_auth import authenticated_app  # noqa: F401


BODY = {'server_url': 'https://panel.example', 'agent_name': 'new-dns', 'agent_ip': '192.168.50.20',
        'data_dir': '/srv/oxidns', 'oxidns_port': 8082, 'network_mode': 'host'}


def capability(manager, body=None):
    return enrollment.issue(manager.repository, body or BODY, manager.repository.default_profile_id())


def enroll_path(cap):
    return '/api/agents/docker-oxidns-ha-enroll/' + cap['url'].rsplit('/', 1)[1]


def test_docker_ha_enrollment_guide_is_authenticated_and_does_not_register(authenticated_app, monkeypatch):
    client, manager, _, admin, _ = authenticated_app
    monkeypatch.setenv('AGENT_REGISTRATION_KEY', 'global-registration-secret')
    before = manager.get_all_agents()
    assert client.post('/api/agents/docker-oxidns-ha-guide', json=BODY).status_code == 401
    response = client.post('/api/agents/docker-oxidns-ha-guide', json=BODY, headers=admin)
    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'no-store'
    result = response.get_json()
    assert result['enrollment_expires_at'] > time.time()
    assert 'docker-oxidns-ha-enroll/' in result['install_command']
    assert admin['Authorization'].split()[1] not in response.text
    assert 'global-registration-secret' not in response.text
    assert manager.get_all_agents() == before


def test_docker_ha_enrollment_locked_registration_and_lost_response_replay(authenticated_app, monkeypatch):
    client, manager, _, _, _ = authenticated_app
    monkeypatch.setenv('AGENT_REGISTRATION_KEY', 'global-registration-secret')
    cap = capability(manager)
    path, headers = enroll_path(cap), {'Authorization': 'Bearer ' + cap['token']}
    assert client.post(path, json={}).status_code == 401
    assert client.post(path, json={}, headers={'Authorization': 'Bearer ' + 'x' * 43}).status_code == 401
    response = client.post(path, json={}, headers=headers)
    assert response.status_code == 200
    identity = response.get_json()
    assert set(identity) == {'success', 'agent_id', 'token'}
    assert client.post(path, json={}, headers=headers).get_json() == identity
    agent = manager.get_agent_by_id(identity['agent_id'])
    assert identity['token'] == agent['token']
    assert agent['name'] == 'new-dns-oxidns' and agent['host'] == BODY['agent_ip']
    assert agent['service_type'] == 'oxidns' and agent['deployment_method'] == 'docker'
    assert agent['status'] == 'offline'  # Only an actual heartbeat can say installed/online.
    heartbeat = client.post('/api/agents/' + identity['agent_id'] + '/heartbeat',
                            json={'version': '1.8.47-go', 'service_status': 'running'},
                            headers={'Authorization': 'Bearer ' + identity['token']})
    assert heartbeat.status_code == 200, heartbeat.get_json()
    assert manager.get_agent_by_id(identity['agent_id'])['status'] == 'online'
    assert client.post(path, json={'host': '192.168.50.99'}, headers=headers).status_code == 400


def test_docker_ha_enrollment_rejects_expired_and_changed_identity(authenticated_app):
    client, manager, _, _, _ = authenticated_app
    cap = capability(manager)
    headers = {'Authorization': 'Bearer ' + cap['token']}
    identity = client.post(enroll_path(cap), json={}, headers=headers).get_json()
    manager._update_agents(lambda agents: next(a for a in agents if a['id'] == identity['agent_id']).update(port=12345))
    assert client.post(enroll_path(cap), json={}, headers=headers).status_code == 409
    path = manager.repository.data_dir / 'docker-ha-installations' / (cap['url'].rsplit('/', 1)[1] + '.json')
    session = json.loads(path.read_text())
    assert path.stat().st_mode & 0o777 == 0o600
    session['expires'] = time.time() - 1
    path.write_text(json.dumps(session))
    assert client.post(enroll_path(cap), json={}, headers=headers).status_code == 401


def test_docker_ha_enrollment_cannot_take_over_existing_device(authenticated_app):
    client, manager, _, _, _ = authenticated_app
    old = manager.register_agent({'name': 'new-dns-oxidns', 'host': BODY['agent_ip'], 'port': 8082,
                                  'service_type': 'oxidns', 'deployment_method': 'docker'})
    cap = capability(manager)
    result = client.post(enroll_path(cap), json={}, headers={'Authorization': 'Bearer ' + cap['token']})
    assert result.status_code == 409
    assert manager.get_agent_by_id(old['id'])['token'] == old['token']


def test_docker_ha_enrollment_regeneration_and_expiry_keep_exact_installation(authenticated_app):
    client, manager, _, _, _ = authenticated_app
    cap = capability(manager)
    assert capability(manager) == cap
    identity = client.post(enroll_path(cap), json={}, headers={'Authorization': 'Bearer ' + cap['token']}).get_json()
    path = manager.repository.data_dir / 'docker-ha-installations' / (cap['url'].rsplit('/', 1)[1] + '.json')
    session = json.loads(path.read_text())
    session['expires'] = time.time() - 1
    path.write_text(json.dumps(session))
    renewed = capability(manager)
    assert renewed['url'] == cap['url'] and renewed['token'] != cap['token']
    assert client.post(enroll_path(cap), json={}, headers={'Authorization': 'Bearer ' + cap['token']}).status_code == 401
    assert client.post(enroll_path(renewed), json={}, headers={'Authorization': 'Bearer ' + renewed['token']}).get_json() == identity
    changed = capability(manager, {**BODY, 'data_dir': '/different/data'})
    assert changed['url'] != cap['url']
    assert client.post(enroll_path(changed), json={}, headers={'Authorization': 'Bearer ' + changed['token']}).status_code == 409


def test_docker_ha_enrollment_concurrent_retries_create_one_identity(authenticated_app):
    _, manager, _, _, _ = authenticated_app
    cap = capability(manager)
    session_id = cap['url'].rsplit('/', 1)[1]
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        values = list(pool.map(lambda _: enrollment.enroll(manager.repository, manager, session_id, cap['token']), range(6)))
    assert all(value == values[0] for value in values)
    assert len([a for a in manager.get_all_agents() if a['name'] == 'new-dns-oxidns']) == 1


@pytest.mark.parametrize('body', [{'action': 'inspect'}, {'action': 'verify', 'vip': '192.168.50.53'}])
def test_docker_ha_enrollment_read_only_steps_issue_no_capability(authenticated_app, body):
    client, manager, _, admin, _ = authenticated_app
    response = client.post('/api/agents/docker-oxidns-ha-guide', json=body, headers=admin)
    assert response.status_code == 200
    assert not (manager.repository.data_dir / 'docker-ha-installations').exists()
