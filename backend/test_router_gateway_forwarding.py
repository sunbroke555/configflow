"""The 上游路由 page's IPv4 forwarding view and one-click persistence, with fake Agents."""
from datetime import datetime, timedelta
import json

import pytest
import requests

from backend.agents import linux_gateway as bridge
from backend.test_agent_upgrade_auth import authenticated_app  # noqa: F401

STANDALONE = bridge.STANDALONE_FORWARDING_MIN_AGENT_VERSION
AIO = bridge.FORWARDING_MIN_AGENT_VERSION
PATH = '/api/routers/gateway-forwarding'


def forwarding(**fields):
    return {'protocol': 1, 'supported': True, 'current': '1', 'config': 'managed', 'hook_script': 'managed',
            'startup_hook': 'registered', 'persisted': True, 'layout': 'standalone', **fields}


def status(managed=None, top_supported=True, **fields):
    return {'success': True, 'supported': top_supported, 'managed': managed or {'active': False},
            'ipv4_forwarding': forwarding(**fields)}


class Reply:
    def __init__(self, payload, code=200):
        self.status_code = code
        self.raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def __enter__(self): return self
    def __exit__(self, *args): pass
    def iter_content(self, size):
        yield self.raw


@pytest.fixture
def home(authenticated_app, monkeypatch):
    """A profile whose routers point at fake Agents; replies are keyed by Agent port."""
    client, manager, _, admin, _ = authenticated_app
    repository = manager.repository
    repository.create_profile({'id': 'home', 'name': 'Home'})
    repository.create_profile({'id': 'other', 'name': 'Other'})
    seen, replies = [], {}

    def request(session, method, url, **kwargs):
        port = int(url.split('//', 1)[1].split('/', 1)[0].rsplit(':', 1)[1])
        operation = url.rsplit('/', 1)[1]
        seen.append((port, method, operation, kwargs.get('json')))
        reply = replies.get((port, operation))
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, Reply) else Reply(reply or {'success': True})
    monkeypatch.setattr(requests.Session, 'request', request)

    def agent(port, **fields):
        data = {'name': f'agent-{port}', 'host': '127.0.0.1', 'port': port, 'service_type': 'mihomo',
                'deployment_method': 'shell', 'version': STANDALONE, 'profile_id': 'home'}
        registered = manager.register_agent({**data, **{k: v for k, v in fields.items() if k in data}})
        extra = {k: v for k, v in fields.items() if k not in data}
        if extra:  # Heartbeat fields, which only the Agent's own reports change.
            def change(agents):
                for item in agents:
                    if item['id'] == registered['id']:
                        item.update(extra)
            manager._update_agents(change)
        return registered['id']

    def routers(profile, *links):
        items = [{'id': f'r{index}', 'name': f'路由器{index}', 'type': 'routeros', 'endpoint': 'https://r.test',
                  'username': 'u', 'password': 'p', 'proxy_gateway_agent_id': link}
                 for index, link in enumerate(links, 1)]
        repository.save_profile(profile, {'routers': {'items': items}})

    headers = {**admin, 'X-ConfigFlow-Profile': 'home'}
    return client, headers, agent, routers, seen, replies


def entries(client, headers):
    response = client.get(PATH, headers=headers)
    assert response.status_code == 200, response.get_json()
    assert response.headers['Cache-Control'] == 'no-store'
    return response.get_json()['gateways']


def test_view_lists_each_proxy_gateway_once_in_router_order(home):
    client, headers, agent, routers, seen, replies = home
    first, second = agent(9001), agent(9002)
    routers('home', second, '', first, second)
    replies[(9001, 'status')] = status()
    replies[(9002, 'status')] = status(current='0', persisted=False, config='absent')
    result = entries(client, headers)
    assert [item['agent_id'] for item in result] == [second, first]
    assert result[0]['routers'] == [{'id': 'r1', 'name': '路由器1'}, {'id': 'r4', 'name': '路由器4'}]
    assert result[0]['state'] == 'needs_persist' and result[0]['can_persist'] is True
    assert result[0]['current'] == '0' and result[0]['config'] == 'absent'
    assert result[1]['state'] == 'persisted' and result[1]['can_persist'] is False
    assert result[1]['layout'] == 'standalone' and result[1]['agent_name'] == 'agent-9001'
    assert sorted(item[2] for item in seen) == ['status', 'status']


