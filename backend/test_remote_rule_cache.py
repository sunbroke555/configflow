"""Reject complete invalid refreshes while preserving previous validated bytes."""
from copy import deepcopy

import pytest
import requests
from flask import Flask

from backend.common import config as config_store
from backend.common.config_repository import ProfileRepository
from backend.routes import register_blueprints
from backend.routes.rules import get_ruleset_content
from backend.utils.rule_utils import (
    rule_cache_filename, read_validated_rule_cache, refresh_remote_rule, save_rule_to_local,
    write_validated_rule_cache,
)
from backend.utils.ruleset_content import RemoteRuleValidationError


@pytest.fixture
def app_source(tmp_path, monkeypatch):
    previous = config_store._repository
    repository = ProfileRepository(tmp_path)
    config_store.set_repository(repository)
    monkeypatch.setattr('backend.common.auth.is_auth_enabled', lambda: False)
    app = Flask(__name__)
    register_blueprints(app)
    rule = dict(id='remote', name='远端规则', source_type='url', url='https://rules.test/remote.txt',
                behavior='domain', format='text', enabled=True)
    repository.save_shared({'rule_library': [rule]})
    yield app.test_client(), repository, rule
    config_store.set_repository(previous)


def response(raw):
    result = requests.Response()
    result.status_code = 200
    result._content = raw
    return result


def mock_remote(monkeypatch, raw):
    for module in ('rule_utils',):
        monkeypatch.setattr(f'backend.utils.{module}.request_rule', lambda *a, **kw: response(raw))
    monkeypatch.setattr('backend.routes.rules.request_rule', lambda *a, **kw: response(raw))


def cache_path(repository, rule):
    return repository.shared_rules_dir() / rule_cache_filename(rule)


@pytest.mark.parametrize(('behavior', 'valid', 'invalid'), [
    ('domain', b'+.example.com\n*.test.com\n', b'good.test\nDOMAIN-SUFFIX,example.com\n'),
    ('ipcidr', b'10.0.0.0/8\n2001:db8::/32\n', b'10.0.0.0/8\n+.example.com\n'),
    ('classical', b'DOMAIN,example.com\n+.test.com\n', b'DOMAIN,example.com\nfull:other.test\n'),
])
def test_refresh_rejects_entire_body_keeps_bytes_and_enabled(app_source, monkeypatch, behavior, valid, invalid):
    client, repository, rule = app_source
    rule['behavior'] = behavior
    repository.save_shared({'rule_library': [rule]})
    write_validated_rule_cache(rule, valid)
    path = cache_path(repository, rule)
    stamp = path.stat().st_mtime_ns
    before = repository.export_all()
    mock_remote(monkeypatch, invalid)
    result = client.post('/api/rule-library/cache', json={'rule_ids': [rule['id']]})
    assert result.status_code == 200
    assert result.json['failed_count'] == 1
    failure = result.json['results'][0]
    assert failure['cache_retained'] is True
    assert '第 2 行' in failure['error']
    assert invalid.decode().splitlines()[1] in failure['error']
    assert path.read_bytes() == valid
    assert path.stat().st_mtime_ns == stamp
    assert repository.export_all() == before
    for suffix in ('', '?target=mihomo'):
        delivered = client.get('/api/rules/local/' + rule['name'] + suffix)
        assert delivered.status_code == 200
        assert delivered.headers['X-ConfigFlow-Cache'] == 'stale'
        assert invalid.splitlines()[1] not in delivered.data
    assert path.read_bytes() == valid


def test_no_valid_cache_returns_specific_error_and_never_saves_partial_response(app_source, monkeypatch):
    client, repository, rule = app_source
    raw = b'good.test\nDOMAIN,other.test\n'
    mock_remote(monkeypatch, raw)
    result = client.get('/api/rules/local/' + rule['name'])
    assert result.status_code == 422
    assert '第 2 行' in result.json['message']
    assert result.json['errors'][0]['before'] == 'DOMAIN,other.test'
    assert not cache_path(repository, rule).exists()
    result = client.post('/api/rule-library/cache', json={'rule_ids': [rule['id']]})
    assert result.json['results'][0]['cache_retained'] is False
    with pytest.raises(RemoteRuleValidationError):
        get_ruleset_content({}, rule)
    assert not cache_path(repository, rule).exists()


