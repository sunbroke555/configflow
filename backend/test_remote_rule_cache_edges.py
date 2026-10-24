"""Remote cache ownership and metadata edits stay safe during offline updates."""
import base64
from copy import deepcopy
import hashlib

import pytest
import requests

from backend.test_remote_rule_cache import app_source, cache_path, mock_remote  # noqa: F401
from backend.test_remote_ruleset_validation import NATIVE_MRS
from backend.utils.rule_utils import (
    read_validated_rule_cache, rule_cache_filename, rule_cache_read_path,
    sanitize_rule_name, write_validated_rule_cache,
)


def legacy_path(repository, rule, kind):
    relative = (f'by-id/{hashlib.sha256(rule["id"].encode()).hexdigest()}.list'
                if kind == 'id' else sanitize_rule_name(rule['name']) + '.list')
    return repository.shared_rules_dir() / relative


def write_legacy(repository, rule, kind, content):
    path = legacy_path(repository, rule, kind)
    repository.write_shared_text('rules/' + str(path.relative_to(repository.shared_rules_dir())), content)
    return path


@pytest.mark.parametrize('kind', ['id', 'name'])
@pytest.mark.parametrize('change', [
    {'url': 'https://rules.test/new-source.txt'}, {'behavior': 'classical'}, {'format': 'yaml'},
])
def test_proposed_source_cannot_borrow_existing_legacy_cache(app_source, kind, change):
    _, repository, rule = app_source
    path = write_legacy(repository, rule, kind, b'old.example')
    proposed = {**rule, **change}
    assert read_validated_rule_cache(rule) == b'old.example'
    assert rule_cache_read_path(proposed) != str(path)
    assert read_validated_rule_cache(proposed) is None
    assert path.read_bytes() == b'old.example'


@pytest.mark.parametrize('kind', ['id', 'name'])
def test_stale_snapshot_cannot_claim_current_source_legacy_cache(app_source, kind):
    _, repository, rule = app_source
    old = deepcopy(rule)
    snapshot = {'rule_library': [old]}
    rule['url'] = 'https://rules.test/current-source.txt'
    repository.save_shared({'rule_library': [rule]})
    legacy = write_legacy(repository, rule, kind, b'current.example')
    assert read_validated_rule_cache(rule) == b'current.example'
    assert read_validated_rule_cache(old, snapshot) is None
    assert rule_cache_read_path(old, snapshot['rule_library']) != str(legacy)
    # A source-keyed snapshot cache has its own identity and remains usable.
    write_validated_rule_cache(old, b'previous.example')
    assert read_validated_rule_cache(old, snapshot) == b'previous.example'
    assert read_validated_rule_cache(rule) == b'current.example'


@pytest.mark.parametrize('kind', ['id', 'name'])
def test_old_invalid_legacy_cache_is_never_promoted_or_served(app_source, monkeypatch, kind):
    client, repository, rule = app_source
    original = b'valid.example\nDOMAIN,wrong.example\n'
    legacy = write_legacy(repository, rule, kind, original)
    assert read_validated_rule_cache(rule) is None
    mock_remote(monkeypatch, original)
    response = client.get('/api/rules/local/' + rule['name'])
    assert response.status_code == 422
    assert response.json['errors'][0]['line'] == 2
    assert response.json['cache_retained'] is False
    assert legacy.read_bytes() == original
    assert not cache_path(repository, rule).exists()


@pytest.mark.parametrize('change', [{'name': '改名后'}, {'enabled': False}])
def test_metadata_edit_does_not_download_and_preserves_valid_cache(app_source, monkeypatch, change):
    client, repository, rule = app_source
    write_validated_rule_cache(rule, b'+.old.example\r\n')
    path = cache_path(repository, rule)
    calls = []
    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise requests.Timeout('remote unavailable')
    monkeypatch.setattr('backend.utils.rule_utils.request_rule', forbidden)
    response = client.put('/api/rule-library/' + rule['id'], json={**rule, **change})
    assert response.status_code == 200, response.json
    assert calls == []
    current = repository.get_shared()['rule_library'][0]
    assert all(current[key] == value for key, value in change.items())
    assert read_validated_rule_cache(current) == b'+.old.example\r\n'
    assert path.read_bytes() == b'+.old.example\r\n'


def test_offline_rename_preserves_last_valid_legacy_name_cache(app_source, monkeypatch):
    client, repository, rule = app_source
    write_legacy(repository, rule, 'name', b'legacy.example\r\n')
    def forbidden(*args, **kwargs):
        pytest.fail('Renaming a source must not download it')
    monkeypatch.setattr('backend.utils.rule_utils.request_rule', forbidden)
    response = client.put('/api/rule-library/' + rule['id'], json={**rule, 'name': '离线改名'})
    assert response.status_code == 200, response.json
    current = repository.get_shared()['rule_library'][0]
    assert read_validated_rule_cache(current) == b'legacy.example\r\n'


@pytest.mark.parametrize('behavior', ['domain', 'ipcidr'])
def test_create_cache_refresh_and_stale_delivery_preserve_native_mrs_bytes(app_source, monkeypatch, behavior):
    client, repository, existing = app_source
    raw = base64.b64decode(NATIVE_MRS[behavior])
    item = {**existing, 'id': 'binary', 'name': '二进制规则', 'behavior': behavior,
            'url': 'https://rules.test/binary.mrs', 'format': 'mrs'}
    mock_remote(monkeypatch, raw)
    created = client.post('/api/rule-library', json=item)
    assert created.status_code == 200, created.json
    saved = next(rule for rule in repository.get_shared()['rule_library'] if rule['id'] == 'binary')
    path = cache_path(repository, saved)
    assert path.read_bytes() == raw
    refreshed = client.post('/api/rule-library/cache', json={'rule_ids': ['binary']})
    assert refreshed.json['success_count'] == 1
    assert path.read_bytes() == raw
    mock_remote(monkeypatch, raw[:-1])
    stamp = path.stat().st_mtime_ns
    rejected = client.post('/api/rule-library/cache', json={'rule_ids': ['binary']})
    assert rejected.json['failed_count'] == 1
    assert rejected.json['results'][0]['cache_retained'] is True
    assert 'MRS' in rejected.json['results'][0]['error']
    for suffix in ('', '?target=mihomo'):
        delivered = client.get('/api/rules/local/' + saved['name'] + suffix)
        assert delivered.status_code == 200, delivered.data
        assert delivered.headers['X-ConfigFlow-Cache'] == 'stale'
        assert delivered.data == raw
    assert path.read_bytes() == raw and path.stat().st_mtime_ns == stamp


@pytest.mark.parametrize('behavior', ['domain', 'ipcidr'])
def test_mrs_type_change_failure_preserves_catalog_and_cache(app_source, monkeypatch, behavior):
    client, repository, rule = app_source
    rule.update(behavior=behavior, format='mrs', url='https://rules.test/binary.mrs')
    repository.save_shared({'rule_library': [rule]})
    raw = base64.b64decode(NATIVE_MRS[behavior])
    write_validated_rule_cache(rule, raw)
    before = repository.export_all()
    mock_remote(monkeypatch, raw)
    other = 'ipcidr' if behavior == 'domain' else 'domain'
    rejected = client.put('/api/rule-library/' + rule['id'], json={**rule, 'behavior': other})
    assert rejected.status_code == 400
    assert f'实际类型为 {behavior}' in rejected.json['message']
    assert repository.export_all() == before
    assert read_validated_rule_cache(rule) == raw
