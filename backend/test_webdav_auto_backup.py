"""Automatic backups follow successful configuration commits, never live devices."""
import json
import sys
import threading
import types
from pathlib import Path

import pytest

from backend.common.config_repository import ProfileRepository, ProfileValidationError
from backend.utils import webdav_backup


def wait_idle(worker):
    with worker._condition:
        thread = worker._thread
    if thread is not None:
        thread.join(3)
        assert not thread.is_alive(), 'Backup worker must finish without retrying forever'


@pytest.fixture
def dav(tmp_path, monkeypatch):
    uploaded = []
    completed = threading.Event()

    class FakeClient:
        def __init__(self, options):
            assert options['webdav_timeout'] == 30

        def check(self, path):
            return True

        def upload_sync(self, *, remote_path, local_path):
            uploaded.append((remote_path, json.loads(Path(local_path).read_text()), local_path))
            completed.set()

    package = types.ModuleType('webdav3')
    clients = types.ModuleType('webdav3.client')
    clients.Client = FakeClient
    monkeypatch.setitem(sys.modules, 'webdav3', package)
    monkeypatch.setitem(sys.modules, 'webdav3.client', clients)
    worker = webdav_backup._AutoBackupWorker(delay=0.04)
    monkeypatch.setattr(webdav_backup, '_worker', worker)
    repository = ProfileRepository(tmp_path)
    settings = {'webdav_url': 'https://dav.test', 'webdav_username': 'user',
                'webdav_password': 'secret', 'webdav_path': '/backups/', 'auto_backup': True}
    yield repository, settings, uploaded, completed, clients, worker
    wait_idle(worker)


def test_enabled_auto_backup_uploads_the_complete_committed_document(dav):
    repository, settings, uploaded, completed, _, worker = dav
    repository.update_system_transaction(lambda system: system.update(backup=settings))
    repository.save_profile('default', {'mihomo': {'custom_config': 'mixed-port: 7891'}})
    assert completed.wait(3), 'Enabling auto_backup must back up a saved configuration'
    wait_idle(worker)
    assert len(uploaded) == 1
    assert uploaded[-1][1] == repository.export_all()
    assert uploaded[-1][1]['profiles']['default']['mihomo']['custom_config'] == 'mixed-port: 7891'
    assert not Path(uploaded[-1][2]).exists()


def test_disabled_reads_and_failed_commits_do_not_schedule_backups(dav, monkeypatch):
    repository, settings, uploaded, _, _, worker = dav
    repository.save_profile('default', {'mihomo': {'custom_config': 'mode: rule'}})
    assert worker._thread is None
    repository.update_system_transaction(lambda system: system.update(backup=settings))
    wait_idle(worker)
    uploaded.clear()
    repository.get_system()
    repository.get_profile('default')
    repository.export_all()
    with pytest.raises(ProfileValidationError):
        repository.save_profile('default', {'proxy_groups': [
            {'id': 'duplicate', 'name': 'One', 'type': 'select'},
            {'id': 'duplicate', 'name': 'Two', 'type': 'select'},
        ]})
    original = repository._write_json
    def fail(path, value):
        if path == repository.path:
            raise OSError('disk failure')
        return original(path, value)
    monkeypatch.setattr(repository, '_write_json', fail)
    with pytest.raises(OSError):
        repository.save_profile('default', {'mihomo': {'custom_config': 'not committed'}})
    assert worker._thread is None
    assert uploaded == []


def test_runtime_agent_changes_are_ignored_but_agent_settings_add_edit_delete_are_backed_up(dav):
    repository, settings, uploaded, _, _, worker = dav
    agent = {'id': 'one', 'name': 'One', 'host': 'agent.test', 'port': 8080,
             'service_type': 'mihomo', 'profile_id': 'default', 'token': 'private'}
    repository.update_system_transaction(lambda system: system.update(backup=settings, agents=[agent]))
    wait_idle(worker)
    uploaded.clear()
    repository.update_system_transaction(lambda system: system['agents'][0].update(
        last_heartbeat='later', status='online', system_metrics={'cpu': 30},
        version='2', config_version='new', latest_deployment={'status': 'succeeded'},
        latest_upgrade={'status': 'succeeded'}, applied_content={'digest': 'latest'},
        updated_at='later'))
    assert worker._thread is None
    assert uploaded == []
    for change in (
        lambda system: system['agents'][0].update(host='new-agent.test'),
        lambda system: system['agents'][0].update(logging_config={'enabled': True}),
        lambda system: system['agents'].append({**agent, 'id': 'two'}),
        lambda system: system['agents'].__delitem__(-1),
    ):
        count = len(uploaded)
        repository.update_system_transaction(change)
        wait_idle(worker)
        assert len(uploaded) == count + 1
        assert uploaded[-1][1] == repository.export_all()


def test_slow_upload_does_not_block_saves_and_pending_changes_coalesce_to_latest_snapshot(dav, monkeypatch):
    repository, settings, uploaded, _, clients, worker = dav
    started, release = threading.Event(), threading.Event()
    original = clients.Client.upload_sync
    def blocked(self, **kwargs):
        original(self, **kwargs)
        if len(uploaded) == 1:
            started.set()
            assert release.wait(3)
    monkeypatch.setattr(clients.Client, 'upload_sync', blocked)
    repository.update_system_transaction(lambda system: system.update(backup=settings))
    assert started.wait(3)
    saved = threading.Event()
    def save_many():
        for port in range(7890, 7900):
            repository.save_profile('default', {'mihomo': {'custom_config': f'mixed-port: {port}'}})
        saved.set()
    saving = threading.Thread(target=save_many)
    saving.start()
    try:
        assert saved.wait(1), 'Saving must not wait for a slow remote WebDAV server'
        assert worker._pending is repository
        assert len(uploaded) == 1
    finally:
        release.set()
        saving.join(3)
        wait_idle(worker)
    assert len(uploaded) == 2
    assert uploaded[-1][1] == repository.export_all()
    assert uploaded[-1][1]['profiles']['default']['mihomo']['custom_config'] == 'mixed-port: 7899'
    assert len({row[0] for row in uploaded}) == 2
    assert all(not Path(row[2]).exists() for row in uploaded)


