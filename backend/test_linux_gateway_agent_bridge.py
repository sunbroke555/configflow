"""No host networking is touched: test the opt-in, bound-agent HTTP boundary."""
import copy
from copy import deepcopy
import json

import pytest
import requests

from backend.agents import linux_gateway as bridge
from backend.agents.oxidns_monitor import MonitorError
from backend.test_agent_upgrade_auth import authenticated_app  # noqa: F401

SPEC = {'interface': 'eth0', 'address': '192.168.1.2', 'lan_cidr': '192.168.1.0/24',
        'upstream_gateway': '192.168.1.1'}
AGENT = {'id': 'gateway', 'profile_id': 'home', 'service_type': 'mihomo',
         'deployment_method': 'shell', 'version': bridge.MIN_AGENT_VERSION,
         'host': '127.0.0.1', 'port': 8080, 'token': 'private-agent-token'}


class Manager:
    def __init__(self, **changes):
        self.agent = {**AGENT, **changes}

    def get_agent_by_id(self, agent_id):
        return copy.deepcopy(self.agent) if agent_id == 'gateway' else None


class Response:
    status_code = 200

    def __init__(self, payload=None, status=200, raw=None):
        self.status_code = status
        self.raw = raw if raw is not None else json.dumps(payload or {'success': True}).encode()

    def __enter__(self): return self
    def __exit__(self, *args): pass
    def iter_content(self, size):
        yield self.raw


@pytest.fixture
def calls(monkeypatch):
    seen = []
    replies = []

    def request(session, method, url, **kwargs):
        seen.append({'method': method, 'url': url, 'trust_env': session.trust_env, **kwargs})
        return replies.pop(0) if replies else Response()

    monkeypatch.setattr(requests.Session, 'request', request)
    monkeypatch.setattr(bridge, 'get_config', lambda profile: {'mihomo': {'linux_gateway': {'enabled': True}}})
    # These profiles are no router split; their setup sessions and topology are not stored here.
    monkeypatch.setattr('backend.common.router_split.router_split_state',
                        lambda repository, profile, config: {'mode': None})
    return seen, replies


def test_apply_uses_registered_host_and_bound_profile(calls):
    seen, _ = calls
    assert bridge.call(Manager(), 'gateway', 'apply', 'POST', 'home', SPEC)['success']
    assert seen[0]['url'] == 'http://127.0.0.1:8080/api/linux-gateway/apply'
    assert seen[0]['json'] == {**SPEC, 'profile_id': 'home'}
    assert seen[0]['headers']['Authorization'] == 'Bearer private-agent-token'
    assert not seen[0]['trust_env'] and not seen[0]['allow_redirects']


@pytest.mark.parametrize('changes,profile,code', [
    ({}, 'other', 'PROFILE_CHANGED'),
    ({}, None, 'PROFILE_CHANGED'),
    ({'service_type': 'oxidns'}, 'home', 'WRONG_SERVICE'),
    ({'deployment_method': 'docker'}, 'home', 'UNSUPPORTED_ENVIRONMENT'),
    ({'version': '1.8.18-go'}, 'home', 'UPGRADE_REQUIRED'),
])
def test_cannot_apply_to_another_profile_or_unsupported_agent(calls, changes, profile, code):
    with pytest.raises(MonitorError) as error:
        bridge.call(Manager(**changes), 'gateway', 'apply', 'POST', profile, SPEC)
    assert error.value.code == code
    assert calls[0] == []


def test_disabled_blocks_apply_but_allows_preview_and_restore(calls, monkeypatch):
    monkeypatch.setattr(bridge, 'get_config', lambda profile: {'mihomo': {}})
    with pytest.raises(MonitorError) as error:
        bridge.call(Manager(), 'gateway', 'apply', 'POST', 'home', SPEC)
    assert error.value.code == 'GATEWAY_DISABLED'
    assert calls[0] == []
    bridge.call(Manager(), 'gateway', 'preview', 'POST', 'home', SPEC)
    bridge.call(Manager(), 'gateway', 'remove', 'POST', 'home', {})
    assert len(calls[0]) == 2


@pytest.mark.parametrize('change', [
    {'interface': 'eth0; reboot'}, {'interface': 'lo'}, {'address': '192.168.1.1'},
    {'address': '192.168.2.2'}, {'upstream_gateway': '192.168.1.255'},
    {'lan_cidr': '0.0.0.0/0'}, {'lan_cidr': '192.168.1.2/24'},
    {'command': 'reboot'}, {'profile_id': 'other'}, {'address': True},
])
def test_strict_payload_validation_happens_before_agent_request(calls, change):
    with pytest.raises(MonitorError):
        bridge.call(Manager(), 'gateway', 'preview', 'POST', 'home', {**SPEC, **change})
    assert calls[0] == []


