"""Full HTTP auth and durable onboarding ordering without touching host networking."""
from copy import deepcopy
from datetime import datetime, timedelta
import fcntl
import json
from pathlib import Path
import stat
from urllib.parse import urlsplit

import pytest

from backend.agents import gateway_installation as sessions
from backend.agents.manager import AgentDeploymentConflict
from backend.common.gateway_onboarding import prepare_gateway_onboarding
from backend.test_agent_upgrade_auth import authenticated_app
from backend.test_gateway_onboarding import snapshot, form

SPEC = {'interface': 'eth0', 'address': '192.168.1.2', 'lan_cidr': '192.168.1.0/24', 'upstream_gateway': '192.168.1.1'}
BASE = '/api/agents/gateway-installations'


@pytest.fixture
def setup(authenticated_app):
    client, manager, _, admin, _ = authenticated_app
    repo = manager.repository
    repo.create_profile({'id': 'home', 'name': 'Gateway'})
    config = snapshot()
    config, report = prepare_gateway_onboarding(config, form())
    assert report['ready']
    repo.save_system({'system_config': config['system_config']})
    repo.save_shared({'nodes': config['nodes'], 'rule_library': config['rule_library']})
    repo.save_profile('home', {key: config[key] for key in ('proxy_groups', 'rule_configs', 'mihomo', 'oxidns')})
    body = {'profile_id': 'home', 'name': 'home-linux', 'server_url': 'https://config.example',
            'network': SPEC, 'config_revision': repo.config_revision('home')}
    response = client.post(BASE, headers=admin, json=body)
    assert response.status_code == 200, response.get_json()
    public = response.get_json()
    saved = sessions._read(repo, public['id'])
    return client, manager, admin, body, saved


def register_pair(manager, session, profile='home'):
    return {service: manager.register_agent({'name': session['name'] + '-' + service,
        'host': SPEC['address'], 'port': session['ports'][service], 'deployment_method': 'shell',
        'service_type': service, 'profile_id': profile, 'version': '1.8.25-go'})
        for service in sessions.SERVICES}


def bind(manager, session, pair):
    return sessions.enroll(manager.repository, manager, session['id'], session['token'],
                          {'agents': {service: {key: pair[service][key] for key in ('id', 'token')}
                                      for service in sessions.SERVICES}})


def auto_install(setup, ip=''):
    client, manager, admin, body, _ = setup
    body = {key: value for key, value in body.items() if key != 'network'}
    body.update(name='auto-linux', auto_network=True, agent_ip=ip)
    response = client.post(BASE, headers=admin, json=body)
    assert response.status_code == 200, response.get_json()
    saved = sessions._read(manager.repository, response.json['id'])
    return body, saved


def auto_register(setup, saved, service, host=SPEC['address'], **kwargs):
    client, _, _, _, _ = setup
    return client.post('/api/agents/register', json={
        'name': saved['name'] + '-' + service, 'host': host, 'port': saved['ports'][service],
        'service_type': service, 'deployment_method': 'shell', 'version': '1.8.25-go',
        'installation_id': saved['id'], 'installation_token': saved['token']}, **kwargs)


def network_detection(spec=SPEC):
    return {'interfaces': [{'name': spec['interface'], 'addresses': [spec['address'] + '/24']}],
            'default_routes': [{'interface': spec['interface'], 'gateway': spec['upstream_gateway']}], 'suggested': spec}


def test_network_confirmation_reuses_live_agent_and_preserves_config_revision(setup, monkeypatch):
    client, manager, admin, _, _ = setup
    _, saved = auto_install(setup)
    pair = {service: auto_register(setup, saved, service).json for service in sessions.SERVICES}
    bind(manager, saved, pair)
    calls = []
    def status(*args):
        calls.append(args)
        return {'success': True, 'supported': True, 'managed': {'active': False}, 'detection': network_detection()}
    monkeypatch.setattr('backend.agents.linux_gateway.call', status)
    before = manager.repository.export_all()
    url = BASE + '/' + saved['id'] + '/network'
    assert client.post(url, json={'auto_network': True}).status_code == 401
    for body in ({'auto_network': True}, {'network': SPEC}):
        response = client.post(url, headers=admin, json=body)
        assert response.status_code == 200, response.json
        assert response.json['network'] == SPEC
        assert response.json['phase'] == 'enrolled'
        assert calls[-1] == (manager, pair['mihomo']['id'], 'status', 'GET', 'home')
        assert manager.repository.export_all() == before
    assert sessions._read(manager.repository, saved['id'])['auto_network'] is False


@pytest.mark.parametrize('change', [
    {'interfaces': []}, {'default_routes': []}, {'default_routes': [{'interface': 'other', 'gateway': SPEC['upstream_gateway']}]},
    {'default_routes': [{}, {}]}, {'interfaces': [{'name': 'eth0', 'addresses': ['malformed']}]},
])
def test_network_confirmation_rejects_mismatched_detection_without_writes(setup, monkeypatch, change):
    client, manager, admin, _, _ = setup
    _, saved = auto_install(setup)
    pair = {service: auto_register(setup, saved, service).json for service in sessions.SERVICES}
    bind(manager, saved, pair)
    monkeypatch.setattr('backend.agents.linux_gateway.call', lambda *_: {'success': True, 'supported': True, 'managed': {}, 'detection': {**network_detection(), **change}})
    before = sessions._read(manager.repository, saved['id'])
    response = client.post(BASE + '/' + saved['id'] + '/network', headers=admin, json={'network': SPEC})
    assert response.status_code == 409, response.json
    assert sessions._read(manager.repository, saved['id']) == before


