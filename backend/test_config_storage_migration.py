"""Migration into configflow.sqlite3, rollback detection, corruption and concurrency."""

import copy
import hashlib
import json
import logging
import multiprocessing
import os
import sqlite3
import stat
import threading
import time
from pathlib import Path

import pytest

from backend.common.config_repository import ProfileRepository, ProfileRepositoryError
from backend.common.config_store import ConfigStore, ConfigStorageError
from backend.test_split_layout_migration import _bundle, _install_split, _source_bytes

DB = 'configflow.sqlite3'
LOGGER = 'backend.common.config_repository'


def legacy_single():
    return {
        'subscriptions': [{'id': 'sub-1', 'name': 'Legacy', 'url': 'https://legacy.example/sub'}],
        'nodes': [{'id': 'node-1', 'name': 'Node', 'type': 'ss', 'server': 'n.example', 'port': 1}],
        'rule_configs': [{'id': 'rule-1', 'itemType': 'rule', 'rule_type': 'DOMAIN',
                          'value': 'example.com', 'policy': 'DIRECT'}],
        'system_config': {'server_domain': 'https://config.example', 'config_token': 'keep'},
        'agents': [{'id': 'agent-1', 'name': 'legacy-agent'}],
        'backup': {'webdav_url': 'https://backup.example'},
    }


