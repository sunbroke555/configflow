"""Upgrade persistence, acceptance/health distinction and failure concurrency."""
from datetime import datetime
import json
import time

import pytest
import requests

from backend.agents import upgrades
from backend.agents.manager import AgentManager
from backend.common import config as config_module
from backend.common.config_repository import ProfileRepository


def response(payload, status=200):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(payload).encode()
    result._content_consumed = True
    return result


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = ProfileRepository(tmp_path)
    config_module.set_repository(repo)
    manager = AgentManager(repo)
    agent = manager.register_agent({'name': 'upgrade', 'host': 'localhost', 'port': 1234,
                                   'service_type': 'mihomo', 'deployment_method': 'shell', 'version': '1.1.0-go'})
    ident = agent['id']
    monkeypatch.setattr(upgrades, 'artifacts', lambda: {arch: {'sha256': 'a' * 64, 'size': 100} for arch in upgrades.ARCHES})
    return repo, manager, ident


def test_acceptance_is_not_success_and_reserves_across_restart(env, monkeypatch):
    repo, manager, ident = env
    info = {'agent_id': ident, 'deployment_method': 'shell', 'architecture': 'linux-amd64', 'version': '1.1.0-go'}
    monkeypatch.setattr(upgrades, '_request', lambda a, m, p, **kw: response(info if p.endswith('info') else {}, 200))
    state = upgrades.start(manager, ident)
    assert state['status'] == 'queued'  # Acceptance is not an observed worker stage.
    restarted = AgentManager(repo)
    with pytest.raises(ValueError, match='已有更新'):
        upgrades.start(restarted, ident)
    assert restarted.get_agent_by_id(ident)['latest_upgrade']['update_id'] == state['update_id']


def test_worker_success_still_requires_version_identity_and_migration(env, monkeypatch):
    _, manager, ident = env
    state = {'update_id': 'job', 'target_version': upgrades.get_latest_version(), 'previous_version': '1.1.0-go',
             'status': 'checking', 'protocol': 2, 'started_at': time.time()}
    manager._update_agents(lambda agents: agents[0].update(latest_upgrade=state))
    info = {'agent_id': ident, 'version': state['target_version'], 'migration_ready': False}
    monkeypatch.setattr(upgrades, '_request', lambda a, m, p, **kw: response(info if p.endswith('info') else {'update_id': 'job', 'agent_id': ident, 'status': 'succeeded'}))
    assert upgrades.poll(manager, ident)['status'] == 'checking'
    info['migration_ready'] = True
    info['agent_id'] = 'wrong-agent'
    assert upgrades.poll(manager, ident)['status'] == 'checking'
    info['agent_id'] = ident
    assert upgrades.poll(manager, ident)['status'] == 'succeeded'


def test_legacy_rollback_requires_fresh_authenticated_heartbeat(env, monkeypatch):
    _, manager, ident = env
    state = {'update_id': 'job', 'target_version': upgrades.get_latest_version(), 'previous_version': '1.1.0-go',
             'status': 'downloading', 'protocol': 1, 'started_at': time.time()}
    manager._update_agents(lambda agents: agents[0].update(latest_upgrade=state))
    def request(a, m, p, **kw):
        assert p != '/api/update', 'legacy GET endpoint is unsupported'
        return response({}, 404)
    monkeypatch.setattr(upgrades, '_request', request)
    assert upgrades.report_legacy(manager, ident, {'update_id': 'job', 'status': 'rolling_back'})['status'] == 'rolling_back'
    assert upgrades.poll(manager, ident)['status'] == 'rolling_back'
    manager.update_heartbeat(ident, {'version': '1.1.0-go'})
    assert upgrades.poll(manager, ident)['status'] == 'rolled_back'


def test_unknown_timeout_blocks_retry_and_does_not_claim_rollback(env, monkeypatch):
    _, manager, ident = env
    state = {'update_id': 'job', 'target_version': upgrades.get_latest_version(), 'previous_version': '1.1.0-go',
             'status': 'checking', 'protocol': 2, 'started_at': time.time() - 601}
    stale = datetime.fromtimestamp(state['started_at']).isoformat()
    manager._update_agents(lambda agents: agents[0].update(latest_upgrade=state, last_heartbeat=stale))
    monkeypatch.setattr(upgrades, '_request', lambda *a, **kw: response({}, 404))
    assert upgrades.poll(manager, ident)['status'] == 'unknown'
    with pytest.raises(ValueError, match='已有更新'):
        upgrades.start(manager, ident)


def test_old_version_heartbeat_after_timeout_fails_and_allows_retry(env, monkeypatch):
    _, manager, ident = env
    state = {'update_id': 'job', 'target_version': upgrades.get_latest_version(), 'previous_version': '1.1.0-go',
             'status': 'unknown', 'protocol': 2, 'started_at': time.time() - 601}
    manager._update_agents(lambda agents: agents[0].update(latest_upgrade=state))
    manager.update_heartbeat(ident, {'version': '1.1.0-go'})
    info = {'agent_id': ident, 'deployment_method': 'shell', 'architecture': 'linux-amd64', 'version': '1.1.0-go'}
    monkeypatch.setattr(upgrades, '_request', lambda a, m, p, **kw: response(info if p.endswith('info') else {}, 404 if m == 'GET' and p == '/api/update' else 200))
    result = upgrades.poll(manager, ident)
    assert result['status'] == 'failed'
    assert '1.1.0-go' in result['error']
    assert upgrades.start(manager, ident)['status'] == 'queued'


