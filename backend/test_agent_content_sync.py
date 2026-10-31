"""Content status is bounded, snapshot-based and never downloads in list requests."""
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
import json
import threading

import pytest

from backend.agents.content_sync import ContentSyncChecker
from backend.agents.deployment_bundle import build_deployment_bundle, deployment_content_fingerprint


class DeferredExecutor:
    def __init__(self):
        self.jobs = []

    def submit(self, function, *args):
        future = Future()
        self.jobs.append((future, function, args))
        return future

    def run(self, index=0):
        future, function, args = self.jobs.pop(index)
        future.set_running_or_notify_cancel()
        try:
            result = function(*args)
        except BaseException as error:
            future.set_exception(error)
            raise
        future.set_result(result)


@pytest.fixture
def setup():
    executor = DeferredExecutor()
    now = [100.0]
    checker = ContentSyncChecker(executor=executor, clock=lambda: now[0])
    config = {'profile_id': 'default', 'system_config': {'server_domain': 'https://config.example.test'},
              'test_main': 'mode: rule\nrules: []\n', 'test_rule': 'DOMAIN,example.test\n'}
    agent = {'id': 'agent-test', 'profile_id': 'default', 'service_type': 'mihomo', 'config_version': 'applied-config'}
    calls = []

    def bundle_for(item, snapshot, *, base_url='', deployment_id='previous'):
        return build_deployment_bundle(item, snapshot, snapshot['test_main'],
                                       custom_files=[{'path': 'rules/example.list', 'content': snapshot['test_rule']}],
                                       base_url=base_url, deployment_id=deployment_id)

    previous = bundle_for(agent, config)
    agent['applied_content'] = {
        'fingerprint': deployment_content_fingerprint(previous.manifest), 'profile_id': 'default',
        'service_type': 'mihomo', 'config_version': 'applied-config', 'deployment_id': 'previous',
        'base_url': 'https://config.example.test', 'config_path': 'config.yaml',
    }

    def prepare(item, snapshot, **kwargs):
        calls.append((deepcopy(item), deepcopy(snapshot), kwargs))
        return bundle_for(item, snapshot, **kwargs)

    return checker, executor, now, agent, config, prepare, calls


def query(setup, **kwargs):
    checker, _, _, agent, config, prepare, _ = setup
    return checker.get_content_sync(agent, config, 'https://config.example.test', prepare, **kwargs)


def test_list_only_schedules_and_repeated_polls_share_one_preparation(setup):
    _, executor, _, _, _, _, calls = setup
    for _ in range(20):
        assert query(setup) == {'status': 'checking'}
    assert calls == [] and len(executor.jobs) == 1
    executor.run()
    result = query(setup)
    assert result['status'] == 'synced' and result['checked_at'].endswith('Z')
    for _ in range(20):
        assert query(setup) == result
    assert len(calls) == 1 and executor.jobs == []
    result['status'] = 'changed'
    assert query(setup)['status'] == 'synced'


def test_only_rule_file_change_is_detected_with_unchanged_main_config(setup):
    _, executor, _, _, config, _, _ = setup
    query(setup)
    executor.run()
    config['test_rule'] += 'DOMAIN,other.example.test\n'
    assert query(setup)['status'] == 'checking'
    executor.run()
    assert query(setup)['status'] == 'changed'


def test_runtime_metadata_and_heartbeat_do_not_invalidate_cache(setup):
    _, executor, _, agent, config, _, calls = setup
    query(setup)
    executor.run()
    agent.update(last_heartbeat='later', cpu=90, version='new-agent')
    config.update(_revision=1234, updated_at='later', created_at='different', agents=[agent],
                  name='renamed', description='updated', backup={'last_run': 'later'})
    assert query(setup)['status'] == 'synced'
    assert len(calls) == 1 and executor.jobs == []


