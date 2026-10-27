"""A planned release must move and verify the VIP before touching its old holder."""
from copy import deepcopy

import pytest

from backend.agents import oxidns_ha_publish as publications
from backend.agents.manager import AgentManager
from backend.test_oxidns_ha_publish import PRIMARY, SECONDARY, ha_publish  # noqa: F401


def result(env, candidate, manager=None):
    return (manager or env.manager).get_deployment(env.primary['id'], candidate.manifest['deployment_id'])


@pytest.mark.parametrize('holder', [PRIMARY, SECONDARY])
def test_planned_release_moves_actual_vip_then_updates_original_holder_and_cleans_up(ha_publish, holder):
    env = ha_publish
    env.pair()
    env.devices.vip_holder = holder
    target = SECONDARY if holder == PRIMARY else PRIMARY
    candidate = env.bundle()
    checked = []

    def before_upload(host):
        if host == holder:
            assert env.devices.vip_holder == target
            assert env.devices.handoffs[holder]['active'] is True
            assert env.devices.handoffs[holder]['operation_id'] == env.manager.get_agent_by_id(env.primary['id'])[publications.FIELD]['id']
            checked.append(host)
    env.devices.before_upload = before_upload
    env.manager.publish_deployment(env.primary['id'], candidate)
    env.drain()
    assert result(env, candidate)['status'] == 'succeeded'
    assert checked == [holder]
    assert [upload['host'] for upload in env.devices.uploads] == [target, holder]
    assert env.devices.uploads[-1]['activate'] is False
    calls = env.devices.calls
    before = calls.index((target, 'post', 'deployments'))
    handoff = calls.index((holder, 'post', 'handoff'))
    update = calls.index((holder, 'post', 'deployments'))
    cleanup = calls.index((holder, 'delete', 'handoff'))
    activation = next(index for index, (host, method, path) in enumerate(calls)
                      if host == holder and method == 'post' and path.endswith('/activate'))
    assert before < handoff < update < activation < cleanup
    assert (holder, 'get', 'handoff') in calls[cleanup + 1:]
    assert env.devices.handoffs == {}


@pytest.mark.parametrize('host', [PRIMARY, SECONDARY])
def test_missing_handoff_protocol_is_rejected_before_updating_either_member(ha_publish, host):
    env = ha_publish
    env.pair()
    env.devices.handoff_protocols[host] = []
    candidate = env.bundle()
    try:
        env.manager.publish_deployment(env.primary['id'], candidate)
        env.drain()
    except ValueError:
        pass
    else:
        assert result(env, candidate)['status'] == 'failed'
    assert env.devices.uploads == []
    assert not any(method in ('post', 'delete') for _, method, _ in env.devices.handoff_calls)


@pytest.mark.parametrize('problem', ['old-holder', 'both-holders', 'no-holder', 'source-vip-dns', 'target-vip-dns'])
def test_old_holder_is_untouched_until_unique_takeover_and_both_vip_queries_succeed(ha_publish, monkeypatch, problem):
    env = ha_publish
    env.pair()
    original = deepcopy(env.devices.installed[PRIMARY])
    handoff = env.devices.handoff

    def unsafe_handoff(agent, method, operation_id=None):
        response = handoff(agent, method, operation_id)
        if method.lower() == 'post':
            if problem == 'old-holder':
                env.devices.vip_holder = PRIMARY
            elif problem == 'both-holders':
                env.devices.vip_holders = {PRIMARY, SECONDARY}
            elif problem == 'no-holder':
                env.devices.vip_holders = set()
            else:
                env.devices.vip_unhealthy.add(PRIMARY if problem == 'source-vip-dns' else SECONDARY)
            return env.devices.status(agent)
        return response
    monkeypatch.setattr(env.devices, 'handoff', unsafe_handoff)
    candidate = env.bundle()
    env.manager.publish_deployment(env.primary['id'], candidate)
    env.drain()
    state = result(env, candidate)
    assert state['status'] != 'succeeded'
    assert [upload['host'] for upload in env.devices.uploads] == [SECONDARY]
    assert env.devices.installed[PRIMARY] == original
    assert not any(host == PRIMARY and path.endswith('/activate') for host, path in env.devices.mutations())
    if state['status'] not in ('failed', 'rollback_failed'):
        rejected = env.manager.begin_deployment(env.secondary['id'], 'overlap')
        assert not rejected['success'] and rejected['http_status'] == 409


