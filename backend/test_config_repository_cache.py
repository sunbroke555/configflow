"""Validated-document reuse must preserve persistence and snapshot boundaries."""

import copy
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from backend.common.config_repository import (
    ProfileRepository,
    ProfileRepositoryError,
    ProfileValidationError,
)


pytestmark = pytest.mark.skipif(os.name == 'nt', reason='document reuse requires POSIX change time')


@pytest.fixture
def repository(tmp_path):
    return ProfileRepository(tmp_path)


def observe_reads(repository, monkeypatch):
    counts = {'read': 0, 'validate': 0}
    read, validate = repository._read_json, repository._validate_document

    def tracked_read(path):
        if path == repository.path:
            counts['read'] += 1
        return read(path)

    def tracked_validate(document):
        counts['validate'] += 1
        return validate(document)

    monkeypatch.setattr(repository, '_read_json', tracked_read)
    monkeypatch.setattr(repository, '_validate_document', tracked_validate)
    return counts


def external_document(repository, name):
    document = json.loads(repository.path.read_text())
    document['profiles']['default']['name'] = name
    return document


def replace_document(repository, document, *, preserve_mtime=False):
    previous = repository.path.stat()
    temporary = repository.path.with_name('external.json')
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n')
    if preserve_mtime:
        os.utime(temporary, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    os.replace(temporary, repository.path)


def test_unchanged_getters_read_and_validate_once(repository, monkeypatch):
    counts = observe_reads(repository, monkeypatch)
    for _ in range(3):
        repository.get_system()
        repository.get_shared()
        repository.get_profile('default')
        repository.get_compat_config('default')
        repository.export_all()
        repository.list_profiles()
        repository.config_revision('default')
    assert counts == {'read': 1, 'validate': 1}


def test_public_results_cannot_mutate_cached_document(repository):
    repository.update_shared_transaction(lambda shared: shared.update(
        nodes=[{'id': 'n', 'name': 'Node'}],
        subscription_aggregations=[{'id': 'a', 'name': {'display': ['Aggregate']}, 'nodes': ['n']}],
        rule_library=[{'id': 'r', 'name': 'Rule', 'enabled': {'custom': []}}],
    ))
    repository.update_profile_fields('default', {
        'proxy_groups': [{'id': 'g', 'name': 'Group', 'type': 'select', 'manual_nodes': ['n']}],
        'rule_configs': [{'id': 'rule', 'itemType': 'ruleset', 'library_rule_id': 'r'}],
    })
    before = repository.export_all()
    system = repository.get_system()
    system['system_config']['surprise'] = []
    repository.get_shared()['nodes'][0]['name'] = 'Changed'
    repository.get_profile('default')['proxy_groups'].clear()
    repository.export_all()['profiles'].clear()
    repository.export_profile('default')['rule_configs'].clear()
    repository.list_profiles()[0]['name'] = 'Changed'
    resolved = repository.get_compat_config('default')
    resolved['nodes'][0]['name'] = 'Changed'
    resolved['rule_configs'][0]['library_enabled']['custom'].append('Changed')
    usages = repository.get_resource_usage('nodes', 'n')
    usages[0]['name']['display'].append('Changed')
    assert repository.export_all() == before


@pytest.mark.parametrize('atomic', [False, True])
def test_external_same_size_same_mtime_edit_invalidates(repository, atomic):
    repository.update_profile('default', {'name': 'Alpha'})
    assert repository.get_profile('default')['name'] == 'Alpha'
    previous = repository.path.stat()
    changed = external_document(repository, 'Bravo')
    if atomic:
        replace_document(repository, changed, preserve_mtime=True)
    else:
        repository.path.write_text(json.dumps(changed, ensure_ascii=False, indent=2) + '\n')
        os.utime(repository.path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        # This branch relies on ctime, which may not advance within one Linux
        # clock tick. Establish that premise without changing size or mtime.
        deadline = time.monotonic() + 2
        while repository.path.stat().st_ctime_ns == previous.st_ctime_ns and time.monotonic() < deadline:
            time.sleep(0.01)
            os.utime(repository.path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        assert repository.path.stat().st_ctime_ns != previous.st_ctime_ns
    current = repository.path.stat()
    assert (current.st_size, current.st_mtime_ns) == (previous.st_size, previous.st_mtime_ns)
    assert repository.get_profile('default')['name'] == 'Bravo'


def test_replacement_during_read_never_labels_old_document_as_new(repository, monkeypatch):
    old_read = repository._read_recoverable
    changed = external_document(repository, 'External')
    replaced = False

    def replace_after_read(path, validator):
        nonlocal replaced
        document = old_read(path, validator)
        if not replaced:
            replaced = True
            replace_document(repository, changed)
        return document

    monkeypatch.setattr(repository, '_read_recoverable', replace_after_read)
    assert repository.get_profile('default')['name'] != 'External'
    assert repository.get_profile('default')['name'] == 'External'


@pytest.mark.parametrize('damage', ['invalid_json', 'invalid_schema', 'missing'])
def test_warm_cache_does_not_bypass_backup_recovery(repository, damage):
    repository.update_profile('default', {'name': 'Backup'})
    repository.update_profile('default', {'name': 'Current'})
    backup_path = repository.path.with_name('config.json.bak')
    backup = backup_path.read_bytes()
    assert repository.get_profile('default')['name'] == 'Current'
    if damage == 'missing':
        repository.path.unlink()
    elif damage == 'invalid_schema':
        changed = external_document(repository, 'Invalid')
        changed['system']['agents'] = [{'id': 'bad', 'profile_id': 'missing'}]
        replace_document(repository, changed)
    else:
        repository.path.write_text('{')
    assert repository.get_profile('default')['name'] == 'Backup'
    assert repository.export_all() == json.loads(backup)
    assert backup_path.read_bytes() == backup
    if damage != 'missing':
        assert list(repository.data_dir.glob('config.json.corrupt-*'))


def test_unrecoverable_damage_raises_instead_of_using_cached_copy(repository):
    original = repository.export_all()
    repository.path.write_text('{')
    repository.path.with_name('config.json.bak').write_text('{')
    with pytest.raises(ProfileRepositoryError):
        repository.get_system()
    replace_document(repository, original)
    assert repository.export_all() == original


def test_invalid_write_and_callback_failure_do_not_poison_cache(repository):
    before = repository.export_all()
    with pytest.raises(ProfileValidationError):
        repository.update_system_transaction(
            lambda system: system['agents'].append({'id': 'bad', 'profile_id': 'missing'}))
    assert repository.export_all() == before

    def fail_after_mutating(system):
        system['system_config']['should_not_persist'] = True
        raise RuntimeError('callback failed')

    with pytest.raises(RuntimeError, match='callback failed'):
        repository.update_system_transaction(fail_after_mutating)
    assert repository.export_all() == before


def test_failed_commit_restores_file_and_invalidates_cache(repository, monkeypatch):
    before = repository.export_all()
    original_fsync = repository._fsync_dir
    failed = False

    def fail_first_fsync(directory):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError('injected failure after replacement')
        return original_fsync(directory)

    monkeypatch.setattr(repository, '_fsync_dir', fail_first_fsync)
    with pytest.raises(OSError, match='injected failure'):
        repository.update_profile('default', {'name': 'Uncommitted'})
    assert json.loads(repository.path.read_text()) == before
    assert repository.export_all() == before


def test_nested_reads_use_transaction_and_failed_transaction_is_not_cached(repository):
    before = repository.export_all()
    with pytest.raises(RuntimeError, match='abort'):
        with repository._document(write=True) as document:
            document['profiles']['default']['name'] = 'In transaction'
            assert repository.get_profile('default')['name'] == 'In transaction'
            raise RuntimeError('abort')
    assert repository.export_all() == before


def test_separate_repository_caches_observe_concurrent_commits(repository):
    other = ProfileRepository(repository.data_dir)
    repository.get_system()
    other.get_system()
    barrier = Barrier(2)

    def increment(repo):
        barrier.wait(timeout=5)
        for _ in range(5):
            def update(system):
                settings = system['system_config']
                settings['counter'] = settings.get('counter', 0) + 1
            repo.update_system_transaction(update)
            repo.get_system()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(increment, repo) for repo in (repository, other)]
        for future in futures:
            future.result(timeout=10)
    assert repository.get_system()['system_config']['counter'] == 10
    assert other.get_system()['system_config']['counter'] == 10


def test_large_document_is_not_retained_in_cache(repository, monkeypatch):
    repository.get_system()
    monkeypatch.setattr(repository, 'DOCUMENT_CACHE_MAX_BYTES', repository.path.stat().st_size - 1)
    counts = observe_reads(repository, monkeypatch)
    repository.get_system()
    repository.get_system()
    assert counts == {'read': 2, 'validate': 2}
    assert repository._document_cache is None


def test_import_and_reset_invalidate_previous_generation(repository):
    saved = repository.export_all()
    imported = copy.deepcopy(saved)
    imported['profiles']['default']['name'] = 'Imported'
    repository.import_all(imported)
    assert repository.get_profile('default')['name'] == 'Imported'
    repository.reset_all()
    assert repository.get_profile('default')['name'] == saved['profiles']['default']['name']
