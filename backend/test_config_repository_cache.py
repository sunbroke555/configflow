"""Validated-document reuse must preserve persistence and snapshot boundaries."""

import copy
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
import threading
from threading import Barrier

import pytest

from backend.common.config_repository import (
    ProfileRepository,
    ProfileRepositoryError,
    ProfileValidationError,
)


@pytest.fixture
def repository(tmp_path):
    return ProfileRepository(tmp_path)


def observe_reads(repository, monkeypatch):
    counts = {'read': 0, 'validate': 0}
    load, validate = repository._store.load, repository._validate_document

    def tracked_load(*args, **kwargs):
        counts['read'] += 1
        return load(*args, **kwargs)

    def tracked_validate(document):
        counts['validate'] += 1
        return validate(document)

    monkeypatch.setattr(repository._store, 'load', tracked_load)
    monkeypatch.setattr(repository, '_validate_document', tracked_validate)
    return counts


def raw_rename(repository, name):
    """Change the default profile's name behind the repository's back."""
    with sqlite3.connect(repository.db_path) as connection:
        connection.execute("UPDATE fields SET value = ? WHERE scope = 'profile' AND owner = 'default' AND field = 'name'",
                           (json.dumps(name),))
        connection.execute("UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'generation'")
    connection.close()


def test_unchanged_getters_read_and_validate_once(repository, monkeypatch):
    repository._drop_cache()
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


def test_external_commit_invalidates_by_generation(repository, monkeypatch):
    repository.update_profile('default', {'name': 'Alpha'})
    assert repository.get_profile('default')['name'] == 'Alpha'
    counts = observe_reads(repository, monkeypatch)
    other = ProfileRepository(repository.data_dir)
    other.update_profile('default', {'name': 'Bravo'})
    assert repository.get_profile('default')['name'] == 'Bravo'
    assert repository.get_profile('default')['name'] == 'Bravo'
    assert counts['read'] == 1
    raw_rename(repository, 'Charlie')
    assert repository.get_profile('default')['name'] == 'Charlie'


def test_rebuilt_database_with_same_generation_invalidates(repository):
    repository.update_profile('default', {'name': 'Alpha'})
    head = repository._document_cache.head
    document = repository._store.load().document
    document['profiles']['default']['name'] = 'Rebuilt'
    # Same generation number, different database: store_id must differ.
    repository._store.create(document, meta={})
    with sqlite3.connect(repository.db_path) as connection:
        connection.execute("UPDATE meta SET value = ? WHERE key = 'generation'", (json.dumps(head.generation),))
    connection.close()
    assert repository._store.load().head.generation == head.generation
    assert repository.get_profile('default')['name'] == 'Rebuilt'


def test_commit_during_read_never_labels_old_document_as_new(repository, monkeypatch):
    other = ProfileRepository(repository.data_dir)
    original_load = repository._store.load
    committed = []

    def load_then_commit(*args, **kwargs):
        entry = original_load(*args, **kwargs)
        if not committed:
            # A writer in another thread cannot commit while this read
            # transaction is open; it commits right after it ends.
            thread = threading.Thread(target=lambda: committed.append(
                other.update_profile('default', {'name': 'External'})))
            thread.start()
            committed.append(thread)
        return entry

    repository._drop_cache()
    monkeypatch.setattr(repository._store, 'load', load_then_commit)
    assert repository.get_profile('default')['name'] != 'External'
    committed[0].join(10)
    assert repository.get_profile('default')['name'] == 'External'


def test_damaged_database_raises_instead_of_using_cached_copy(repository):
    original = repository.export_all()
    with sqlite3.connect(repository.db_path) as connection:
        connection.execute("UPDATE fields SET value = '{' WHERE scope = 'system' AND field = 'agents'")
        connection.execute("UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'generation'")
    connection.close()
    with pytest.raises((ProfileRepositoryError, OSError)):
        repository.get_system()
    repository._store.create(original, meta={})
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


def test_failed_commit_keeps_data_and_drops_cache(repository, monkeypatch):
    before = repository.export_all()
    fingerprint = repository._store.fingerprint()

    def fail(connection):
        raise OSError('injected failure at COMMIT')

    monkeypatch.setattr(repository._store, '_commit', fail)
    with pytest.raises(OSError, match='injected failure'):
        repository.update_profile('default', {'name': 'Uncommitted'})
    assert repository._document_cache is None
    assert repository._store.fingerprint() == fingerprint
    counts = observe_reads(repository, monkeypatch)
    assert repository.export_all() == before
    assert counts['read'] == 1


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
    monkeypatch.setattr(repository, 'DOCUMENT_CACHE_MAX_BYTES', repository._store.load().size - 1)
    repository._drop_cache()
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