@pytest.mark.parametrize('body', [{'auto_network': 1}, {'auto_network': False}, {'network': SPEC, 'auto_network': True}, {'network': {**SPEC, 'address': '192.168.1.99'}}])
def test_network_confirmation_cannot_switch_hosts_or_skip_registration(setup, body):
    client, manager, admin, _, saved = setup
    before = sessions._read(manager.repository, saved['id'])
    response = client.post(BASE + '/' + saved['id'] + '/network', headers=admin, json=body)
    assert response.status_code in (400, 409)
    assert sessions._read(manager.repository, saved['id']) == before


def test_network_changes_cannot_redirect_active_forwarding_or_switch_registered_host(setup, monkeypatch):
    client, manager, admin, _, _ = setup
    _, saved = auto_install(setup)
    pair = {service: auto_register(setup, saved, service).json for service in sessions.SERVICES}
    bind(manager, saved, pair)
    current = sessions._read(manager.repository, saved['id'])
    current['network'] = SPEC
    sessions._write(manager.repository, current)
    alternate = {**SPEC, 'interface': 'eth1'}
    status = {'success': True, 'supported': True, 'managed': {'active': True}, 'detection': network_detection(alternate)}
    monkeypatch.setattr('backend.agents.linux_gateway.call', lambda *_: status)
    url = BASE + '/' + saved['id'] + '/network'
    response = client.post(url, headers=admin, json={'network': alternate})
    assert response.status_code == 409 and '撤销' in response.json['message']
    status['managed'] = {'active': False}
    response = client.post(url, headers=admin, json={'network': {**SPEC, 'address': '192.168.1.99'}})
    assert response.status_code == 409 and '目标主机' in response.json['message']
    assert sessions._read(manager.repository, saved['id']) == current


def test_reinstall_on_a_forwarding_host_confirms_its_live_network(setup, monkeypatch):
    # Preparing the installation again after a configuration change keeps the
    # host's forwarding; detecting the same network must not demand removing it.
    client, manager, admin, _, _ = setup
    _, saved = auto_install(setup)
    pair = {service: auto_register(setup, saved, service).json for service in sessions.SERVICES}
    bind(manager, saved, pair)
    status = {'success': True, 'supported': True, 'managed': {'active': True, 'spec': SPEC},
              'detection': network_detection()}
    monkeypatch.setattr('backend.agents.linux_gateway.call', lambda *_: status)
    url = BASE + '/' + saved['id'] + '/network'
    response = client.post(url, headers=admin, json={'auto_network': True})
    assert response.status_code == 200, response.json
    assert sessions._read(manager.repository, saved['id'])['network'] == SPEC
    status.update(managed={'active': True, 'spec': {**SPEC, 'interface': 'eth1'}})
    response = client.post(url, headers=admin, json={'auto_network': True})
    assert response.status_code == 200
    current = sessions._read(manager.repository, saved['id'])
    current['network'] = {**SPEC, 'address': SPEC['address'], 'interface': '', 'lan_cidr': '', 'upstream_gateway': ''}
    sessions._write(manager.repository, current)
    response = client.post(url, headers=admin, json={'auto_network': True})
    assert response.status_code == 409 and '撤销' in response.json['message']


@pytest.mark.parametrize('ip', ['', SPEC['address']])
def test_optional_ip_reuses_agent_detection_and_pins_exact_pair(setup, ip):
    client, manager, admin, _, _ = setup
    body, saved = auto_install(setup, ip)
    assert saved['network'] == {**dict.fromkeys(SPEC, ''), 'address': ip}
    assert f'"agent_ip": "{ip}"' in saved['script']
    assert 'detect-gateway-network' not in saved['script']
    first = auto_register(setup, saved, 'mihomo')
    assert first.status_code == 200, first.json
    assert sessions._read(manager.repository, saved['id'])['network']['address'] == SPEC['address']
    assert auto_register(setup, saved, 'oxidns', '192.168.1.99').status_code == 409
    second = auto_register(setup, saved, 'oxidns')
    assert second.status_code == 200, second.json
    pair = {'mihomo': first.json, 'oxidns': second.json}
    assert bind(manager, saved, pair)['success']
    public = sessions.get(manager.repository, manager, saved['id'])
    assert public['phase'] == 'enrolled' and public['auto_network'] is True
    assert all(agent['host'] == SPEC['address'] for agent in public['agents'].values())
    assert client.post(BASE, headers=admin, json=body).json['id'] == saved['id']


def test_automatic_ip_registration_keeps_empty_host_source_fallback(setup):
    _, manager, _, _, _ = setup
    _, saved = auto_install(setup)
    first = auto_register(setup, saved, 'mihomo', '', environ_overrides={'REMOTE_ADDR': SPEC['address']})
    assert first.status_code == 200, first.json
    assert manager.get_agent_by_id(first.json['id'])['host'] == SPEC['address']


def test_auto_ip_keeps_owned_host_after_lost_session_receipt(setup):
    _, manager, _, _, _ = setup
    _, saved = auto_install(setup)
    assert auto_register(setup, saved, 'mihomo').status_code == 200
    sessions._write(manager.repository, saved)  # Simulate crash before receipt commit.
    assert auto_register(setup, saved, 'oxidns', '192.168.1.99').status_code == 409
    replay = auto_register(setup, saved, 'mihomo')
    assert replay.status_code == 200
    assert sessions._read(manager.repository, saved['id'])['network']['address'] == SPEC['address']


@pytest.mark.parametrize('changes', [
    {'agent_ip': '127.0.0.1'}, {'agent_ip': '0.0.0.0'}, {'agent_ip': 'localhost'},
    {'agent_ip': '192.168.1.2;reboot'}, {'agent_ip': None}, {'auto_network': 'true'}, {'network': SPEC},
])
def test_invalid_auto_network_request_cannot_create_or_change_installation(setup, changes):
    client, manager, admin, _, _ = setup
    body, saved = auto_install(setup)
    before = manager.repository.export_all()
    assert client.post(BASE, headers=admin, json={**body, **changes}).status_code == 400
    assert manager.repository.export_all() == before
    assert sessions._read(manager.repository, saved['id']) == saved


