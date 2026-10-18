"""Diagnostics are consistent across immediate errors and later task states."""
import json

import pytest
import requests
from flask import Flask

from backend.routes import register_blueprints
from backend.test_agent_deployments import env, response


def _record_task(manager, agent, status, deployment_id='diagnostic-state'):
    manager.begin_deployment(agent['id'], deployment_id)
    manager.record_deployment(agent['id'], deployment_id, {'status': status, 'success': True, 'submitted': True})
    manager.finish_deployment_preparation(agent['id'], deployment_id)
    return deployment_id


@pytest.mark.parametrize('operation,status', [('activate', 'ready'), ('rollback', 'succeeded')])
def test_immediate_rejections_are_redacted_like_recorded_history(env, monkeypatch, operation, status):
    _, manager, agent, _ = env
    deployment_id = _record_task(manager, agent, status)
    monkeypatch.setattr('backend.routes.agents.get_agent_manager', lambda: manager)
    monkeypatch.setattr('backend.agents.manager.requests.get', lambda *_a, **_k: response({'success': True, 'status': 'ready'}))
    raw = {'success': False, 'message': 'Candidate rejected',
           'error_details': f'line 7: invalid listener\nAuthorization: Bearer {agent["token"]}\npassword=private-value'}
    monkeypatch.setattr('backend.agents.manager.requests.post', lambda *_a, **_k: response(raw, 409))
    app = Flask(__name__)
    register_blueprints(app)
    client = app.test_client()
    rejected = client.post(f'/api/agents/{agent["id"]}/deployments/{deployment_id}/{operation}')
    assert rejected.status_code == 409
    body = rejected.json
    assert 'line 7: invalid listener' in body['error_details']
    assert 'private-value' not in json.dumps(body)
    assert agent['token'] not in json.dumps(body)
    saved = manager.get_agent_by_id(agent['id'])['deployments'][deployment_id]
    assert saved['status'] == status
    if operation == 'rollback':
        assert saved['rollback_rejected'] is True
        assert saved['rollback_error_details'] == body['error_details']
        assert not saved.get('manual_rollback')


@pytest.mark.parametrize('method', ['push', 'restart'])
@pytest.mark.parametrize('payload', [None, [], 'unexpected text', 17])
def test_legacy_success_http_requires_a_json_object(env, monkeypatch, method, payload):
    repository, manager, agent, _ = env
    repository.update_system_transaction(lambda system: system['agents'][0].update(service_type='surge'))
    before = manager.get_agent_by_id(agent['id'])
    monkeypatch.setattr('backend.agents.manager.requests.post', lambda *_a, **_k: response(payload))
    result = (manager.push_config_to_agent(agent['id'], '[General]') if method == 'push'
              else manager.restart_agent_service(agent['id']))
    assert result == {'success': False, 'message': 'Invalid Agent response'}
    saved = manager.get_agent_by_id(agent['id'])
    assert saved.get('config_version') == before.get('config_version')
    assert saved.get('pushed_at') == before.get('pushed_at')


@pytest.mark.parametrize('method', ['push', 'restart'])
def test_legacy_invalid_json_keeps_safe_plain_text_explanation(env, monkeypatch, method):
    repository, manager, agent, _ = env
    repository.update_system_transaction(lambda system: system['agents'][0].update(service_type='surge'))
    value = requests.Response()
    value.status_code = 200
    value._content = b'upstream service unavailable\npassword=do-not-display'
    value.headers['Content-Type'] = 'text/plain'
    monkeypatch.setattr('backend.agents.manager.requests.post', lambda *_a, **_k: value)
    result = (manager.push_config_to_agent(agent['id'], '[General]') if method == 'push'
              else manager.restart_agent_service(agent['id']))
    assert result['success'] is False
    assert 'upstream service unavailable' in result['error_details']
    assert 'do-not-display' not in result['error_details']


@pytest.mark.parametrize('new_status', ['verifying', 'checking', 'failed', 'rolling_back'])
def test_confirmed_agent_stage_discards_old_unknown_transport_details(env, new_status):
    _, manager, agent, _ = env
    deployment_id = _record_task(manager, agent, 'unknown')
    manager.record_deployment(agent['id'], deployment_id, {
        'status': 'unknown', 'success': True, 'error_details': 'previous connection refused',
        'rollback_error_details': 'previous connection reset'})
    update = {'status': new_status, 'success': new_status != 'failed'}
    if new_status == 'failed':
        update['error'] = 'configuration rejected by older Agent'
    confirmed = manager.record_deployment(agent['id'], deployment_id, update)
    assert confirmed['status'] == new_status
    assert 'error_details' not in confirmed
    assert 'rollback_error_details' not in confirmed
    if new_status == 'failed':
        assert confirmed['error'] == update['error']


def test_confirmed_failure_replaces_transport_details_and_retains_on_same_error_poll(env):
    _, manager, agent, _ = env
    deployment_id = _record_task(manager, agent, 'unknown')
    manager.record_deployment(agent['id'], deployment_id, {
        'status': 'unknown', 'error_details': 'previous connection refused'})
    details = 'config.yaml:18: duplicate plugin'
    failed = manager.record_deployment(agent['id'], deployment_id, {
        'status': 'failed', 'success': False, 'error': 'Configuration rejected', 'error_details': details})
    assert failed['error_details'] == details
    same = manager.record_deployment(agent['id'], deployment_id, {
        'status': 'failed', 'success': False, 'error': 'Configuration rejected'})
    assert same['error_details'] == details


def test_rollback_retry_clears_old_rejection_details_before_request(env, monkeypatch):
    _, manager, agent, _ = env
    deployment_id = _record_task(manager, agent, 'succeeded')
    calls = []
    def reject(*_args, **_kwargs):
        calls.append(True)
        pending = manager.get_agent_by_id(agent['id'])['deployments'][deployment_id]
        assert pending['status'] == 'rolling_back'
        assert pending['manual_rollback'] is True
        assert not pending.get('rollback_error_details')
        assert not pending.get('rollback_message')
        result = {'success': False, 'message': 'First rejection' if len(calls) == 1 else 'Second rejection'}
        if len(calls) == 1:
            result['error_details'] = 'first checksum mismatch'
        return response(result, 409)
    monkeypatch.setattr('backend.agents.manager.requests.post', reject)
    manager.rollback_deployment(agent['id'], deployment_id)
    first = manager.get_agent_by_id(agent['id'])['deployments'][deployment_id]
    assert first['rollback_error_details'] == 'first checksum mismatch'
    manager.rollback_deployment(agent['id'], deployment_id)
    second = manager.get_agent_by_id(agent['id'])['deployments'][deployment_id]
    assert second['status'] == 'succeeded'
    assert second['rollback_message'] == 'Second rejection'
    assert 'rollback_error_details' not in second
    assert len(calls) == 2


def test_deployment_busy_retry_is_unchanged_by_diagnostic_sanitization(env, monkeypatch):
    _, manager, agent, _ = env
    values = iter([response({'success': False, 'message': 'another service operation is in progress'}, 409),
                   response({'success': True, 'status': 'rolling_back', 'manual_rollback': True}, 202)])
    calls = []
    def request(*_args, **_kwargs):
        calls.append(True)
        return next(values)
    monkeypatch.setattr('backend.agents.manager.requests.post', request)
    monkeypatch.setattr('backend.agents.manager.time.sleep', lambda _delay: None)
    result = manager._deployment_request(agent, 'post', 'deployments/id/rollback')
    assert result == {'success': True, 'status': 'rolling_back', 'manual_rollback': True}
    assert len(calls) == 2
