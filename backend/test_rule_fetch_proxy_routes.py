"""Check the persisted proxy reaches each rule-download entry point."""
import io
import logging

import pytest
import requests
from flask import Flask

from backend.common import config as config_module
from backend.common.config_repository import ProfileRepository
from backend.routes import register_blueprints
from backend.routes.rules import get_ruleset_content
from backend.utils.rule_fetch import request_rule
from backend.utils.rule_utils import save_rule_to_local


PROXY = 'http://proxy-user:proxy-secret@proxy.test:7890'
SOURCE = 'https://raw.githubusercontent.com/example/rules/main/direct.list'
MIRROR = 'https://mirror.test/'
BODY = 'DOMAIN,example.test'


@pytest.fixture
def rule_app(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    repository.update_system_transaction(lambda system: system['system_config'].update({
        'rule_fetch_proxy': PROXY, 'github_proxy_domain': MIRROR,
        'server_domain': 'https://config.test',
    }))
    repository.save_shared({'rule_library': [{
        'id': 'source', 'name': 'Remote', 'source_type': 'url', 'url': SOURCE, 'behavior': 'classical',
        'enabled': True,
    }]})
    repository.create_profile({'id': 'alpha', 'name': 'Alpha'})
    config_module.set_repository(repository)
    monkeypatch.setattr('backend.common.auth.is_auth_enabled', lambda: False)
    app = Flask(__name__)
    register_blueprints(app)
    calls = []

    def fetch(method):
        def send(url, **kwargs):
            calls.append((method, url, kwargs))
            response = requests.Response()
            response.status_code = 200
            response.encoding = 'utf-8'
            response._content = BODY.encode()
            response.raw = io.BytesIO(response._content)
            return response
        return send

    monkeypatch.setattr(requests, 'get', fetch('get'))
    monkeypatch.setattr(requests, 'head', fetch('head'))
    monkeypatch.setattr('backend.utils.rule_fetch._RuleSession.get', lambda self, url, **kwargs: fetch('get')(url, **kwargs))
    return app, repository, calls


@pytest.mark.parametrize('path,body,method', [
    ('/api/rule-library/test-single', {'url': SOURCE}, 'get'),
    ('/api/rule-library/test', {}, 'head'),
    ('/api/rule-library/cache', {'rule_ids': ['source']}, 'get'),
    ('/api/rule-library', {'id': 'new', 'name': 'New', 'source_type': 'url', 'url': SOURCE, 'behavior': 'classical'}, 'get'),
])
def test_rule_library_fetches_use_global_proxy_and_original_first(rule_app, path, body, method):
    app, _, calls = rule_app
    response = app.test_client().post(path, json=body, headers={'X-ConfigFlow-Profile': 'alpha'})
    assert response.status_code == 200
    assert len(calls) == 1
    assert calls[0][0:2] == (method, SOURCE)
    assert calls[0][2]['proxies'] == {'http': PROXY, 'https': PROXY}


def test_local_refresh_and_ruleset_fallback_use_proxy(rule_app):
    app, repository, calls = rule_app
    response = app.test_client().get('/api/profiles/alpha/rules/local/Remote')
    assert response.status_code == 200
    assert response.get_data(as_text=True) == BODY
    from backend.utils.rule_utils import rule_cache_filename
    assert (repository.shared_rules_dir() / rule_cache_filename(repository.get_shared()['rule_library'][0])).read_text() == BODY
    with app.test_request_context('/api/profiles/alpha/rules'):
        # Different names exercise the URL fallbacks without hitting the cache.
        assert get_ruleset_content({'name': 'Fallback', 'url': SOURCE, 'behavior': 'classical'}) == BODY
        assert get_ruleset_content({}, {'name': 'Library', 'source_type': 'url', 'url': SOURCE, 'behavior': 'classical'}) == BODY
    assert len(calls) == 3
    assert all(url == SOURCE for _, url, _ in calls)
    assert all(options['proxies'] == {'http': PROXY, 'https': PROXY} for _, _, options in calls)


def test_local_refresh_fetches_legacy_entry_without_source_type(rule_app):
    app, repository, calls = rule_app
    repository.save_shared({'rule_library': [{
        'id': 'legacy', 'name': 'Legacy', 'url': SOURCE, 'behavior': 'classical', 'enabled': True,
    }]})
    from backend.utils.rule_utils import rule_cache_filename
    cache = repository.shared_rules_dir() / rule_cache_filename(repository.get_shared()['rule_library'][0])
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text('DOMAIN,stale.test')
    response = app.test_client().get('/api/profiles/alpha/rules/local/Legacy')
    assert response.get_data(as_text=True) == BODY
    assert cache.read_text() == BODY
    assert [url for _, url, _ in calls] == [SOURCE]


def test_mosdns_conversion_uses_proxy_and_original_hostname(rule_app):
    app, repository, calls = rule_app
    token = repository.get_system()['system_config']['rule_proxy_token']
    response = app.test_client().get('/api/mosdns/rule-proxy', query_string={'url': SOURCE, 'token': token})
    assert response.status_code == 200
    assert response.get_data(as_text=True) == 'full:example.test'
    assert calls[0][1] == SOURCE
    assert calls[0][2]['proxies'] == {'http': PROXY, 'https': PROXY}


def test_internal_callback_with_github_query_bypasses_both_proxies(rule_app):
    _, _, calls = rule_app
    url = 'https://CONFIG.test:443/api/mosdns/rule-proxy?url=' + SOURCE + '&token=internal'
    request_rule(url)
    assert calls[0][1] == url
    assert calls[0][2]['proxies'] == {'http': '', 'https': ''}


def test_clearing_setting_affects_next_download(rule_app):
    app, _, calls = rule_app
    client = app.test_client()
    assert client.post('/api/settings/rule-fetch-proxy', json={'rule_fetch_proxy': ''}).status_code == 200
    assert client.post('/api/rule-library/test-single', json={'url': SOURCE}).status_code == 200
    assert calls[0][2]['proxies'] == {'http': '', 'https': ''}


def test_proxy_failure_preserves_cache_and_redacts_errors(rule_app, monkeypatch, caplog):
    app, repository, _ = rule_app
    repository.write_shared_text('rules/Remote.list', 'cached')
    def fail(*args, **kwargs):
        raise requests.exceptions.ProxyError('Failed through ' + PROXY)
    monkeypatch.setattr(requests, 'get', fail)
    monkeypatch.setattr(requests, 'head', fail)
    with caplog.at_level(logging.WARNING):
        client = app.test_client()
        responses = [
            client.post('/api/rule-library/test-single', json={'url': SOURCE}),
            client.post('/api/rule-library/test', json={}),
            client.post('/api/rule-library/cache', json={'rule_ids': ['source']}),
            client.get('/api/rules/local/Remote'),
        ]
        with pytest.raises(requests.RequestException) as error:
            save_rule_to_local({'name': 'Remote', 'source_type': 'url', 'url': SOURCE, 'behavior': 'classical'})
    assert responses[0].status_code == 500
    assert responses[1].json['failed_count'] == 1
    assert responses[2].json['failed_count'] == 1
    assert responses[3].get_data(as_text=True) == 'cached'
    assert (repository.shared_rules_dir() / 'Remote.list').read_text() == 'cached'
    text = caplog.text + str(error.value) + ''.join(r.get_data(as_text=True) for r in responses)
    assert 'proxy-secret' not in text
    assert 'proxy-user' not in text
