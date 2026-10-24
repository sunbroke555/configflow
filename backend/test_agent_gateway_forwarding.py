"""The Agents page's IPv4 forwarding view: proxy gateways linked by routers in any profile."""
import pytest

from backend.agents import linux_gateway as bridge
from backend.test_agent_upgrade_auth import authenticated_app  # noqa: F401
from backend.test_router_gateway_forwarding import Reply, forwarding, home, status  # noqa: F401

PATH = '/api/agents/gateway-forwarding'


def entries(client, headers):
    response = client.get(PATH, headers=headers)
    assert response.status_code == 200, response.get_json()
    assert response.headers['Cache-Control'] == 'no-store'
    return response.get_json()['gateways']


def post(client, headers, body):
    response = client.post(PATH, headers=headers, json=body)
    assert response.headers['Cache-Control'] == 'no-store'
    return response.status_code, response.get_json()


@pytest.fixture
def unscoped(home):
    """The Agents page is global: no profile header."""
    client, headers, agent, routers, seen, replies = home
    headers = {key: value for key, value in headers.items() if key != 'X-ConfigFlow-Profile'}
    return client, headers, agent, routers, seen, replies


def test_view_follows_router_links_in_every_profile_and_names_them(unscoped):
    client, headers, agent, routers, seen, replies = unscoped
    shared, other_only, unlinked = agent(9101), agent(9102, profile_id='other'), agent(9103)
    routers('home', shared)
    routers('other', other_only, shared)
    replies[(9101, 'status')] = status()
    replies[(9102, 'status')] = status(current='0', persisted=False, config='absent')
    result = {item['agent_id']: item for item in entries(client, headers)}
    assert set(result) == {shared, other_only}
    assert result[shared]['routers'] == [
        {'id': 'r1', 'name': '路由器1', 'profile_id': 'home', 'profile_name': 'Home'},
        {'id': 'r2', 'name': '路由器2', 'profile_id': 'other', 'profile_name': 'Other'}]
    assert result[shared]['state'] == 'persisted'
    assert result[other_only]['state'] == 'needs_persist' and result[other_only]['can_persist'] is True
    # Each linked Agent is asked once; Agents no router uses are never asked.
    assert sorted((item[0], item[2]) for item in seen) == [(9101, 'status'), (9102, 'status')]
    assert unlinked not in result


def test_view_is_empty_without_router_links_and_asks_no_agent(unscoped):
    client, headers, agent, routers, seen, replies = unscoped
    agent(9104)
    routers('home', '')
    assert entries(client, headers) == []
    assert seen == []


def test_route_is_not_taken_for_an_agent_id(unscoped):
    client, headers, agent, routers, seen, replies = unscoped
    response = client.get('/api/agents/' + agent(9105), headers=headers)
    assert response.status_code == 200
    assert 'gateways' not in (response.get_json() or {})


def test_view_requires_authentication(unscoped):
    client, *_ = unscoped
    assert client.get(PATH).status_code == 401
    assert client.post(PATH, json={'agent_id': 'x'}).status_code == 401


def test_persist_works_for_a_gateway_linked_only_in_another_profile(unscoped):
    client, headers, agent, routers, seen, replies = unscoped
    agent_id = agent(9106, profile_id='other')
    routers('other', agent_id)
    replies[(9106, 'forwarding')] = {'success': True, 'ipv4_forwarding': forwarding(), 'managed': {'active': False}}
    code, data = post(client, headers, {'agent_id': agent_id})
    assert code == 200, data
    assert seen == [(9106, 'POST', 'forwarding', {})]
    assert data['gateway']['state'] == 'persisted'
    assert data['gateway']['routers'] == [{'id': 'r1', 'name': '路由器1', 'profile_id': 'other', 'profile_name': 'Other'}]


@pytest.mark.parametrize('body', [None, [], {}, {'agent_id': 1}, {'agent_id': 'a', 'profile_id': 'home'}])
def test_persist_takes_exactly_an_agent_id(unscoped, body):
    client, headers, *_ = unscoped
    code, data = post(client, headers, body)
    assert (code, data['code']) == (400, 'INVALID_REQUEST')


def test_persist_refuses_an_agent_no_router_uses(unscoped):
    client, headers, agent, routers, seen, replies = unscoped
    agent_id = agent(9107)
    routers('home', '')
    code, data = post(client, headers, {'agent_id': agent_id})
    assert (code, data['code']) == (404, 'NOT_LINKED')
    assert seen == []


def test_persist_maps_agent_errors(unscoped):
    client, headers, agent, routers, seen, replies = unscoped
    agent_id = agent(9108, status='offline')
    routers('home', agent_id)
    code, data = post(client, headers, {'agent_id': agent_id})
    assert (code, data['code']) == (409, 'AGENT_OFFLINE')
    assert seen == []


def test_upgrade_reply_is_reported_per_agent(unscoped):
    client, headers, agent, routers, seen, replies = unscoped
    agent_id = agent(9109)
    routers('home', agent_id)
    replies[(9109, 'status')] = Reply(b'not found', 404)
    [entry] = entries(client, headers)
    assert entry['state'] == 'upgrade_required'
    assert entry['message'] == bridge.GATEWAY_FORWARDING_MESSAGES['upgrade_required']
