"""Background tasks release request snapshots and completed result payloads."""
import os
from pathlib import Path
import subprocess
import sys
import threading
import weakref

import pytest

from backend.common import config as config_module
from backend.utils import domain_discovery_service as service


class Payload(dict):
    pass


@pytest.fixture(autouse=True)
def isolated_jobs(monkeypatch):
    monkeypatch.setattr(service, '_JOBS', {})
    config_module.reset_config_context()
    yield
    config_module.reset_config_context()


def fill_context():
    config_module._CONFIG_CACHE.set({'default': Payload(items=list(range(30)))})
    config_module._CONFIG_BASELINES.set({'default': Payload(items=list(range(30)))})


def test_synchronous_result_is_returned_without_retention_in_job_history():
    refs = []

    def run(progress):
        fill_context()
        results = Payload(host={'direct': {'delays': [1, 2, 3]}})
        refs.append(weakref.ref(results))
        progress(1, 1)
        return {'results': results, 'errors': [{'message': 'partial failure'}]}

    result = service._start_job('default', 'probe', run, wait=True)
    assert result['results']['host']['direct']['delays'] == [1, 2, 3]
    saved = service.get_job(result['id'])
    assert saved['status'] == 'done' and saved['done'] == saved['total'] == 1
    assert saved['errors'] == [{'message': 'partial failure'}]
    assert saved['finished_at'] and 'results' not in saved
    assert service.public_job(result) == service.public_job(saved)
    assert config_module._CONFIG_CACHE.get() is None
    assert config_module._CONFIG_BASELINES.get() is None
    del result
    assert refs[0]() is None


def test_background_job_remains_exclusive_and_releases_results(monkeypatch):
    started, release = threading.Event(), threading.Event()
    workers, refs = [], []
    original_thread = threading.Thread

    def thread_factory(**kwargs):
        worker = original_thread(**kwargs)
        workers.append(worker)
        return worker

    monkeypatch.setattr(service.threading, 'Thread', thread_factory)

    def run(progress):
        fill_context()
        results = Payload(host={'verdict': 'both_ok'})
        refs.append(weakref.ref(results))
        progress(0, 1)
        started.set()
        assert release.wait(3)
        progress(1, 1)
        return {'results': results}

    job = service._start_job('default', 'probe', run)
    try:
        assert started.wait(3)
        with pytest.raises(service.ProbeBusy) as error:
            service._start_job('other', 'region', lambda progress: {})
        assert error.value.job_id == job['id']
        assert service.public_job(service.get_job(job['id']))['status'] == 'running'
    finally:
        release.set()
        for worker in workers:
            worker.join(3)
            assert not worker.is_alive()
    assert service.public_job(service.get_job(job['id']))['status'] == 'done'
    assert 'results' not in service.get_job(job['id'])
    assert refs[0]() is None


@pytest.mark.parametrize('error', [service.ProbeError('expected failure'), RuntimeError('internal failure')])
def test_failed_job_releases_context_and_preserves_public_failure(error):
    def run(progress):
        fill_context()
        raise error

    with pytest.raises(service.ProbeError):
        service._start_job('default', 'probe', run, wait=True)
    saved = next(iter(service._JOBS.values()))
    assert saved['status'] == 'failed' and saved['finished_at']
    assert saved['message'] == ('expected failure' if isinstance(error, service.ProbeError)
                                else '执行失败，请查看服务端日志')
    assert 'results' not in service.public_job(saved)
    assert config_module._CONFIG_CACHE.get() is None
    assert config_module._CONFIG_BASELINES.get() is None


@pytest.mark.parametrize('error', [None, service.ProbeBusy('another'), RuntimeError('round failed')])
def test_auto_loop_clears_context_after_every_profile_before_sleep(tmp_path, monkeypatch, error):
    import time

    class StopLoop(Exception):
        pass

    targets, visited = [], []

    class CapturedThread:
        def __init__(self, *, target, **kwargs):
            targets.append(target)

        def start(self):
            pass

    class Repository:
        def list_profiles(self):
            return [{'id': 'first'}, {'id': 'second'}]

    def run(profile_id):
        assert config_module._CONFIG_CACHE.get() is None
        assert config_module._CONFIG_BASELINES.get() is None
        visited.append(profile_id)
        fill_context()
        if error:
            raise error
        return {'probed': 0, 'applied': 0, 'rechecked': 0}

    def sleep(seconds):
        if seconds == service.AUTO_INTERVAL_SECONDS:
            assert visited == ['first', 'second']
            assert config_module._CONFIG_CACHE.get() is None
            assert config_module._CONFIG_BASELINES.get() is None
            raise StopLoop

    monkeypatch.setattr(service, '_AUTO_STARTED', False)
    monkeypatch.setattr(service.threading, 'Thread', CapturedThread)
    monkeypatch.setattr(config_module, 'get_repository', lambda: Repository())
    monkeypatch.setattr(service, 'run_auto_task', run)
    monkeypatch.setattr(time, 'sleep', sleep)
    service.start_auto_loop(tmp_path)
    with pytest.raises(StopLoop):
        targets[0]()


def test_application_startup_discards_only_temporary_views(tmp_path):
    # Exercise the real import/initialization in a fresh process; all writes go
    # to this test's temporary directory, including MetricsHistory's fallback.
    code = '''
import os
from backend.agents import manager
from backend.agents.metrics_history import MetricsHistory
manager.MetricsHistory = lambda: MetricsHistory(os.path.join(os.environ['DATA_DIR'], 'metrics'))
from backend import app
from backend.common import config
assert config._CONFIG_CACHE.get() is None
assert config._CONFIG_BASELINES.get() is None
assert config.get_repository().get_profile('default')['id'] == 'default'
response = app.app.test_client().get('/api/profiles')
assert response.status_code == 200
assert config._CONFIG_CACHE.get() is None
assert config._CONFIG_BASELINES.get() is None
'''
    env = dict(os.environ, DATA_DIR=str(tmp_path), LOG_FILE=str(tmp_path / 'app.log'),
               ADMIN_USERNAME='', ADMIN_PASSWORD='', PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).resolve().parents[1],
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