def test_lost_handoff_reply_is_reconciled_after_restart_without_repeating_updates(ha_publish):
    env = ha_publish
    env.pair()
    env.devices.handoff_lost_reply.add((PRIMARY, 'post'))
    candidate = env.bundle()
    env.manager.publish_deployment(env.primary['id'], candidate)
    restarted = AgentManager(env.repository)
    env.drain(manager=restarted)
    assert result(env, candidate, restarted)['status'] == 'succeeded'
    assert [upload['host'] for upload in env.devices.uploads] == [SECONDARY, PRIMARY]
    operations = [operation for host, method, operation in env.devices.handoff_calls if method == 'post']
    assert operations and len(set(operations)) == 1
    assert env.devices.handoffs == {}


def test_unavailable_handoff_status_keeps_original_holder_untouched_and_pair_reserved(ha_publish):
    env = ha_publish
    env.pair()
    env.devices.handoff_unreachable.add((PRIMARY, 'get'))
    candidate = env.bundle()
    env.manager.publish_deployment(env.primary['id'], candidate)
    env.drain()
    state = result(env, candidate)
    assert state['status'] == 'unknown'
    assert not any(upload['host'] == PRIMARY for upload in env.devices.uploads)
    assert env.manager.begin_deployment(env.secondary['id'], 'overlap')['http_status'] == 409
    env.devices.handoff_unreachable.clear()
    restarted = AgentManager(env.repository)
    env.drain(manager=restarted)
    assert result(env, candidate, restarted)['status'] == 'succeeded'
    assert [upload['host'] for upload in env.devices.uploads] == [SECONDARY, PRIMARY]


@pytest.mark.parametrize('primary_status', ['succeeded', 'failed'])
def test_cleanup_must_be_confirmed_before_releasing_pair_or_reporting_outcome(ha_publish, primary_status):
    env = ha_publish
    env.pair()
    env.devices.activation_status[PRIMARY] = primary_status
    env.devices.handoff_unreachable.add((PRIMARY, 'delete'))
    candidate = env.bundle()
    env.manager.publish_deployment(env.primary['id'], candidate)
    env.drain()
    state = result(env, candidate)
    assert state['status'] == 'unknown'
    assert env.devices.handoffs
    assert env.manager.begin_deployment(env.secondary['id'], 'overlap')['http_status'] == 409
    assert [upload['host'] for upload in env.devices.uploads] == [SECONDARY, PRIMARY]
    env.devices.handoff_unreachable.clear()
    restarted = AgentManager(env.repository)
    env.drain(manager=restarted)
    assert result(env, candidate, restarted)['status'] == primary_status
    assert env.devices.handoffs == {}
    assert [upload['host'] for upload in env.devices.uploads] == [SECONDARY, PRIMARY]


def test_lost_cleanup_reply_is_confirmed_by_reading_absence_after_restart(ha_publish, monkeypatch):
    env = ha_publish
    env.pair()
    env.devices.handoff_lost_reply.add((PRIMARY, 'delete'))
    handoff = env.devices.handoff

    def lost_cleanup(agent, method, operation_id=None):
        if method.lower() == 'delete':
            env.devices.handoff_unreachable.add((PRIMARY, 'get'))
        return handoff(agent, method, operation_id)
    monkeypatch.setattr(env.devices, 'handoff', lost_cleanup)
    candidate = env.bundle()
    env.manager.publish_deployment(env.primary['id'], candidate)
    env.drain()
    assert result(env, candidate)['status'] == 'unknown'
    assert env.devices.handoffs == {}
    monkeypatch.setattr(env.devices, 'handoff', handoff)
    env.devices.handoff_unreachable.clear()
    restarted = AgentManager(env.repository)
    env.drain(manager=restarted)
    assert result(env, candidate, restarted)['status'] == 'succeeded'
    assert env.devices.handoffs == {}
    assert [upload['host'] for upload in env.devices.uploads] == [SECONDARY, PRIMARY]