def test_nested_config_fields_are_not_silently_removed_from_cache_key(setup):
    _, executor, _, _, config, _, _ = setup
    config['test_plugin'] = {'created_at': 'first'}
    query(setup)
    executor.run()
    config['test_plugin']['created_at'] = 'second'
    assert query(setup)['status'] == 'checking'
    assert len(executor.jobs) == 1


def test_remote_changes_are_rechecked_only_after_success_ttl(setup):
    _, executor, now, _, _, _, calls = setup
    query(setup)
    executor.run()
    now[0] += 899
    assert query(setup)['status'] == 'synced'
    now[0] += 1
    assert query(setup)['status'] == 'checking'
    assert len(calls) == 1 and len(executor.jobs) == 1
    executor.run()
    assert len(calls) == 2


def test_changed_input_never_publishes_old_worker_result(setup):
    _, executor, _, _, config, _, calls = setup
    query(setup)
    config['test_rule'] = 'DOMAIN,new.example.test\n'
    assert query(setup)['status'] == 'checking'
    assert len(executor.jobs) == 1
    executor.run()
    assert calls[0][1]['test_rule'] == 'DOMAIN,example.test\n'
    assert query(setup)['status'] == 'checking'
    assert len(executor.jobs) == 1
    executor.run()
    assert query(setup)['status'] == 'changed'


def test_new_successful_baseline_invalidates_in_flight_comparison(setup):
    _, executor, _, agent, _, _, _ = setup
    query(setup)
    agent['config_version'] = agent['applied_content']['config_version'] = 'new-success'
    agent['applied_content']['deployment_id'] = 'new-success-id'
    query(setup)
    executor.run()
    assert query(setup)['status'] == 'checking'
    executor.run()
    assert query(setup)['status'] == 'synced'


def test_returning_to_original_input_still_discards_obsolete_worker(setup):
    _, executor, _, _, config, _, _ = setup
    original = config['test_rule']
    query(setup)
    config['test_rule'] = 'DOMAIN,temporary.example.test\n'
    query(setup)
    config['test_rule'] = original
    query(setup)
    executor.run()
    assert query(setup)['status'] == 'checking'
    executor.run()
    assert query(setup)['status'] == 'synced'


def test_generation_system_settings_and_base_url_invalidate_cached_result(setup):
    checker, executor, _, agent, config, prepare, _ = setup
    query(setup)
    executor.run()
    config['system_config']['server_domain'] = 'https://new.example.test'
    assert query(setup)['status'] == 'checking'
    executor.run()
    assert query(setup)['status'] == 'synced'
    assert checker.get_content_sync(agent, config, 'https://another.example.test', prepare)['status'] == 'checking'


@pytest.mark.parametrize('status', ['ready', 'preparing', 'uploading', 'checking', 'unknown', 'rollback_failed'])
def test_incomplete_deployment_invalidates_in_flight_success(setup, status):
    _, executor, _, agent, _, _, _ = setup
    query(setup)
    agent['latest_deployment'] = {'status': status}
    assert query(setup) == {'status': 'unknown', 'reason': 'deployment_pending'}
    executor.run()
    assert query(setup)['status'] == 'unknown'
    agent['latest_deployment'] = {'status': 'rolled_back'}
    assert query(setup)['status'] == 'checking'


@pytest.mark.parametrize('status', ['failed', 'rolled_back'])
def test_failed_deployment_can_still_compare_confirmed_prior_baseline(setup, status):
    _, executor, _, agent, _, _, _ = setup
    agent['latest_deployment'] = {'status': status}
    query(setup)
    executor.run()
    assert query(setup)['status'] == 'synced'


@pytest.mark.parametrize('fingerprint', ['', 'a' * 64, 'v2:' + 'a' * 64, 'v1:' + 'a' * 63, 'v1:' + 'A' * 64])
def test_invalid_baseline_fingerprint_never_starts_preparation(setup, fingerprint):
    _, executor, _, agent, _, _, _ = setup
    agent['applied_content']['fingerprint'] = fingerprint
    assert query(setup) == {'status': 'unknown', 'reason': 'baseline_invalid'}
    assert executor.jobs == []


