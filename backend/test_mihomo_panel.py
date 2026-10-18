"""The Mihomo panel only forwards whitelisted controller calls through the Agent."""
import copy
import json
from urllib.parse import quote

import pytest
import requests
from werkzeug.datastructures import MultiDict

from backend.agents import mihomo_panel as panel
from backend.agents.oxidns_monitor import MonitorError
from backend.test_agent_upgrade_auth import authenticated_app  # noqa: F401  (fixture)

AGENT = {'id': 'mihomo', 'name': 'Gateway', 'host': '127.0.0.1', 'port': 8080,
         'service_type': 'mihomo', 'version': '1.8.15-go', 'status': 'online', 'token': 'agent-private-token'}


class Manager:
    def __init__(self, agent=None):
        self.agent = copy.deepcopy(AGENT if agent is None else agent)

    def get_agent_by_id(self, aid):
        return self.agent if aid == 'mihomo' else None


class Response:
    def __init__(self, data=None, status=200, raw=None):
        self.status_code = status
        self.body = raw if raw is not None else json.dumps(data).encode()

    def __enter__(self): return self
    def __exit__(self, *args): pass

    def iter_content(self, size):
        for start in range(0, len(self.body), size):
            yield self.body[start:start + size]


class Calls(list):
    replies: list


@pytest.fixture
def calls(monkeypatch):
    seen = Calls()
    replies = []

    def request(self, method, url, **kwargs):
        seen.append({'method': method, 'url': url, **kwargs})
        return replies.pop(0) if replies else Response({'ok': True})

    monkeypatch.setattr(requests.Session, 'request', request)
    seen.replies = replies
    return seen


def test_forwards_named_resources_as_single_encoded_segment(calls):
    result = panel.call(Manager(), 'mihomo', 'proxy', 'PUT', MultiDict({'name': '节点/香港 01'}), {'name': 'HK'})
    assert result == {'success': True, 'data': {'ok': True}}
    call = calls[0]
    assert call['method'] == 'PUT'
    assert call['url'] == 'http://127.0.0.1:8080/api/mihomo/proxies/%E8%8A%82%E7%82%B9%2F%E9%A6%99%E6%B8%AF%2001'
    assert json.loads(call['data']) == {'name': 'HK'}
    assert call['headers']['Authorization'] == 'Bearer agent-private-token'
    assert call['allow_redirects'] is False


def test_delay_query_is_validated_and_defaulted(calls):
    panel.call(Manager(), 'mihomo', 'group/delay', 'GET', MultiDict({'name': 'Auto', 'extra': 'x'}))
    assert calls[0]['params'] == {'url': panel.DEFAULT_TEST_URL, 'timeout': '5000'}
    with pytest.raises(MonitorError) as exc:
        panel.call(Manager(), 'mihomo', 'group/delay', 'GET', MultiDict({'name': 'Auto', 'url': 'file:///etc/passwd'}))
    assert exc.value.status == 400


@pytest.mark.parametrize('resource,method,body', [
    ('configs', 'PUT', {'mode': 'rule'}),
    ('restart', 'POST', None),
    ('configs', 'PATCH', {'mode': 'rule', 'tun': {'enable': False}}),
    ('configs', 'PATCH', {'external-controller': '0.0.0.0:9090'}),
    ('proxy', 'PUT', {'name': ''}),
])
def test_rejects_operations_outside_the_panel(calls, resource, method, body):
    with pytest.raises(MonitorError):
        panel.call(Manager(), 'mihomo', resource, method, MultiDict({'name': 'G'}), body)
    assert calls == []


def test_mode_switch_only_sends_mode(calls):
    panel.call(Manager(), 'mihomo', 'configs', 'PATCH', MultiDict(), {'mode': 'global'})
    assert json.loads(calls[0]['data']) == {'mode': 'global'}


def test_requires_new_mihomo_agent(calls):
    with pytest.raises(MonitorError) as exc:
        panel.call(Manager({**AGENT, 'version': '1.8.14-go'}), 'mihomo', 'version', 'GET', MultiDict())
    assert exc.value.code == 'UPGRADE_REQUIRED'
    with pytest.raises(MonitorError) as exc:
        panel.call(Manager({**AGENT, 'service_type': 'oxidns'}), 'mihomo', 'version', 'GET', MultiDict())
    assert exc.value.code == 'WRONG_SERVICE'
    assert calls == []