def schema5(tmp_path, *, token=True):
    """A valid schema-5 document as older versions wrote it to config.json."""
    document = ProfileRepository(tmp_path / 'seed').export_all()
    document['profiles']['default']['description'] = 'from config.json'
    if not token:
        document['system']['system_config'].pop('rule_proxy_token', None)
        document['system']['system_config'].pop('retired_rule_proxy_tokens', None)
    return document


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def snapshots(root, reason='*'):
    return sorted((root / 'migrations').glob(f'*/{reason}'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bump(path, edit):
    """Simulate an older version editing config.json during a rollback."""
    document = json.loads(path.read_text(encoding='utf-8'))
    edit(document)
    write_json(path, document)
    return document


# -- migration --------------------------------------------------------------


def test_fresh_directory_creates_database_without_config_json(tmp_path):
    repository = ProfileRepository(tmp_path)
    assert (tmp_path / DB).is_file()
    assert not (tmp_path / 'config.json').exists()
    assert repository._store.meta('migration')['source'] == 'defaults'
    record = repository._store.meta('legacy_config')
    assert record['sha256'] is None and record['size'] is None
    assert not list(tmp_path.glob(f'.{DB}.*'))
    assert not snapshots(tmp_path)
    assert repository.list_profiles()[0]['id'] == 'default'


def _assert_migrated_once(root, repository, before, source):
    assert _source_bytes(root) == before
    assert not list(root.glob('config.json.corrupt-*'))
    assert len(snapshots(root, 'migration')) == 1
    migration = repository._store.meta('migration')
    assert migration['source'] == source
    assert migration['snapshot'] == str(snapshots(root, 'migration')[0].relative_to(root))
    record = repository._store.meta('legacy_config')
    legacy = root / 'config.json'
    if legacy.is_file():
        assert (record['sha256'], record['size']) == (sha(legacy), legacy.stat().st_size)
    else:
        assert record['sha256'] is None
    assert record['status'] == 'migrated'


def test_migrates_legacy_single_file(tmp_path):
    write_json(tmp_path / 'config.json', legacy_single())
    before = _source_bytes(tmp_path)
    repository = ProfileRepository(tmp_path)
    _assert_migrated_once(tmp_path, repository, before, 'legacy-single')
    assert repository.get_shared()['subscriptions'][0]['url'] == 'https://legacy.example/sub'
    assert repository.get_system()['system_config']['config_token'] == 'keep'
    assert (snapshots(tmp_path, 'migration')[0] / 'config.json').read_bytes() == before['config.json']


@pytest.mark.parametrize('legacy', ['stale', 'missing', 'corrupt'])
def test_migrates_split_layout(tmp_path, legacy):
    bundle = _bundle()
    before = _install_split(tmp_path, bundle, legacy=legacy, backups=True)
    repository = ProfileRepository(tmp_path)
    _assert_migrated_once(tmp_path, repository, before, 'split-v2')
    assert set(repository.export_all()['profiles']) == set(bundle['profiles'])
    assert (snapshots(tmp_path, 'migration')[0] / 'system.json').read_bytes() == before['system.json']


@pytest.mark.parametrize('token', [True, False])
def test_migrates_schema5_config_json(tmp_path, token):
    document = schema5(tmp_path, token=token)
    root = tmp_path / 'data'
    root.mkdir()
    write_json(root / 'config.json', document)
    before = _source_bytes(root)
    repository = ProfileRepository(root)
    _assert_migrated_once(root, repository, before, 'config-v5')
    exported = repository.export_all()
    settings = exported['system']['system_config']
    assert settings['rule_proxy_token']
    if token:
        assert exported == document
    else:
        exported['system']['system_config'] = {key: value for key, value in settings.items()
                                               if key not in ('rule_proxy_token', 'retired_rule_proxy_tokens')}
        assert exported == document


def test_corrupt_config_json_migrates_from_its_backup_without_repairing(tmp_path):
    document = schema5(tmp_path)
    root = tmp_path / 'data'
    root.mkdir()
    write_json(root / 'config.json.bak', document)
    (root / 'config.json').write_bytes(b'{"schema_version": 5, "sys')
    before = _source_bytes(root)
    repository = ProfileRepository(root)
    _assert_migrated_once(root, repository, before, 'config-v5')
    assert repository.export_all() == document


def test_restart_is_idempotent(tmp_path):
    write_json(tmp_path / 'config.json', legacy_single())
    repository = ProfileRepository(tmp_path)
    repository.update_profile('default', {'name': 'Changed after migration'})
    fingerprint = repository._store.fingerprint()
    taken = snapshots(tmp_path)
    for _ in range(3):
        reopened = ProfileRepository(tmp_path)
        assert reopened._store.fingerprint() == fingerprint
        assert reopened.get_profile('default')['name'] == 'Changed after migration'
    assert snapshots(tmp_path) == taken


@pytest.mark.parametrize('after_replace', [False, True])
def test_failed_database_build_keeps_sources_and_retries(tmp_path, monkeypatch, after_replace):
    write_json(tmp_path / 'config.json', legacy_single())
    before = _source_bytes(tmp_path)
    real_replace = os.replace
    failed = False

    def fail_once(source, destination):
        nonlocal failed
        if Path(destination) == tmp_path / DB and not failed:
            failed = True
            if after_replace:
                real_replace(source, destination)
            raise OSError('injected failure at database install')
        return real_replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(os, 'replace', fail_once)
        with pytest.raises(OSError, match='injected'):
            ProfileRepository(tmp_path)
    assert failed
    assert _source_bytes(tmp_path) == before
    assert not list(tmp_path.glob(f'.{DB}.*'))
    assert (tmp_path / DB).exists() == after_replace
    repository = ProfileRepository(tmp_path)
    assert repository.get_shared()['subscriptions'][0]['id'] == 'sub-1'
    assert _source_bytes(tmp_path) == before


def test_stale_temporary_build_is_discarded(tmp_path):
    (tmp_path / f'.{DB}.abc.tmp').write_bytes(b'partial build')
    (tmp_path / f'.{DB}.abc.tmp-journal').write_bytes(b'partial journal')
    ProfileRepository(tmp_path)
    assert not list(tmp_path.glob(f'.{DB}.*'))


# -- rollback detection -------------------------------------------------------


def test_config_json_edited_by_older_version_is_imported_once(tmp_path, caplog):
    root = tmp_path / 'data'
    root.mkdir()
    write_json(root / 'config.json', schema5(tmp_path, token=False))
    repository = ProfileRepository(root)
    new_token = repository.get_system()['system_config']['rule_proxy_token']
    repository.create_profile({'id': 'newer', 'name': 'Made by the new version'})
    before_rollback = repository.export_all()

    def edit(document):
        document['system']['system_config']['rule_proxy_token'] = 'older-version-token'
        document['profiles']['default']['name'] = 'Edited during rollback'
        document['profiles']['default']['_revision'] += 1
    bump(root / 'config.json', edit)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        reopened = ProfileRepository(root)
    assert reopened.get_profile('default')['name'] == 'Edited during rollback'
    assert 'newer' not in {profile['id'] for profile in reopened.list_profiles()}
    settings = reopened.get_system()['system_config']
    assert settings['rule_proxy_token'] == 'older-version-token'
    assert new_token in settings['retired_rule_proxy_tokens']
    assert any('config.json' in record.getMessage() and record.levelno == logging.WARNING
               for record in caplog.records)
    record = reopened._store.meta('legacy_config')
    assert record['status'] == 'imported' and record['sha256'] == sha(root / 'config.json')
    [snapshot] = snapshots(root, 'legacy-config')
    assert reopened._store.meta('last_legacy_import')['snapshot'] == str(snapshot.relative_to(root))
    assert (snapshot / 'config.json').read_bytes() == (root / 'config.json').read_bytes()
    saved = ConfigStore(snapshot / DB, lambda: 1.0).load().document
    assert saved == before_rollback
    assert stat.S_IMODE((snapshot / DB).stat().st_mode) == 0o600

    fingerprint = reopened._store.fingerprint()
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        again = ProfileRepository(root)
    assert again._store.fingerprint() == fingerprint
    assert len(snapshots(root, 'legacy-config')) == 1
    assert not caplog.records


@pytest.mark.parametrize('rewrite', ['reformatted', 'token-added', 'remigrated'])
def test_config_json_rewritten_without_edits_is_not_imported(tmp_path, rewrite):
    root = tmp_path / 'data'
    root.mkdir()
    if rewrite == 'remigrated':
        write_json(root / 'config.json', legacy_single())
    else:
        write_json(root / 'config.json', schema5(tmp_path, token=rewrite != 'token-added'))
    repository = ProfileRepository(root)
    repository.update_profile('default', {'name': 'Newer data'})
    current = repository.export_all()
    generation = repository._store.fingerprint()[1]
    if rewrite == 'reformatted':
        (root / 'config.json').write_text(json.dumps(json.loads((root / 'config.json').read_text())))
    elif rewrite == 'token-added':
        bump(root / 'config.json', lambda document: document['system']['system_config'].update(
            rule_proxy_token='older-version-token'))
    else:
        # An older version re-migrates the untouched legacy file at its startup.
        scratch = tmp_path / 'older'
        scratch.mkdir()
        write_json(scratch / 'config.json', legacy_single())
        write_json(root / 'config.json', ProfileRepository(scratch).export_all())
    reopened = ProfileRepository(root)
    assert reopened.export_all() == current
    assert reopened._store.fingerprint()[1] == generation
    record = reopened._store.meta('legacy_config')
    assert record['status'] == 'ignored' and record['sha256'] == sha(root / 'config.json')
    assert not snapshots(root, 'legacy-config')


def test_invalid_config_json_rewrite_is_rejected_once(tmp_path, caplog):
    write_json(tmp_path / 'config.json', legacy_single())
    repository = ProfileRepository(tmp_path)
    current = repository.export_all()
    generation = repository._store.fingerprint()[1]
    (tmp_path / 'config.json').write_text('{"schema_version": 5, "system": []}')
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        reopened = ProfileRepository(tmp_path)
    assert reopened.export_all() == current
    assert reopened._store.fingerprint()[1] == generation
    assert reopened._store.meta('legacy_config')['status'] == 'rejected'
    assert [record.levelno for record in caplog.records] == [logging.WARNING]
    fingerprint = reopened._store.fingerprint()
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert ProfileRepository(tmp_path)._store.fingerprint() == fingerprint
    assert not caplog.records


def test_config_json_written_after_fresh_install_is_imported_when_edited(tmp_path):
    repository = ProfileRepository(tmp_path)
    document = repository.export_all()
    document['profiles']['default']['name'] = 'Written by an older version'
    document['profiles']['default']['_revision'] += 1
    write_json(tmp_path / 'config.json', document)
    reopened = ProfileRepository(tmp_path)
    assert reopened.get_profile('default')['name'] == 'Written by an older version'
    assert len(snapshots(tmp_path, 'legacy-config')) == 1


def test_older_version_defaults_after_fresh_install_are_ignored(tmp_path):
    repository = ProfileRepository(tmp_path)
    repository.create_profile({'id': 'kept', 'name': 'Kept'})
    write_json(tmp_path / 'config.json', ProfileRepository(tmp_path / 'older').export_all())
    reopened = ProfileRepository(tmp_path)
    # A changed profile set is an edit only relative to the migration baseline:
    # the defaults an older version writes match it, so nothing is replaced.
    assert 'kept' in {profile['id'] for profile in reopened.list_profiles()}
    assert reopened._store.meta('legacy_config')['status'] == 'ignored'


# -- corruption -----------------------------------------------------------------


def _migrated_with_legacy(tmp_path):
    write_json(tmp_path / 'config.json', legacy_single())
    repository = ProfileRepository(tmp_path)
    repository.update_profile('default', {'name': 'Lost by a rebuild'})
    return repository


def _damage_fields_table(path):
    with sqlite3.connect(path) as connection:
        page_size = connection.execute('PRAGMA page_size').fetchone()[0]
        root = connection.execute("SELECT rootpage FROM sqlite_master WHERE name = 'fields'").fetchone()[0]
    connection.close()
    with open(path, 'r+b') as handle:
        handle.seek((root - 1) * page_size)
        handle.write(b'\xa5' * page_size)


def test_garbage_database_is_preserved_and_rebuilt_from_matching_config_json(tmp_path, caplog):
    _migrated_with_legacy(tmp_path)
    garbage = b'this is not an sqlite database' * 64
    (tmp_path / DB).write_bytes(garbage)
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        repository = ProfileRepository(tmp_path)
    [corrupt] = tmp_path.glob(f'{DB}.corrupt-*')
    assert corrupt.read_bytes() == garbage
    assert repository.get_shared()['subscriptions'][0]['id'] == 'sub-1'
    assert repository.get_profile('default')['name'] != 'Lost by a rebuild'
    recovered = repository._store.meta('recovered_from')
    assert recovered['source'] == 'config.json' and recovered['corrupt_file'] == corrupt.name
    assert repository._store.meta('migration')['source'] == 'corrupt-rebuild'
    assert any(record.levelno == logging.ERROR and corrupt.name in record.getMessage()
               for record in caplog.records)
    # The rebuilt database is a normal one: restarting changes nothing.
    fingerprint = repository._store.fingerprint()
    assert ProfileRepository(tmp_path)._store.fingerprint() == fingerprint


def test_page_damage_is_detected_and_rebuilt_with_salvaged_record(tmp_path):
    repository = _migrated_with_legacy(tmp_path)
    record = repository._store.meta('legacy_config')
    _damage_fields_table(tmp_path / DB)
    with pytest.raises(Exception):
        ConfigStore(tmp_path / DB, lambda: 1.0).check()
    reopened = ProfileRepository(tmp_path)
    assert len(list(tmp_path.glob(f'{DB}.corrupt-*'))) == 1
    assert reopened._store.meta('legacy_config')['sha256'] == record['sha256']
    assert reopened.get_shared()['subscriptions'][0]['id'] == 'sub-1'


def test_corrupt_database_with_changed_config_json_fails_loudly(tmp_path):
    _migrated_with_legacy(tmp_path)
    _damage_fields_table(tmp_path / DB)
    (tmp_path / 'config.json').write_text(json.dumps({**legacy_single(), 'agents': []}))
    damaged = (tmp_path / DB).read_bytes()
    with pytest.raises(ProfileRepositoryError, match='已损坏'):
        ProfileRepository(tmp_path)
    assert (tmp_path / DB).read_bytes() == damaged
    assert not list(tmp_path.glob(f'{DB}.corrupt-*'))


def test_corrupt_database_without_any_source_fails_loudly(tmp_path):
    ProfileRepository(tmp_path)
    (tmp_path / DB).write_bytes(b'garbage' * 100)
    with pytest.raises(ProfileRepositoryError, match='没有可安全使用的恢复来源'):
        ProfileRepository(tmp_path)
    assert (tmp_path / DB).read_bytes() == b'garbage' * 100
    assert not list(tmp_path.glob(f'{DB}.corrupt-*'))


def test_empty_database_file_counts_as_corrupt(tmp_path):
    _migrated_with_legacy(tmp_path)
    (tmp_path / DB).write_bytes(b'')
    repository = ProfileRepository(tmp_path)
    assert len(list(tmp_path.glob(f'{DB}.corrupt-*'))) == 1
    assert repository.get_shared()['subscriptions'][0]['id'] == 'sub-1'


def test_hot_journal_moves_with_the_corrupt_database(tmp_path):
    # SQLite itself rolls back or discards a journal it can read when the
    # file is opened; one that is still there belongs to the damaged file.
    ProfileRepository(tmp_path)
    store = ConfigStore(tmp_path / DB, lambda: 1.0)
    database = (tmp_path / DB).read_bytes()
    (tmp_path / f'{DB}-journal').write_bytes(b'journal bytes')
    corrupt = store.preserve_corrupt()
    assert corrupt.read_bytes() == database
    assert Path(str(corrupt) + '-journal').read_bytes() == b'journal bytes'
    assert not (tmp_path / DB).exists() and not (tmp_path / f'{DB}-journal').exists()


def test_orphan_journal_without_database_is_set_aside(tmp_path):
    (tmp_path / f'{DB}-journal').write_bytes(b'orphan')
    ProfileRepository(tmp_path)
    assert [path.read_bytes() for path in tmp_path.glob(f'{DB}-journal.orphan-*')] == [b'orphan']


def test_locked_database_fails_without_renaming(tmp_path, monkeypatch):
    ProfileRepository(tmp_path)
    monkeypatch.setenv('CONFIGFLOW_LOCK_TIMEOUT_SECONDS', '0.2')
    holder = sqlite3.connect(tmp_path / DB, timeout=0, isolation_level=None)
    try:
        holder.execute('BEGIN EXCLUSIVE')
        with pytest.raises(ConfigStorageError, match='未改动'):
            ProfileRepository(tmp_path)
    finally:
        holder.execute('ROLLBACK')
        holder.close()
    assert not list(tmp_path.glob(f'{DB}.corrupt-*'))
    ProfileRepository(tmp_path)


def test_newer_storage_version_fails_without_renaming(tmp_path):
    ProfileRepository(tmp_path)
    with sqlite3.connect(tmp_path / DB) as connection:
        connection.execute("UPDATE meta SET value = '2' WHERE key = 'storage_version'")
    connection.close()
    with pytest.raises(ProfileRepositoryError, match='更新版本'):
        ProfileRepository(tmp_path)
    assert not list(tmp_path.glob(f'{DB}.corrupt-*'))


def test_invalid_rows_fail_validation_without_rebuilding(tmp_path):
    _migrated_with_legacy(tmp_path)
    with sqlite3.connect(tmp_path / DB) as connection:
        connection.execute("UPDATE fields SET value = ? WHERE scope = 'system' AND field = 'agents'",
                           (json.dumps([{'id': 'a', 'profile_id': 'missing'}]),))
    connection.close()
    fingerprint = ConfigStore(tmp_path / DB, lambda: 1.0).fingerprint()
    with pytest.raises(ProfileRepositoryError, match='未通过校验'):
        ProfileRepository(tmp_path)
    assert ConfigStore(tmp_path / DB, lambda: 1.0).fingerprint() == fingerprint
    assert not list(tmp_path.glob(f'{DB}.corrupt-*'))


def test_symlinked_database_is_rejected(tmp_path):
    ProfileRepository(tmp_path / 'real')
    root = tmp_path / 'data'
    root.mkdir()
    (root / DB).symlink_to(tmp_path / 'real' / DB)
    with pytest.raises(ProfileRepositoryError):
        ProfileRepository(root)


# -- concurrency ------------------------------------------------------------------


def _increment(repository, count):
    for _ in range(count):
        repository.update_system_transaction(lambda system: system['system_config'].update(
            counter=system['system_config'].get('counter', 0) + 1))


def test_two_threads_on_one_instance_lose_no_update(tmp_path):
    repository = ProfileRepository(tmp_path)
    threads = [threading.Thread(target=_increment, args=(repository, 10)) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    assert repository.get_system()['system_config']['counter'] == 20
    assert ProfileRepository(tmp_path).get_system()['system_config']['counter'] == 20


def test_reader_is_not_blocked_by_a_writer_holding_its_transaction(tmp_path):
    writer = ProfileRepository(tmp_path)
    reader = ProfileRepository(tmp_path)
    original_name = reader.get_profile('default')['name']
    inside, release = threading.Event(), threading.Event()
    original_apply = writer._store._apply

    def held_apply(*args):
        original_apply(*args)
        inside.set()
        assert release.wait(10)

    writer._store._apply = held_apply
    thread = threading.Thread(target=writer.update_profile, args=('default', {'name': 'Committed'}))
    thread.start()
    try:
        assert inside.wait(10)
        reader._drop_cache()
        started = time.monotonic()
        assert reader.get_profile('default')['name'] == original_name
        assert time.monotonic() - started < 2
    finally:
        release.set()
        thread.join(10)
    assert reader.get_profile('default')['name'] == 'Committed'


def test_reader_does_not_need_the_document_lock(tmp_path):
    repository = ProfileRepository(tmp_path)
    reader = ProfileRepository(tmp_path)
    held, release = threading.Event(), threading.Event()

    def hold():
        with repository._lock(repository.initialization_lock_file):
            held.set()
            release.wait(10)

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert held.wait(10)
        reader._drop_cache()
        started = time.monotonic()
        assert reader.list_profiles()[0]['id'] == 'default'
        assert time.monotonic() - started < 2
    finally:
        release.set()
        thread.join(10)


def _write_in_child(data_dir):
    ProfileRepository(data_dir).update_profile('default', {'name': 'From another process'})


def test_write_from_another_process_is_seen(tmp_path):
    repository = ProfileRepository(tmp_path)
    assert repository.get_profile('default')['name'] != 'From another process'
    process = multiprocessing.get_context('spawn').Process(target=_write_in_child, args=(str(tmp_path),))
    process.start()
    process.join(60)
    assert process.exitcode == 0
    assert repository.get_profile('default')['name'] == 'From another process'


# -- generation cache -------------------------------------------------------------


def test_cache_follows_the_generation(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    repository.update_profile('default', {'name': 'Cached'})
    counts = {'load': 0, 'validate': 0}
    load, validate = repository._store.load, repository._validate_document

    def counted_load(*args, **kwargs):
        counts['load'] += 1
        return load(*args, **kwargs)

    def counted_validate(document):
        counts['validate'] += 1
        return validate(document)

    monkeypatch.setattr(repository._store, 'load', counted_load)
    monkeypatch.setattr(repository, '_validate_document', counted_validate)
    for _ in range(3):
        repository.get_profile('default')
        repository.export_all()
    assert counts == {'load': 0, 'validate': 0}
    ProfileRepository(tmp_path).update_profile('default', {'name': 'Elsewhere'})
    for _ in range(3):
        assert repository.get_profile('default')['name'] == 'Elsewhere'
    assert counts == {'load': 1, 'validate': 1}


# -- delete tombstones --------------------------------------------------------------


def test_deleting_a_profile_without_derived_files_keeps_its_json(tmp_path):
    repository = ProfileRepository(tmp_path)
    repository.create_profile({'id': 'gone', 'name': 'Gone', 'description': '含凭据'})
    repository.update_profile_fields('gone', {'routers': {'items': [
        {'id': 'r', 'name': 'Router', 'type': 'routeros', 'endpoint': 'https://r.example',
         'username': 'admin', 'password': 'secret'}]}})
    exported = repository.export_profile('gone')
    assert not repository.profile_dir('gone').exists()
    repository.delete_profile('gone')
    [tombstone] = repository.migrations_dir.glob('deleted-gone-*')
    saved = tombstone / 'deleted-profile.json'
    assert json.loads(saved.read_text(encoding='utf-8')) == exported
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    assert not list(tombstone.glob('*.tmp'))


@pytest.mark.parametrize('with_directory', [False, True])
def test_failed_delete_removes_the_tombstone(tmp_path, monkeypatch, with_directory):
    repository = ProfileRepository(tmp_path)
    repository.create_profile({'id': 'kept', 'name': 'Kept'})
    if with_directory:
        repository.profile_dir('kept').mkdir()
        (repository.profile_dir('kept') / 'important.txt').write_text('keep')

    def fail(*args):
        raise OSError('injected commit failure')

    monkeypatch.setattr(repository._store, '_apply', fail)
    with pytest.raises(OSError, match='injected'):
        repository.delete_profile('kept')
    assert 'kept' in {profile['id'] for profile in repository.list_profiles()}
    assert not list(repository.migrations_dir.glob('deleted-kept-*'))
    if with_directory:
        assert sorted(path.name for path in repository.profile_dir('kept').iterdir()) == ['important.txt']
    else:
        assert not repository.profile_dir('kept').exists()


# -- export and import ----------------------------------------------------------------


def test_export_import_round_trip_and_import_snapshot(tmp_path):
    repository = ProfileRepository(tmp_path)
    repository.create_profile({'id': 'second', 'name': 'Second'})
    repository.update_shared_transaction(lambda shared: shared['nodes'].append(
        {'id': 'n', 'name': 'Node', 'type': 'ss', 'server': 's.example', 'port': 1}))
    exported = repository.export_all()
    imported = repository.import_all(copy.deepcopy(exported))

    def without_revisions(document):
        document = copy.deepcopy(document)
        for section in (document['system'], document['shared'], *document['profiles'].values()):
            section.pop('_revision')
        return document

    assert without_revisions(imported) == without_revisions(exported)
    assert json.dumps(list(imported['profiles'])) == json.dumps(list(exported['profiles']))
    [snapshot] = snapshots(tmp_path, 'import')
    assert ConfigStore(snapshot / DB, lambda: 1.0).load().document == exported
    assert not (snapshot / 'config.json').exists()


@pytest.mark.parametrize('version', [None, 2, 5])
def test_backups_of_every_supported_schema_import(tmp_path, version):
    if version is None:
        backup = legacy_single()
    elif version == 2:
        backup = _bundle()
    else:
        backup = schema5(tmp_path)
    repository = ProfileRepository(tmp_path / 'data')
    result = repository.import_all(backup)
    assert result['schema_version'] == 5
    assert ProfileRepository(tmp_path / 'data').export_all() == result
    assert not (tmp_path / 'data' / 'config.json').exists()
