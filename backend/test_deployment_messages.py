"""Chinese publication explanations must not replace source diagnostics."""
from copy import deepcopy

import pytest

from backend.agents.deployment_messages import deployment_message
from backend.routes.agents import _public_deployment
from backend.test_agent_upgrade_auth import authenticated_app  # noqa: F401


@pytest.mark.parametrize('raw, explanation', [
    ('Deployment preparation failed', '准备发布文件失败'),
    ('Upload outcome unknown; query deployment status', '暂时无法确认上传结果'),
    ('Rollback outcome unknown; awaiting Agent status', '暂时无法确认回滚结果'),
    ('Only a successful deployment can be rolled back', '发布成功'),
    ('service start failed', '服务启动失败'),
    ('backup checksum mismatch: rules/Claude.mrs', '旧配置备份校验失败'),
    ('rollback cannot stop service: rc-service exited with code 1', '回滚时无法停止当前服务'),
    ('Agent returned HTTP 503', 'HTTP 状态码'),
    ('Rule Claude: Invalid rule YAML payload', '规则集准备失败'),
])
def test_known_failures_keep_the_exact_original(raw, explanation):
    display = deployment_message(raw)
    assert explanation in display
    assert display.split('\n原始错误：', 1)[1] == raw
    assert deployment_message(display) == display


@pytest.mark.parametrize('raw', [
    'yaml: unmarshal errors:\n  line 6: cannot unmarshal !!str into config.RawConfig',
    'upstream https://dns.example/dns-query returned status 503: SERVFAIL',
    'invalid value for argument new-routing-mark',
    'Query Refused (REFUSED)',
    '配置已保存，等待发布',
])
def test_unknown_core_and_source_errors_are_not_translated(raw):
    assert deployment_message(raw) == raw


def test_display_copy_keeps_protocol_fields_and_both_failure_causes():
    record = {'deployment_id': 'task', 'status': 'rollback_failed', 'success': False,
              'error': 'service start failed', 'rollback_error': 'backup checksum mismatch: config.yaml',
              'check_output': 'yaml: line 3: unexpected EOF', 'code': 'CORE_CHECK_FAILED',
              'error_details': 'config.yaml:17: unknown plugin forward_remote\nexit status 1',
              'rollback_error_details': 'rc-service exited with code 3',
              'http_status': 409, 'prepared_content': {'private': True},
              'oxidns_validation_receipt': {'private': True}}
    original = deepcopy(record)
    display = _public_deployment(record)
    assert record == original
    assert display['status'] == 'rollback_failed'
    assert display['success'] is False
    assert display['code'] == 'CORE_CHECK_FAILED'
    assert display['check_output'] == record['check_output']
    assert display['error_details'] == record['error_details']
    assert display['rollback_error_details'] == record['rollback_error_details']
    assert display['http_status'] == 409
    assert display['error'].endswith(record['error'])
    assert display['rollback_error'].endswith(record['rollback_error'])
    assert 'prepared_content' not in display
    assert 'oxidns_validation_receipt' not in display


def test_list_history_and_poll_explain_failures_without_changing_saved_record(authenticated_app):
    client, manager, (agent, _), admin, _ = authenticated_app
    record = {'deployment_id': 'failed-publish', 'status': 'rolled_back', 'success': False,
              'profile_id': 'default', 'service_type': 'mihomo',
              'error': 'service start failed', 'rollback_error': '',
              'check_output': 'failed to parse rule[4]: unknown rule type', 'code': 'CORE_CHECK_FAILED'}
    manager._update_agents(lambda agents: next(
        item for item in agents if item['id'] == agent['id']
    ).update(deployments={'failed-publish': deepcopy(record)}, latest_deployment=deepcopy(record)))

    listed = client.get('/api/agents', headers=admin)
    polled = client.get(f'/api/agents/{agent["id"]}/deployments/failed-publish', headers=admin)
    assert listed.status_code == polled.status_code == 200
    public = next(item for item in listed.json if item['id'] == agent['id'])
    for state in (public['latest_deployment'], public['deployments']['failed-publish'], polled.json):
        assert state['error'] == deployment_message(record['error'])
        assert state['status'] == record['status']
        assert state['check_output'] == record['check_output']
        assert state['code'] == record['code']
    saved = manager.get_agent_by_id(agent['id'])
    assert saved['latest_deployment'] == record
    assert saved['deployments']['failed-publish'] == record


def test_publish_preflight_errors_are_chinese_without_starting_a_task(authenticated_app):
    client, manager, (agent, _), admin, _ = authenticated_app
    original = deepcopy(manager.get_agent_by_id(agent['id']))
    missing = client.post('/api/agents/missing/push-config', headers=admin, json={})
    assert missing.status_code == 404
    assert '找不到这台 Agent' in missing.json['message']
    invalid = client.post(f'/api/agents/{agent["id"]}/push-config', headers=admin, json=[1])
    assert invalid.status_code == 400
    assert '发布请求格式无效' in invalid.json['message']
    invalid_restart = client.post(f'/api/agents/{agent["id"]}/push-config', headers=admin, json={'restart': 'yes'})
    assert invalid_restart.status_code == 400
    assert '重启选项无效' in invalid_restart.json['message']
    assert manager.get_agent_by_id(agent['id']) == original