def test_empty_reply_and_core_errors(calls):
    calls.replies.extend([Response(raw=b'', status=204), Response({'message': 'Timeout'}, status=504),
                          Response({'success': False, 'code': 'CONTROLLER_UNAVAILABLE'}, status=409)])
    assert panel.call(Manager(), 'mihomo', 'connection', 'DELETE', MultiDict({'name': 'abc'})) == {'success': True, 'data': None}
    with pytest.raises(MonitorError) as exc:
        panel.call(Manager(), 'mihomo', 'proxy/delay', 'GET', MultiDict({'name': 'HK'}))
    assert (exc.value.status, exc.value.message) == (504, 'Timeout')
    with pytest.raises(MonitorError) as exc:
        panel.call(Manager(), 'mihomo', 'version', 'GET', MultiDict())
    assert exc.value.code == 'CONTROLLER_UNAVAILABLE'


def test_route_requires_admin_auth_and_forwards_name(authenticated_app, calls):
    client, manager, _, admin, _ = authenticated_app
    registered = manager.register_agent({k: v for k, v in AGENT.items() if k not in ('id', 'token')})
    aid = registered['id']
    url = f'/api/agents/{aid}/mihomo/proxy?name=%E8%8A%82%E7%82%B9%2FA'
    assert client.put(url, json={'name': 'HK'}).status_code == 401
    assert client.put(url, json={'name': 'HK'}, headers={'Authorization': 'Bearer ' + registered['token']}).status_code == 401
    assert calls == []
    response = client.put(url, json={'name': 'HK'}, headers=admin)
    assert response.status_code == 200 and response.headers['Cache-Control'] == 'no-store'
    assert calls[0]['url'].endswith('/api/mihomo/proxies/%E8%8A%82%E7%82%B9%2FA')
    assert client.get(f'/api/agents/{aid}/mihomo/logs', headers=admin).status_code == 404


def test_reload_and_upgrade_need_newer_agent_and_send_no_body(calls):
    old = Manager()  # 1.8.15-go
    for resource, method in (('configs/reload', 'PUT'), ('upgrade', 'POST')):
        with pytest.raises(MonitorError) as exc:
            panel.call(old, 'mihomo', resource, method, MultiDict(), {'path': '/tmp/evil.yaml'})
        assert exc.value.code == 'UPGRADE_REQUIRED' and '1.8.16-go' in exc.value.message
    assert calls == []
    new = Manager({**AGENT, 'version': '1.8.16-go'})
    panel.call(new, 'mihomo', 'configs/reload', 'PUT', MultiDict(), {'path': '/tmp/evil.yaml', 'payload': 'x'})
    panel.call(new, 'mihomo', 'upgrade', 'POST', MultiDict({'channel': 'alpha'}))
    assert [(c['method'], c['url'].rsplit('/api/mihomo/', 1)[1], c['data'], c['params']) for c in calls] == [
        ('PUT', 'configs', None, {}), ('POST', 'upgrade', None, {})]
    assert calls[1]['timeout'] == (3, 200)


def test_provider_node_delay_encodes_both_names_and_keeps_test_options(calls):
    provider = '订阅 / A%?#'
    name = '🇯🇵 JP-05 · AWS2 / 50%?#'
    url = 'http://127.0.0.1:18080/generate_204?value=a&other=b'
    calls.replies.append(Response({'delay': 123}))
    result = panel.call(Manager({**AGENT, 'version': '1.8.18-go'}), 'mihomo',
                        'providers/proxy/delay', 'GET', MultiDict({
                            'provider': provider, 'name': name, 'url': url,
                            'timeout': '7300', 'unrelated': 'discard'}))
    assert result == {'success': True, 'data': {'delay': 123}}
    forwarded = calls[0]
    assert forwarded['url'].endswith(
        '/api/mihomo/providers/proxies/' + quote(provider, safe='') + '/' + quote(name, safe='') + '/healthcheck')
    assert forwarded['params'] == {'url': url, 'timeout': '7300'}
    assert forwarded['method'] == 'GET' and forwarded['data'] is None
    assert forwarded['timeout'] == (3, 40)


