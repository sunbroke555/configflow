"""Failure diagnostics survive Agent transport, persistence and public APIs."""
import json

import pytest
import requests
from flask import Flask

from backend.agents.diagnostics import DETAIL_LIMIT, failure_text, response_failure
from backend.agents.manager import AgentManager
from backend.routes import register_blueprints
from backend.test_agent_deployments import env, minimal_bundle, response


@pytest.mark.parametrize('service', ['mihomo', 'mosdns', 'oxidns'])
def test_failure_details_survive_restart_history_and_public_api(env, monkeypatch, service):
    repository, manager, agent, _ = env
    repository.update_system_transaction(lambda system: system['agents'][0].update(service_type=service))
    manager.begin_deployment(agent['id'], 'failed-check')
    details = 'config.yaml:17: plugin "forward_remote" references missing upstream\nexit status 1'
    manager.record_deployment(agent['id'], 'failed-check', {
        'success': False, 'status': 'failed', 'failed_stage': 'verifying',
        'error': 'Configuration check failed', 'error_details': details})
    manager.finish_deployment_preparation(agent['id'], 'failed-check')
    restarted = AgentManager(repository)
    monkeypatch.setattr('backend.routes.agents.get_agent_manager', lambda: restarted)
    app = Flask(__name__); register_blueprints(app)
    result = app.test_client().get(f'/api/agents/{agent["id"]}/deployments/failed-check')
    assert result.status_code == 200
    assert result.json['error_details'] == details
    assert result.json['failed_stage'] == 'verifying'
    listed = app.test_client().get('/api/agents').json[0]
    assert listed['latest_deployment']['error_details'] == details
    assert listed['deployments']['failed-check']['error_details'] == details


def test_failure_and_rollback_details_are_independent_and_recovery_clears_only_rollback(env):
    repository, manager, agent, _ = env
    manager.begin_deployment(agent['id'], 'rollback')
    failed = manager.record_deployment(agent['id'], 'rollback', {
        'status': 'rollback_failed', 'error': 'start failed', 'error_details': 'new service: address already in use',
        'rollback_error': 'old service failed', 'rollback_error_details': 'old service: permission denied'})
    assert failed['error_details'] != failed['rollback_error_details']
    recovered = manager.record_deployment(agent['id'], 'rollback', {'status': 'rolled_back', 'success': False})
    assert recovered['error_details'] == 'new service: address already in use'
    assert not recovered.get('rollback_error_details')
    assert not recovered.get('rollback_error')
    manager.finish_deployment_preparation(agent['id'], 'rollback')


def test_details_are_bounded_redacted_before_persistence_and_success_clears_them(env):
    repository, manager, agent, _ = env
    manager.begin_deployment(agent['id'], 'transport')
    raw = f'connection refused\nAuthorization: Bearer {agent["token"]}\npassword: "sensitive password"\n' + '内容' * 20000
    state = manager.record_deployment(agent['id'], 'transport', {
        'status': 'unknown', 'error_details': raw})
    detail = state['error_details']
    assert 'connection refused' in detail and '已截断' in detail
    assert 'sensitive password' not in detail and agent['token'] not in detail
    assert len(detail.encode()) <= DETAIL_LIMIT
    disk = json.dumps(repository.get_system()['agents'][0]['deployments'])
    assert 'sensitive password' not in disk and agent['token'] not in disk
    state = manager.record_deployment(agent['id'], 'transport', {'success': True, 'status': 'succeeded'})
    assert 'error_details' not in state
    manager.finish_deployment_preparation(agent['id'], 'transport')


@pytest.mark.parametrize('method', ['push', 'restart'])
@pytest.mark.parametrize('status', [200, 400, 500])
def test_legacy_errors_keep_agent_reason_on_all_http_statuses(env, monkeypatch, method, status):
    repository, manager, agent, _ = env
    repository.update_system_transaction(lambda system: system['agents'][0].update(service_type='surge'))
    monkeypatch.setattr('backend.agents.manager.requests.post', lambda *_a, **_k: response({
        'success': False, 'message': 'Invalid configuration',
        'error_details': 'line 7: proxy group does not exist\npassword=do-not-echo'}, status))
    result = manager.push_config_to_agent(agent['id'], '[General]') if method == 'push' else manager.restart_agent_service(agent['id'])
    assert result['success'] is False
    assert result['message'] == 'Invalid configuration'
    assert 'line 7' in result['error_details'] and 'do-not-echo' not in result['error_details']