@pytest.mark.parametrize('detection', [SPEC, {**SPEC, 'address': '192.168.1.99'}, None])
def test_auto_network_reads_registered_agent_and_rejects_ambiguous_or_other_host(setup, monkeypatch, detection):
    _, manager, _, _, _ = setup
    _, saved = auto_install(setup)
    pair = {service: auto_register(setup, saved, service).json for service in sessions.SERVICES}
    bind(manager, saved, pair)
    saved = sessions._read(manager.repository, saved['id'])
    before = deepcopy(saved)
    calls = []
    def status(*args):
        calls.append(args)
        return {'success': True, 'supported': True, 'detection': {'suggested': detection}}
    monkeypatch.setattr('backend.agents.linux_gateway.call', status)
    if detection == SPEC:
        sessions._resolve_auto_network(manager.repository, manager, saved)
        assert saved['network'] == SPEC
    else:
        with pytest.raises(sessions.InstallationError):
            sessions._resolve_auto_network(manager.repository, manager, saved)
        assert saved == before
    assert calls == [(manager, pair['mihomo']['id'], 'status', 'GET', 'home')]


def test_auto_network_resolves_before_publication_without_implicitly_applying(setup, monkeypatch):
    client, manager, admin, _, _ = setup
    _, saved = auto_install(setup)
    pair = {service: auto_register(setup, saved, service).json for service in sessions.SERVICES}
    bind(manager, saved, pair)
    calls = []
    def status(*args):
        calls.append(args[2])
        return {'success': True, 'supported': True, 'detection': {'suggested': SPEC}}
    monkeypatch.setattr('backend.agents.linux_gateway.call', status)
    monkeypatch.setattr(sessions, 'run_publication', lambda *args: None)
    class Thread:
        def __init__(self, *, target, **kwargs): self.target = target
        def start(self): self.target()
    monkeypatch.setattr(sessions.threading, 'Thread', Thread)
    response = client.post(BASE + '/' + saved['id'] + '/publish', headers=admin, json={})
    assert response.status_code == 202, response.json
    assert response.json['network'] == SPEC
    assert response.json['phase'] == 'publishing'
    assert calls == ['status']


def test_auto_network_will_not_accept_a_changed_host_during_detection(setup, monkeypatch):
    _, manager, _, _, _ = setup
    _, saved = auto_install(setup)
    pair = {service: auto_register(setup, saved, service).json for service in sessions.SERVICES}
    bind(manager, saved, pair)
    saved = sessions._read(manager.repository, saved['id'])
    before = deepcopy(saved)
    def status(*args):
        manager._update_agents(lambda agents: next(a for a in agents if a['id'] == pair['mihomo']['id']).update(host='192.168.1.99'))
        return {'success': True, 'supported': True, 'detection': {'suggested': SPEC}}
    monkeypatch.setattr('backend.agents.linux_gateway.call', status)
    with pytest.raises(sessions.InstallationError):
        sessions._resolve_auto_network(manager.repository, manager, saved)
    assert saved == before


def test_repreparing_a_changed_auto_installation_does_not_reuse_its_blocked_capability(setup):
    client, manager, admin, _, _ = setup
    body, saved = auto_install(setup)
    pair = {service: auto_register(setup, saved, service).json for service in sessions.SERVICES}
    bind(manager, saved, pair)
    manager._update_agents(lambda agents: next(a for a in agents if a['id'] == pair['mihomo']['id']).update(host='192.168.1.99'))
    assert sessions.get(manager.repository, manager, saved['id'])['target_changed']
    response = client.post(BASE, headers=admin, json=body)
    assert response.status_code == 200, response.json
    assert response.json['id'] != saved['id'] and not response.json['target_changed']


def prepare_run(manager, session):
    pair = register_pair(manager, session)
    bind(manager, session, pair)
    saved = sessions._read(manager.repository, session['id'])
    saved.update(phase='publishing', deployments={s: {'deployment_id': 'job-' + s, 'status': 'preparing'} for s in sessions.SERVICES})
    sessions._write(manager.repository, saved)
    return saved


def test_private_session_creation_idempotent_exact_profile_scoped_and_script_scoped(setup):
    client, manager, admin, body, saved = setup
    before = manager.repository.export_all()
    response = client.post(BASE, headers=admin, json=body)
    assert response.get_json()['id'] == saved['id']
    assert manager.repository.export_all() == before
    public = client.get(BASE + '/' + saved['id'], headers=admin).get_json()
    assert 'script' not in public and 'token' not in public
    assert saved['token'] in public['command']  # Only administrator receives the capability command.
    short_path = urlsplit(public['download_url']).path
    assert short_path == saved['download_path']
    assert short_path.startswith('/api/agents/i/')
    assert client.post(BASE, headers=admin, json=body).json['download_url'] == public['download_url']
    assert stat.S_IMODE(sessions._path(manager.repository, saved['id']).stat().st_mode) == 0o600
    assert client.get(BASE, headers=admin, query_string={'profile_id': 'default'}).get_json()['session'] is None
    assert client.get(BASE, headers=admin, query_string={'profile_id': 'home'}).get_json()['session']['id'] == saved['id']
    path = BASE + '/' + saved['id'] + '/script'
    assert client.get(path).status_code == 401
    assert client.get(path, query_string={'token': '中文'}).status_code == 401
    script = client.get(path, query_string={'token': saved['token']})
    assert script.status_code == 200 and script.data.startswith(b'#!/bin/sh')
    assert script.headers['Cache-Control'] == 'no-store'
    short = client.get(short_path)
    assert short.status_code == 200 and short.data == script.data
    assert b'exec sudo sh "$0" "$@"' in short.data
    assert saved['token'] not in str(before)


