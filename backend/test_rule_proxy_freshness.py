"""A live refresh must never replace newer device rules with an old library cache."""
import io
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlencode

import pytest
from flask import Flask

from backend.common import config as config_module
from backend.common.config_repository import ProfileRepository
from backend.routes import register_blueprints
from backend.utils.mosdns_archive import build_mosdns_zip
from backend.utils.rule_utils import rule_cache_filename


OLD = 'DOMAIN,old.test\nIP-CIDR,192.0.2.0/24'
NEW = 'DOMAIN,new.test\nIP-CIDR,198.51.100.0/24'


@pytest.fixture
def origin():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            body = self.server.body.encode()
            self.send_response(self.server.status)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.body, server.status = OLD, 200
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f'http://127.0.0.1:{server.server_port}/rules?token=origin-private'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.fixture
def app_repo(tmp_path, monkeypatch, origin):
    _, url = origin
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY'):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    repository = ProfileRepository(tmp_path)
    repository.create_profile({'id': 'office', 'name': 'Office'})
    repository.save_shared({'rule_library': [
        {'id': 'remote', 'name': 'Remote', 'source_type': 'url',
         'url': url, 'behavior': 'classical', 'enabled': True},
        {'id': 'inline', 'name': 'Inline', 'source_type': 'content',
         'content': 'DOMAIN,inline.test', 'enabled': True},
    ]})
    for profile in ('default', 'office'):
        repository.save_profile(profile, {'rule_configs': [
            {'id': 'selected', 'itemType': 'ruleset', 'library_rule_id': 'remote',
             'policy': 'DIRECT', 'enabled': True},
            {'id': 'local', 'itemType': 'ruleset', 'library_rule_id': 'inline',
             'policy': 'DIRECT', 'enabled': True},
        ]})
    repository.write_shared_text('rules/' + rule_cache_filename({'id': 'remote'}), OLD)
    config_module.set_repository(repository)
    monkeypatch.setattr('backend.common.auth.is_auth_enabled', lambda: False)
    app = Flask(__name__)
    register_blueprints(app)
    try:
        yield app, repository
    finally:
        config_module.reset_config_context()


@pytest.mark.parametrize('endpoint', ['/api/mosdns/rule-proxy',
    '/api/profiles/default/mosdns/rule-proxy', '/api/profiles/office/mosdns/rule-proxy'])
@pytest.mark.parametrize('part,expected', [('domain', 'full:new.test'),
                                         ('ip', '198.51.100.0/24')])
def test_real_origin_b_then_failure_cannot_serve_cache_a(app_repo, origin, endpoint, part, expected, caplog):
    app, repository = app_repo
    server, url = origin
    token = repository.get_system()['system_config']['rule_proxy_token']
    query = {'url': url, 'part': part, 'token': token}
    client = app.test_client()
    server.body = NEW
    fresh = client.get(endpoint, query_string=query)
    assert fresh.status_code == 200
    assert fresh.get_data(as_text=True) == expected
    assert fresh.headers['Cache-Control'] == 'no-store'
    cache = repository.shared_rules_dir() / rule_cache_filename({'id': 'remote'})
    assert cache.read_text() == OLD  # Deliberately older than the device's B.

    server.status = 503
    unavailable = client.get(endpoint, query_string=query)
    assert unavailable.status_code == 502
    assert unavailable.json['success'] is False
    assert unavailable.json['code'] == 'RULE_SOURCE_UNAVAILABLE'
    assert unavailable.headers['Cache-Control'] == 'no-store'
    assert 'full:old.test' not in unavailable.get_data(as_text=True)
    assert 'origin-private' not in unavailable.get_data(as_text=True) + caplog.text
    assert token not in unavailable.get_data(as_text=True) + caplog.text
    assert cache.read_text() == OLD

    server.status = 200
    recovered = client.get(endpoint, query_string=query)
    assert recovered.status_code == 200
    assert recovered.get_data(as_text=True) == expected


def test_inline_rules_remain_available_while_origin_is_down(app_repo, origin):
    app, repository = app_repo
    server, _ = origin
    server.status = 503
    token = repository.get_system()['system_config']['rule_proxy_token']
    result = app.test_client().get('/api/profiles/office/mosdns/rule-proxy', query_string={
        'url': '/api/profiles/office/rule-library/content/inline',
        'token': token, 'part': 'domain',
    })
    assert result.status_code == 200
    assert result.get_data(as_text=True) == 'full:inline.test'


def test_explicit_offline_zip_still_uses_matching_cache(app_repo, origin):
    _, repository = app_repo
    server, url = origin
    server.status = 503
    config = repository.get_compat_config('office')
    callback = '/api/profiles/office/mosdns/rule-proxy?' + urlencode({'url': url, 'part': 'domain'})
    archive = build_mosdns_zip(config, 'plugins: []', [
        {'name': 'Remote', 'url': callback, 'local_path': './rules/remote.txt'},
    ], [])
    with zipfile.ZipFile(io.BytesIO(archive.getvalue())) as output:
        assert output.read('rules/remote.txt') == b'full:old.test'
