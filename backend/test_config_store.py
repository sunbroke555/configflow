"""SQLite storage engine: row layout, pragmas, diff commits and conflicts."""

import json
import sqlite3
import stat

import pytest

from backend.common.config_repository import ProfileRepository
from backend.common.config_store import (
    ConfigStore, StoreConflict, build_document, document_rows, dumps,
)


def sample_document():
    return {
        'schema_version': 5,
        'system': {'system_config': {'server_domain': 'https://例子.test', 'z': 1, 'a': 2},
                   'backup': {}, 'agents': [], '_revision': 3},
        'shared': {'subscriptions': [], 'nodes': [{'id': 'n', 'name': '节点 🚀', 'port': 1.5}],
                   'subscription_aggregations': [], 'rule_library': [], '_revision': 7},
        'profiles': {
            'zeta': {'id': 'zeta', 'name': 'Zeta', '_revision': 2, 'description': '最后创建',
                     'created_at': '2026-01-02T00:00:00Z', 'updated_at': '2026-01-03T00:00:00Z',
                     'extra': {'b': [1, {'c': None}], 'a': True}},
            'default': {'name': 'Default', 'id': 'default', '_revision': 0, 'description': '',
                        'created_at': '2026-01-01T00:00:00Z', 'updated_at': '2026-01-01T00:00:00Z'},
        },
        'future_root_key': {'kept': True},
    }


def rows(path):
    with sqlite3.connect(path) as connection:
        result = {(scope, owner, field): (value, revision) for scope, owner, field, value, revision in
                  connection.execute('SELECT scope, owner, field, value, revision FROM fields')}
    connection.close()
    return result


def store(tmp_path):
    return ConfigStore(tmp_path / 'configflow.sqlite3', lambda: 2.5)


def test_rows_round_trip_preserves_order_unicode_and_extra_keys(tmp_path):
    document = sample_document()
    root_order, sections, fields = document_rows(document)
    assert root_order == ['schema_version', 'system', 'shared', 'profiles', 'future_root_key']
    assert sections[('profile', 'zeta')] == 0 and sections[('profile', 'default')] == 1
    parsed = {key: (position, json.loads(text)) for key, (position, text) in fields.items()}
    assert json.dumps(build_document(root_order, sections, parsed)) == json.dumps(document)

    target = store(tmp_path)
    target.create(document, meta={})
    loaded = ConfigStore(target.path, lambda: 1.0).load().document
    assert json.dumps(loaded, ensure_ascii=False) == json.dumps(document, ensure_ascii=False)
    assert list(loaded['profiles']) == ['zeta', 'default']
    assert list(loaded['profiles']['default']) == list(document['profiles']['default'])


def test_repository_export_is_byte_identical_after_reopening(tmp_path):
    repository = ProfileRepository(tmp_path)
    repository.create_profile({'id': 'second', 'name': '第二个', 'description': 'desc'})
    repository.create_profile({'id': 'alpha', 'name': 'Alpha'})
    repository.update_profile('second', {'name': '改名'})
    exported = json.dumps(repository.export_all(), ensure_ascii=False)
    reopened = ProfileRepository(tmp_path)
    assert json.dumps(reopened.export_all(), ensure_ascii=False) == exported
    assert [item['id'] for item in reopened.list_profiles()] == ['default', 'second', 'alpha']
    profile = reopened.get_profile('second')
    assert (profile['name'], profile['description'], profile['_revision']) == ('改名', 'desc', 1)


def test_pragmas_and_file_mode(tmp_path, monkeypatch):
    monkeypatch.setenv('CONFIGFLOW_LOCK_TIMEOUT_SECONDS', '7')
    repository = ProfileRepository(tmp_path)
    connection = repository._store.connect()
    try:
        assert connection.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        assert connection.execute('PRAGMA synchronous').fetchone()[0] == 2
        assert connection.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
        assert connection.execute('PRAGMA busy_timeout').fetchone()[0] == 7000
        assert connection.execute('PRAGMA application_id').fetchone()[0] == 0x43464C57
    finally:
        connection.close()
    assert stat.S_IMODE(repository.db_path.stat().st_mode) == 0o600