def test_old_invalid_cache_is_never_used_as_fallback(app_source, monkeypatch):
    client, repository, rule = app_source
    path = cache_path(repository, rule)
    repository.write_shared_text('rules/' + rule_cache_filename(rule), 'DOMAIN,wrong.test')
    assert read_validated_rule_cache(rule) is None
    mock_remote(monkeypatch, b'DOMAIN-SUFFIX,wrong.test')
    assert client.get('/api/rules/local/' + rule['name']).status_code == 422
    assert get_ruleset_content({}, rule, allow_network=False) == ''
    assert path.read_text() == 'DOMAIN,wrong.test'


def test_good_refresh_recovers_and_preserves_original_framing(app_source, monkeypatch):
    client, repository, rule = app_source
    write_validated_rule_cache(rule, b'old.test')
    raw = b'\xef\xbb\xbf# source\r\n+.new.test\r\n'
    mock_remote(monkeypatch, raw)
    result = client.post('/api/rule-library/cache', json={'rule_ids': [rule['id']]})
    assert result.json['success_count'] == 1
    assert cache_path(repository, rule).read_bytes() == raw
    delivered = client.get('/api/rules/local/' + rule['name'])
    assert delivered.data == raw
    assert 'X-ConfigFlow-Cache' not in delivered.headers


@pytest.mark.parametrize('change', [{'behavior': 'ipcidr'}, {'url': 'https://rules.test/replaced.txt'}, {'format': 'yaml'}])
def test_failed_edit_preserves_declaration_and_previous_cache(app_source, monkeypatch, change):
    client, repository, rule = app_source
    write_validated_rule_cache(rule, b'old.test')
    before = repository.export_all()
    mock_remote(monkeypatch, b'<html>wrong upstream</html>')
    result = client.put('/api/rule-library/remote', json={**rule, **change})
    assert result.status_code == 400
    assert '远端规则集' in result.json['message']
    assert repository.export_all() == before
    assert read_validated_rule_cache(rule) == b'old.test'


def test_invalid_create_does_not_commit_catalog_entry(app_source, monkeypatch):
    client, repository, rule = app_source
    before = repository.export_all()
    mock_remote(monkeypatch, b'DOMAIN,wrong.test')
    result = client.post('/api/rule-library', json={**rule, 'id': 'new', 'name': '新增规则'})
    assert result.status_code == 400
    assert 'DOMAIN,wrong.test' in result.json['message']
    assert repository.export_all() == before


def test_source_identity_prevents_late_old_refresh_overwriting_changed_source(app_source):
    _, repository, rule = app_source
    old = deepcopy(rule)
    rule['url'] = 'https://rules.test/new.txt'
    repository.save_shared({'rule_library': [rule]})
    write_validated_rule_cache(rule, b'new.test')
    write_validated_rule_cache(old, b'old.test')
    assert rule_cache_filename(rule) != rule_cache_filename(old)
    assert read_validated_rule_cache(rule) == b'new.test'
    assert read_validated_rule_cache(old) == b'old.test'


def test_network_failure_also_keeps_valid_cache(app_source):
    _, repository, rule = app_source
    write_validated_rule_cache(rule, b'old.test')
    def fail(_):
        raise requests.Timeout('controlled')
    value, error = refresh_remote_rule(rule, fetch=fail, allow_stale=True)
    assert value == b'old.test'
    assert isinstance(error, requests.Timeout)
    with pytest.raises(requests.Timeout):
        refresh_remote_rule(rule, fetch=fail)
    assert cache_path(repository, rule).read_bytes() == b'old.test'
