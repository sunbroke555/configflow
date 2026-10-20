"""Cache flushing targets one registered OxiDNS Agent and its own cache tags."""
import copy
import json

import pytest
import requests

from backend.agents import oxidns_cache as caches
from backend.agents.oxidns_monitor import MonitorError
from backend.test_agent_upgrade_auth import authenticated_app  # noqa: F401

AGENTS = {
    'dns': {'id': 'dns', 'host': '127.0.0.1', 'port': 8082, 'service_type': 'oxidns',
            'version': '1.8.36-go', 'token': 'dns-secret'},
    'other': {'id': 'other', 'host': '127.0.0.2', 'port': 8083, 'service_type': 'oxidns',
              'version': '1.8.36-go', 'token': 'other-secret'},
    'proxy': {'id': 'proxy', 'host': '127.0.0.1', 'port': 8084, 'service_type': 'mihomo',
              'version': '1.8.36-go', 'token': 'proxy-secret'},
}


class Manager:
    def __init__(self, **changes):
        self.agents = copy.deepcopy(AGENTS)
        for agent_id, fields in changes.items():
            self.agents[agent_id].update(fields)

    def get_agent_by_id(self, agent_id):
        return self.agents.get(agent_id)


@pytest.fixture
def calls(monkeypatch):
    seen, replies = [], []

    def request(session, method, url, **kwargs):
        seen.append({'method': method, 'url': url, 'trust_env': session.trust_env, **kwargs})
        status, data = replies.pop(0)
        response = requests.Response()
        response.status_code = status
        response._content = data if isinstance(data, bytes) else json.dumps(data).encode()
        response._content_consumed = True
        return response

    monkeypatch.setattr(requests.Session, 'request', request)
    return seen, replies


def test_list_and_flush_reach_only_the_named_agent(calls):
    seen, replies = calls
    replies[:] = [(200, {'success': True, 'caches': ['main_cache', 'probe']}),
                  (200, {'success': True, 'flushed': ['probe']})]
    manager = Manager()
    assert caches.call(manager, 'dns', 'GET') == {'success': True, 'caches': ['main_cache', 'probe']}
    assert caches.call(manager, 'dns', 'POST', ['probe']) == {'success': True, 'flushed': ['probe']}
    assert [entry['url'] for entry in seen] == ['http://127.0.0.1:8082/api/oxidns/cache',
                                                'http://127.0.0.1:8082/api/oxidns/cache/flush']
    assert all(entry['headers']['Authorization'] == 'Bearer dns-secret' for entry in seen)
    assert seen[0]['data'] is None and json.loads(seen[1]['data']) == {'tags': ['probe']}
    assert all(entry['trust_env'] is False and entry['allow_redirects'] is False for entry in seen)
    assert 'other-secret' not in json.dumps(seen)


@pytest.mark.parametrize('agent_id,changes,code', [
    ('missing', {}, 'AGENT_NOT_FOUND'),
    ('proxy', {}, 'WRONG_SERVICE'),
    ('dns', {'dns': {'version': '1.8.35-go'}}, 'UPGRADE_REQUIRED'),
])
def test_rejects_unsupported_targets_before_request(calls, agent_id, changes, code):
    seen, _ = calls
    with pytest.raises(MonitorError) as error:
        caches.call(Manager(**changes), agent_id, 'POST', ['probe'])
    assert error.value.code == code and seen == []


@pytest.mark.parametrize('method,tags', [
    ('POST', None), ('POST', []), ('POST', ['../reload']), ('POST', ['probe', 'probe']),
    ('POST', ['_x']), ('POST', ['a' * 65]), ('POST', 'probe'), ('GET', ['probe']), ('DELETE', None),
])
def test_rejects_invalid_requests_before_contacting_agent(calls, method, tags):
    seen, _ = calls
    with pytest.raises(MonitorError) as error:
        caches.call(Manager(), 'dns', method, tags)
    assert error.value.code == 'INVALID_REQUEST' and seen == []


def test_partial_failure_reports_completed_caches(calls):
    _, replies = calls
    replies[:] = [(502, {'success': False, 'code': 'CACHE_FLUSH_FAILED', 'message': 'core secret detail',
                         'flushed': ['main_cache']})]
    with pytest.raises(MonitorError) as error:
        caches.call(Manager(), 'dns', 'POST', ['main_cache', 'probe'])
    assert error.value.code == 'CACHE_FLUSH_FAILED' and error.value.flushed == ['main_cache']
    assert 'secret' not in error.value.message


