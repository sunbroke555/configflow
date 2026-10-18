import json
import subprocess
from types import SimpleNamespace

import pytest

from backend.common import config
from backend.common.config_repository import ProfileRepository
from backend.test_flow_ui_apis import make_client
from backend.utils import node_latency as latency


@pytest.fixture
def repository(tmp_path, monkeypatch):
    repo = ProfileRepository(tmp_path)
    config.set_repository(repo)
    repo.save_shared({'nodes': [{'id': 'a', 'name': 'A', 'type': 'trojan', 'server': 'example.invalid',
                                 'port': 443, 'password': 'secret', 'tls': True}]})
    monkeypatch.setattr(latency, '_probe_binary', lambda: '/test/probe')
    return repo


def test_raw_and_cached_node_credentials_are_preserved(repository, monkeypatch):
    raw = latency._proxy_of(latency.collect_candidates()['A'])
    assert raw['password'] == 'secret' and raw['tls'] is True
    node = {'name': 'nested', 'type': 'vless', 'server': 'example.invalid', 'port': 443,
            'params': {'uuid': 'uuid', 'network': 'ws', 'ws-opts': {'path': '/path'},
                       'reality-opts': {'public-key': 'public'}}}
    proxy = latency._proxy_of(node)
    assert proxy['uuid'] == 'uuid'
    assert proxy['ws-opts'] == {'path': '/path'}
    assert proxy['reality-opts']['public-key'] == 'public'
    assert 'short-id' not in node['params']['reality-opts']  # no mutation of the cache


def test_legacy_tcp_results_are_hidden_and_not_mixed(repository, monkeypatch):
    repository.write_shared_json(latency.LATENCY_FILE, {'A': {'latency': 2, 'history': [1, 2]}})
    assert latency.load_results() == {}
    monkeypatch.setattr(latency, '_run_probe', lambda proxies, options: {'A': {'latency': 150}})
    result = latency.run_tests(['A', 'A'])['results']['A']
    assert result['history'] == [150]
    assert result['method'] == latency.METHOD
    monkeypatch.setenv('CONFIGFLOW_NODE_TEST_URL', 'https://test.example/generate_204')
    assert latency.run_tests(['A'])['results']['A']['history'] == [150]


def test_worker_gets_stdin_json_and_no_credentials_in_command(repository, monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        payload = json.loads(kwargs['input'])
        assert payload['nodes'][0]['password'] == 'secret'
        assert kwargs['timeout'] == 10
        return SimpleNamespace(returncode=0, stdout=json.dumps({'version': 1, 'results': [
            {'name': 'A', 'latency': 99, 'first_latency': 250}]}))
    monkeypatch.setattr(latency.subprocess, 'run', run)
    result = latency.run_tests(['A'])['results']['A']
    assert calls == [['/test/probe']]
    assert result['latency'] == 99 and result['first_latency'] == 250


def test_subscription_cache_keeps_protocol_options_and_manual_priority(repository, monkeypatch):
    repository.save_shared({'subscriptions': [{'id': 'sub', 'name': 'test', 'enabled': True}]})
    monkeypatch.setattr(latency, 'load_subscription_cache', lambda _: {'nodes': [
        {'name': 'A', 'type': 'http', 'server': 'other.invalid', 'port': 443},
        {'name': 'cached', 'type': 'trojan', 'server': 'proxy.invalid', 'port': 443,
         'params': {'password': 'cached-secret', 'sni': 'tls.invalid'}},
    ]})
    candidates = latency.collect_candidates()
    assert candidates['A']['password'] == 'secret'
    proxy = latency._proxy_of(candidates['cached'])
    assert proxy['password'] == 'cached-secret' and proxy['sni'] == 'tls.invalid'


@pytest.mark.parametrize('output', ['not JSON', '{}', '{"version":1,"results":[]}',
    '{"version":1,"results":[{"name":"A","latency":true}]}',
    '{"version":1,"results":[{"name":"A","latency":null}]}'])
def test_invalid_worker_output_does_not_replace_history(repository, monkeypatch, output):
    monkeypatch.setattr(latency.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=output))
    with pytest.raises(latency.ProbeUnavailable):
        latency.run_tests(['A'])
    assert latency.load_results() == {}


def test_worker_timeout_releases_lock_and_preserves_results(repository, monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('/test/probe', 10, stderr='secret')
    monkeypatch.setattr(latency.subprocess, 'run', timeout)
    client = make_client(repository)
    for _ in range(2):
        response = client.post('/api/nodes/latency', json={'names': ['A']})
        assert response.status_code == 503
        assert b'secret' not in response.data
    assert latency.load_results() == {}


def test_parse_failure_is_a_result_not_missing(repository, monkeypatch):
    monkeypatch.setattr(latency, '_proxy_of', lambda node: None)
    result = latency.run_tests(['A', 'deleted'])
    assert result['results']['A']['error_code'] == 'config'
    assert result['missing'] == ['deleted']


def test_busy_and_batch_limits(repository):
    client = make_client(repository)
    with latency._test_lock:
        assert client.post('/api/nodes/latency', json={'names': ['A']}).status_code == 409
    assert client.post('/api/nodes/latency', json={'names': [str(i) for i in range(21)]}).status_code == 400
    for body in [[], 'text', {'names': [None]}, {'names': 'A'}]:
        assert client.post('/api/nodes/latency', json=body).status_code == 400


def test_missing_worker_is_explicit_and_never_falls_back_to_tcp(tmp_path, monkeypatch):
    monkeypatch.setenv('CONFIGFLOW_NODE_PROBE_BIN', str(tmp_path / 'missing'))
    with pytest.raises(latency.ProbeUnavailable, match='未安装'):
        latency._probe_binary()


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'https://user:secret@example.com', 'not-a-url'])
def test_invalid_url_is_rejected(monkeypatch, url):
    monkeypatch.setenv('CONFIGFLOW_NODE_TEST_URL', url)
    with pytest.raises(latency.ProbeUnavailable):
        latency.settings()