def test_view_uses_the_routers_profile_not_the_agents_binding(home):
    client, headers, agent, routers, seen, replies = home
    elsewhere = agent(9003, profile_id='other')
    routers('home', elsewhere)
    routers('other', '')
    replies[(9003, 'status')] = status()
    assert [item['state'] for item in entries(client, headers)] == ['persisted']
    assert entries(client, {**headers, 'X-ConfigFlow-Profile': 'other'}) == []


@pytest.mark.parametrize('fields,reply,state,message', [
    (None, None, 'missing', bridge.GATEWAY_FORWARDING_MESSAGES['missing']),
    ({'service_type': 'oxidns'}, None, 'not_mihomo', bridge.GATEWAY_FORWARDING_MESSAGES['not_mihomo']),
    ({'deployment_method': 'docker'}, None, 'docker', bridge.GATEWAY_FORWARDING_MESSAGES['docker']),
    ({'status': 'offline'}, None, 'offline', 'Agent 离线。'),
    ({'last_heartbeat': (datetime.now() - timedelta(minutes=5)).isoformat()}, None, 'offline', 'Agent 离线。'),
    ({'version': '1.8.24-go'}, None, 'upgrade_required', bridge.GATEWAY_FORWARDING_MESSAGES['upgrade_required']),
    ({'version': '1.8.39-go'}, None, 'upgrade_required', bridge.GATEWAY_FORWARDING_MESSAGES['upgrade_required']),
    ({}, Reply(b'not found', 404), 'upgrade_required', bridge.GATEWAY_FORWARDING_MESSAGES['upgrade_required']),
    ({}, Reply({'success': False, 'message': '忙'}, 409), 'error', '忙'),
    ({}, requests.ConnectionError('down'), 'error', '暂时无法连接 Agent，请检查设备连接。'),
    ({'version': AIO}, status(supported=False, unsupported_reason='仅支持普通 Shell 同机安装'), 'upgrade_required',
     bridge.STANDALONE_UPGRADE),
    ({}, status(top_supported=False, supported=False, unsupported_reason='Agent 需要 root 权限'), 'unsupported',
     'Agent 需要 root 权限'),
    ({}, status(config='foreign', persisted=False), 'blocked', bridge.GATEWAY_FORWARDING_MESSAGES['config_blocked']),
    ({}, status(config='symlink', persisted=False), 'blocked', bridge.GATEWAY_FORWARDING_MESSAGES['config_blocked']),
    ({}, status(hook_script='modified', persisted=False), 'blocked', bridge.GATEWAY_FORWARDING_MESSAGES['hook_blocked']),
    ({}, status(current='1', persisted=False, startup_hook='missing'), 'needs_persist', ''),
    ({}, status(current='', persisted=False, error='无法读取 IPv4 转发状态'), 'needs_persist', '无法读取 IPv4 转发状态'),
    ({'version': AIO}, status(layout='native_aio'), 'persisted', ''),
])
def test_view_states(home, fields, reply, state, message):
    client, headers, agent, routers, seen, replies = home
    agent_id = 'deleted-agent' if fields is None else agent(9010, **fields)
    routers('home', agent_id)
    if reply is not None:
        replies[(9010, 'status')] = reply
    [entry] = entries(client, headers)
    assert (entry['state'], entry['message']) == (state, message)
    assert entry['can_persist'] is (state == 'needs_persist')
    assert entry['minimum_version'] == (STANDALONE if state == 'upgrade_required' else None)
    # Agents that cannot persist are never asked.
    assert len(seen) == (0 if reply is None else 1)


def test_view_reports_a_gateway_record_and_whether_removal_keeps_forwarding(home):
    client, headers, agent, routers, seen, replies = home
    routers('home', agent(9011))
    replies[(9011, 'status')] = status({'active': True, 'healthy': True, 'preserves_forwarding': False},
                                       current='1', persisted=False, config='absent')
    [entry] = entries(client, headers)
    assert entry['state'] == 'needs_persist'
    assert entry['gateway_record'] is True and entry['preserves_forwarding'] is False


def post(client, headers, body):
    response = client.post(PATH, headers=headers, json=body)
    assert response.headers['Cache-Control'] == 'no-store'
    return response.status_code, response.get_json()