@pytest.mark.parametrize('status,data,code', [
    (404, b'404 page not found', 'UPGRADE_REQUIRED'),
    (409, {'success': False, 'code': 'CACHE_UNKNOWN'}, 'CACHE_UNKNOWN'),
    (200, {'success': True, 'flushed': ['main_cache']}, 'INVALID_RESPONSE'),
    (200, {'success': True, 'flushed': ['../x']}, 'INVALID_RESPONSE'),
    (302, {'success': True}, 'INVALID_RESPONSE'),
    (401, {'success': False}, 'AGENT_AUTH_FAILED'),
])
def test_untrusted_agent_responses_are_validated(calls, status, data, code):
    _, replies = calls
    replies[:] = [(status, data)]
    with pytest.raises(MonitorError) as error:
        caches.call(Manager(), 'dns', 'POST', ['probe'])
    assert error.value.code == code


def test_route_requires_admin_and_passes_only_the_path_agent(authenticated_app, monkeypatch):
    client, _, (agent, _), admin, _ = authenticated_app
    seen = []
    monkeypatch.setattr(caches, 'call', lambda *args: seen.append(args) or {'success': True, 'flushed': ['probe']})
    path = '/api/agents/' + agent['id'] + '/oxidns/cache/flush'
    for headers in ({}, {'Authorization': 'Bearer ' + agent['token']}):
        assert client.post(path, json={'tags': ['probe']}, headers=headers).status_code == 401
    assert seen == []
    result = client.post(path, json={'tags': ['probe']}, headers={**admin, 'X-ConfigFlow-Profile': 'unrelated'})
    assert result.status_code == 200 and result.headers['Cache-Control'] == 'no-store'
    assert seen[0][1:] == (agent['id'], 'POST', ['probe'])
    monkeypatch.setattr(caches, 'call', lambda *args: seen.append(args) or {'success': True, 'caches': []})
    assert client.get('/api/agents/' + agent['id'] + '/oxidns/cache', headers=admin).get_json()['caches'] == []
    assert seen[1][1:] == (agent['id'], 'GET', None)


def test_route_returns_partial_flush_details(authenticated_app, monkeypatch):
    client, _, (agent, _), admin, _ = authenticated_app
    def fail(*_args):
        error = caches._fail('CACHE_FLUSH_FAILED')
        error.flushed = ['main_cache']
        raise error
    monkeypatch.setattr(caches, 'call', fail)
    result = client.post('/api/agents/' + agent['id'] + '/oxidns/cache/flush',
                         json={'tags': ['main_cache', 'probe']}, headers=admin)
    assert result.status_code == 502
    assert result.get_json() == {'success': False, 'code': 'CACHE_FLUSH_FAILED',
                                 'message': caches.MESSAGES['CACHE_FLUSH_FAILED'], 'flushed': ['main_cache']}


@pytest.mark.parametrize('method,suffix,data,content_type', [
    ('POST', '?tag=probe', '{"tags":["probe"]}', 'application/json'),
    ('POST', '', '{}', 'application/json'),
    ('POST', '', '{"tags":["probe"],"agent_id":"other"}', 'application/json'),
    ('POST', '', '{"tags":["../reload"]}', 'application/json'),
    ('POST', '', '{"tags":["probe"]}', 'text/plain'),
    ('POST', '', '{', 'application/json'),
    ('POST', '', ' ' * (16 * 1024 + 1), 'application/json'),
])
def test_route_rejects_injected_or_invalid_payloads(authenticated_app, monkeypatch,
                                                 method, suffix, data, content_type):
    client, _, (agent, _), admin, _ = authenticated_app
    monkeypatch.setattr(caches, 'call', lambda *args: pytest.fail('unexpected Agent request'))
    path = '/api/agents/' + agent['id'] + '/oxidns/cache/flush' + suffix
    response = client.open(path, method=method, data=data, content_type=content_type, headers=admin)
    assert response.status_code == 400 and response.get_json()['code'] == 'INVALID_REQUEST'
    response = client.open('/api/agents/' + agent['id'] + '/oxidns/cache', method='GET', data='{}',
                           content_type='application/json', headers=admin)
    assert response.status_code == 400
