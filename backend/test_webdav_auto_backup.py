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
    remote = {}  # directory -> names, as the WebDAV server lists them

    class FakeClient:
        def __init__(self, options):
            assert options['webdav_timeout'] == 30

        def check(self, path):
            return True

        def upload_sync(self, *, remote_path, local_path):
            uploaded.append((remote_path, json.loads(Path(local_path).read_text()), local_path))
            directory, name = remote_path.rsplit('/', 1)
            remote.setdefault(directory + '/', []).append(name)
            completed.set()

        def list(self, path):
            return list(remote.get(path, []))

        def clean(self, path):
            directory, name = path.rsplit('/', 1)
            remote[directory + '/'].remove(name)

    package = types.ModuleType('webdav3')
    clients = types.ModuleType('webdav3.client')
    clients.Client = FakeClient
    clients.remote = remote
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
    # Queue both commits before the worker runs, regardless of disk/scheduler speed.
    with worker._condition:
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
    # Keep the upload pending until disabling auto-backup has committed.
    with worker._condition:
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
    # Exercise one failed upload without racing the debounce deadline.
    with worker._condition:
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


@pytest.fixture
def backup_api(dav):
    from flask import Flask
    from backend.common import config as config_store
    from backend.routes import register_blueprints

    repository, settings, _, _, _, _ = dav
    repository.update_system_transaction(
        lambda system: system.update(backup={**settings, 'auto_backup': False}))
    previous = config_store._repository
    config_store.set_repository(repository)
    try:
        app = Flask(__name__)
        register_blueprints(app)
        yield app.test_client()
    finally:
        config_store.set_repository(previous)


@pytest.mark.parametrize('endpoint', ['config', 'test', 'now'])
@pytest.mark.parametrize('request_options', [
    {},
    {'data': '{}', 'content_type': 'text/plain'},
    {'data': '{', 'content_type': 'application/json'},
    {'data': 'null', 'content_type': 'application/json'},
    {'json': []},
    {'json': [['webdav_url', 'https://dav.test']]},
    {'json': 'not an object'},
    {'json': {}},
    {'json': {'webdav_url': None}},
    {'json': {'auto_backup': 'false'}},
    {'json': {'unrecognized': True}},
])
def test_invalid_backup_requests_preserve_saved_configuration(backup_api, dav, endpoint, request_options):
    repository, _, uploaded, _, _, _ = dav
    before = repository.export_all()
    response = backup_api.post(f'/api/backup/{endpoint}', **request_options)
    assert response.status_code == 400
    assert response.json['success'] is False
    assert '父级目录' not in response.json['message']
    assert repository.export_all() == before
    assert uploaded == []


def test_partial_backup_settings_update_preserves_credentials_and_masked_password(backup_api, dav):
    repository, _, _, _, _, _ = dav
    before = repository.get_system()['backup']
    response = backup_api.post('/api/backup/config', json={
        'webdav_path': '/new-backups/', 'webdav_password': '******'})
    assert response.status_code == 200
    assert repository.get_system()['backup'] == {**before, 'webdav_path': '/new-backups/'}


@pytest.mark.parametrize('endpoint', ['test', 'now'])
def test_backup_remote_errors_report_http_status_without_secrets(backup_api, dav, monkeypatch, caplog, endpoint):
    _, settings, _, _, clients, _ = dav

    class RemoteError(Exception):
        code = 403

    def fail(*args, **kwargs):
        raise RemoteError('https://user:private-password@dav.test/ private-response-body')

    monkeypatch.setattr(clients.Client, 'check' if endpoint == 'test' else 'upload_sync', fail)
    response = backup_api.post(f'/api/backup/{endpoint}', json=settings)
    assert response.status_code == 500
    assert 'HTTP 403' in response.json['message']
    assert 'HTTP 403' in caplog.text
    for secret in ('private-password', 'private-response-body'):
        assert secret not in response.get_data(as_text=True)
        assert secret not in caplog.text


