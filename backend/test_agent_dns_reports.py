"""Candidate DNS diagnostics survive publication without changing task semantics."""
import json

import pytest
from flask import Flask

from backend.agents.diagnostics import (
    DNS_CHECK_LIMIT, DNS_FIX_LIMIT, DNS_MESSAGE_LIMIT, DNS_NAME_LIMIT,
)
from backend.agents.manager import AgentManager
from backend.routes import register_blueprints
from backend.test_agent_deployments import env, minimal_bundle, response


CHECKED_AT = '2026-10-18T10:00:00.123456789Z'
WARNING = {'name': 'dns_hijack', 'level': 'warn',
           'message': 'DNS 劫持可能绕过 OxiDNS。', 'fix': '检查 TUN 中的 DNS 劫持范围。'}
ADJUSTMENT = {'name': 'All in One DNS 自动调整', 'level': 'warn',
              'message': "tun.dns-hijack 已自动调整：['any:53', 'tcp://any:53'] → ['any:1053', 'tcp://any:1053']。"}
CONFLICT = {'name': 'dns_listener', 'level': 'error',
            'message': 'UDP 53 已被其他程序占用。', 'fix': '调整 DNS 监听端口。'}


def public_client(manager, monkeypatch):
    monkeypatch.setattr('backend.routes.agents.get_agent_manager', lambda: manager)
    app = Flask(__name__)
    register_blueprints(app)
    return app.test_client()


@pytest.mark.parametrize('status,http_status,check', [('ready', 202, WARNING), ('failed', 400, CONFLICT)])
def test_upload_report_reaches_panel_and_persisted_history(env, monkeypatch, status, http_status, check):
    repository, manager, agent, config = env
    bundle = minimal_bundle(agent, config, 'dns-upload')
    monkeypatch.setattr('backend.routes.agents._prepare_transactional_content', lambda *_a, **_k: {
        'bundle': bundle, 'prepared_providers': [], 'config_content': 'mode: rule',
        'provider_downloads': [], 'ruleset_downloads': [], 'custom_files': [],
    })
    monkeypatch.setattr('backend.agents.manager.requests.get', lambda *_a, **_k: response({
        'success': True, 'deployment_protocols': [1], 'service_type': 'mihomo'}))
    monkeypatch.setattr('backend.agents.manager.requests.post', lambda *_a, **_k: response({
        'success': status == 'ready', 'status': status, 'dns_checks': [check],
        'dns_checked_at': CHECKED_AT,
    }, http_status))
    result = public_client(manager, monkeypatch).post(
        f'/api/agents/{agent["id"]}/push-config',
        json={'deployment_id': 'dns-upload', 'restart': False})
    assert result.status_code == http_status
    assert result.json['dns_checks'] == [check]
    assert result.json['dns_checked_at'] == CHECKED_AT
    assert result.json['status'] == status
    assert result.json['success'] is (status == 'ready')
    restarted = AgentManager(repository)
    client = public_client(restarted, monkeypatch)
    listed = client.get('/api/agents').json[0]
    assert listed['latest_deployment']['dns_checks'] == [check]
    assert listed['deployments']['dns-upload']['dns_checked_at'] == CHECKED_AT
    assert listed['config_version'] == '0'
    if status == 'failed':
        failed = client.get(f'/api/agents/{agent["id"]}/deployments/dns-upload')
        assert failed.status_code == 200
        assert failed.json['dns_checks'] == [check]


@pytest.mark.parametrize('check', [WARNING, ADJUSTMENT])
def test_warning_report_survives_ready_poll_activation_and_success(env, monkeypatch, check):
    repository, manager, agent, _ = env
    manager.begin_deployment(agent['id'], 'dns-warning')
    manager.record_deployment(agent['id'], 'dns-warning', {
        'success': True, 'status': 'ready', 'submitted': True,
        'dns_checks': [check], 'dns_checked_at': CHECKED_AT,
    })
    manager.finish_deployment_preparation(agent['id'], 'dns-warning')
    monkeypatch.setattr('backend.agents.manager.requests.get', lambda *_a, **_k: response({
        'success': True, 'status': 'ready'}))
    monkeypatch.setattr('backend.agents.manager.requests.post', lambda *_a, **_k: response({
        'success': True, 'status': 'succeeded', 'config_version': 'dns-approved'}))
    client = public_client(manager, monkeypatch)
    polled = client.get(f'/api/agents/{agent["id"]}/deployments/dns-warning')
    assert polled.json['dns_checks'] == [check]
    activated = client.post(f'/api/agents/{agent["id"]}/deployments/dns-warning/activate')
    assert activated.status_code == 200
    assert activated.json['success'] is True
    assert activated.json['status'] == 'succeeded'
    assert activated.json['dns_checks'] == [check]
    assert activated.json['dns_checked_at'] == CHECKED_AT
    restarted = AgentManager(repository)
    final = restarted.get_deployment(agent['id'], 'dns-warning')
    assert final['dns_checks'] == [check]
    assert restarted.get_agent_by_id(agent['id'])['config_version'] == 'dns-approved'
    manager.begin_deployment(agent['id'], 'next-candidate')
    latest = manager.get_agent_by_id(agent['id'])['latest_deployment']
    assert 'dns_checks' not in latest and 'dns_checked_at' not in latest
    manager.finish_deployment_preparation(agent['id'], 'next-candidate')