@pytest.mark.parametrize('operation', ['list', 'get', 'create', 'publish'])
def test_admin_apis_reject_anonymous_and_agent_tokens(setup, operation):
    client, manager, admin, body, saved = setup
    pair = register_pair(manager, saved)
    for headers in ({}, {'Authorization': 'Bearer ' + pair['mihomo']['token']}):
        if operation == 'list': response = client.get(BASE, headers=headers)
        elif operation == 'get': response = client.get(BASE + '/' + saved['id'], headers=headers)
        elif operation == 'create': response = client.post(BASE, headers=headers, json=body)
        else: response = client.post(BASE + '/' + saved['id'] + '/publish', headers=headers, json={})
        assert response.status_code == 401


@pytest.mark.parametrize('changes', [
    {'network': {**SPEC, 'interface': 'eth0; reboot'}}, {'server_url': 'http://localhost:5000'},
    {'name': 'bad;name'}, {'mihomo_agent_port': 1053}, {'mihomo_agent_port': 8082},
    {'config_revision': True}, {'config_revision': 0}, {'command': 'nft flush ruleset'},
])
def test_invalid_create_never_changes_existing_profile_or_session(setup, changes):
    client, manager, admin, body, saved = setup
    before = manager.repository.export_all()
    previous = sessions._read(manager.repository, saved['id'])
    response = client.post(BASE, headers=admin, json={**body, **changes})
    assert response.status_code in (400, 409), response.get_json()
    assert manager.repository.export_all() == before
    assert sessions._read(manager.repository, saved['id']) == previous


def test_ordinary_profile_cannot_create_gateway_session(setup):
    client, manager, admin, body, saved = setup
    manager.repository.save_profile('home', {'mihomo': {'linux_gateway': {'enabled': False}}})
    body['config_revision'] = manager.repository.config_revision('home')
    assert client.post(BASE, headers=admin, json=body).status_code == 400


def test_expired_capability_cannot_read_script_enroll_or_register(setup):
    client, manager, admin, body, saved = setup
    saved['expires'] = 1
    sessions._write(manager.repository, saved)
    public = client.get(BASE + '/' + saved['id'], headers=admin).get_json()
    assert public['expired'] and 'command' not in public
    assert client.get(BASE + '/' + saved['id'] + '/script', query_string={'token': saved['token']}).status_code == 401
    with pytest.raises(sessions.InstallationError, match='过期'):
        sessions.registration_profile(manager.repository, saved['id'], saved['token'], {})
    pair = register_pair(manager, saved)
    with pytest.raises(sessions.InstallationError, match='过期'):
        bind(manager, saved, pair)


def test_install_registration_bypasses_global_key_only_for_exact_intended_identity(setup, monkeypatch):
    client, manager, admin, body, saved = setup
    monkeypatch.setenv('AGENT_REGISTRATION_KEY', 'private-global-key')
    data = {'name': saved['name'] + '-mihomo', 'host': SPEC['address'], 'port': 8080,
        'service_type': 'mihomo', 'deployment_method': 'shell', 'version': '1.8.25-go',
        'installation_id': saved['id'], 'installation_token': saved['token'], 'profile_id': 'default'}
    assert client.post('/api/agents/register', json={k: v for k, v in data.items() if not k.startswith('installation_')}).status_code == 403
    for changes in ({'host': '192.168.1.3'}, {'port': 8081}, {'service_type': 'mosdns'}, {'installation_token': '中文'}):
        assert client.post('/api/agents/register', json={**data, **changes}).status_code in (401, 403, 409)
    response = client.post('/api/agents/register', json=data)
    assert response.status_code == 200, response.get_json()
    identity = response.get_json()
    agent = manager.get_agent_by_id(identity['id'])
    assert agent['profile_id'] == 'home'
    assert 'installation_token' not in agent and 'installation_id' not in agent
    # A lost first response can recover only the identity this session created.
    replay = client.post('/api/agents/register', json=data)
    assert replay.status_code == 200
    assert replay.get_json()['id'] == identity['id'] and replay.get_json()['token'] == identity['token']
    assert client.post('/api/agents/register', json=data, headers={'Authorization': 'Bearer ' + identity['token']}).status_code == 200


def test_enroll_checks_both_credentials_before_any_rebinding_and_is_idempotent(setup):
    client, manager, admin, body, saved = setup
    pair = register_pair(manager, saved, profile='default')
    invalid = deepcopy(pair)
    invalid['oxidns']['token'] = '中文'
    before = manager.repository.export_all()
    with pytest.raises(sessions.InstallationError, match='身份'):
        bind(manager, saved, invalid)
    assert manager.repository.export_all() == before
    assert bind(manager, saved, pair)['success']
    assert bind(manager, saved, pair)['success']
    public = sessions.get(manager.repository, manager, saved['id'])
    assert public['phase'] == 'enrolled'
    assert all(manager.get_agent_by_id(agent['id'])['profile_id'] == 'home' for agent in pair.values())
    for item in pair.values():
        assert item['token'] not in json.dumps(public)


