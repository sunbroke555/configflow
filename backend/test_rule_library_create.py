"""A failed import must not publish a source or overwrite another cache."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
import requests
from flask import Flask

from backend.common import config as config_store
from backend.common.config_repository import ProfileRepository, ProfileValidationError
from backend.routes import register_blueprints
from backend.utils.rule_utils import rule_cache_filename, save_rule_to_local


@pytest.fixture
def library(tmp_path, monkeypatch):
    previous = config_store._repository
    repository = ProfileRepository(tmp_path)
    config_store.set_repository(repository)
    monkeypatch.setattr('backend.common.auth.is_auth_enabled', lambda: False)
    app = Flask(__name__)
    register_blueprints(app)
    yield app, repository
    config_store.set_repository(previous)


def source(identity, **fields):
    return {'id': identity, 'name': identity, 'source_type': 'url', 'behavior': 'domain',
            'url': 'https://rules.test/' + identity, **fields}


def reply(content='new.test', status=200):
    response = requests.Response()
    response.status_code = status
    response.encoding = 'utf-8'
    response._content = content.encode()
    return response


def cache_path(repository, rule):
    return repository.shared_rules_dir() / rule_cache_filename(rule)


def test_failed_download_does_not_publish_source_or_cache(library, monkeypatch):
    app, repository = library
    existing = source('custom', name='failed', source_type='content', content='custom.test')
    repository.save_shared({'rule_library': [existing]})
    save_rule_to_local(existing)
    repository.write_shared_text('rules/failed.list', 'legacy-custom.test')
    custom_cache = cache_path(repository, existing)
    custom_mtime = custom_cache.stat().st_mtime_ns
    before = repository.export_all()
    rule = source('failed')
    monkeypatch.setattr('backend.utils.rule_utils.request_rule', lambda *a, **kw: reply(status=503))

    response = app.test_client().post('/api/rule-library', json=rule)

    assert response.status_code == 502
    assert response.json['success'] is False
    assert '未添加规则' in response.json['message']
    assert repository.export_all() == before
    assert not cache_path(repository, rule).exists()
    assert custom_cache.read_text() == 'custom.test'
    assert custom_cache.stat().st_mtime_ns == custom_mtime
    assert (repository.shared_rules_dir() / 'failed.list').read_text() == 'legacy-custom.test'


def test_mixed_import_keeps_only_successful_sources_and_caches(library, monkeypatch):
    app, repository = library
    monkeypatch.setattr('backend.utils.rule_utils.request_rule',
                        lambda url, **kw: reply(url.rsplit('/', 1)[-1] + '.test', 503 if url.endswith('bad') else 200))
    client = app.test_client()
    results = [client.post('/api/rule-library', json=source(identity)) for identity in ('first', 'bad', 'last')]

    assert [result.status_code for result in results] == [200, 502, 200]
    assert [item['id'] for item in client.get('/api/rule-library').json] == ['first', 'last']
    assert cache_path(repository, source('first')).read_text() == 'first.test'
    assert cache_path(repository, source('last')).read_text() == 'last.test'
    assert not cache_path(repository, source('bad')).exists()


@pytest.mark.parametrize('failure', ['cache-before-write', 'cache-after-write', 'document-before-write', 'document-after-write', 'validation'])
def test_failed_commit_rolls_back_new_source_and_cache(library, monkeypatch, failure):
    app, repository = library
    rule = source('new', source_type='content', content='new.test')
    path = cache_path(repository, rule)
    before = repository._store.fingerprint()
    original_atomic = repository._write_atomic
    original_apply = repository._store._apply

    def atomic(target, content):
        if target == path and failure == 'cache-before-write':
            raise OSError('controlled cache failure')
        original_atomic(target, content)
        if target == path and failure == 'cache-after-write':
            raise OSError('controlled post-replace cache failure')

    def apply(*args):
        if failure == 'document-before-write':
            raise OSError('controlled document failure')
        original_apply(*args)
        if failure == 'document-after-write':
            raise OSError('controlled post-write document failure')

    monkeypatch.setattr(repository, '_write_atomic', atomic)
    monkeypatch.setattr(repository._store, '_apply', apply)
    if failure == 'validation':
        original_validate = repository._validate_document

        def validate(document):
            original_validate(document)
            if document['shared']['rule_library']:
                raise ProfileValidationError('controlled validation failure')
        monkeypatch.setattr(repository, '_validate_document', validate)

    response = app.test_client().post('/api/rule-library', json=rule)

    assert response.status_code == (400 if failure == 'validation' else 500)
    assert response.json['success'] is False
    assert repository._store.fingerprint() == before
    assert repository.get_shared()['rule_library'] == []
    assert not path.exists()
    assert not path.with_name(path.name + '.bak').exists()
    assert not list(path.parent.glob('*.tmp'))


def test_duplicate_id_preserves_existing_custom_and_legacy_cache(library, monkeypatch):
    app, repository = library
    original = source('existing', source_type='content', content='custom.test', name='Custom')
    repository.save_shared({'rule_library': [original]})
    save_rule_to_local(original)
    repository.write_shared_text('rules/Custom.list', 'legacy-custom.test')
    path = cache_path(repository, original)
    before = repository.export_all()
    before_stat = path.stat()
    fetch = lambda *a, **kw: pytest.fail('duplicate IDs must be rejected before downloading')
    monkeypatch.setattr('backend.utils.rule_utils.request_rule', fetch)

    response = app.test_client().post('/api/rule-library', json=source('existing', name='Replacement'))

    assert response.status_code == 409
    assert repository.export_all() == before
    assert path.read_text() == 'custom.test'
    assert path.stat().st_mtime_ns == before_stat.st_mtime_ns
    assert (repository.shared_rules_dir() / 'Custom.list').read_text() == 'legacy-custom.test'


@pytest.mark.parametrize('committed', [False, True])
def test_commit_failure_has_truthful_creation_result(library, monkeypatch, committed):
    app, repository = library
    rule = source('new', source_type='content', content='new.test')
    path = cache_path(repository, rule)
    before = repository._store.fingerprint()
    original_commit = repository._store._commit
    reported = False

    def commit(connection):
        nonlocal reported
        reported = True
        if committed:
            # The outcome is unknown to the caller: COMMIT happened, yet failed.
            original_commit(connection)
        raise OSError('controlled COMMIT failure')

    monkeypatch.setattr(repository._store, '_commit', commit)

    response = app.test_client().post('/api/rule-library', json=rule)

    assert reported
    if committed:
        assert response.status_code == 503
        assert response.json['confirmation_required'] is True
        assert '待确认' in response.json['message']
        assert '未添加' not in response.json['message']
        assert repository.get_shared()['rule_library'][0]['id'] == 'new'
        assert path.read_text() == 'new.test'
    else:
        assert response.status_code == 500
        assert '未添加' in response.json['message']
        assert repository._store.fingerprint() == before
        assert repository.get_shared()['rule_library'] == []
        assert not path.exists()


@pytest.mark.parametrize('backup_only', [False, True])
def test_orphaned_custom_cache_is_not_overwritten_by_creation(library, backup_only):
    app, repository = library
    rule = source('orphan', source_type='content', content='replacement.test')
    path = cache_path(repository, rule)
    repository.write_shared_text('rules/' + rule_cache_filename(rule), 'orphaned-custom.test')
    existing = path.with_name(path.name + '.bak') if backup_only else path
    if backup_only:
        path.rename(existing)
    before = existing.stat()

    response = app.test_client().post('/api/rule-library', json=rule)

    assert response.status_code == 409
    assert repository.get_shared()['rule_library'] == []
    assert existing.read_text() == 'orphaned-custom.test'
    assert existing.stat().st_mtime_ns == before.st_mtime_ns
    if backup_only:
        assert not path.exists()


def test_late_download_cannot_replace_a_concurrently_created_custom_source(library, monkeypatch):
    app, repository = library
    fetching, finish = Event(), Event()

    def fetch(*a, **kw):
        fetching.set()
        assert finish.wait(5)
        return reply('late-download.test')
    monkeypatch.setattr('backend.utils.rule_utils.request_rule', fetch)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(lambda: app.test_client().post('/api/rule-library', json=source('same-id')))
        try:
            assert fetching.wait(5)
            winner = source('same-id', name='Custom winner', source_type='content', content='winner.test')
            assert app.test_client().post('/api/rule-library', json=winner).status_code == 200
            save_rule_to_local({**winner, 'content': 'newer-custom-cache.test'})
        finally:
            finish.set()
        assert pending.result(timeout=5).status_code == 409

    assert repository.get_shared()['rule_library'] == [{key: value for key, value in winner.items() if key != 'url'}]
    assert cache_path(repository, winner).read_text() == 'newer-custom-cache.test'


def test_failed_commit_cannot_remove_a_waiting_cache_writers_new_data(library, monkeypatch):
    app, repository = library
    rule = source('new', source_type='content', content='uncommitted.test')
    commit, fail, writer_started = Event(), Event(), Event()
    def apply(*args):
        commit.set()
        assert fail.wait(5)
        raise OSError('controlled commit failure')

    def write_new_cache():
        writer_started.set()
        repository.write_shared_text('rules/' + rule_cache_filename(rule), 'concurrent-cache.test')

    monkeypatch.setattr(repository._store, '_apply', apply)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(lambda: app.test_client().post('/api/rule-library', json=rule))
        try:
            assert commit.wait(5)
            writer = pool.submit(write_new_cache)
            assert writer_started.wait(5)
        finally:
            fail.set()
        assert pending.result(timeout=5).status_code == 500
        writer.result(timeout=5)

    assert repository.get_shared()['rule_library'] == []
    assert cache_path(repository, rule).read_text() == 'concurrent-cache.test'


def test_existing_source_refresh_still_falls_back_to_custom_cache(library, monkeypatch):
    app, repository = library
    rule = source('existing')
    repository.save_shared({'rule_library': [rule]})
    repository.write_shared_text('rules/' + rule_cache_filename(rule), 'cached.test')
    monkeypatch.setattr('backend.utils.rule_utils.request_rule', lambda *a, **kw: reply(status=503))
    monkeypatch.setattr('backend.routes.rules.request_rule', lambda *a, **kw: reply(status=503))

    with pytest.raises(requests.RequestException):
        save_rule_to_local(rule)
    response = app.test_client().get('/api/rules/local/existing')

    assert response.status_code == 200
    assert response.get_data(as_text=True) == 'cached.test'
    assert repository.get_shared()['rule_library'] == [rule]
    assert cache_path(repository, rule).read_text() == 'cached.test'
