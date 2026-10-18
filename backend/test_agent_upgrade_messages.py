"""Localize the admin view without changing Agent update or recovery records."""
from copy import deepcopy

import pytest

from backend.agents import upgrades
from backend.agents.upgrade_messages import public_upgrade_state, upgrade_error_message
from backend.test_agent_upgrade_auth import (  # noqa: F401
    authenticated_app, agent_response, seed_upgrade,
)


@pytest.mark.parametrize('raw, expected', [
    ('Agent download connection failed', '无法连接 Agent 更新下载地址'),
    ('Agent download HTTP 404', '服务器返回 HTTP 404'),
    ('Agent download write failed', '下载或写入失败'),
    ('Agent download size or SHA-256 mismatch', '文件大小或 SHA-256 与预期不符'),
    ('candidate version or executable validation failed', '新版 Agent 无法运行或版本不符'),
    ('Agent did not confirm expected version, identity and migration readiness', '未能确认 Agent 的版本'),
    ('another Agent update or service operation is active', '请等待完成后再试'),
    ('deployment previous-job requires rollback recovery', '配置发布尚未完成恢复'),
    ('OpenRC supervise-daemon is required for recoverable updates', '设备缺少 OpenRC'),
    ('cannot infer custom lifecycle; configure service_manager and service_unit before upgrading', '请先配置 service_manager 和 service_unit'),
    ('open /opt/agent: permission denied', '权限不足'),
    ('write /opt/agent: no space left on device', '磁盘空间不足'),
    ('rename /opt/agent: read-only file system', '文件系统只读'),
    ('open /opt/agent: no such file or directory', '文件或目录不存在'),
    ('dial tcp: lookup config.test: no such host', '无法解析服务器地址'),
    ('context deadline exceeded', '操作超时'),
    ('systemctl operation failed: Job failed with exit status 1', '服务管理命令执行失败'),
    ('rc-service operation failed: permission denied', '权限不足'),
    ('new unexpected worker failure', 'Agent 更新遇到异常'),
    ('   ', 'Agent 更新遇到异常'),
])
def test_upgrade_failures_have_actionable_chinese_messages(raw, expected):
    message = upgrade_error_message(raw)
    assert expected in message
    if raw.strip():
        assert message.endswith('原始错误：' + raw)
    assert upgrade_error_message(message) == message


def test_failed_update_and_failed_rollback_both_remain_visible():
    raw = ('candidate version or executable validation failed; rollback: '
           'Agent backup hash mismatch')
    message = upgrade_error_message(raw)
    assert '新版 Agent 无法运行或版本不符' in message
    assert '恢复旧版本失败' in message
    assert '备份文件校验失败' in message
    assert '已回滚' not in message
    assert message.endswith('原始错误：' + raw)
    assert message.count('原始错误：') == 1


def test_nested_migration_failure_retains_both_causes():
    message = upgrade_error_message(
        'migration failed; configuration rollback failed: '
        'open /opt/agent/config.json: permission denied')
    assert '配置迁移失败，恢复原配置也失败' in message
    assert '权限不足' in message


def test_response_copy_preserves_protocol_fields_and_original_error():
    state = {'update_id': 'job', 'status': 'rollback_failed', 'protocol': 2,
             'target_version': '1.8.16-go', 'previous_version': '1.8.15-go',
             'error': 'Agent backup hash mismatch', 'updated_at': 123,
             'artifacts': {'linux-amd64': {'size': 100, 'sha256': 'a' * 64}}}
    original = deepcopy(state)
    public = public_upgrade_state(state)
    assert state == original
    assert public['error'] != state['error']
    assert public['error'].endswith('原始错误：' + state['error'])
    assert {key: value for key, value in public.items() if key != 'error'} == {
        key: value for key, value in original.items() if key != 'error'}
    assert public_upgrade_state(public) == public


@pytest.mark.parametrize('state', [{}, {'status': 'succeeded', 'error': ''},
                                  {'status': 'checking', 'error': '请求结果待确认，正在查询 Agent'}])
def test_empty_success_and_existing_chinese_messages_are_preserved(state):
    assert public_upgrade_state(state) == state