@pytest.mark.parametrize('busy', ['deployment', 'dns_follow_target', 'dns_follow_source'])
def test_rebind_cannot_move_busy_agent_during_enrollment_or_registration(setup, busy):
    client, manager, admin, body, saved = setup
    pair = register_pair(manager, saved, profile='default')
    agent_id = pair['oxidns']['id']
    if busy == 'deployment':
        manager.begin_deployment(agent_id, 'unrelated-job')
    elif busy == 'dns_follow_target':
        manager._update_agents(lambda agents: next(a for a in agents if a['id'] == agent_id).update(
            dns_follow_state={'status': 'publishing'}))
    else:
        manager._update_agents(lambda agents: next(a for a in agents if a['id'] == pair['mihomo']['id']).update(
            dns_follow={'source_agent_id': agent_id}, dns_follow_state={'status': 'preparing'}))
    before = manager.repository.export_all()
    try:
        with pytest.raises(sessions.InstallationError, match='未完成'):
            bind(manager, saved, pair)
        assert manager.repository.export_all() == before
        agent = manager.get_agent_by_id(agent_id)
        response = client.post('/api/agents/register', json={**agent, 'installation_id': saved['id'],
            'installation_token': saved['token'], 'profile_id': 'home'},
            headers={'Authorization': 'Bearer ' + pair['oxidns']['token']})
        assert response.status_code == 409
        assert manager.repository.export_all() == before
    finally:
        manager.finish_deployment_preparation(agent_id, 'unrelated-job')


def test_publication_is_ordered_and_never_implicitly_applies_forwarding(setup, monkeypatch):
    client, manager, admin, body, saved = setup
    saved = prepare_run(manager, saved)
    calls = []
    def publish(repo, mgr, session, service):
        calls.append(service)
        return {'deployment_id': session['deployments'][service]['deployment_id'], 'status': 'succeeded', 'success': True}
    monkeypatch.setattr(sessions, '_publish_service', publish)
    monkeypatch.setattr('backend.agents.linux_gateway.call', lambda *a, **kw: pytest.fail('forwarding must remain explicit'))
    sessions.run_publication(manager.repository, manager, saved['id'], pause=lambda _: None)
    assert calls == ['mihomo', 'oxidns']
    result = sessions._read(manager.repository, saved['id'])
    assert result['phase'] == 'published'
    assert all(task['status'] == 'succeeded' for task in result['deployments'].values())


@pytest.mark.parametrize('status', ['failed', 'rolled_back', 'rollback_failed'])
def test_failed_mihomo_never_starts_oxidns(setup, monkeypatch, status):
    client, manager, admin, body, saved = setup
    saved = prepare_run(manager, saved)
    calls = []
    def publish(repo, mgr, session, service):
        calls.append(service)
        return {'deployment_id': 'job-mihomo', 'status': status, 'error': 'core health failed'}
    monkeypatch.setattr(sessions, '_publish_service', publish)
    sessions.run_publication(manager.repository, manager, saved['id'], pause=lambda _: None)
    assert calls == ['mihomo']
    assert sessions._read(manager.repository, saved['id'])['phase'] == 'publish_failed'


def test_uncertain_publication_resumes_same_ids_then_explicitly_activates(setup, monkeypatch):
    client, manager, admin, body, saved = setup
    saved = prepare_run(manager, saved)
    calls = []
    def publish(repo, mgr, session, service):
        return {'deployment_id': session['deployments'][service]['deployment_id'], 'status': 'unknown'}
    def poll(agent_id, deployment_id):
        calls.append(('poll', deployment_id))
        return {'deployment_id': deployment_id, 'status': 'ready'}
    def activate(agent_id, deployment_id):
        calls.append(('activate', deployment_id))
        return {'deployment_id': deployment_id, 'status': 'succeeded'}
    monkeypatch.setattr(sessions, '_publish_service', publish)
    monkeypatch.setattr(manager, 'get_deployment', poll)
    monkeypatch.setattr(manager, 'activate_deployment', activate)
    sessions.run_publication(manager.repository, manager, saved['id'], pause=lambda _: None)
    assert calls == [('poll', 'job-mihomo'), ('activate', 'job-mihomo'), ('poll', 'job-oxidns'), ('activate', 'job-oxidns')]
    assert sessions._read(manager.repository, saved['id'])['phase'] == 'published'


@pytest.mark.parametrize('mutation', ['revision', 'binding', 'offline', 'stale'])
def test_changed_config_or_target_blocks_publication_before_any_push(setup, monkeypatch, mutation):
    client, manager, admin, body, saved = setup
    saved = prepare_run(manager, saved)
    if mutation == 'revision': manager.repository.save_profile('home', {'name': 'Changed'})
    elif mutation == 'binding': manager.update_agent(saved['agent_ids']['mihomo'], {'profile_id': 'default'})
    else:
        manager._update_agents(lambda agents: next(a for a in agents if a['id'] == saved['agent_ids']['mihomo']).update(
            {'status': 'offline'} if mutation == 'offline' else {'last_heartbeat': (datetime.now() - timedelta(minutes=3)).isoformat()}))
    monkeypatch.setattr(sessions, '_publish_service', lambda *a: pytest.fail('stale install cannot publish'))
    sessions.run_publication(manager.repository, manager, saved['id'])
    assert sessions._read(manager.repository, saved['id'])['phase'] == 'publish_failed'
    public = sessions.get(manager.repository, manager, saved['id'])
    if mutation in ('revision', 'binding'):
        assert public['config_changed'] or public['target_changed']


def test_process_lease_prevents_second_worker_or_resetting_deployment_ids(setup, monkeypatch):
    client, manager, admin, body, saved = setup
    saved = prepare_run(manager, saved)
    path = sessions._path(manager.repository, saved['id']).with_suffix('.publish.lock')
    with open(path, 'a+b') as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        monkeypatch.setattr(sessions.threading, 'Thread', lambda *a, **kw: pytest.fail('duplicate worker'))
        response = client.post(BASE + '/' + saved['id'] + '/publish', headers=admin, json={'retry_failed': True})
        assert response.status_code == 202
        assert sessions._read(manager.repository, saved['id'])['deployments'] == saved['deployments']