def test_directory_creation_error_preserves_http_status_without_secrets(dav, monkeypatch, caplog):
    repository, settings, _, _, clients, worker = dav

    class RemoteError(Exception):
        code = 403

    def fail(*args, **kwargs):
        raise RemoteError('private-password private-response-body')

    monkeypatch.setattr(clients.Client, 'check', lambda self, path: path == '/')
    monkeypatch.setattr(clients.Client, 'mkdir', fail, raising=False)
    with pytest.raises(webdav_backup.WebDAVConfigurationError) as error:
        webdav_backup.upload_backup(repository.export_all(), settings)
    assert 'private-password' not in str(error.value)
    repository.update_system_transaction(lambda system: system.update(backup=settings))
    wait_idle(worker)
    assert 'HTTP 403' in caplog.text
    assert 'private-password' not in caplog.text
    assert 'private-response-body' not in caplog.text


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


def backup_name(day, time='120000', micro='000000'):
    return f'config_backup_{day}_{time}_{micro}.json'


def test_retention_keeps_latest_plus_newest_of_each_recent_day_and_ignores_foreign_files():
    from datetime import date
    names = [backup_name('20261022', f'1{n:05d}') for n in range(5)]          # today, 5 backups
    names += [backup_name('20261021', '090000'), backup_name('20261021', '230000')]
    names += [backup_name('20261019', '080000'), backup_name('20260901', '100000')]
    names += ['config_backup_20261020_120000.json']                           # older name without microseconds
    foreign = ['notes.txt', 'config_backup_latest.json', 'config_backup_20261022_120000_1.json', 'app']
    expired = webdav_backup.expired_backups(names + foreign, 3, 3, date(2026, 10, 22))
    # Newest 3 are today's; 21st and 20th keep their last backup; 19th and September are outside both windows.
    assert sorted(expired) == sorted([
        backup_name('20261022', '100000'), backup_name('20261022', '100001'),
        backup_name('20261021', '090000'),
        backup_name('20261019', '080000'), backup_name('20260901', '100000'),
    ])
    assert not set(expired) & set(foreign)


def test_retention_orders_same_second_backups_by_microseconds_and_zero_disables_cleanup():
    from datetime import date
    names = [backup_name('20261022', '120000', f'{n:06d}') for n in (9, 10, 500000)]
    assert webdav_backup.expired_backups(names, 1, 0, date(2026, 10, 22)) == [
        backup_name('20261022', '120000', '000010'), backup_name('20261022', '120000', '000009')]
    assert webdav_backup.expired_backups(names, 0, 0, date(2026, 10, 22)) == []
    assert webdav_backup.expired_backups(names, 0, 30, date(2026, 10, 22)) == []


def test_upload_prunes_expired_backups_only_after_a_successful_upload(dav, monkeypatch):
    repository, settings, uploaded, _, clients, _ = dav
    old = [backup_name('20200101', f'1{n:05d}') for n in range(4)]
    clients.remote['/backups/'] = old + ['keep-me.txt']
    result = webdav_backup.upload_backup(repository.export_all(), {**settings, 'keep_latest': 2, 'keep_daily': 0})
    assert result.removed == 3
    assert sorted(clients.remote['/backups/']) == sorted([result.filename, old[-1], 'keep-me.txt'])

    def fail(self, **kwargs):
        raise OSError('upload failed')
    monkeypatch.setattr(clients.Client, 'upload_sync', fail)
    before = list(clients.remote['/backups/'])
    with pytest.raises(OSError):
        webdav_backup.upload_backup(repository.export_all(), {**settings, 'keep_latest': 1, 'keep_daily': 0})
    assert clients.remote['/backups/'] == before


def test_settings_without_retention_use_defaults(dav):
    repository, settings, _, _, clients, _ = dav
    assert 'keep_latest' not in settings
    old = [backup_name('20200101', f'{n:06d}') for n in range(webdav_backup.DEFAULT_KEEP_LATEST + 5)]
    clients.remote['/backups/'] = list(old)
    result = webdav_backup.upload_backup(repository.export_all(), settings)
    # The new upload counts towards the newest 30; the old ones are outside the 30-day window.
    assert result.removed == 6
    assert len(clients.remote['/backups/']) == webdav_backup.DEFAULT_KEEP_LATEST


