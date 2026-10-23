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
# Persists forwarding on either Shell layout, so no layout check precedes the request.
STANDALONE = bridge.STANDALONE_FORWARDING_MIN_AGENT_VERSION


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
    result = bridge.call(Manager(version=STANDALONE), 'gateway', 'forwarding', 'POST', 'home', {'prepare_tun': True})
    assert result['ipv4_forwarding']['persisted'] is True
    assert seen[0]['method'] == 'POST'
    assert seen[0]['url'] == 'http://127.0.0.1:8080/api/linux-gateway/forwarding'
    assert seen[0]['json'] == {'prepare_tun': True}
    for body in (None, {}):
        bridge.call(Manager(version=STANDALONE), 'gateway', 'forwarding', 'POST', 'home', body)
        assert seen[-1]['json'] == {}
    with pytest.raises(MonitorError) as error:
        bridge.call(Manager(version=STANDALONE), 'gateway', 'forwarding', 'GET', 'home')
    assert error.value.code == 'NOT_ALLOWED'


@pytest.mark.parametrize('body', [{'prepare_tun': 1}, {'prepare_tun': 'yes'}, {'prepare_tun': True, 'x': 1},
                                  {'command': 'sysctl'}, [], 'x'])
def test_forwarding_accepts_only_prepare_tun(split, calls, body):
    split['value'] = 'full_tun'
    with pytest.raises(MonitorError) as error:
        bridge.call(Manager(version=STANDALONE), 'gateway', 'forwarding', 'POST', 'home', body)
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
        bridge.call(Manager(version=STANDALONE), 'gateway', 'forwarding', 'POST', 'home', {})
    assert error.value.code == 'UPGRADE_REQUIRED' and CURRENT in error.value.message


def test_forwarding_uncertain_result_is_not_repeated(split, calls, monkeypatch):
    split['value'] = 'full_tun'
    def lost(*args, **kwargs):
        raise requests.ConnectionError('reset')
    monkeypatch.setattr(requests.Session, 'request', lost)
    with pytest.raises(MonitorError) as error:
        bridge.call(Manager(version=STANDALONE), 'gateway', 'forwarding', 'POST', 'home', {})
    assert error.value.message == '未能确认转发持久化结果，请先刷新设备状态，勿重复操作。'


@pytest.mark.parametrize('mode', [None, 'legacy', 'invalid'])
def test_forwarding_is_only_for_a_full_tun_profile(split, calls, mode):
    split['value'] = mode
    for body in ({}, {'prepare_tun': True}):
        with pytest.raises(MonitorError) as error:
            bridge.call(Manager(version=CURRENT), 'gateway', 'forwarding', 'POST', 'home', body)
        assert error.value.code == 'ROUTER_SPLIT_REQUIRED' and error.value.status == 409
        assert error.value.message == ('仅主路由分流（完整 TUN）的配置空间，或「上游路由」中作为代理网关的 '
                                       'Mihomo Agent 可开启转发持久化。')
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
    # The host's status is read first: no gateway record here, so removal is sent.
    assert [(item['method'], item['url'].rsplit('/', 1)[1], item['json']) for item in calls[0]] == [
        ('GET', 'status', None), ('POST', 'remove', {})]


@pytest.mark.parametrize('managed', [
    {'active': True, 'healthy': True}, {'active': False, 'pending': True}, {'active': False, 'error': 'journal unreadable'},
])
def test_full_tun_remove_keeps_forwarding_that_is_not_persisted(split, calls, managed):
    """Without the AIO forwarding file the Agent writes the journal's original 0 back."""
    seen, replies = calls
    split['value'] = 'full_tun'
    replies.append(Response({'success': True, 'managed': {**managed, 'preserves_forwarding': False}}))
    with pytest.raises(MonitorError) as error:
        bridge.call(Manager(version=CURRENT), 'gateway', 'remove', 'POST', 'home', {})
    assert error.value.code == 'FORWARDING_NOT_PERSISTED' and error.value.status == 409
    assert error.value.message == bridge.FORWARDING_NOT_KEPT.format(action='')
    assert [item['url'].rsplit('/', 1)[1] for item in seen] == ['status']
    replies.append(Response({'success': True, 'managed': {**managed, 'preserves_forwarding': True}}))
    bridge.call(Manager(version=CURRENT), 'gateway', 'remove', 'POST', 'home', {})
    assert [item['url'].rsplit('/', 1)[1] for item in seen] == ['status', 'status', 'remove']