def test_worker_restart_reuses_ids_and_failed_retry_requires_explicit_choice(setup, monkeypatch):
    client, manager, admin, body, saved = setup
    saved = prepare_run(manager, saved)
    seen = []
    def run(repo, mgr, sid):
        seen.append(sessions._read(repo, sid)['deployments'])
    class ImmediateThread:
        def __init__(self, target, **kwargs): self.target = target
        def start(self): self.target()
    monkeypatch.setattr(sessions.threading, 'Thread', ImmediateThread)
    monkeypatch.setattr(sessions, 'run_publication', run)
    assert client.post(BASE + '/' + saved['id'] + '/publish', headers=admin, json={}).status_code == 202
    assert seen[0] == saved['deployments']
    saved['phase'] = 'publish_failed'
    saved['deployments']['mihomo']['status'] = 'failed'
    sessions._write(manager.repository, saved)
    assert client.post(BASE + '/' + saved['id'] + '/publish', headers=admin, json={}).status_code == 409
    assert client.post(BASE + '/' + saved['id'] + '/publish', headers=admin, json={'retry_failed': True}).status_code == 202
    assert seen[-1]['mihomo']['deployment_id'] != 'job-mihomo'
    assert seen[-1]['oxidns'] == saved['deployments']['oxidns']


@pytest.mark.parametrize('failure', ['failed', 'rolled_back', 'rollback_failed'])
def test_oxidns_retry_preserves_successful_mihomo_without_reupload(setup, monkeypatch, failure):
    client, manager, admin, body, saved = setup
    saved = prepare_run(manager, saved)
    # Persist the first core's actual manager record, as a completed first pass
    # would. The retry exercises _publish_service's existing-task path.
    identity = saved['agent_ids']['mihomo']
    manager.begin_deployment(identity, 'job-mihomo')
    manager.record_deployment(identity, 'job-mihomo', {'success': True, 'status': 'succeeded'})
    manager.finish_deployment_preparation(identity, 'job-mihomo')
    saved['phase'] = 'publish_failed'
    saved['deployments']['mihomo'].update(status='succeeded', success=True)
    saved['deployments']['oxidns'].update(status=failure, error='core start failed')
    sessions._write(manager.repository, saved)
    original_publish = sessions._publish_service
    uploads = []
    def publish(repo, mgr, session, service):
        if service == 'mihomo':
            return original_publish(repo, mgr, session, service)
        uploads.append((service, session['deployments'][service]['deployment_id']))
        return {'deployment_id': session['deployments'][service]['deployment_id'], 'status': 'succeeded', 'success': True}
    class ImmediateThread:
        def __init__(self, target, **kwargs): self.target = target
        def start(self): self.target()
    monkeypatch.setattr(sessions.threading, 'Thread', ImmediateThread)
    monkeypatch.setattr(sessions, '_publish_service', publish)
    monkeypatch.setattr(manager, 'push_config_to_agent', lambda *a, **kw: pytest.fail('successful core must not be uploaded again'))
    response = client.post(BASE + '/' + saved['id'] + '/publish', headers=admin, json={'retry_failed': True})
    assert response.status_code == 202, response.get_json()
    result = sessions._read(manager.repository, saved['id'])
    assert result['phase'] == 'published', result['message']
    assert result['deployments']['mihomo']['deployment_id'] == 'job-mihomo'
    assert uploads == [('oxidns', result['deployments']['oxidns']['deployment_id'])]
    assert uploads[0][1] != 'job-oxidns'


@pytest.mark.parametrize('mutation', ['revision', 'binding', 'latest_deployment'])
def test_failed_retry_cannot_reuse_success_after_config_or_target_changes(setup, monkeypatch, mutation):
    client, manager, admin, body, saved = setup
    saved = prepare_run(manager, saved)
    identity = saved['agent_ids']['mihomo']
    manager.begin_deployment(identity, 'job-mihomo')
    manager.record_deployment(identity, 'job-mihomo', {'success': True, 'status': 'succeeded'})
    manager.finish_deployment_preparation(identity, 'job-mihomo')
    saved['phase'] = 'publish_failed'
    saved['deployments']['mihomo']['status'] = 'succeeded'
    saved['deployments']['oxidns']['status'] = 'rolled_back'
    sessions._write(manager.repository, saved)
    if mutation == 'revision':
        manager.repository.save_profile('home', {'name': 'Changed'})
    elif mutation == 'binding':
        manager.update_agent(identity, {'profile_id': 'default'})
    else:
        manager._update_agents(lambda agents: next(a for a in agents if a['id'] == identity).update(
            latest_deployment={'deployment_id': 'other-job', 'status': 'succeeded'}))
    monkeypatch.setattr(sessions.threading, 'Thread', lambda *a, **kw: pytest.fail('changed install must not resume'))
    response = client.post(BASE + '/' + saved['id'] + '/publish', headers=admin, json={'retry_failed': True})
    assert response.status_code == 409
    assert sessions._read(manager.repository, saved['id']) == saved