def test_cleanup_failures_keep_the_backup_and_hide_remote_details(dav, monkeypatch, caplog):
    repository, settings, _, _, clients, _ = dav

    class RemoteError(Exception):
        code = 423

    old = [backup_name('20200101', f'1{n:05d}') for n in range(3)]
    clients.remote['/backups/'] = list(old)
    original = clients.Client.clean
    def clean(self, path):
        if path.endswith(old[0]):
            raise RemoteError('https://user:private-password@dav.test private-response-body')
        original(self, path)
    monkeypatch.setattr(clients.Client, 'clean', clean)
    result = webdav_backup.upload_backup(repository.export_all(), {**settings, 'keep_latest': 1, 'keep_daily': 0})
    assert result.removed == 2
    assert old[0] in clients.remote['/backups/'] and result.filename in clients.remote['/backups/']
    assert 'HTTP 423' in caplog.text

    def list_fails(self, path):
        raise RemoteError('private-password private-response-body')
    monkeypatch.setattr(clients.Client, 'list', list_fails)
    result = webdav_backup.upload_backup(repository.export_all(), {**settings, 'keep_latest': 1, 'keep_daily': 0})
    assert result.removed == 0 and result.filename
    assert '无法列出备份目录' in caplog.text
    assert 'private-password' not in caplog.text and 'private-response-body' not in caplog.text


def test_auto_backup_applies_saved_retention(dav):
    repository, settings, uploaded, _, clients, worker = dav
    clients.remote['/backups/'] = [backup_name('20200101', f'1{n:05d}') for n in range(5)]
    repository.update_system_transaction(
        lambda system: system.update(backup={**settings, 'keep_latest': 2, 'keep_daily': 0}))
    wait_idle(worker)
    assert len(uploaded) == 1
    assert len(clients.remote['/backups/']) == 2
    assert uploaded[0][0].endswith(clients.remote['/backups/'][-1])


def test_backup_settings_return_retention_defaults_and_save_valid_values(backup_api, dav):
    repository, _, _, _, _, _ = dav
    response = backup_api.get('/api/backup/config')
    assert (response.json['keep_latest'], response.json['keep_daily']) == (30, 30)
    response = backup_api.post('/api/backup/config', json={'keep_latest': 0, 'keep_daily': 3650})
    assert response.status_code == 200
    saved = repository.get_system()['backup']
    assert (saved['keep_latest'], saved['keep_daily']) == (0, 3650)
    assert backup_api.get('/api/backup/config').json['keep_latest'] == 0


@pytest.mark.parametrize('endpoint', ['config', 'now'])
@pytest.mark.parametrize('value', [
    {'keep_latest': -1}, {'keep_latest': 1001}, {'keep_latest': True}, {'keep_latest': '30'},
    {'keep_latest': 1.5}, {'keep_latest': None}, {'keep_daily': -1}, {'keep_daily': 3651},
])
def test_invalid_retention_values_are_rejected_without_changes(backup_api, dav, endpoint, value):
    repository, settings, uploaded, _, _, _ = dav
    before = repository.export_all()
    response = backup_api.post(f'/api/backup/{endpoint}', json={**settings, **value})
    assert response.status_code == 400
    assert '保留' in response.json['message']
    assert repository.export_all() == before
    assert uploaded == []


def test_manual_backup_uses_saved_retention_and_reports_cleanup(backup_api, dav):
    repository, settings, _, _, clients, _ = dav
    repository.update_system_transaction(lambda system: system['backup'].update(keep_latest=1, keep_daily=0))
    clients.remote['/backups/'] = [backup_name('20200101', f'1{n:05d}') for n in range(3)]
    response = backup_api.post('/api/backup/now', json={**settings, 'webdav_password': '******'})
    assert response.status_code == 200, response.json
    assert '已清理 3 个旧备份' in response.json['message']
    assert len(clients.remote['/backups/']) == 1


def test_invalid_saved_retention_falls_back_to_defaults_without_failing_the_upload(backup_api, dav):
    repository, settings, _, _, clients, _ = dav
    # Imported or hand-edited settings bypass the API validation.
    repository.update_system_transaction(
        lambda system: system['backup'].update(keep_latest='5', keep_daily=-3))
    assert webdav_backup.retention_settings(repository.get_system()['backup']) == (30, 30)
    response = backup_api.get('/api/backup/config')
    assert (response.json['keep_latest'], response.json['keep_daily']) == (30, 30)
    clients.remote['/backups/'] = [backup_name('20200101', f'{n:06d}') for n in range(31)]
    result = webdav_backup.upload_backup(repository.export_all(), repository.get_system()['backup'])
    assert result.filename and result.removed == 2