def test_plain_text_legacy_error_is_retained_but_html_is_not():
    value = requests.Response(); value.status_code = 502; value._content = b'connection refused on port 9090'
    value.headers['Content-Type'] = 'text/plain; charset=utf-8'
    assert response_failure(value, fallback='HTTP 502')['error_details'] == value.text
    value.headers['Content-Type'] = 'text/html'
    assert 'error_details' not in response_failure(value, fallback='HTTP 502')


def test_redaction_keeps_rule_names_and_file_locations():
    text = failure_text('\x1b[31mconfig.yaml:42: missing rule MyProxy\x1b[0m\n'
                        'fetch https://name:pass@rules.test/list?token=hidden&format=yaml\n'
                        '{"password": "a secret", "api_key": "hide-this"}\nAuthorization: Bearer xyz')
    assert 'config.yaml:42: missing rule MyProxy' in text
    assert 'rules.test/list' in text and 'format=yaml' in text
    for secret in ('name:pass', 'hidden', 'a secret', 'hide-this', 'xyz', '\x1b'):
        assert secret not in text


@pytest.mark.parametrize('credential', [
    'password: |\n  private-first\n  private-second\n',
    'private-key: >-\n  private-first\n\n  private-second\n',
    '  - password: |\n      private-first\n      private-second\n',
    r'password: "first \"private-first\" private-second"' + '\n',
    "password: 'first ''private-first'' private-second'\n",
    'password: "private-first\n  private-second"\n',
    '-----BEGIN PRIVATE KEY-----\nprivate-first\nprivate-second\n-----END PRIVATE KEY-----\n',
])
def test_multiline_and_escaped_credentials_are_hidden(credential):
    detail = failure_text('config.yaml:42: invalid credential\n' + credential + 'next failure: upstream unavailable')
    assert 'private-first' not in detail and 'private-second' not in detail
    assert 'config.yaml:42' in detail and 'next failure: upstream unavailable' in detail


def test_yaml_secret_block_does_not_hide_sibling_diagnostics():
    detail = failure_text('  password: |\n    private-first\n  error: bad proxy name\n')
    assert 'private-first' not in detail
    assert 'error: bad proxy name' in detail


def test_capability_connection_failure_retains_reason_and_does_not_upload(env, monkeypatch):
    _, manager, agent, config = env
    def fail(*_a, **_k):
        raise requests.ConnectionError('connection refused to http://user:secret@agent.test:8080/api/capabilities')
    monkeypatch.setattr(manager, '_deployment_request', fail)
    result = manager.publish_deployment(agent['id'], minimal_bundle(agent, config, 'capability'))
    assert result['status'] == 'failed'
    assert 'connection refused' in result['error_details']
    assert 'user:secret' not in result['error_details']
    assert not result.get('submitted')


def test_preparation_exception_keeps_safe_details_in_http_response_and_history(env, monkeypatch):
    _, manager, agent, _ = env
    monkeypatch.setattr('backend.routes.agents.get_agent_manager', lambda: manager)
    def fail(*_a, **_k):
        raise RuntimeError('rules/example.list: permission denied\nsecret="hidden value"')
    monkeypatch.setattr('backend.routes.agents._prepare_transactional_content', fail)
    app = Flask(__name__); register_blueprints(app)
    client = app.test_client()
    result = client.post(f'/api/agents/{agent["id"]}/push-config', json={'deployment_id': 'prepare-error'})
    assert result.status_code == 500
    assert 'permission denied' in result.json['error_details']
    assert 'hidden value' not in result.json['error_details']
    assert result.json['failed_stage'] == 'preparing'
    assert client.get(f'/api/agents/{agent["id"]}/deployments/prepare-error').json['error_details'] == result.json['error_details']