def test_real_bundle_preparation_and_manager_delivery_use_exact_pair_revision_and_sequence(setup, monkeypatch):
    """Only the Agent transport is mocked; both converters and bundles are real."""
    import io
    import tarfile
    import yaml
    client, manager, admin, body, saved = setup
    saved = prepare_run(manager, saved)
    deliveries = []
    def request(agent, method, path, **kwargs):
        if path == 'capabilities':
            return {'deployment_protocols': [1], 'service_type': agent['service_type'], 'config_path': 'config.yaml'}
        assert method == 'post' and path == 'deployments'
        assert kwargs['headers']['X-Activate'] == 'true'
        with tarfile.open(fileobj=io.BytesIO(kwargs['data']), mode='r:gz') as archive:
            manifest = json.load(archive.extractfile('manifest.json'))
            main = yaml.safe_load(archive.extractfile('files/config.yaml').read())
        deliveries.append((agent['service_type'], manifest, main))
        assert manifest['profile_id'] == 'home'
        assert manifest['deployment_id'] == saved['deployments'][agent['service_type']]['deployment_id']
        return {'status': 'succeeded', 'success': True, 'deployment_id': manifest['deployment_id']}
    monkeypatch.setattr(manager, '_deployment_request', request)
    sessions.run_publication(manager.repository, manager, saved['id'], pause=lambda _: None)
    result = sessions._read(manager.repository, saved['id'])
    assert result['phase'] == 'published', result['message']
    assert [entry[0] for entry in deliveries] == ['mihomo', 'oxidns']
    mihomo = deliveries[0][2]
    assert mihomo['tun']['enable'] and mihomo['tun']['route-address-set']
    assert 'cf_gateway_fake_ip' in mihomo['rule-providers']
    oxidns = deliveries[1][2]
    plugins = {item['tag']: item for item in oxidns['plugins']}
    assert plugins['forward_remote']['args']['upstreams'][0]['addr'] == '127.0.0.1:1053'
    assert plugins['server_udp']['args']['listen'] == '0.0.0.0:53'
    public = sessions.get(manager.repository, manager, saved['id'])
    assert not public['target_changed'] and not public['config_changed']
    for service, identity in saved['agent_ids'].items():
        agent = manager.get_agent_by_id(identity)
        assert agent['config_revision'] == saved['config_revision']
        assert agent['applied_content']['deployment_id'] == saved['deployments'][service]['deployment_id']
    manager._update_agents(lambda agents: next(a for a in agents if a['id'] == saved['agent_ids']['oxidns']).update(
        latest_deployment={'deployment_id': 'different', 'status': 'succeeded'}))
    assert sessions.get(manager.repository, manager, saved['id'])['target_changed']


def test_install_address_must_match_persisted_rule_callback_origin(setup):
    client, manager, admin, body, saved = setup
    before = manager.repository.export_all()
    response = client.post(BASE, headers=admin, json={**body, 'server_url': 'https://different.example'})
    assert response.status_code == 409
    result = response.get_json()
    assert result['code'] == 'server_url_mismatch'
    assert 'https://config.example' in result['message'] and 'https://different.example' in result['message']
    assert manager.repository.export_all() == before
    response = client.post(BASE, headers=admin, json={**body, 'server_url': 'https://different.example',
                                                       'use_as_server_domain': 'yes'})
    assert response.status_code == 400
    assert manager.repository.export_all() == before


def test_confirmed_install_address_becomes_the_system_server_address(setup):
    client, manager, admin, body, saved = setup
    response = client.post(BASE, headers=admin, json={**body, 'server_url': 'https://different.example',
                                                       'use_as_server_domain': True})
    assert response.status_code == 200, response.get_json()
    assert manager.repository.get_system()['system_config']['server_domain'] == 'https://different.example'
    assert sessions._read(manager.repository, response.get_json()['id'])['server_url'] == 'https://different.example'


def test_enrolled_installation_can_resume_with_admin_after_capability_expiry(setup, monkeypatch):
    client, manager, admin, body, saved = setup
    saved = prepare_run(manager, saved)
    saved['expires'] = 1
    sessions._write(manager.repository, saved)
    seen = []
    class ImmediateThread:
        def __init__(self, target, **kwargs): self.target = target
        def start(self): self.target()
    monkeypatch.setattr(sessions.threading, 'Thread', ImmediateThread)
    monkeypatch.setattr(sessions, 'run_publication', lambda repo, mgr, sid: seen.append(sid))
    response = client.post(BASE + '/' + saved['id'] + '/publish', headers=admin, json={})
    assert response.status_code == 202 and seen == [saved['id']]
    assert response.get_json()['expired'] and 'command' not in response.get_json()


def test_capability_never_recovers_independently_installed_agent_token(setup):
    client, manager, admin, body, saved = setup
    pair = register_pair(manager, saved)
    agent = manager.get_agent_by_id(pair['mihomo']['id'])
    request_body = {**agent, 'installation_id': saved['id'], 'installation_token': saved['token'],
                    'gateway_installation_id': saved['id']}  # An untrusted JSON marker is ignored.
    before = manager.repository.export_all()
    response = client.post('/api/agents/register', json=request_body)
    assert response.status_code == 401
    assert manager.repository.export_all() == before
    response = client.post('/api/agents/register', json=request_body,
                           headers={'Authorization': 'Bearer ' + pair['mihomo']['token']})
    assert response.status_code == 200 and 'token' not in response.get_json()
    assert 'gateway_installation_id' not in manager.get_agent_by_id(agent['id'])


def test_registration_recovery_survives_missing_session_receipt_and_rejects_foreign_capability(setup):
    client, manager, admin, body, saved = setup
    data = {'name': saved['name'] + '-mihomo', 'host': SPEC['address'], 'port': 8080,
            'service_type': 'mihomo', 'deployment_method': 'shell', 'profile_id': 'home'}
    # Reproduce a crash after the repository's atomic creation but before the
    # installer response/session receipt; the trusted owner marker survives.
    identity = manager.register_agent(data, installation_id=saved['id'])
    assert sessions._read(manager.repository, saved['id'])['registered'] == {}
    response = client.post('/api/agents/register', json={**data, 'installation_id': saved['id'],
                                                        'installation_token': saved['token']})
    assert response.status_code == 200 and response.get_json()['token'] == identity['token']
    saved['expires'] = 1
    sessions._write(manager.repository, saved)
    newer = client.post(BASE, headers=admin, json=body).get_json()
    next_session = sessions._read(manager.repository, newer['id'])
    assert client.post('/api/agents/register', json={**data, 'installation_id': newer['id'],
        'installation_token': next_session['token']}).status_code == 401