def test_ordinary_gateway_remove_is_unchanged(split, calls):
    bridge.call(Manager(), 'gateway', 'remove', 'POST', 'home', {})
    bridge.call(Manager(), 'gateway', 'remove', 'POST', 'home', {'confirm_router_split_teardown': True})
    assert [item['json'] for item in calls[0]] == [{}, {}]


AGENT_REMOVED = {'success': True, 'message': '受管网关配置已撤销；请恢复客户端原来的网关和 DNS 设置', 'managed': {'active': False}}


@pytest.mark.parametrize('mode,managed,message', [
    ('full_tun', {'active': False, 'preserves_forwarding': True}, '设备上没有旧网关记录，无需清理；IPv4 转发保持不变。'),
    ('full_tun', {'active': True, 'healthy': True, 'preserves_forwarding': True}, '旧网关记录已清理，IPv4 转发保持开启。'),
    ('legacy', None, '网关转发已撤销，主路由转来的连接已中断。请删除主路由上指向这台 Linux 的静态路由，或重新应用网关转发。'),
])
def test_router_split_remove_never_tells_clients_to_restore_their_gateway(split, calls, mode, managed, message):
    """The main router forwards to Linux; its clients never changed their gateway."""
    _, replies = calls
    split['value'] = mode
    if managed is not None:
        replies.append(Response({'success': True, 'managed': managed}))
    replies.append(Response(AGENT_REMOVED))
    body = {'confirm_router_split_teardown': True} if mode == 'legacy' else {}
    result = bridge.call(Manager(version=CURRENT), 'gateway', 'remove', 'POST', 'home', body)
    assert result == {**AGENT_REMOVED, 'message': message}


def test_selective_gateway_remove_keeps_the_agent_text(split, calls):
    calls[1].append(Response(AGENT_REMOVED))
    assert bridge.call(Manager(), 'gateway', 'remove', 'POST', 'home', {}) == AGENT_REMOVED


@pytest.fixture
def legacy_full_tun(calls, monkeypatch):
    """An old router split (topology match) whose base YAML gained a full TUN: its mode is None."""
    from backend.common.router_split import full_tun_base_yaml
    context = {'session_goal': None, 'has_gateway_session': False, 'mihomo_agent_ids': ['gateway']}
    monkeypatch.setattr(bridge, 'get_config', lambda profile: {
        'mihomo': {'linux_gateway': {'enabled': True}, 'custom_config': full_tun_base_yaml('')},
        'routers': {'items': [{'id': 'r1', 'proxy_gateway_agent_id': 'gateway'}]}})
    monkeypatch.setattr('backend.common.router_split.legacy_context', lambda repository, profile: deepcopy(context))
    return context


def test_old_split_with_a_full_tun_base_cannot_remove_a_record_that_turns_forwarding_off(legacy_full_tun, calls):
    """Review: 「撤销网关转发」 in mode null sent a plain remove, and the Agent set ip_forward back to 0."""
    seen, replies = calls
    record = {'success': True, 'managed': {'active': True, 'healthy': True, 'preserves_forwarding': False}}
    replies.append(Response(record))
    with pytest.raises(MonitorError) as error:
        bridge.call(Manager(version=CURRENT), 'gateway', 'remove', 'POST', 'home', {})
    assert error.value.code == 'FORWARDING_NOT_PERSISTED' and error.value.status == 409
    assert error.value.message == bridge.LEGACY_REMOVE_NOT_KEPT
    replies.append(Response(record))
    with pytest.raises(MonitorError) as error:
        bridge.call(Manager(version='1.8.39-go'), 'gateway', 'remove', 'POST', 'home', {})
    assert error.value.code == 'UPGRADE_REQUIRED' and error.value.message == bridge.LEGACY_REMOVE_UPGRADE
    assert [item['url'].rsplit('/', 1)[1] for item in seen] == ['status', 'status']
    # A confirmed teardown accepts the interruption, as for a legacy split.
    replies.extend([Response(record), Response(AGENT_REMOVED)])
    result = bridge.call(Manager(version=CURRENT), 'gateway', 'remove', 'POST', 'home',
                         {'confirm_router_split_teardown': True})
    assert result['message'] == bridge.REMOVED_MESSAGES['legacy']
    assert seen[-1]['json'] == {} and [item['url'].rsplit('/', 1)[1] for item in seen][-2:] == ['status', 'remove']