def test_ordinary_node_still_works_on_older_agent_and_provider_requires_update(calls):
    calls.replies.append(Response({'delay': 81}))
    result = panel.call(Manager(), 'mihomo', 'proxy/delay', 'GET', MultiDict({'name': '普通节点'}))
    assert result['data']['delay'] == 81
    assert '/proxies/' in calls[0]['url'] and '/providers/' not in calls[0]['url']
    assert calls[0]['params'] == {'url': panel.DEFAULT_TEST_URL, 'timeout': '5000'}
    with pytest.raises(MonitorError) as exc:
        panel.call(Manager({**AGENT, 'version': '1.8.17-go'}), 'mihomo',
                   'providers/proxy/delay', 'GET', MultiDict({'provider': 'Airport', 'name': 'HK'}))
    assert exc.value.code == 'UPGRADE_REQUIRED'
    assert '1.8.18-go' in exc.value.message and '订阅节点测速' in exc.value.message
    assert len(calls) == 1


@pytest.mark.parametrize('params', [
    {'name': 'HK'}, {'provider': 'Airport'}, {'provider': '', 'name': 'HK'},
    {'provider': 'bad\nname', 'name': 'HK'}, {'provider': 'Airport', 'name': 'bad\0name'},
    {'provider': 'x' * 513, 'name': 'HK'},
    {'provider': 'Airport', 'name': 'HK', 'url': 'file:///tmp/probe'},
    {'provider': 'Airport', 'name': 'HK', 'timeout': 'invalid'},
    {'provider': 'Airport', 'name': 'HK', 'timeout': '30001'},
    {'provider': 'Airport', 'name': 'HK', 'timeout': '499'},
])
def test_provider_node_uses_existing_name_and_delay_validation(calls, params):
    with pytest.raises(MonitorError) as exc:
        panel.call(Manager({**AGENT, 'version': '1.8.18-go'}), 'mihomo',
                   'providers/proxy/delay', 'GET', MultiDict(params))
    assert exc.value.code == 'INVALID_REQUEST' and exc.value.status == 400
    assert calls == []


@pytest.mark.parametrize('status,message,expected_status', [
    (404, 'Resource not found', 400), (504, 'Timeout', 504),
    (503, 'An error occurred in the delay test', 502),
])
def test_provider_node_core_failure_is_reported_without_testing_another_node(calls, status, message, expected_status):
    calls.replies.append(Response({'message': message}, status=status))
    with pytest.raises(MonitorError) as exc:
        panel.call(Manager({**AGENT, 'version': '1.8.18-go'}), 'mihomo',
                   'providers/proxy/delay', 'GET', MultiDict({'provider': 'Airport', 'name': 'HK'}))
    assert (exc.value.code, exc.value.status, exc.value.message) == ('CORE_ERROR', expected_status, message)
    assert len(calls) == 1


def test_provider_node_route_requires_admin_and_keeps_provider_healthcheck(authenticated_app, calls):
    client, manager, _, admin, _ = authenticated_app
    registered = manager.register_agent({**{k: v for k, v in AGENT.items() if k not in ('id', 'token')}, 'version': '1.8.18-go'})
    url = f'/api/agents/{registered["id"]}/mihomo/providers/proxy/delay'
    params = {'provider': '订阅/A', 'name': '节点/B'}
    assert client.get(url, query_string=params).status_code == 401
    assert calls == []
    calls.replies.append(Response({'delay': 19}))
    result = client.get(url, query_string=params, headers=admin)
    assert result.status_code == 200 and result.json['data'] == {'delay': 19}
    assert result.headers['Cache-Control'] == 'no-store'
    assert client.post(url, query_string=params, headers=admin).status_code == 404
    assert len(calls) == 1
    panel.call(Manager(), 'mihomo', 'providers/proxy/healthcheck', 'GET', MultiDict({'name': 'Airport'}))
    assert calls[-1]['url'].endswith('/providers/proxies/Airport/healthcheck')
    assert calls[-1]['params'] == {}