@pytest.mark.parametrize('status', ['failed', 'rolled_back', 'rollback_failed'])
def test_list_detail_and_status_localize_old_records_without_rewriting_them(
        authenticated_app, monkeypatch, status):
    client, manager, (agent, _), admin, _ = authenticated_app
    seed_upgrade(manager, agent)
    raw = 'Agent download size or SHA-256 mismatch'
    manager._update_agents(lambda agents: next(
        item for item in agents if item['id'] == agent['id']
    )['latest_upgrade'].update(status=status, error=raw))
    original = deepcopy(manager.get_agent_by_id(agent['id'])['latest_upgrade'])
    monkeypatch.setattr(upgrades, '_request', lambda *_a, **_kw: agent_response({}, 404))

    listing = client.get('/api/agents', headers=admin)
    detail = client.get(f'/api/agents/{agent["id"]}', headers=admin)
    progress = client.get(f'/api/agents/{agent["id"]}/upgrade', headers=admin)
    assert listing.status_code == detail.status_code == progress.status_code == 200
    states = [next(item for item in listing.json if item['id'] == agent['id'])['latest_upgrade'],
              detail.json['latest_upgrade'], progress.json]
    for state in states:
        assert state['error'] == upgrade_error_message(raw)
        assert state['status'] == status
        assert state['update_id'] == original['update_id']
    assert manager.get_agent_by_id(agent['id'])['latest_upgrade'] == original

    # The old Agent's bootstrap still sees its original protocol response.
    own = client.get(f'/api/agents/{agent["id"]}/upgrade',
                     headers={'Authorization': 'Bearer ' + agent['token']})
    assert own.json == original


def test_rejected_update_response_is_chinese_but_persisted_error_is_original(
        authenticated_app, monkeypatch):
    client, manager, (agent, _), admin, _ = authenticated_app
    raw = 'another Agent update or service operation is active'

    def request(_agent, method, path, **_kwargs):
        if path == '/api/upgrade-info':
            return agent_response({'agent_id': agent['id'], 'deployment_method': 'shell',
                                   'architecture': 'linux-amd64'})
        assert (method, path) == ('POST', '/api/update')
        return agent_response({'success': False, 'message': raw}, 409)

    monkeypatch.setattr(upgrades, '_request', request)
    result = client.post(f'/api/agents/{agent["id"]}/update', headers=admin, json={})
    assert result.status_code == 409
    assert result.json['status'] == 'failed'
    assert result.json['error'] == upgrade_error_message(raw)
    assert manager.get_agent_by_id(agent['id'])['latest_upgrade']['error'] == raw


def test_live_rollback_failure_is_localized_without_changing_result(
        authenticated_app, monkeypatch):
    client, manager, (agent, _), admin, _ = authenticated_app
    state = seed_upgrade(manager, agent)
    manager._update_agents(lambda agents: next(
        item for item in agents if item['id'] == agent['id']
    )['latest_upgrade'].update(protocol=2))
    raw = ('Agent did not confirm expected version, identity and migration readiness; '
           'rollback: open /opt/agent/backup: no such file or directory')
    monkeypatch.setattr(upgrades, '_request', lambda *_a, **_kw: agent_response({
        'update_id': state['update_id'], 'agent_id': agent['id'],
        'status': 'rollback_failed', 'error': raw,
    }))
    result = client.get(f'/api/agents/{agent["id"]}/upgrade', headers=admin)
    assert result.status_code == 200
    assert result.json['status'] == 'rollback_failed'
    assert '未能确认 Agent 的版本' in result.json['error']
    assert '恢复旧版本失败' in result.json['error']
    assert manager.get_agent_by_id(agent['id'])['latest_upgrade']['error'] == raw


def test_legacy_migration_failure_is_localized_when_viewed_by_admin(
        authenticated_app, monkeypatch):
    client, manager, (agent, _), admin, _ = authenticated_app
    state = seed_upgrade(manager, agent)
    raw = 'automatic migration failed: systemctl operation failed: failed to start'
    monkeypatch.setattr(upgrades, '_request', lambda *_a, **_kw: agent_response({}, 404))
    path = f'/api/agents/{agent["id"]}/upgrade'
    result = client.post(path + '/report', headers={'Authorization': 'Bearer ' + agent['token']},
                         json={'update_id': state['update_id'], 'status': 'rolling_back', 'error': raw})
    assert result.status_code == 200
    viewed = client.get(path, headers=admin)
    assert viewed.json['status'] == 'rolling_back'
    assert '自动迁移失败' in viewed.json['error']
    assert '服务管理命令执行失败' in viewed.json['error']
    assert manager.get_agent_by_id(agent['id'])['latest_upgrade']['error'] == raw


def test_missing_agent_and_update_record_errors_are_chinese(authenticated_app):
    client, _, (agent, _), admin, _ = authenticated_app
    missing = client.post('/api/agents/missing/update', headers=admin, json={})
    assert missing.status_code == 409
    assert missing.json['message'] == upgrade_error_message('Agent not found')
    no_record = client.get(f'/api/agents/{agent["id"]}/upgrade', headers=admin)
    assert no_record.status_code == 404
    assert no_record.json['message'] == upgrade_error_message('No upgrade recorded')
