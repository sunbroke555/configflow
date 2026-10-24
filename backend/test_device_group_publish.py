"""Durable batch sequencing and recovery, with real profile snapshots and CAS."""
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
import uuid

import pytest

from backend.agents import device_group_publish as service, device_group_versions
from backend.common.config_repository import ProfileInUse, ProfileRepository, ProfileValidationError
from backend.common import device_group_workspace as workspace

REAL_PUBLISH = service._publish


class Manager:
    def __init__(self, repository, tasks=None):
        self.repository = repository
        self.tasks = tasks if tasks is not None else {}
        self.reads, self.activations = [], []
        self.pinned_reads, self.pinned_activations = [], []

    def get_all_agents(self):
        return self.repository.get_publish_snapshot('default')[0]['agents']

    def get_agent_by_id(self, identity):
        return next((a for a in self.get_all_agents() if a['id'] == identity), None)

    def get_deployment(self, identity, deployment_id, *, expected_target_fingerprint=None):
        self.reads.append((identity, deployment_id))
        self.pinned_reads.append((identity, expected_target_fingerprint))
        assert expected_target_fingerprint == workspace.target_fingerprint(
            self.get_agent_by_id(identity), self.repository.default_profile_id())
        return deepcopy(self.tasks.get((identity, deployment_id), {
            'http_status': 404, 'success': False, 'message': 'Deployment not found'}))

    def activate_deployment(self, identity, deployment_id, *, expected_target_fingerprint=None):
        self.activations.append((identity, deployment_id))
        self.pinned_activations.append((identity, expected_target_fingerprint))
        assert expected_target_fingerprint == workspace.target_fingerprint(
            self.get_agent_by_id(identity), self.repository.default_profile_id())
        self.tasks[(identity, deployment_id)] = {'status': 'succeeded', 'success': True}
        return deepcopy(self.tasks[(identity, deployment_id)])