def test_poll_saves_dns_failure_and_keeps_previous_version(env, monkeypatch):
    repository, manager, agent, _ = env
    manager.begin_deployment(agent['id'], 'dns-poll')
    manager.record_deployment(agent['id'], 'dns-poll', {
        'success': True, 'status': 'verifying', 'submitted': True})
    manager.finish_deployment_preparation(agent['id'], 'dns-poll')
    monkeypatch.setattr('backend.agents.manager.requests.get', lambda *_a, **_k: response({
        'success': False, 'status': 'failed', 'failed_stage': 'verifying',
        'dns_checks': [CONFLICT], 'dns_checked_at': CHECKED_AT}))
    result = public_client(manager, monkeypatch).get(
        f'/api/agents/{agent["id"]}/deployments/dns-poll')
    assert result.status_code == 200
    assert result.json['status'] == 'failed'
    assert result.json['dns_checks'] == [CONFLICT]
    saved = AgentManager(repository).get_agent_by_id(agent['id'])
    assert saved['latest_deployment']['dns_checks'] == [CONFLICT]
    assert saved['config_version'] == '0'


def test_older_agent_without_dns_report_remains_unknown(env, monkeypatch):
    _, manager, agent, config = env
    monkeypatch.setattr('backend.agents.manager.requests.get', lambda *_a, **_k: response({
        'success': True, 'deployment_protocols': [1], 'service_type': 'mihomo'}))
    monkeypatch.setattr('backend.agents.manager.requests.post', lambda *_a, **_k: response({
        'success': True, 'status': 'succeeded', 'config_version': 'legacy'}))
    result = manager.publish_deployment(agent['id'], minimal_bundle(agent, config, 'legacy'))
    assert result['status'] == 'succeeded'
    assert 'dns_checks' not in result and 'dns_checked_at' not in result
    listed = public_client(manager, monkeypatch).get('/api/agents').json[0]
    assert 'dns_checks' not in listed['latest_deployment']


@pytest.mark.parametrize('boundary', ['record', 'transport'])
def test_dns_report_is_bounded_filtered_and_redacted_at_each_boundary(env, monkeypatch, boundary):
    repository, manager, agent, _ = env
    secret = agent['token']
    raw = {
        'status': 'ready', 'success': True,
        'dns_checked_at': 'not-a-timestamp ' + secret,
        'dns_checks': [
            {'name': 'dns_' + secret + ' 名称' * 100, 'level': 'warn',
             'message': 'https://name:password@dns.test/query?token=private-token\n' + secret + '内容' * 2000,
             'fix': 'password="private-fix"\n' + '修复' * 1000,
             'unexpected': secret},
            {'name': 'invalid-level', 'level': 'unknown', 'message': secret},
            'invalid-item',
        ] + [WARNING] * DNS_CHECK_LIMIT,
    }
    if boundary == 'record':
        manager.begin_deployment(agent['id'], 'safe-report')
        result = manager.record_deployment(agent['id'], 'safe-report', raw)
        manager.finish_deployment_preparation(agent['id'], 'safe-report')
        saved = repository.get_system()['agents'][0]['latest_deployment']
        assert saved['dns_checks'] == result['dns_checks']
    else:
        monkeypatch.setattr('backend.agents.manager.requests.post', lambda *_a, **_k: response(raw, 409))
        result = manager._deployment_request(agent, 'post', 'deployments/safe-report/activate')
        assert result['http_status'] == 409
    checks = result['dns_checks']
    assert len(checks) <= DNS_CHECK_LIMIT
    assert checks[-1]['name'] == 'DNS 报告完整性'
    assert checks[-1]['level'] == 'warn'
    assert all(set(check) <= {'name', 'level', 'message', 'fix'} for check in checks)
    assert len(checks[0]['name'].encode()) <= DNS_NAME_LIMIT
    assert len(checks[0]['message'].encode()) <= DNS_MESSAGE_LIMIT
    assert len(checks[0]['fix'].encode()) <= DNS_FIX_LIMIT
    assert 'dns_checked_at' not in result
    serialized = json.dumps(result)
    for private in (secret, 'name:password', 'private-token', 'private-fix'):
        assert private not in serialized


def test_malformed_or_absent_report_does_not_erase_previous_result(env):
    _, manager, agent, _ = env
    manager.begin_deployment(agent['id'], 'keep-report')
    manager.record_deployment(agent['id'], 'keep-report', {
        'status': 'ready', 'dns_checks': [WARNING], 'dns_checked_at': CHECKED_AT})
    result = manager.record_deployment(agent['id'], 'keep-report', {
        'status': 'succeeded', 'success': True,
        'dns_checks': 'invalid', 'dns_checked_at': 'invalid'})
    manager.finish_deployment_preparation(agent['id'], 'keep-report')
    assert result['dns_checks'] == [WARNING]
    assert result['dns_checked_at'] == CHECKED_AT