@pytest.mark.parametrize('managed,message', [
    ({'active': True, 'healthy': True, 'preserves_forwarding': True}, '旧网关记录已清理，IPv4 转发保持开启。'),
    ({'active': False, 'preserves_forwarding': False}, '设备上没有旧网关记录，无需清理；IPv4 转发保持不变。'),
])
def test_old_split_with_a_full_tun_base_removes_a_record_that_keeps_forwarding(legacy_full_tun, calls, managed, message):
    seen, replies = calls
    replies.extend([Response({'success': True, 'managed': managed}), Response(AGENT_REMOVED)])
    result = bridge.call(Manager(version=CURRENT), 'gateway', 'remove', 'POST', 'home', {})
    assert result == {**AGENT_REMOVED, 'message': message}
    assert [(item['url'].rsplit('/', 1)[1], item['json']) for item in seen] == [('status', None), ('remove', {})]


def test_full_tun_base_without_the_old_split_keeps_the_selective_remove(legacy_full_tun, calls):
    legacy_full_tun.update(mihomo_agent_ids=['other'])
    calls[1].append(Response(AGENT_REMOVED))
    assert bridge.call(Manager(version=CURRENT), 'gateway', 'remove', 'POST', 'home', {}) == AGENT_REMOVED
    assert [item['url'].rsplit('/', 1)[1] for item in calls[0]] == ['remove']


def test_http_router_split_codes_reach_the_editor(authenticated_app, monkeypatch):
    """The stored setup session and marker decide, and the editor gets the code and text."""
    from backend.common import setup_flow
    from backend.common.router_split import MESSAGES
    client, manager, _, admin, _ = authenticated_app
    repository = manager.repository
    repository.create_profile({'id': 'home', 'name': 'Home'})
    seen = []

    def request(session, method, url, **kwargs):
        seen.append((url.rsplit('/', 1)[-1], kwargs['json']))
        return Response({'success': True, 'managed': {'active': False}})
    monkeypatch.setattr(requests.Session, 'request', request)
    agent = manager.register_agent({'name': 'split', 'host': '127.0.0.1', 'port': 8080, 'service_type': 'mihomo',
                                    'deployment_method': 'shell', 'version': STANDALONE, 'profile_id': 'home'})
    headers = {**admin, 'X-ConfigFlow-Profile': 'home'}
    path = f"/api/agents/{agent['id']}/linux-gateway/"

    def mihomo(**fields):
        value = repository.get_profile('home').get('mihomo') or {}
        repository.save_profile('home', {'mihomo': {**value, **fields}})
    # Legacy: a router setup session from before the marker, with the gateway on.
    mihomo(linux_gateway={'enabled': True, 'rule_set_ids': []})
    setup_flow.write(repository, {'id': 'd' * 32, 'profile_id': 'home', 'name': 'Home', 'goal': 'router',
                                  'step': 4, 'max_step': 4, 'prepared': True, 'completed': True})
    response = client.post(path + 'remove', headers=headers, json={})
    assert response.status_code == 409
    assert response.get_json() == {'success': False, 'code': 'ROUTER_SPLIT_LEGACY_TEARDOWN',
                                   'message': MESSAGES['ROUTER_SPLIT_LEGACY_TEARDOWN']}
    assert client.post(path + 'forwarding', headers=headers, json={}).get_json()['code'] == 'ROUTER_SPLIT_REQUIRED'
    assert seen == []
    response = client.post(path + 'remove', headers=headers, json={'confirm_router_split_teardown': True})
    assert response.status_code == 200, response.get_json()
    assert seen == [('remove', {})]
    # Full TUN: forwarding goes through, the selective gateway never does.
    mihomo(linux_gateway={'enabled': False, 'rule_set_ids': []}, router_split=deepcopy(MARKER))
    response = client.post(path + 'forwarding', headers=headers, json={})
    assert response.status_code == 200, response.get_json()
    assert seen[-1] == ('forwarding', {})
    response = client.post(path + 'apply', headers=headers, json=SPEC)
    assert response.status_code == 409 and response.get_json()['code'] == 'ROUTER_SPLIT_FULL_TUN'
    assert len(seen) == 2