@pytest.mark.parametrize('change, reason', [
    ({'profile_id': 'other'}, 'profile_mismatch'),
    ({'service_type': 'mosdns'}, 'service_mismatch'),
    ({'config_version': 'different'}, 'version_mismatch'),
])
def test_binding_service_or_reported_version_mismatch_is_unknown(setup, change, reason):
    _, executor, _, agent, _, _, _ = setup
    agent.update(change)
    assert query(setup) == {'status': 'unknown', 'reason': reason}
    assert executor.jobs == []


def test_missing_baseline_is_not_backfilled_from_current_config(setup):
    _, executor, _, agent, _, _, _ = setup
    del agent['applied_content']
    assert query(setup) == {'status': 'unknown', 'reason': 'baseline_missing'}
    agent['config_version'] = '0'
    agent['deployments'] = {'previous': {'status': 'succeeded'}}
    assert query(setup)['reason'] == 'baseline_missing'
    agent['deployments'] = {'previous': {'status': 'failed'}}
    assert query(setup) == {'status': 'never-pushed'}
    assert executor.jobs == []


def test_error_is_secret_free_and_uses_short_ttl(setup):
    checker, executor, now, agent, config, _, _ = setup
    secret = 'https://user:password@private.example.test/?token=secret'

    def fail(*_args, **_kwargs):
        raise ValueError(secret)

    def get():
        return checker.get_content_sync(agent, config, '', fail)

    assert get()['status'] == 'checking'
    executor.run()
    result = get()
    assert result == {'status': 'unknown', 'reason': 'check_failed'}
    assert secret not in json.dumps(result)
    now[0] += 29
    assert get() == result and executor.jobs == []
    now[0] += 1
    assert get()['status'] == 'checking' and len(executor.jobs) == 1


def test_workers_are_bounded_without_queued_jobs(setup):
    checker, executor, _, agent, config, prepare, _ = setup
    for index in range(30):
        item = {**agent, 'id': f'agent-{index}'}
        assert checker.get_content_sync(item, config, '', prepare)['status'] == 'checking'
    assert len(executor.jobs) == 2
    executor.run()
    item = {**agent, 'id': 'agent-29'}
    assert checker.get_content_sync(item, config, '', prepare)['status'] == 'checking'
    assert len(executor.jobs) == 2


def test_repository_scope_isolates_identical_agent_ids(setup):
    _, executor, _, _, _, _, _ = setup
    assert query(setup, scope='repository-one')['status'] == 'checking'
    assert query(setup, scope='repository-two')['status'] == 'checking'
    assert len(executor.jobs) == 2
    executor.run()
    assert query(setup, scope='repository-one')['status'] == 'synced'
    assert query(setup, scope='repository-two')['status'] == 'checking'


def test_repository_change_before_worker_starts_skips_preparation(setup, monkeypatch):
    from backend.common import config as config_module
    _, executor, _, _, _, _, calls = setup
    original, replacement = object(), object()
    monkeypatch.setattr(config_module, '_repository', original)
    assert query(setup, repository=original)['status'] == 'checking'
    monkeypatch.setattr(config_module, '_repository', replacement)
    executor.run()
    assert calls == []
    # Restore the request's repository to inspect its rejected worker result.
    monkeypatch.setattr(config_module, '_repository', original)
    assert query(setup, repository=original) == {'status': 'unknown', 'reason': 'check_failed'}


def test_repository_change_during_preparation_never_publishes_success(setup, monkeypatch):
    from backend.common import config as config_module
    checker, executor, _, agent, config, prepare, _ = setup
    original, replacement = object(), object()
    monkeypatch.setattr(config_module, '_repository', original)

    def switching_prepare(*args, **kwargs):
        bundle = prepare(*args, **kwargs)
        monkeypatch.setattr(config_module, '_repository', replacement)
        return bundle

    assert checker.get_content_sync(agent, config, '', switching_prepare, repository=original)['status'] == 'checking'
    executor.run()
    monkeypatch.setattr(config_module, '_repository', original)
    assert checker.get_content_sync(agent, config, '', switching_prepare, repository=original) == {
        'status': 'unknown', 'reason': 'check_failed',
    }