@pytest.fixture
def env(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    repository.update_profile_transaction('default', lambda p: p.update(
        device_groups={'version': 1, 'groups': [
            {'id': 'default', 'name': '默认组', 'members': []},
            {'id': 'dg_aaaaaa', 'name': '直连设备', 'members': ['192.168.60.99'], 'mode': 'direct'},
        ]},
        policy_routing_sources={'include': [], 'exclude_groups': []}))
    agents = [{'id': identity, 'name': identity, 'host': '192.168.60.' + str(index), 'port': 8080,
               'token': 'private-token', 'service_type': kind, 'profile_id': 'default',
               'enabled': True, 'status': 'online', 'version': '1.8.41-go',
               'last_heartbeat': datetime.now().isoformat()}
              for index, (identity, kind) in enumerate([('o1', 'oxidns'), ('m2', 'mihomo'), ('m1', 'mihomo')], 10)]
    repository.update_system_transaction(lambda system: system.update(agents=agents))
    manager = Manager(repository)
    starts, uploads = [], []
    monkeypatch.setattr(service, '_start', lambda *_args: starts.append(_args[-1]))
    monkeypatch.setattr(device_group_versions.mihomo_panel, 'call', lambda *_: {'data': {'version': 'v1.19.32'}})

    def publish(_manager, agent, _config, batch, target):
        stored = service._read(repository, 'default', batch['batch_id'])
        assert next(row for row in stored['targets'] if row['agent_id'] == agent['id'])['deployment_id'] == target['deployment_id']
        uploads.append((agent['id'], target['deployment_id']))
        task = {'status': 'succeeded', 'success': True}
        manager.tasks[uploads[-1]] = task
        return task

    monkeypatch.setattr(service, '_publish', publish)
    return repository, manager, starts, uploads


def payload(repository, **changes):
    config, revision = repository.get_publish_snapshot('default')
    base_url = changes.get('base_url', '')
    result = {'batch_id': uuid.uuid4().hex, 'base_url': base_url,
              'preview_token': workspace.preview_token(config, revision, config, base_url=base_url),
              'target_ids': ['o1', 'm2', 'm1']}
    result.update(changes)
    return result


def created(env, **changes):
    repository, manager, _, _ = env
    body = payload(repository, **changes)
    service.create(repository, manager, 'default', body)
    return body['batch_id']


def test_every_mihomo_succeeds_before_first_oxidns_and_duplicate_create_is_idempotent(env):
    repository, manager, starts, uploads = env
    body = payload(repository, base_url='https://config.test')
    first = service.create(repository, manager, 'default', body)
    assert service.create(repository, manager, 'default', body)['batch_id'] == first['batch_id']
    assert len(starts) == 1
    service.advance(repository, manager, 'default', body['batch_id'])
    result = service.get(repository, 'default', body['batch_id'])
    assert [identity for identity, _ in uploads] == ['m1', 'm2', 'o1']
    assert result['state'] == 'succeeded' and result['phase'] == 'completed'
    assert all(row['state'] == 'succeeded' for row in result['targets'])
    assert all('target_fingerprint' not in row for row in result['targets'])


@pytest.mark.parametrize('status', ['unknown', 'ready', 'failed', 'rollback_failed'])
def test_ambiguous_ready_and_failed_first_target_pause_all_later_targets(env, monkeypatch, status):
    repository, manager, _, uploads = env
    def publish(_manager, agent, _config, _batch, target):
        uploads.append((agent['id'], target['deployment_id']))
        manager.tasks[uploads[-1]] = {'status': status, 'message': '等待处理'}
        return manager.tasks[uploads[-1]]
    monkeypatch.setattr(service, '_publish', publish)
    identity = created(env)
    service.advance(repository, manager, 'default', identity)
    result = service.get(repository, 'default', identity)
    assert result['state'] == 'paused' and [a for a, _ in uploads] == ['m1']
    assert result['actions']['activate_agent_ids'] == (['m1'] if status == 'ready' else [])


def test_lost_post_result_is_reconciled_under_original_id_before_more_uploads(env, monkeypatch):
    repository, manager, _, uploads = env
    original = service._publish
    def publish(*args):
        task = original(*args)
        if args[1]['id'] == 'm1':
            return {**task, 'status': 'unknown'}  # Agent succeeded; POST reply was lost.
        return task
    monkeypatch.setattr(service, '_publish', publish)
    identity = created(env)
    service.advance(repository, manager, 'default', identity)
    first_id = uploads[0][1]
    service.retry(repository, manager, 'default', identity, {'action': 'reconcile'})
    service.advance(repository, manager, 'default', identity)
    assert ('m1', first_id) in manager.reads
    assert [a for a, _ in uploads] == ['m1', 'm2', 'o1']
    assert service.get(repository, 'default', identity)['state'] == 'succeeded'


@pytest.mark.parametrize('task', [{'status': 'unknown'}, {'status': 'failed', 'pending': True},
                                 {'status': 'failed', 'query_error': True}, {'status': 'failed', 'http_status': 502},
                                 {'http_status': 404}])
def test_retry_never_replaces_an_ambiguous_or_missing_deployment_id(env, monkeypatch, task):
    repository, manager, _, _ = env
    identity = created(env)
    batch = service._read(repository, 'default', identity)
    first = batch['targets'][0]
    first.update(deployment_id='durable-id', state='paused', deployment=task)
    batch['state'] = 'paused'
    service._save(repository, batch)
    manager.tasks[('m1', 'durable-id')] = task
    service.retry(repository, manager, 'default', identity, {'action': 'retry'})
    assert service._read(repository, 'default', identity)['targets'][0]['deployment_id'] == 'durable-id'


@pytest.mark.parametrize('status', ['ready', 'failed', 'succeeded'])
def test_cached_task_status_with_failed_query_cannot_be_superseded_or_activated(env, status):
    repository, manager, _, _ = env
    identity = created(env)
    batch = service._read(repository, 'default', identity)
    task = {'status': status, 'pending': True}
    batch.update(state='paused')
    batch['targets'][0].update(state='paused', deployment_id='pending-id', deployment=task)
    service._save(repository, batch)
    manager.tasks[('m1', 'pending-id')] = task
    assert not service.get(repository, 'default', identity)['actions']['activate_agent_ids']
    with pytest.raises(ProfileInUse, match='已有设备组批次'):
        service.create(repository, manager, 'default', payload(repository))
    with pytest.raises(ProfileInUse, match='只能激活'):
        service.retry(repository, manager, 'default', identity, {'action': 'activate', 'agent_id': 'm1'})
    assert not manager.activations


def test_confirmed_failed_attempt_gets_new_id_only_after_old_id_is_queried(env):
    repository, manager, _, _ = env
    identity = created(env)
    batch = service._read(repository, 'default', identity)
    batch['state'] = 'paused'
    batch['targets'][0].update(deployment_id='old-id', state='failed', deployment={'status': 'failed'})
    service._save(repository, batch)
    manager.tasks[('m1', 'old-id')] = {'status': 'failed'}
    service.retry(repository, manager, 'default', identity, {'action': 'retry'})
    assert manager.reads == [('m1', 'old-id')]
    service.advance(repository, manager, 'default', identity)
    assert service._read(repository, 'default', identity)['targets'][0]['deployment_id'] != 'old-id'


def test_ready_activation_uses_same_id_and_then_resumes_ordered_batch(env):
    repository, manager, _, _ = env
    identity = created(env)
    batch = service._read(repository, 'default', identity)
    batch['state'] = 'paused'
    batch['targets'][0].update(deployment_id='ready-id', state='paused', deployment={'status': 'ready'})
    service._save(repository, batch)
    manager.tasks[('m1', 'ready-id')] = {'status': 'ready'}
    service.retry(repository, manager, 'default', identity, {'action': 'activate', 'agent_id': 'm1'})
    assert manager.activations == [('m1', 'ready-id')]
    assert manager.pinned_activations == [('m1', batch['targets'][0]['target_fingerprint'])]
    service.advance(repository, manager, 'default', identity)
    assert service.get(repository, 'default', identity)['state'] == 'succeeded'


def test_running_batch_duplicate_create_and_recovery_return_without_waiting_for_worker_lock(env, monkeypatch):
    repository, manager, _, _ = env
    body = payload(repository)
    service.create(repository, manager, 'default', body)
    def locked(*_):
        pytest.fail('running worker lock must not be acquired by a duplicate or rejected action')
    monkeypatch.setattr(service, '_lock', locked)
    assert service.create(repository, manager, 'default', body)['batch_id'] == body['batch_id']
    with pytest.raises(ProfileInUse, match='已有设备组批次'):
        service.create(repository, manager, 'default', payload(repository))
    with pytest.raises(ProfileInUse, match='仍在执行'):
        service.retry(repository, manager, 'default', body['batch_id'], {'action': 'retry'})


@pytest.mark.parametrize('version', ['v1.19.0', 'alpha-unknown', None])
def test_preflight_checks_every_mihomo_version_before_first_upload(env, monkeypatch, version):
    repository, manager, _, uploads = env
    monkeypatch.setattr(device_group_versions.mihomo_panel, 'call',
                        lambda _m, identity, *_: {'data': {'version': version if identity == 'm2' else 'v1.19.32'}})
    identity = created(env)
    service.advance(repository, manager, 'default', identity)
    assert not uploads
    assert service.get(repository, 'default', identity)['state'] == 'paused'


@pytest.mark.parametrize('shape', ['default_only', 'follow', 'empty'])
def test_groups_without_generated_segments_do_not_require_new_mihomo_syntax(env, monkeypatch, shape):
    repository, manager, _, uploads = env
    def change(profile):
        rows = profile['device_groups']['groups']
        group = next(row for row in rows if row['id'] == 'dg_aaaaaa')
        if shape == 'default_only':
            profile['device_groups']['groups'] = [row for row in rows if row['id'] == 'default']
        elif shape == 'follow':
            group['mode'] = 'follow'
        else:
            group['members'] = []
    repository.update_profile_transaction('default', change)
    monkeypatch.setattr(device_group_versions.mihomo_panel, 'call', lambda *_: pytest.fail('no group syntax to preflight'))
    identity = created(env)
    service.advance(repository, manager, 'default', identity)
    assert service.get(repository, 'default', identity)['state'] == 'succeeded'
    assert [a for a, _ in uploads] == ['m1', 'm2', 'o1']


def test_config_cas_and_target_rebinding_require_a_new_preview(env):
    repository, manager, _, uploads = env
    identity = created(env)
    repository.update_profile_transaction('default', lambda p: p.update(description='changed'))
    service.advance(repository, manager, 'default', identity)
    result = service.get(repository, 'default', identity)
    assert result['actions']['requires_new_preview'] and not uploads
    with pytest.raises(ProfileInUse):
        service.retry(repository, manager, 'default', identity, {'action': 'retry'})


def test_binding_change_before_worker_cannot_switch_its_pinned_target(env):
    repository, manager, _, uploads = env
    identity = created(env)
    repository.update_system_transaction(lambda s: s['agents'][1].update(host='192.0.2.99'))
    service.advance(repository, manager, 'default', identity)
    assert not uploads
    assert service.get(repository, 'default', identity)['actions']['requires_new_preview']


def test_restart_status_restarts_original_batch_and_uses_original_deployment_id(env, monkeypatch):
    repository, manager, starts, uploads = env
    identity = created(env)
    batch = service._read(repository, 'default', identity)
    batch['state'] = 'running'
    batch['targets'][0].update(deployment_id='restart-id', state='running')
    service._save(repository, batch)
    manager.tasks[('m1', 'restart-id')] = {'status': 'succeeded'}
    restarted = Manager(repository, manager.tasks)
    monkeypatch.setattr('backend.common.agent_manager.get_agent_manager', lambda: restarted)
    assert service.get(repository, 'default', identity)['state'] == 'running'
    assert starts[-1] == identity
    service.advance(repository, restarted, 'default', identity)
    assert ('m1', 'restart-id') in restarted.reads
    assert all(a != 'm1' for a, _ in uploads)


def test_active_batch_blocks_dns_follow_even_across_profile_links(env):
    repository, manager, _, _ = env
    repository.create_profile({'id': 'elsewhere', 'name': '其他空间'})
    identity = created(env)
    source = manager.get_agent_by_id('m1')
    target = {'id': 'foreign', 'profile_id': 'elsewhere'}
    assert service.blocks_follow(manager, target, source)
    service.advance(repository, manager, 'default', identity)
    assert not service.blocks_follow(manager, target, source)


def test_inflight_dns_follow_blocks_new_batch_before_any_durable_publication(env):
    repository, manager, starts, _ = env
    repository.update_system_transaction(lambda s: s['agents'][0].update(
        dns_follow={'source_agent_id': 'm1'}, dns_follow_state={'status': 'publishing'}))
    with pytest.raises(ProfileInUse, match='DNS 联动'):
        service.create(repository, manager, 'default', payload(repository))
    assert service.latest(repository, 'default') is None and not starts


@pytest.mark.parametrize('sync', [{'status': 'running'}, {'status': 'pending', 'native_task': 'accepted-task'}])
def test_inflight_rule_sync_blocks_new_batch_before_any_durable_publication(env, sync):
    repository, manager, starts, _ = env
    repository.update_system_transaction(lambda s: s['agents'][0].update(rule_sync={'state': sync}))
    with pytest.raises(ProfileInUse, match='规则自动同步'):
        service.create(repository, manager, 'default', payload(repository))
    assert service.latest(repository, 'default') is None and not starts


@pytest.mark.parametrize('change', [{'base_url': 123}, {'target_ids': [{}, {}]}, {'target_ids': ['m1']},
                                  {'target_ids': ['m1', 'm1', 'o1']}])
def test_invalid_or_incomplete_target_requests_do_not_create_a_batch(env, change):
    repository, manager, starts, _ = env
    body = payload(repository)
    body.update(change)
    with pytest.raises(ProfileValidationError):
        service.create(repository, manager, 'default', body)
    assert not starts


def test_base_url_cannot_change_without_a_matching_preview(env):
    repository, manager, _, _ = env
    body = payload(repository, base_url='https://first.test')
    body['base_url'] = 'https://changed.test'
    with pytest.raises(ProfileInUse, match='预览'):
        service.create(repository, manager, 'default', body)


def test_public_batch_reuses_deployment_projection_and_preserves_private_persisted_state(env):
    repository, manager, _, _ = env
    identity = created(env)
    batch = service._read(repository, 'default', identity)
    message = 'Upload outcome unknown; query deployment status'
    batch.update(state='paused', message=message)
    batch['targets'][0].update(state='paused', message=message, deployment={
        'status': 'unknown', 'message': message,
        'prepared_content': {'private': 'internal-source'}, 'oxidns_validation_receipt': {'private': 'receipt'},
    })
    service._save(repository, batch)
    public = service.get(repository, 'default', identity)
    target = public['targets'][0]
    assert 'prepared_content' not in target['deployment'] and 'oxidns_validation_receipt' not in target['deployment']
    assert all(value.startswith('暂时无法确认上传结果') for value in (
        public['message'], target['message'], target['deployment']['message']))
    stored = service._read(repository, 'default', identity)
    assert stored['targets'][0]['deployment']['oxidns_validation_receipt'] == {'private': 'receipt'}
    assert stored['message'] == message


@pytest.mark.parametrize('extra', [{'pending': True}, {'query_error': True}, {'http_status': 502}])
def test_succeeded_status_with_ambiguous_evidence_never_opens_the_barrier(env, monkeypatch, extra):
    repository, manager, _, uploads = env
    original = service._publish
    monkeypatch.setattr(service, '_publish', lambda *args: {**original(*args), **extra})
    identity = created(env)
    service.advance(repository, manager, 'default', identity)
    assert [a for a, _ in uploads] == ['m1']
    assert service.get(repository, 'default', identity)['state'] == 'paused'


def test_mihomo_status_is_rechecked_at_the_dns_phase_boundary(env, monkeypatch):
    repository, manager, _, uploads = env
    original = service._publish
    def publish(*args):
        result = original(*args)
        if args[1]['id'] == 'm2':
            first = next(key for key in manager.tasks if key[0] == 'm1')
            manager.tasks[first] = {'status': 'unknown', 'message': '回滚结果尚未确认'}
        return result
    monkeypatch.setattr(service, '_publish', publish)
    identity = created(env)
    service.advance(repository, manager, 'default', identity)
    assert [a for a, _ in uploads] == ['m1', 'm2']
    assert service.get(repository, 'default', identity)['state'] == 'paused'


def test_stale_reconcile_only_queries_original_id_and_never_resumes_publication(env):
    repository, manager, starts, uploads = env
    identity = created(env)
    batch = service._read(repository, 'default', identity)
    batch['state'] = 'paused'
    batch['targets'][0].update(deployment_id='old-id', state='paused', deployment={'status': 'unknown'})
    service._save(repository, batch)
    manager.tasks[('m1', 'old-id')] = {'status': 'succeeded'}
    repository.update_profile_transaction('default', lambda p: p.update(description='new draft'))
    starts.clear()
    result = service.retry(repository, manager, 'default', identity, {'action': 'reconcile'})
    assert manager.reads == [('m1', 'old-id')] and not starts and not uploads
    assert result['state'] == 'paused' and result['actions']['requires_new_preview']
    assert result['targets'][0]['deployment_id'] == 'old-id'
    assert result['targets'][0]['state'] == 'succeeded'


def test_stale_reconcile_does_not_query_an_agent_whose_endpoint_changed(env):
    repository, manager, _, uploads = env
    identity = created(env)
    batch = service._read(repository, 'default', identity)
    batch['state'] = 'paused'
    batch['targets'][0].update(deployment_id='old-id', state='paused')
    service._save(repository, batch)
    repository.update_system_transaction(lambda s: next(a for a in s['agents'] if a['id'] == 'm1').update(host='192.0.2.99'))
    result = service.retry(repository, manager, 'default', identity, {'action': 'reconcile'})
    assert result['actions']['requires_new_preview'] and not manager.reads and not uploads


def test_missing_original_deployment_after_crash_is_paused_without_resubmission(env):
    repository, manager, _, uploads = env
    identity = created(env)
    batch = service._read(repository, 'default', identity)
    batch['targets'][0].update(deployment_id='durable-before-reservation', state='running')
    service._save(repository, batch)
    service.advance(repository, manager, 'default', identity)
    result = service.get(repository, 'default', identity)
    assert result['state'] == 'paused' and not uploads
    assert result['targets'][0]['deployment_id'] == 'durable-before-reservation'


def use_real_publication_with_busy_reservation(env, monkeypatch, *, lose_post=False):
    repository, manager, _, uploads = env
    reservations, prepared = [], []
    manager.tasks[('m1', 'other-ready-id')] = {'status': 'ready'}
    def begin(identity, deployment_id):
        reservations.append((identity, deployment_id))
        if identity == 'm1' and manager.tasks[('m1', 'other-ready-id')]['status'] == 'ready':
            return {'success': False, 'status': 'ready', 'deployment_id': 'other-ready-id', 'http_status': 409,
                    'message': 'Agent already has an unfinished deployment'}
        manager.tasks[(identity, deployment_id)] = {'status': 'preparing', 'success': True}
        return manager.tasks[(identity, deployment_id)]
    def prepare(agent, _config, **kwargs):
        batch = service._latest_raw(repository, 'default')
        target = next(row for row in batch['targets'] if row['agent_id'] == agent['id'])
        assert not target.get('reservation_rejected')  # Durable before downloading/generating.
        prepared.append((agent['id'], kwargs['deployment_id']))
        return {'prepared_providers': [], 'bundle': SimpleNamespace(deployment_id=kwargs['deployment_id'])}
    def publish(identity, bundle, **_kwargs):
        uploads.append((identity, bundle.deployment_id))
        result = {'status': 'succeeded', 'success': True, 'deployment_id': bundle.deployment_id}
        if lose_post and identity == 'm1':
            result.update(status='unknown', pending=True)
        manager.tasks[uploads[-1]] = result
        return result
    manager.begin_deployment = begin
    manager.finish_deployment_preparation = lambda *_: None
    manager.publish_deployment = publish
    manager.record_deployment = lambda *_args, **_kwargs: None
    monkeypatch.setattr('backend.routes.agents._prepare_transactional_content', prepare)
    monkeypatch.setattr(service, '_publish', REAL_PUBLISH)
    return reservations, prepared


def test_known_reservation_rejection_retries_the_same_candidate_after_old_task_finishes(env, monkeypatch):
    repository, manager, _, uploads = env
    reservations, prepared = use_real_publication_with_busy_reservation(env, monkeypatch)
    identity = created(env)
    service.advance(repository, manager, 'default', identity)
    stored = service._read(repository, 'default', identity)
    candidate_id = stored['targets'][0]['deployment_id']
    assert stored['state'] == 'paused' and stored['targets'][0]['reservation_rejected']
    assert not uploads and not prepared and candidate_id != 'other-ready-id'
    assert 'reservation_rejected' not in service.get(repository, 'default', identity)['targets'][0]
    manager.tasks[('m1', 'other-ready-id')]['status'] = 'succeeded'
    service.retry(repository, manager, 'default', identity, {'action': 'retry'})
    service.advance(repository, manager, 'default', identity)
    assert reservations[:2] == [('m1', candidate_id), ('m1', candidate_id)]
    assert [a for a, _ in uploads] == ['m1', 'm2', 'o1']
    assert service.get(repository, 'default', identity)['state'] == 'succeeded'


def test_new_preview_can_replace_only_a_proven_unsubmitted_reservation_rejection(env, monkeypatch):
    repository, manager, _, uploads = env
    use_real_publication_with_busy_reservation(env, monkeypatch)
    identity = created(env)
    service.advance(repository, manager, 'default', identity)
    repository.update_profile_transaction('default', lambda p: p.update(description='new draft'))
    with pytest.raises(ProfileInUse):
        service.retry(repository, manager, 'default', identity, {'action': 'retry'})
    new_batch = service.create(repository, manager, 'default', payload(repository))
    assert new_batch['batch_id'] != identity and not uploads
    assert service.get(repository, 'default', identity)['state'] == 'failed'


def test_retry_proof_is_cleared_before_post_and_a_lost_receipt_never_repeats_reservation(env, monkeypatch):
    repository, manager, _, uploads = env
    reservations, _ = use_real_publication_with_busy_reservation(env, monkeypatch, lose_post=True)
    identity = created(env)
    service.advance(repository, manager, 'default', identity)
    candidate_id = service._read(repository, 'default', identity)['targets'][0]['deployment_id']
    manager.tasks[('m1', 'other-ready-id')]['status'] = 'succeeded'
    service.retry(repository, manager, 'default', identity, {'action': 'retry'})
    service.advance(repository, manager, 'default', identity)
    assert uploads == [('m1', candidate_id)]
    assert not service._read(repository, 'default', identity)['targets'][0].get('reservation_rejected')
    # Even losing the local journal cannot revive proof from the earlier rejection.
    manager.tasks.pop(('m1', candidate_id))
    service.retry(repository, manager, 'default', identity, {'action': 'retry'})
    service.advance(repository, manager, 'default', identity)
    assert uploads == [('m1', candidate_id)] and reservations == [('m1', candidate_id), ('m1', candidate_id)]
    repository.update_profile_transaction('default', lambda p: p.update(description='another draft'))
    with pytest.raises(ProfileInUse, match='已有设备组批次'):
        service.create(repository, manager, 'default', payload(repository))


def test_no_batch_keeps_legacy_dns_follow_unblocked(env):
    _, manager, _, _ = env
    assert service.latest(manager.repository, 'default') is None
    assert not service.blocks_follow(manager, manager.get_agent_by_id('o1'), manager.get_agent_by_id('m1'))


@pytest.mark.parametrize('change_during_prepare', [False, True])
def test_real_prepare_boundary_rechecks_cas_and_passes_expected_target_fingerprint(env, monkeypatch, change_during_prepare):
    repository, manager, _, _ = env
    identity = created(env)
    batch = service._read(repository, 'default', identity)
    target = batch['targets'][0]
    target['deployment_id'] = 'prepared-id'
    config, _ = repository.get_publish_snapshot('default')
    calls, finished = [], []
    manager.begin_deployment = lambda *_: {'success': True}
    manager.finish_deployment_preparation = lambda *args: finished.append(args)
    manager.record_deployment = lambda *args, **kwargs: calls.append(('record', args, kwargs))
    manager.publish_deployment = lambda *args, **kwargs: calls.append(('upload', args, kwargs)) or {'status': 'succeeded'}
    def prepare(*_args, **_kwargs):
        if change_during_prepare:
            repository.update_profile_transaction('default', lambda p: p.update(description='changed during preparation'))
        return {'prepared_providers': [], 'bundle': SimpleNamespace()}
    monkeypatch.setattr('backend.routes.agents._prepare_transactional_content', prepare)
    if change_during_prepare:
        with pytest.raises(ProfileInUse):
            REAL_PUBLISH(manager, manager.get_agent_by_id('m1'), config, batch, target)
        assert all(row[0] != 'upload' for row in calls)
        assert calls[0][2]['only_unsubmitted'] is True
    else:
        REAL_PUBLISH(manager, manager.get_agent_by_id('m1'), config, batch, target)
        assert calls[0][2]['expected_target_fingerprint'] == target['target_fingerprint']
    assert finished == [('m1', 'prepared-id')]