STANDALONE_STATUS = {'success': True, 'supported': True, 'managed': {'active': False},
                     'ipv4_forwarding': {'protocol': 1, 'supported': False, 'current': '0',
                                         'unsupported_reason': '仅支持普通 Shell 同机安装'}}
AIO_STATUS = {'success': True, 'supported': True, 'managed': {'active': False},
              'ipv4_forwarding': {'protocol': 1, 'supported': True, 'current': '0', 'layout': 'native_aio'}}


@pytest.fixture
def proxy_gateway(split, monkeypatch):
    """The profile's upstream router sends proxied traffic to this Agent."""
    routers = {'items': [{'id': 'r1', 'name': '主路由', 'proxy_gateway_agent_id': 'gateway'}]}
    previous = bridge.get_config
    monkeypatch.setattr(bridge, 'get_config', lambda profile: {**previous(profile), 'routers': routers})
    return routers


@pytest.mark.parametrize('mode', [None, 'legacy', 'invalid'])
def test_forwarding_is_allowed_for_a_proxy_gateway(proxy_gateway, split, calls, mode):
    """Persisting only turns forwarding on; a selective gateway's record then keeps it on removal."""
    seen, replies = calls
    split['value'] = mode
    replies.append(Response({'success': True, 'ipv4_forwarding': {'persisted': True}}))
    for body in (None, {}):
        bridge.call(Manager(version=STANDALONE), 'gateway', 'forwarding', 'POST', 'home', body)
    assert [(item['method'], item['url'].rsplit('/', 1)[1], item['json']) for item in seen] == [
        ('POST', 'forwarding', {}), ('POST', 'forwarding', {})]


@pytest.mark.parametrize('body', [{'prepare_tun': True}, {'prepare_tun': False}, {'x': 1}, [], 'x'])
def test_proxy_gateway_forwarding_takes_no_parameters(proxy_gateway, split, calls, body):
    with pytest.raises(MonitorError) as error:
        bridge.call(Manager(version=STANDALONE), 'gateway', 'forwarding', 'POST', 'home', body)
    assert error.value.code == 'INVALID_REQUEST' and error.value.status == 400
    assert calls[0] == []


def test_a_router_of_another_agent_does_not_allow_forwarding(proxy_gateway, split, calls):
    proxy_gateway['items'][0]['proxy_gateway_agent_id'] = 'other'
    with pytest.raises(MonitorError) as error:
        bridge.call(Manager(version=STANDALONE), 'gateway', 'forwarding', 'POST', 'home', {})
    assert error.value.code == 'ROUTER_SPLIT_REQUIRED'
    assert calls[0] == []


@pytest.mark.parametrize('mode', [None, 'full_tun'])
def test_forwarding_below_1_8_41_needs_an_update_only_for_a_single_service_install(proxy_gateway, split, calls, mode):
    seen, replies = calls
    split['value'] = mode
    replies.append(Response(STANDALONE_STATUS))
    with pytest.raises(MonitorError) as error:
        bridge.call(Manager(version=CURRENT), 'gateway', 'forwarding', 'POST', 'home', {})
    assert error.value.code == 'UPGRADE_REQUIRED' and STANDALONE in error.value.message
    assert [(item['method'], item['url'].rsplit('/', 1)[1]) for item in seen] == [('GET', 'status')]
    # The same Agent version on the AIO layout persists as before.
    seen.clear()
    replies.extend([Response(AIO_STATUS), Response({'success': True})])
    bridge.call(Manager(version=CURRENT), 'gateway', 'forwarding', 'POST', 'home', {})
    assert [(item['method'], item['url'].rsplit('/', 1)[1], item['json']) for item in seen] == [
        ('GET', 'status', None), ('POST', 'forwarding', {})]