def test_same_scope_does_not_reuse_another_repository_object_result(setup, monkeypatch):
    from backend.common import config as config_module
    _, executor, _, _, _, _, calls = setup
    monkeypatch.setattr(config_module, '_repository', object())
    query(setup, scope='same-directory')
    executor.run()
    assert query(setup, scope='same-directory')['status'] == 'synced'
    monkeypatch.setattr(config_module, '_repository', object())
    assert query(setup, scope='same-directory')['status'] == 'checking'
    assert len(calls) == 1
    executor.run()
    assert query(setup, scope='same-directory')['status'] == 'synced'
    assert len(calls) == 2


def test_uninitialized_repository_is_not_created_for_standalone_checker(setup, monkeypatch):
    from backend.common import config as config_module
    _, executor, _, _, _, _, _ = setup
    monkeypatch.setattr(config_module, '_repository', None)
    monkeypatch.setattr(config_module, 'get_repository',
                        lambda: pytest.fail('content checker must not initialize a repository'))
    assert query(setup)['status'] == 'checking'
    executor.run()
    assert query(setup)['status'] == 'synced'


def test_stale_request_repository_is_rejected_before_scheduling(setup, monkeypatch):
    from backend.common import config as config_module
    _, executor, _, _, _, _, _ = setup
    monkeypatch.setattr(config_module, '_repository', object())
    assert query(setup, repository=object()) == {'status': 'unknown', 'reason': 'check_failed'}
    assert executor.jobs == []


def test_custom_installed_filename_has_same_content_fingerprint(setup):
    _, executor, _, agent, _, _, _ = setup
    agent['applied_content']['config_path'] = 'custom.conf'
    query(setup)
    executor.run()
    assert query(setup)['status'] == 'synced'


def test_worker_resets_reused_config_context_before_and_after(setup, monkeypatch):
    checker, executor, _, agent, config, prepare, _ = setup
    from backend.common import config as config_module
    resets = []
    monkeypatch.setattr(config_module, 'reset_config_context', lambda: resets.append('reset'))

    def checked_prepare(*args, **kwargs):
        assert resets == ['reset']
        return prepare(*args, **kwargs)

    checker.get_content_sync(agent, config, '', checked_prepare)
    assert resets == []
    executor.run()
    assert resets == ['reset', 'reset']


def test_system_exit_is_not_converted_to_unknown_and_releases_slot(setup):
    checker, executor, _, agent, config, prepare, _ = setup

    def stop(*_args, **_kwargs):
        raise SystemExit('stop')

    checker.get_content_sync(agent, config, '', stop)
    with pytest.raises(SystemExit):
        executor.run()
    assert checker.get_content_sync(agent, config, '', prepare)['status'] == 'checking'
    assert len(executor.jobs) == 1


def test_rejected_executor_submission_does_not_leak_error_or_slot(setup):
    _, _, now, agent, config, prepare, _ = setup

    class RejectOnce(DeferredExecutor):
        rejected = False

        def submit(self, *args):
            if not self.rejected:
                self.rejected = True
                raise RuntimeError('private credentials')
            return super().submit(*args)

    executor = RejectOnce()
    checker = ContentSyncChecker(executor=executor, clock=lambda: now[0], max_workers=1)
    assert checker.get_content_sync(agent, config, '', prepare) == {'status': 'unknown', 'reason': 'check_failed'}
    now[0] += 30
    assert checker.get_content_sync(agent, config, '', prepare)['status'] == 'checking'
    executor.run()
    assert checker.get_content_sync(agent, config, '', prepare)['status'] == 'synced'