def test_enrollment_reuses_proven_existing_agents_and_updates_installer_metadata_atomically(setup):
    client, manager, admin, body, saved = setup
    pair = {service: manager.register_agent({'name': 'old-' + service, 'host': '192.168.1.8',
        'port': 8500 + index, 'service_type': service, 'deployment_method': 'shell',
        'profile_id': 'default', 'version': '1.8.25-go'}) for index, service in enumerate(sessions.SERVICES)}
    ids_before = {agent['id'] for agent in manager._agents()}
    assert bind(manager, saved, pair)['success']
    assert {agent['id'] for agent in manager._agents()} == ids_before
    for service, proof in pair.items():
        actual = manager.get_agent_by_id(proof['id'])
        assert actual['name'] == saved['name'] + '-' + service
        assert actual['host'] == saved['network']['address'] and actual['port'] == saved['ports'][service]
        assert actual['profile_id'] == 'home' and actual['token'] == proof['token']
    manager.update_agent(pair['oxidns']['id'], {'port': 9999})
    with pytest.raises(sessions.InstallationError, match='信息发生变化'):
        bind(manager, saved, pair)


def test_existing_agent_metadata_rebind_cannot_override_same_name_target(setup):
    client, manager, admin, body, saved = setup
    pair = register_pair(manager, saved)
    old = manager.register_agent({'name': 'old-mihomo', 'host': '192.168.1.8', 'port': 8080,
        'profile_id': 'home', 'service_type': 'mihomo', 'deployment_method': 'shell'})
    before = manager.repository.export_all()
    with pytest.raises(sessions.InstallationError, match='另一台'):
        bind(manager, saved, {**pair, 'mihomo': old})
    assert manager.repository.export_all() == before


def test_installation_checks_mihomo_dns_against_entered_linux_lan_address(setup):
    client, manager, admin, body, saved = setup
    config = manager.repository.get_compat_config('home')
    import yaml
    document = yaml.safe_load(config['mihomo']['custom_config'])
    document['dns']['nameserver'] = [SPEC['address']]
    config['mihomo']['custom_config'] = yaml.safe_dump(document)
    manager.repository.save_profile('home', {'mihomo': config['mihomo']})
    response = client.post(BASE, headers=admin, json={**body, 'config_revision': manager.repository.config_revision('home')})
    assert response.status_code == 400
    assert '循环' in response.get_json()['message']


class ImmediateThread:
    def __init__(self, target, **_):
        self.target = target

    def start(self):
        self.target()


def test_repreparing_changed_config_reuses_enrolled_devices_without_reinstall(setup, monkeypatch):
    client, manager, admin, body, saved = setup
    saved = prepare_run(manager, saved)
    saved['phase'] = 'publish_failed'
    saved['deployments']['mihomo']['status'] = 'succeeded'
    saved['deployments']['oxidns']['status'] = 'rolled_back'
    sessions._write(manager.repository, saved)
    command = client.get(BASE + '/' + saved['id'], headers=admin).json['command']
    manager.repository.save_profile('home', {'name': 'Changed'})
    revision = manager.repository.config_revision('home')
    assert revision != saved['config_revision']

    response = client.post(BASE, headers=admin, json={**body, 'config_revision': revision})
    assert response.status_code == 200, response.json
    reused = response.json
    assert reused['id'] == saved['id'] and reused['command'] == command
    assert reused['phase'] == 'enrolled' and reused['deployments'] == {} and not reused['config_changed']
    stored = sessions._read(manager.repository, saved['id'])
    assert stored['agent_ids'] == saved['agent_ids'] and stored['token'] == saved['token']
    assert stored['config_revision'] == revision

    # Both cores publish the newly reviewed revision, Mihomo included.
    published = []
    def publish(repo, mgr, session, service):
        published.append((service, session['config_revision']))
        return {'deployment_id': session['deployments'][service]['deployment_id'], 'status': 'succeeded', 'success': True}
    monkeypatch.setattr(sessions, '_publish_service', publish)
    monkeypatch.setattr(sessions.threading, 'Thread', ImmediateThread)
    assert client.post(BASE + '/' + saved['id'] + '/publish', headers=admin, json={'retry_failed': False}).status_code == 202
    assert published == [('mihomo', revision), ('oxidns', revision)]
    assert sessions._read(manager.repository, saved['id'])['phase'] == 'published'


def test_repreparing_before_installation_keeps_the_same_command(setup):
    client, manager, admin, body, saved = setup
    command = client.get(BASE + '/' + saved['id'], headers=admin).json['command']
    manager.repository.save_profile('home', {'name': 'Changed'})
    response = client.post(BASE, headers=admin, json={**body, 'config_revision': manager.repository.config_revision('home')})
    assert response.status_code == 200, response.json
    assert response.json['id'] == saved['id'] and response.json['phase'] == 'waiting'
    assert response.json['command'] == command


def test_repreparing_is_refused_while_publishing(setup):
    client, manager, admin, body, saved = setup
    saved = prepare_run(manager, saved)
    manager.repository.save_profile('home', {'name': 'Changed'})
    response = client.post(BASE, headers=admin, json={**body, 'config_revision': manager.repository.config_revision('home')})
    assert response.status_code == 409 and '发布进行中' in response.json['message']
    assert sessions._read(manager.repository, saved['id']) == saved


def test_repreparing_a_different_target_still_needs_a_new_installation(setup):
    client, manager, admin, body, saved = setup
    manager.repository.save_profile('home', {'name': 'Changed'})
    response = client.post(BASE, headers=admin, json={**body, 'name': 'other-linux',
                                                      'config_revision': manager.repository.config_revision('home')})
    assert response.status_code == 200, response.json
    assert response.json['id'] != saved['id'] and response.json['phase'] == 'waiting'