def test_docker_is_not_sent_binary_update(env, monkeypatch):
    _, manager, ident = env
    manager._update_agents(lambda agents: agents[0].update(deployment_method='docker'))
    monkeypatch.setattr(upgrades, '_request', lambda *a, **kw: pytest.fail('contacted Docker binary updater'))
    with pytest.raises(ValueError, match='Docker'):
        upgrades.start(manager, ident)


def test_failed_rollback_can_confirm_later_recovery(env, monkeypatch):
    _, manager, ident = env
    state = {'update_id': 'job', 'target_version': upgrades.get_latest_version(), 'previous_version': '1.1.0-go',
             'status': 'rollback_failed', 'protocol': 2, 'started_at': time.time()}
    manager._update_agents(lambda agents: agents[0].update(latest_upgrade=state))
    monkeypatch.setattr(upgrades, '_request', lambda *a, **kw: response({'update_id':'job', 'agent_id':ident, 'status':'rolled_back'}))
    assert upgrades.poll(manager, ident)['status'] == 'rolled_back'


def test_worker_events_survive_restart_and_repeated_polls_without_duplicates(env, monkeypatch):
    repo, manager, ident = env
    state = {'update_id': 'job', 'target_version': upgrades.get_latest_version(), 'previous_version': '1.1.0-go',
             'status': 'queued', 'protocol': 2, 'started_at': time.time()}
    manager._update_agents(lambda agents: agents[0].update(latest_upgrade=state))
    payload = {'update_id': 'job', 'agent_id': ident, 'status': 'verifying',
               'message': '检查 Agent 候选程序与版本', 'updated_at': '2026-10-18T00:00:02Z',
               'events': [{'at': '2026-10-18T00:00:00Z', 'status': 'queued'},
                          {'at': '2026-10-18T00:00:01Z', 'status': 'downloading', 'message': '下载 configflow-agent-linux-amd64'},
                          {'at': '2026-10-18T00:00:02Z', 'status': 'verifying', 'message': '检查 Agent 候选程序与版本'}]}
    monkeypatch.setattr(upgrades, '_request', lambda *_a, **_kw: response(payload))
    first = upgrades.poll(manager, ident)
    second = upgrades.poll(AgentManager(repo), ident)
    assert first['events'] == second['events']
    assert [event['status'] for event in first['events']] == ['queued', 'downloading', 'verifying']
    assert second['message'] == payload['message']


def test_unconfirmed_worker_success_does_not_expose_success_event(env, monkeypatch):
    _, manager, ident = env
    state = {'update_id': 'job', 'target_version': upgrades.get_latest_version(), 'previous_version': '1.1.0-go',
             'status': 'checking', 'protocol': 2, 'started_at': time.time()}
    manager._update_agents(lambda agents: agents[0].update(latest_upgrade=state))
    worker = {'update_id': 'job', 'agent_id': ident, 'status': 'succeeded', 'updated_at': '2026-10-18T00:00:01Z',
              'events': [{'at': '2026-10-18T00:00:00Z', 'status': 'checking'},
                         {'at': '2026-10-18T00:00:01Z', 'status': 'succeeded'}]}
    info = {'agent_id': ident, 'version': state['target_version'], 'migration_ready': False}
    monkeypatch.setattr(upgrades, '_request', lambda _a, _m, path, **_kw: response(info if path.endswith('info') else worker))
    result = upgrades.poll(manager, ident)
    assert result['status'] == 'checking'
    assert not any(event['status'] == 'succeeded' for event in result['events'])
    info['migration_ready'] = True
    assert upgrades.poll(manager, ident)['events'][-1]['status'] == 'succeeded'


def test_late_start_acceptance_keeps_stage_already_observed_by_poll(env, monkeypatch):
    _, manager, ident = env
    info = {'agent_id': ident, 'deployment_method': 'shell', 'architecture': 'linux-amd64'}

    def request(_agent, method, path, **kwargs):
        if path.endswith('info'):
            return response(info)
        upgrades._save(manager, ident, kwargs['json']['update_id'], {'status': 'migrating', 'message': '正在迁移 Agent 配置'})
        return response({})

    monkeypatch.setattr(upgrades, '_request', request)
    result = upgrades.start(manager, ident)
    assert result['status'] == 'migrating'
    assert result['events'][-1]['status'] == 'migrating'


def test_changed_upgrade_during_preflight_prevents_a_second_submission(env, monkeypatch):
    _, manager, ident = env

    def request(_agent, method, path, **_kwargs):
        assert method == 'GET' and path.endswith('info'), 'late request must not submit a second update'
        manager._update_agents(lambda agents: agents[0].update(latest_upgrade={'update_id': 'other-completed', 'status': 'succeeded'}))
        return response({'agent_id': ident, 'deployment_method': 'shell', 'architecture': 'linux-amd64'})

    monkeypatch.setattr(upgrades, '_request', request)
    with pytest.raises(ValueError, match='更新任务已改变'):
        upgrades.start(manager, ident)
    assert manager.get_agent_by_id(ident)['latest_upgrade']['update_id'] == 'other-completed'


def test_stale_worker_response_does_not_undo_newer_state_and_old_worker_has_observed_events(env):
    _, manager, ident = env
    state = {'update_id': 'job', 'status': 'queued', 'protocol': 2, 'started_at': time.time()}
    manager._update_agents(lambda agents: agents[0].update(latest_upgrade=state))
    current = upgrades._save(manager, ident, 'job', {'status': 'checking', 'agent_updated_at': '2026-10-18T00:00:00.1Z'})
    stale = upgrades._save(manager, ident, 'job', {'status': 'downloading', 'agent_updated_at': '2026-10-18T00:00:00Z'})
    assert stale['status'] == 'checking'
    assert stale['events'] == current['events']
    assert [event['status'] for event in current['events']] == ['checking']