def test_an_unsupported_platform_below_1_8_41_is_left_to_the_agent(proxy_gateway, split, calls):
    """A command-managed or non-root host: an update would not help, the Agent says why."""
    seen, replies = calls
    replies.extend([Response({**STANDALONE_STATUS, 'supported': False}),
                    Response({'success': False, 'message': '仅支持 Shell 原生安装'}, 409)])
    with pytest.raises(MonitorError) as error:
        bridge.call(Manager(version=CURRENT), 'gateway', 'forwarding', 'POST', 'home', {})
    assert error.value.code == 'GATEWAY_ERROR' and error.value.message == '仅支持 Shell 原生安装'
    assert len(seen) == 2


@pytest.mark.parametrize('constant', ['GATEWAY_OFF_NOT_KEPT', 'LEGACY_REMOVE_NOT_KEPT'])
def test_not_kept_copy_points_to_the_routers_page(constant):
    text = getattr(bridge, constant)
    assert text.endswith('如这台设备是「上游路由」中的代理网关，可在该页开启并持久化 IPv4 转发后再操作。')
    assert '普通 All-in-One' not in text


FORWARD_FILTER = {
    'protocol': 1, 'state': 'may_drop', 'reason': '', 'device': 'Meta', 'auto_redirect': True, 'policy': 'drop',
    'input_policy': 'accept', 'flows': ['forward'], 'forward_protocols': ['udp', 'icmp'],
    'sources': [{'kind': 'iptables', 'backend': 'nft', 'hook': 'forward', 'family': '', 'table': '', 'chain': '',
                 'policy': 'drop', 'detail': 'FORWARD 默认策略为 DROP'}],
    'remedies': [{'source': 'iptables', 'label': 'iptables', 'device': 'Meta',
                  'commands': ['iptables -I FORWARD -o Meta -j ACCEPT', 'iptables -I FORWARD -i Meta -j ACCEPT'],
                  'persist_note': '这两条规则重启后会丢失。'}],
    'hint': '提醒', 'verify': '验证', 'checked_at': '2026-10-24T03:00:00Z', 'cached': True,
}


def test_status_passes_the_forward_filter_through_sanitized(calls):
    seen, replies = calls
    replies.append(Response({'success': True, 'supported': True, 'forward_filter': {
        **FORWARD_FILTER, 'extra': 'x', 'remedies': FORWARD_FILTER['remedies'] + [
            {'source': 'iptables', 'device': 'Meta', 'commands': ['iptables -F; reboot']}]}}))
    result = bridge.call(Manager(), 'gateway', 'status', 'GET', 'home')
    assert result['supported'] is True
    assert result['forward_filter'] == {**FORWARD_FILTER, 'remedies': FORWARD_FILTER['remedies'] + [
        {'source': 'iptables', 'label': '', 'device': 'Meta', 'commands': [], 'persist_note': ''}]}


@pytest.mark.parametrize('value', ['ok', None, {'state': 'unexpected'}])
def test_status_drops_an_unusable_forward_filter(calls, value):
    seen, replies = calls
    replies.append(Response({'success': True, 'forward_filter': value}))
    result = bridge.call(Manager(), 'gateway', 'status', 'GET', 'home')
    assert result == {'success': True}


def test_status_of_an_older_agent_is_unchanged(calls):
    seen, replies = calls
    payload = {'success': True, 'supported': True, 'ipv4_forwarding': {'current': '1'}, 'tun': {'enabled': True}}
    replies.append(Response(deepcopy(payload)))
    assert bridge.call(Manager(), 'gateway', 'status', 'GET', 'home') == payload


@pytest.mark.parametrize('operation,body', [('preview', SPEC), ('apply', SPEC), ('remove', {})])
def test_other_operations_are_not_rewritten(calls, operation, body):
    seen, replies = calls
    payload = {'success': True, 'forward_filter': {'state': 'odd', 'commands': 'rm -rf /'}}
    replies.append(Response(deepcopy(payload)))
    assert bridge.call(Manager(), 'gateway', operation, 'POST', 'home', body) == payload


def test_forwarding_response_is_not_rewritten(split, calls):
    seen, replies = calls
    split['value'] = 'full_tun'
    payload = {'success': True, 'ipv4_forwarding': {'persisted': True}, 'forward_filter': {'state': 'odd'}}
    replies.append(Response(deepcopy(payload)))
    assert bridge.call(Manager(version=STANDALONE), 'gateway', 'forwarding', 'POST', 'home', {}) == payload