def test_staged_candidates_do_not_handoff_until_activation_and_move_vip_between_activations(ha_publish, monkeypatch):
    env = ha_publish
    env.pair()
    candidate = env.bundle()
    env.manager.publish_deployment(env.primary['id'], candidate, activate=False)
    env.drain()
    assert result(env, candidate)['status'] == 'ready'
    assert not any(method in ('post', 'delete') for _, method, _ in env.devices.handoff_calls)
    original_request = env.devices.request
    checked = []

    def request(agent, method, path, **kwargs):
        if agent['host'] == PRIMARY and method == 'post' and path.endswith('/activate'):
            assert env.devices.vip_holder == SECONDARY
            assert env.devices.handoffs[PRIMARY]['active'] is True
            checked.append(PRIMARY)
        return original_request(agent, method, path, **kwargs)
    monkeypatch.setattr(env.devices, 'request', request)
    env.manager.activate_deployment(env.primary['id'], candidate.manifest['deployment_id'])
    env.drain()
    assert result(env, candidate)['status'] == 'succeeded'
    assert checked == [PRIMARY]
    activations = [(host, path) for host, path in env.devices.mutations() if path.endswith('/activate')]
    assert [host for host, _ in activations] == [SECONDARY, PRIMARY]
    first = env.devices.calls.index((activations[0][0], 'post', activations[0][1]))
    second = env.devices.calls.index((activations[1][0], 'post', activations[1][1]))
    assert first < env.devices.calls.index((PRIMARY, 'post', 'handoff')) < second


def test_handoff_deadline_cleans_up_and_never_updates_the_original_holder(ha_publish, monkeypatch):
    from backend.agents import oxidns_ha_handoff
    env = ha_publish
    env.pair()
    env.devices.auto_handoff = False
    candidate = env.bundle()
    env.manager.publish_deployment(env.primary['id'], candidate)
    env.drain(attempts=3)
    assert env.devices.handoffs
    assert [upload['host'] for upload in env.devices.uploads] == [SECONDARY]
    monkeypatch.setattr(oxidns_ha_handoff, 'TIMEOUT', -1)
    env.drain()
    assert result(env, candidate)['status'] == 'failed'
    assert env.devices.handoffs == {}
    assert [upload['host'] for upload in env.devices.uploads] == [SECONDARY]


def test_cleanup_waits_for_normal_election_and_both_vip_queries_before_success(ha_publish, monkeypatch):
    env = ha_publish
    env.pair()
    handoff = env.devices.handoff

    def slow_election(agent, method, operation_id=None):
        response = handoff(agent, method, operation_id)
        if method.lower() == 'delete':
            env.devices.vip_holder = SECONDARY
            env.devices.vip_unhealthy.add(PRIMARY)
        return response
    monkeypatch.setattr(env.devices, 'handoff', slow_election)
    candidate = env.bundle()
    env.manager.publish_deployment(env.primary['id'], candidate)
    env.drain()
    assert result(env, candidate)['status'] not in ('succeeded', 'failed', 'rollback_failed')
    assert env.devices.handoffs == {}
    assert env.manager.begin_deployment(env.secondary['id'], 'overlap')['http_status'] == 409
    env.devices.vip_holder = PRIMARY
    env.devices.vip_unhealthy.clear()
    restarted = AgentManager(env.repository)
    env.drain(manager=restarted)
    assert result(env, candidate, restarted)['status'] == 'succeeded'
    assert [upload['host'] for upload in env.devices.uploads] == [SECONDARY, PRIMARY]