def test_disabling_auto_backup_before_pending_upload_cancels_it(dav):
    repository, settings, uploaded, _, _, worker = dav
    worker.delay = 0.1
    repository.update_system_transaction(lambda system: system.update(backup=settings))
    repository.update_system_transaction(lambda system: system['backup'].update(auto_backup=False))
    wait_idle(worker)
    assert uploaded == []


def test_upload_failure_preserves_saved_config_cleans_temp_file_and_does_not_retry(dav, monkeypatch, caplog):
    repository, settings, uploaded, _, clients, worker = dav
    paths = []
    def fail(self, *, remote_path, local_path):
        paths.append(local_path)
        raise OSError('remote failure containing secret')
    monkeypatch.setattr(clients.Client, 'upload_sync', fail)
    repository.update_system_transaction(lambda system: system.update(backup=settings))
    repository.save_profile('default', {'mihomo': {'custom_config': 'saved despite upload failure'}})
    wait_idle(worker)
    assert len(paths) == 1
    assert not Path(paths[0]).exists()
    assert repository.get_profile('default')['mihomo']['custom_config'] == 'saved despite upload failure'
    assert '自动备份失败' in caplog.text
    assert 'containing secret' not in caplog.text
    assert uploaded == []


def test_commit_notification_runs_after_lock_release_and_scheduler_failure_does_not_fail_save(dav, monkeypatch):
    from contextlib import contextmanager
    repository, settings, _, _, _, worker = dav
    original_lock = repository._lock
    depth = 0
    @contextmanager
    def tracked(*args, **kwargs):
        nonlocal depth
        with original_lock(*args, **kwargs):
            depth += 1
            try:
                yield
            finally:
                depth -= 1
    def fail_schedule(_repository):
        assert depth == 0
        assert repository._local.document is None
        raise RuntimeError('thread unavailable')
    monkeypatch.setattr(repository, '_lock', tracked)
    monkeypatch.setattr(worker, 'schedule', fail_schedule)
    repository.update_system_transaction(lambda system: system.update(backup=settings))
    repository.save_profile('default', {'mihomo': {'custom_config': 'saved'}})
    assert repository.get_profile('default')['mihomo']['custom_config'] == 'saved'


def test_helper_cleans_temp_file_if_serialization_fails(dav, monkeypatch):
    repository, settings, _, _, _, _ = dav
    paths = []
    original = webdav_backup.tempfile.NamedTemporaryFile
    def temporary(*args, **kwargs):
        handle = original(*args, **kwargs)
        paths.append(handle.name)
        return handle
    monkeypatch.setattr(webdav_backup.tempfile, 'NamedTemporaryFile', temporary)
    with pytest.raises(TypeError):
        webdav_backup.upload_backup({'unserializable': object()}, settings)
    assert len(paths) == 1
    assert not Path(paths[0]).exists()


def test_manual_backup_reuses_full_export_masked_password_and_nested_directory_creation(dav, monkeypatch):
    from flask import Flask
    from backend.common import config as config_store
    from backend.routes import register_blueprints

    repository, settings, uploaded, _, clients, _ = dav
    repository.update_system_transaction(lambda system: system.update(backup={**settings, 'auto_backup': False}))
    options, directories = [], []
    original = clients.Client.__init__
    def capture_options(self, value):
        options.append(value)
        original(self, value)
    monkeypatch.setattr(clients.Client, '__init__', capture_options)
    monkeypatch.setattr(clients.Client, 'check', lambda self, path: path in directories)
    monkeypatch.setattr(clients.Client, 'mkdir', lambda self, path: directories.append(path), raising=False)
    previous = config_store._repository
    config_store.set_repository(repository)
    try:
        app = Flask(__name__)
        register_blueprints(app)
        response = app.test_client().post('/api/backup/now', json={
            **settings, 'webdav_path': 'one/two', 'webdav_password': '******'})
        assert response.status_code == 200, response.json
    finally:
        config_store.set_repository(previous)
    assert directories == ['/one/', '/one/two/']
    assert options[0]['webdav_password'] == 'secret'
    assert uploaded[0][0].startswith('/one/two/config_backup_')
    assert uploaded[0][1] == repository.export_all()
    assert not Path(uploaded[0][2]).exists()


def test_thread_start_failure_does_not_disable_future_auto_backups(dav, monkeypatch):
    repository, settings, uploaded, _, _, worker = dav
    original = threading.Thread.start
    calls = []
    def fail_once(thread):
        calls.append(thread)
        if len(calls) == 1:
            raise RuntimeError('cannot create thread')
        return original(thread)
    monkeypatch.setattr(threading.Thread, 'start', fail_once)
    repository.update_system_transaction(lambda system: system.update(backup=settings))
    assert worker._thread is None
    repository.save_profile('default', {'mihomo': {'custom_config': 'later saved config'}})
    wait_idle(worker)
    assert len(uploaded) == 1
    assert uploaded[0][1] == repository.export_all()