@pytest.mark.parametrize('operation,method,body', [
    ('run', 'POST', SPEC), ('apply', 'GET', None), ('remove', 'POST', {'command': 'flush'}),
])
def test_only_fixed_operations_are_forwarded(calls, operation, method, body):
    with pytest.raises(MonitorError):
        bridge.call(Manager(), 'gateway', operation, method, 'home', body)
    assert calls[0] == []


@pytest.mark.parametrize('response,code', [
    (Response(raw=b'not found', status=404), 'UPGRADE_REQUIRED'),
    (Response(status=403), 'AGENT_AUTH_FAILED'),
    (Response(raw=b'x' * (bridge.MAX_RESPONSE_BYTES + 1)), 'INVALID_RESPONSE'),
    (Response(raw=b'[]'), 'INVALID_RESPONSE'),
    (Response({'success': False, 'message': '网卡不存在'}, 409), 'GATEWAY_ERROR'),
])
def test_unavailable_or_bad_agent_results_are_not_success(calls, response, code):
    calls[1].append(response)
    with pytest.raises(MonitorError) as error:
        bridge.call(Manager(), 'gateway', 'status', 'GET', 'home')
    assert error.value.code == code


def test_http_requires_login_and_explicit_current_binding(authenticated_app, calls):
    client, manager, _, admin, _ = authenticated_app
    registered = manager.register_agent({k: v for k, v in AGENT.items() if k not in ('id', 'profile_id', 'token')})
    path = f"/api/agents/{registered['id']}/linux-gateway/status"
    assert client.get(path).status_code == 401
    assert client.get(path, headers={'Authorization': 'Bearer ' + registered['token']}).status_code == 401
    assert client.get(path, headers=admin).status_code == 400
    bound_profile = manager.get_agent_by_id(registered['id'])['profile_id']
    result = client.get(path, headers={**admin, 'X-ConfigFlow-Profile': bound_profile})
    assert result.status_code == 200
    assert result.headers['Cache-Control'] == 'no-store'
    assert 'private-agent-token' not in result.get_data(as_text=True)


MARKER = {'version': 1, 'takeover': 'full_tun', 'route_rule_set_ids': [], 'origin': 'setup'}
CURRENT = bridge.FORWARDING_MIN_AGENT_VERSION


@pytest.fixture
def split(calls, monkeypatch):
    """Choose the profile's takeover; legacy is resolved through router_split_state."""
    modes = {'value': None}
    def config(profile):
        if modes['value'] == 'full_tun':
            return {'mihomo': {'linux_gateway': {'enabled': False}, 'router_split': deepcopy(MARKER)}}
        if modes['value'] == 'invalid':
            return {'mihomo': {'linux_gateway': {'enabled': True}, 'router_split': {'version': 2}}}
        return {'mihomo': {'linux_gateway': {'enabled': True}}}
    monkeypatch.setattr(bridge, 'get_config', config)
    monkeypatch.setattr('backend.common.router_split.router_split_state',
                        lambda repository, profile, config: {'mode': modes['value']})
    return modes


def test_forwarding_posts_to_its_own_path_for_a_full_tun_profile(split, calls):
    seen, replies = calls
    split['value'] = 'full_tun'
    replies.append(Response({'success': True, 'ipv4_forwarding': {'persisted': True}}))
    result = bridge.call(Manager(version=CURRENT), 'gateway', 'forwarding', 'POST', 'home', {'prepare_tun': True})
    assert result['ipv4_forwarding']['persisted'] is True
    assert seen[0]['method'] == 'POST'
    assert seen[0]['url'] == 'http://127.0.0.1:8080/api/linux-gateway/forwarding'
    assert seen[0]['json'] == {'prepare_tun': True}
    for body in (None, {}):
        bridge.call(Manager(version=CURRENT), 'gateway', 'forwarding', 'POST', 'home', body)
        assert seen[-1]['json'] == {}
    with pytest.raises(MonitorError) as error:
        bridge.call(Manager(version=CURRENT), 'gateway', 'forwarding', 'GET', 'home')
    assert error.value.code == 'NOT_ALLOWED'


@pytest.mark.parametrize('body', [{'prepare_tun': 1}, {'prepare_tun': 'yes'}, {'prepare_tun': True, 'x': 1},
                                  {'command': 'sysctl'}, [], 'x'])
def test_forwarding_accepts_only_prepare_tun(split, calls, body):
    split['value'] = 'full_tun'
    with pytest.raises(MonitorError) as error:
        bridge.call(Manager(version=CURRENT), 'gateway', 'forwarding', 'POST', 'home', body)
    assert error.value.code == 'INVALID_REQUEST' and error.value.status == 400
    assert calls[0] == []