def test_failed_release_keeps_reservation_until_cleanup_election_converges(ha_publish, monkeypatch):
    env = ha_publish
    env.pair()
    env.devices.activation_status[PRIMARY] = 'failed'
    handoff = env.devices.handoff

    def slow_election(agent, method, operation_id=None):
        response = handoff(agent, method, operation_id)
        if method.lower() == 'delete':
            env.devices.vip_holder = SECONDARY
        return response
    monkeypatch.setattr(env.devices, 'handoff', slow_election)
    candidate = env.bundle()
    env.manager.publish_deployment(env.primary['id'], candidate)
    env.drain()
    assert result(env, candidate)['status'] not in ('succeeded', 'failed', 'rollback_failed')
    assert env.devices.handoffs == {}
    assert env.manager.begin_deployment(env.secondary['id'], 'overlap')['http_status'] == 409
    env.devices.vip_holder = PRIMARY
    restarted = AgentManager(env.repository)
    env.drain(manager=restarted)
    assert result(env, candidate, restarted)['status'] == 'failed'
    assert [upload['host'] for upload in env.devices.uploads] == [SECONDARY, PRIMARY]


@pytest.mark.parametrize('problem', ['vip-returned', 'target-unhealthy', 'vip-query-failed'])
def test_long_old_holder_upload_is_staged_and_rechecks_handoff_before_activation(ha_publish, problem):
    env = ha_publish
    env.pair()
    original = deepcopy(env.devices.installed[PRIMARY])

    def conditions_changed_during_upload(host):
        if host == PRIMARY:
            assert env.devices.uploads[-1]['activate'] is False
            if problem == 'vip-returned':
                env.devices.vip_holder = PRIMARY
            elif problem == 'target-unhealthy':
                env.devices.unhealthy.add(SECONDARY)
            else:
                env.devices.vip_unhealthy.add(PRIMARY)
    env.devices.before_upload = conditions_changed_during_upload
    candidate = env.bundle()
    env.manager.publish_deployment(env.primary['id'], candidate)
    env.drain()
    assert result(env, candidate)['status'] != 'succeeded'
    assert [upload['host'] for upload in env.devices.uploads] == [SECONDARY, PRIMARY]
    assert env.devices.installed[PRIMARY] == original
    assert not any(host == PRIMARY and path.endswith('/activate') for host, path in env.devices.mutations())


def test_legacy_accepted_group_resumes_without_requiring_an_impossible_agent_upgrade(ha_publish):
    env = ha_publish
    env.pair()
    env.devices.upload_status[SECONDARY] = 'unknown'
    candidate = env.bundle()
    env.manager.publish_deployment(env.primary['id'], candidate)
    assert [upload['host'] for upload in env.devices.uploads] == [SECONDARY]

    def old_journal(items):
        for agent in items:
            if agent['id'] in (env.primary['id'], env.secondary['id']):
                job = agent[publications.FIELD]
                job.pop('handoff_protocol', None)
                job.pop('handoff_required', None)
    env.manager._update_agents(old_journal)
    env.devices.handoff_protocols = {PRIMARY: [], SECONDARY: []}
    standby_id = env.devices.uploads[0]['id']
    env.devices.journals[SECONDARY, standby_id].update(status='succeeded', success=True)
    env.devices.installed[SECONDARY] = deepcopy(env.devices.candidates[SECONDARY, standby_id])
    restarted = AgentManager(env.repository)
    env.drain(manager=restarted)
    assert result(env, candidate, restarted)['status'] == 'succeeded'
    assert [upload['host'] for upload in env.devices.uploads] == [SECONDARY, PRIMARY]
    assert env.devices.uploads[-1]['activate'] is True
    assert not any(method in ('post', 'delete') for _, method, _ in env.devices.handoff_calls)
