"""Rule delivery uses the shared transport without proxying internal tokens."""
import io
import zipfile
from urllib.parse import urlencode

import pytest
import requests
from flask import Flask

from backend.common import config as config_module
from backend.common.config_repository import ProfileRepository
from backend.routes import register_blueprints
from backend.routes import generate


PROXY = 'http://proxy-user:proxy-password@proxy.test:8080'


@pytest.fixture
def repository(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    monkeypatch.setattr(config_module, '_repository', repository)
    config_module.reset_config_context()
    repository.update_system_transaction(lambda system: system['system_config'].update({
        'rule_fetch_proxy': PROXY,
        'server_domain': 'https://config.test',
    }))
    return repository


def response(status=200, content='domain:example.test', location=None):
    result = requests.Response()
    result.status_code = status
    result._content = content.encode()
    result._content_consumed = True
    result.encoding = 'utf-8'
    if location is not None:
        result.headers['Location'] = location
    return result


def make_generation_client(monkeypatch, urls):
    app = Flask(__name__)
    register_blueprints(app)
    monkeypatch.setattr(generate, 'generate_mosdns_config', lambda *args, **kwargs: 'plugins: []')
    monkeypatch.setattr(generate, 'get_mosdns_custom_files', lambda *args, **kwargs: [])
    monkeypatch.setattr(generate, 'get_mosdns_ruleset_downloads', lambda *args, **kwargs: [
        {'name': f'Rules-{index}', 'url': url, 'local_path': f'./rules/{index}.txt'}
        for index, url in enumerate(urls)
    ])
    return app.test_client()


@pytest.mark.parametrize('configured_origin', [True, False])
def test_mosdns_zip_uses_rule_proxy_without_http_callbacks(repository, monkeypatch, configured_origin):
    if not configured_origin:
        repository.update_system_transaction(lambda system: system['system_config'].update({'server_domain': ''}))
    urls = ['https://rules.test/list', 'https://config.test/api/mosdns/rule-proxy?' + urlencode({
        'url': 'https://rules.test/converted', 'token': 'internal-secret',
    })]
    client = make_generation_client(monkeypatch, urls)
    observed = []

    def fetch(actual_url, **kwargs):
        observed.append((actual_url, kwargs))
        return response()

    monkeypatch.setattr(requests, 'get', fetch)
    monkeypatch.setattr('backend.utils.rule_fetch._RuleSession.get', lambda self, url, **kwargs: fetch(url, **kwargs))
    result = client.post('/api/generate/mosdns', json={'base_url': 'https://config.test'})

    assert result.status_code == 200
    with zipfile.ZipFile(io.BytesIO(result.data)) as archive:
        assert archive.read('rules/0.txt') == b'domain:example.test'
        assert archive.read('rules/1.txt') == b'domain:example.test'
    assert {url for url, _ in observed} == {'https://rules.test/list', 'https://rules.test/converted'}
    assert all(kwargs['proxies'] == {'http': PROXY, 'https': PROXY} for _, kwargs in observed)


def test_mosdns_zip_rejects_callback_without_source_or_leaking_token(repository, monkeypatch):
    client = make_generation_client(monkeypatch, ['https://config.test/api/mosdns/rule-proxy?token=internal-secret'])
    observed = []

    def fetch(actual_url, **kwargs):
        observed.append(actual_url)
        return response(302, location='https://external.test/api/rules?token=internal-secret')

    monkeypatch.setattr(requests, 'get', fetch)
    result = client.post('/api/generate/mosdns', json={})

    assert result.status_code == 500
    assert observed == []  # Malformed callback is rejected before any self-HTTP.
    assert b'internal-secret' not in result.data
    assert b'proxy-password' not in result.data


def test_mosdns_zip_download_error_does_not_leak_proxy_credentials(repository, monkeypatch):
    client = make_generation_client(monkeypatch, ['https://rules.test/list'])

    def fetch(actual_url, **kwargs):
        raise requests.exceptions.ProxyError(f'Cannot connect to {PROXY}')

    monkeypatch.setattr(requests, 'get', fetch)
    monkeypatch.setattr('backend.utils.rule_fetch._RuleSession.get', lambda self, url, **kwargs: fetch(url, **kwargs))
    result = client.post('/api/generate/mosdns', json={})

    assert result.status_code == 500
    assert b'proxy-password' not in result.data