def test_real_executor_runs_preparation_off_request_thread(setup):
    _, _, _, agent, config, prepare, _ = setup
    started, release = threading.Event(), threading.Event()
    request_thread = threading.get_ident()
    worker_threads = []

    def slow_prepare(*args, **kwargs):
        worker_threads.append(threading.get_ident())
        started.set()
        assert release.wait(5)
        return prepare(*args, **kwargs)

    with ThreadPoolExecutor(max_workers=2) as executor:
        checker = ContentSyncChecker(executor=executor)
        try:
            assert checker.get_content_sync(agent, config, '', slow_prepare)['status'] == 'checking'
            assert started.wait(5)
            for _ in range(20):
                assert checker.get_content_sync(agent, config, '', slow_prepare)['status'] == 'checking'
            assert worker_threads == [worker_threads[0]] and worker_threads[0] != request_thread
        finally:
            release.set()


def test_concurrent_list_requests_schedule_one_job(setup):
    _, executor, _, _, _, _, _ = setup
    with ThreadPoolExecutor(max_workers=8) as callers:
        results = list(callers.map(lambda _: query(setup), range(32)))
    assert results == [{'status': 'checking'}] * 32
    assert len(executor.jobs) == 1
    executor.run()
    assert query(setup)['status'] == 'synced'


def compare(setup, **kwargs):
    checker, _, _, agent, config, prepare, _ = setup
    return checker.compare_now(agent, config, 'https://config.example.test', prepare, **kwargs)


def test_compare_now_answers_in_the_callers_thread_and_shares_the_cache(setup):
    _, executor, _, _, config, _, calls = setup
    result = compare(setup)
    assert result['status'] == 'synced' and len(calls) == 1 and executor.jobs == []
    # The list then reads the same answer without scheduling a worker.
    assert query(setup) == result and executor.jobs == []
    config['test_rule'] += 'DOMAIN,other.example.test\n'
    assert compare(setup)['status'] == 'changed' and len(calls) == 2
    assert query(setup)['status'] == 'changed' and executor.jobs == []


def test_compare_now_reuses_a_fresh_list_result_until_it_expires(setup):
    _, executor, now, _, _, _, calls = setup
    query(setup)
    executor.run()
    assert compare(setup)['status'] == 'synced' and len(calls) == 1
    now[0] += 900
    assert compare(setup)['status'] == 'synced' and len(calls) == 2


def test_compare_now_metadata_alone_is_still_synced(setup):
    _, _, _, _, config, _, calls = setup
    assert compare(setup)['status'] == 'synced'
    # A profile's revision and timestamps are not inputs to the delivered files.
    config.update(_revision=99, updated_at='later')
    assert compare(setup)['status'] == 'synced' and len(calls) == 1


@pytest.mark.parametrize('change, reason', [
    ({'applied_content': None, 'config_version': 'applied-config'}, 'baseline_missing'),
    ({'latest_deployment': {'deployment_id': 'next', 'status': 'uploading'}}, 'deployment_pending'),
])
def test_compare_now_never_claims_a_match_without_a_confirmed_baseline(setup, change, reason):
    _, _, _, agent, _, _, calls = setup
    agent.update(change)
    assert compare(setup) == {'status': 'unknown', 'reason': reason}
    assert calls == []


def test_compare_now_preparation_failure_is_secret_free_unknown(setup):
    checker, _, _, agent, config, _, _ = setup
    def failing(*_args, **_kwargs):
        raise ValueError('token=private-secret https://user:pass@example.test')
    assert checker.compare_now(agent, config, '', failing) == {'status': 'unknown', 'reason': 'check_failed'}


def test_compare_now_rejects_another_repository(setup, monkeypatch):
    from backend.common import config as config_module
    _, _, _, _, _, _, calls = setup
    monkeypatch.setattr(config_module, '_repository', object())
    assert compare(setup, repository=object()) == {'status': 'unknown', 'reason': 'check_failed'}
    assert calls == []