def test_persist_posts_an_empty_request_and_returns_the_new_entry(home):
    client, headers, agent, routers, seen, replies = home
    agent_id = agent(9020)
    routers('home', agent_id, agent_id)
    replies[(9020, 'forwarding')] = {
        'success': True, 'message': 'IPv4 转发已开启并持久化', 'ipv4_forwarding': forwarding(),
        'managed': {'active': True, 'healthy': True, 'preserves_forwarding': True}}
    code, data = post(client, headers, {'agent_id': agent_id})
    assert code == 200, data
    assert seen == [(9020, 'POST', 'forwarding', {})]
    gateway = data['gateway']
    assert gateway['state'] == 'persisted' and gateway['agent_id'] == agent_id
    assert gateway['routers'] == [{'id': 'r1', 'name': '路由器1'}, {'id': 'r2', 'name': '路由器2'}]
    assert gateway['gateway_record'] is True and gateway['preserves_forwarding'] is True


def test_persist_on_an_aio_agent_below_1_8_41_reads_its_layout_first(home):
    client, headers, agent, routers, seen, replies = home
    agent_id = agent(9021, version=AIO)
    routers('home', agent_id)
    replies[(9021, 'status')] = status(layout='native_aio', current='0', persisted=False)
    replies[(9021, 'forwarding')] = {'success': True, 'ipv4_forwarding': forwarding(layout='native_aio'),
                                     'managed': {'active': False}}
    code, data = post(client, headers, {'agent_id': agent_id})
    assert code == 200, data
    assert [item[2] for item in seen] == ['status', 'forwarding']


def test_persist_on_a_single_service_agent_below_1_8_41_asks_for_an_update(home):
    client, headers, agent, routers, seen, replies = home
    agent_id = agent(9022, version=AIO)
    routers('home', agent_id)
    replies[(9022, 'status')] = status(supported=False, unsupported_reason='仅支持普通 Shell 同机安装')
    code, data = post(client, headers, {'agent_id': agent_id})
    assert (code, data['code'], data['message']) == (409, 'UPGRADE_REQUIRED', bridge.STANDALONE_UPGRADE)
    assert [item[2] for item in seen] == ['status']


@pytest.mark.parametrize('body', [None, {}, {'agent_id': 1}, {'agent_id': 'x', 'prepare_tun': True}, ['x']])
def test_persist_accepts_only_an_agent_id(home, body):
    client, headers, agent, routers, seen, replies = home
    routers('home', agent(9023))
    code, data = post(client, headers, body)
    assert (code, data['code']) == (400, 'INVALID_REQUEST')
    assert seen == []


def test_persist_needs_a_router_link_in_this_profile(home):
    client, headers, agent, routers, seen, replies = home
    agent_id = agent(9024)
    routers('home', '')
    routers('other', agent_id)
    code, data = post(client, headers, {'agent_id': agent_id})
    assert (code, data['code']) == (404, 'NOT_LINKED')
    code, data = post(client, {**headers, 'X-ConfigFlow-Profile': 'other'}, {'agent_id': agent_id})
    assert code == 200, data
    assert [item[2] for item in seen] == ['forwarding']


@pytest.mark.parametrize('fields,code,error', [
    ({'status': 'offline'}, 409, 'AGENT_OFFLINE'),
    ({'deployment_method': 'docker'}, 409, 'UNSUPPORTED_ENVIRONMENT'),
    ({'service_type': 'oxidns'}, 400, 'WRONG_SERVICE'),
    ({'version': '1.8.39-go'}, 409, 'UPGRADE_REQUIRED'),
])
def test_persist_refuses_agents_that_cannot_persist(home, fields, code, error):
    client, headers, agent, routers, seen, replies = home
    agent_id = agent(9025, **fields)
    routers('home', agent_id)
    status_code, data = post(client, headers, {'agent_id': agent_id})
    assert (status_code, data['code']) == (code, error)
    assert seen == []


def test_persist_refusal_by_the_agent_is_passed_through(home):
    client, headers, agent, routers, seen, replies = home
    agent_id = agent(9026)
    routers('home', agent_id)
    replies[(9026, 'forwarding')] = Reply({'success': False, 'message': 'Agent 服务存在未知的 ExecStartPre，拒绝修改'}, 409)
    code, data = post(client, headers, {'agent_id': agent_id})
    assert (code, data['code'], data['message']) == (409, 'GATEWAY_ERROR', 'Agent 服务存在未知的 ExecStartPre，拒绝修改')


def test_persist_of_a_deleted_agent(home):
    client, headers, agent, routers, seen, replies = home
    routers('home', 'deleted-agent')
    code, data = post(client, headers, {'agent_id': 'deleted-agent'})
    assert (code, data['code']) == (404, 'AGENT_NOT_FOUND')