def test_wal_mode_is_reset_to_delete_at_startup(tmp_path):
    ProfileRepository(tmp_path)
    with sqlite3.connect(tmp_path / 'configflow.sqlite3') as connection:
        assert connection.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
    connection.close()
    repository = ProfileRepository(tmp_path)
    connection = repository._store.connect()
    try:
        assert connection.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
    finally:
        connection.close()


def test_commit_writes_only_changed_rows(tmp_path):
    target = store(tmp_path)
    base = target.create(sample_document(), meta={})
    assert base.head.generation == 1
    document = sample_document()
    document['profiles']['zeta']['name'] = 'Renamed'
    entry = target.commit(base, document)
    assert entry.head == (base.head.store_id, 2)
    stored = rows(target.path)
    assert stored[('profile', 'zeta', 'name')] == (dumps('Renamed'), 2)
    assert {key for key, (_, revision) in stored.items() if revision == 2} == {('profile', 'zeta', 'name')}
    assert entry.document == document
    # Unchanged rows reuse the previous values instead of parsing them again.
    assert entry.document['shared'] is not base.document['shared']
    assert entry.document['shared']['nodes'] is base.document['shared']['nodes']


def test_deleting_a_profile_cascades_its_fields_and_reorders(tmp_path):
    target = store(tmp_path)
    base = target.create(sample_document(), meta={})
    document = sample_document()
    del document['profiles']['zeta']
    entry = target.commit(base, document)
    assert not [key for key in rows(target.path) if key[1] == 'zeta']
    assert target.load().document == document
    assert entry.head.generation == 2


def test_empty_diff_and_meta_only_commit_do_not_bump_generation(tmp_path):
    target = store(tmp_path)
    base = target.create(sample_document(), meta={})
    before = target.fingerprint()
    assert target.commit(base, sample_document()) is None
    assert target.fingerprint() == before
    entry = target.commit(base, sample_document(), meta={'note': {'kept': 1}})
    assert entry.head == base.head
    assert target.meta('note') == {'kept': 1}
    assert target.fingerprint()[:2] == before[:2]
    assert target.fingerprint() != before


def test_reordering_profiles_is_a_change(tmp_path):
    target = store(tmp_path)
    base = target.create(sample_document(), meta={})
    document = sample_document()
    document['profiles'] = dict(reversed(list(document['profiles'].items())))
    target.commit(base, document)
    assert list(target.load().document['profiles']) == ['default', 'zeta']


def test_head_moved_outside_the_lock_raises_conflict(tmp_path):
    target = store(tmp_path)
    base = target.create(sample_document(), meta={})
    with sqlite3.connect(target.path) as connection:
        connection.execute("UPDATE meta SET value = '5' WHERE key = 'generation'")
    connection.close()
    before = target.fingerprint()
    document = sample_document()
    document['shared']['_revision'] = 8
    with pytest.raises(StoreConflict):
        target.commit(base, document)
    assert target.fingerprint() == before


def test_non_json_value_fails_before_writing(tmp_path):
    target = store(tmp_path)
    base = target.create(sample_document(), meta={})
    before = target.fingerprint()
    document = sample_document()
    document['shared']['nodes'] = [object()]
    with pytest.raises(TypeError):
        target.commit(base, document)
    assert target.fingerprint() == before


def test_fingerprint_is_stable_across_reads_and_changes_on_write(tmp_path):
    repository = ProfileRepository(tmp_path)
    first = repository._store.fingerprint()
    repository.get_system()
    repository.export_all()
    ProfileRepository(tmp_path).list_profiles()
    assert repository._store.fingerprint() == first
    repository.update_profile('default', {'description': 'changed'})
    assert repository._store.fingerprint() != first