@pytest.mark.parametrize('version', [bridge.MIN_AGENT_VERSION, '1.8.39-go'])
def test_forwarding_needs_the_forwarding_agent(split, calls, version):
    split['value'] = 'full_tun'
    with pytest.raises(MonitorError) as error:
        bridge.call(Manager(version=version), 'gateway', 'forwarding', 'POST', 'home', {})
    assert error.value.code == 'UPGRADE_REQUIRED' and CURRENT in error.value.message
    assert calls[0] == []


def test_forwarding_on_an_old_agent_route_maps_to_upgrade(split, calls):
    split['value'] = 'full_tun'
    calls[1].append(Response(raw=b'not found', status=404))
    with pytest.raises(MonitorError) as error:
        bridge.call(Manager(version=CURRENT), 'gateway', 'forwarding', 'POST', 'home', {})
    assert error.value.code == 'UPGRADE_REQUIRED' and CURRENT in error.value.message


def test_forwarding_uncertain_result_is_not_repeated(split, calls, monkeypatch):
    split['value'] = 'full_tun'
    def lost(*args, **kwargs):
        raise requests.ConnectionError('reset')
    monkeypatch.setattr(requests.Session, 'request', lost)
    with pytest.raises(MonitorError) as error:
        bridge.call(Manager(version=CURRENT), 'gateway', 'forwarding', 'POST', 'home', {})
    assert error.value.message == '未能确认转发持久化结果，请先刷新设备状态，勿重复操作。'


@pytest.mark.parametrize('mode', [None, 'legacy', 'invalid'])
def test_forwarding_is_only_for_a_full_tun_profile(split, calls, mode):
    split['value'] = mode
    for body in ({}, {'prepare_tun': True}):
        with pytest.raises(MonitorError) as error:
            bridge.call(Manager(version=CURRENT), 'gateway', 'forwarding', 'POST', 'home', body)
        assert error.value.code == 'ROUTER_SPLIT_REQUIRED' and error.value.status == 409
        assert error.value.message == '仅主路由分流（完整 TUN）的配置空间可在此开启转发持久化。'
    assert calls[0] == []


@pytest.mark.parametrize('mode,code', [('full_tun', 'ROUTER_SPLIT_FULL_TUN'), ('invalid', 'ROUTER_SPLIT_INVALID')])
def test_apply_is_refused_for_a_router_split(split, calls, mode, code):
    split['value'] = mode
    with pytest.raises(MonitorError) as error:
        bridge.call(Manager(version=CURRENT), 'gateway', 'apply', 'POST', 'home', SPEC)
    assert error.value.code == code and error.value.status == 409
    assert calls[0] == []


def test_legacy_teardown_needs_confirmation_and_never_forwards_it(split, calls):
    split['value'] = 'legacy'
    for body in (None, {}, {'confirm_router_split_teardown': False}):
        with pytest.raises(MonitorError) as error:
            bridge.call(Manager(), 'gateway', 'remove', 'POST', 'home', body)
        assert error.value.code == ('ROUTER_SPLIT_LEGACY_TEARDOWN' if body != {'confirm_router_split_teardown': False}
                                    else 'INVALID_REQUEST')
    for body in ({'confirm_router_split_teardown': 1}, {'confirm_router_split_teardown': True, 'x': 1}):
        with pytest.raises(MonitorError) as error:
            bridge.call(Manager(), 'gateway', 'remove', 'POST', 'home', body)
        assert error.value.code == 'INVALID_REQUEST'
    assert calls[0] == []
    bridge.call(Manager(), 'gateway', 'remove', 'POST', 'home', {'confirm_router_split_teardown': True})
    assert calls[0][0]['json'] == {}
    assert len(calls[0]) == 1


def test_full_tun_remove_needs_an_agent_that_keeps_forwarding(split, calls):
    split['value'] = 'full_tun'
    with pytest.raises(MonitorError) as error:
        bridge.call(Manager(version='1.8.39-go'), 'gateway', 'remove', 'POST', 'home', {})
    assert error.value.code == 'UPGRADE_REQUIRED' and CURRENT in error.value.message
    bridge.call(Manager(version=CURRENT), 'gateway', 'remove', 'POST', 'home', {})
    assert calls[0][0]['json'] == {}


def test_ordinary_gateway_remove_is_unchanged(split, calls):
    bridge.call(Manager(), 'gateway', 'remove', 'POST', 'home', {})
    bridge.call(Manager(), 'gateway', 'remove', 'POST', 'home', {'confirm_router_split_teardown': True})
    assert [item['json'] for item in calls[0]] == [{}, {}]
