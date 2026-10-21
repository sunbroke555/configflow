"""Agent return routing is opt-in and never touched by default router releases."""
import copy

import pytest

from backend.routers.plan import build_plan
from backend.routers.release import execute
from backend.routers.report import build_report
from backend.test_agent_upgrade_auth import authenticated_app  # noqa: F401 (pytest fixture)
from backend.test_router_release import (
    DESIRED, GATEWAY, HOME_NATS, HOME_ROUTES, SAFE_RETURN_STATUS, FakeAgent, StatefulIKuai,
    _driver as ikuai_driver,
)
from backend.test_routeros_release import driver_for, release_session


class NoAgentCalls:
    def apply(self, *args, **kwargs):
        pytest.fail('Return-routing apply must not be called while disabled')

    def status(self, *args, **kwargs):
        pytest.fail('Return-routing status must not be called while disabled')

    def remove(self, *args, **kwargs):
        pytest.fail('Existing return-routing rules must not be removed while disabled')


def router_driver(vendor):
    session = StatefulIKuai(HOME_ROUTES, HOME_NATS) if vendor == 'ikuai' else release_session()
    driver = ikuai_driver(session) if vendor == 'ikuai' else driver_for(session)
    return driver, session


def plan_for(router, driver, status, desired=DESIRED):
    state = driver.read_state()
    return build_plan(router, desired, state, build_report(desired, state), status, driver)


def return_stage(plan):
    return next(stage for stage in plan['stages'] if stage['key'] == 'return_routing')


@pytest.mark.parametrize('vendor', ['ikuai', 'routeros'])
@pytest.mark.parametrize('setting', ['absent', False])
@pytest.mark.parametrize('status', ['offline', 'unavailable'])
def test_default_off_publishes_to_domain_endpoint_without_return_routing_capability(vendor, setting, status):
    router = {'type': vendor, 'endpoint': 'http://router.local'}
    if setting != 'absent':
        router['manage_return_routing'] = setting
    driver, session = router_driver(vendor)
    plan = plan_for(router, driver, {'status': status, 'message': 'Agent 离线或不支持回流'})
    assert plan['blockers'] == []
    stage = return_stage(plan)
    assert stage['enabled'] is False and stage['ops'] == [] and stage['note']
    assert 'return_routing' not in plan['checks']
    result = execute(plan, driver, NoAgentCalls())
    assert result['ok'], result
    assert session.writes
    assert not any(entry['stage'] == 'return_routing' for entry in result['log'] + result['rollback'])


@pytest.mark.parametrize('gateway', ['', 'gateway.local', '2001:db8::1'])
def test_disabled_return_routing_still_requires_valid_proxy_gateway(gateway):
    driver, session = router_driver('ikuai')
    desired = {**DESIRED, 'proxy_gateway': gateway}
    plan = plan_for({'type': 'ikuai', 'endpoint': 'http://router.local'}, driver,
                    {'status': 'disabled'}, desired)
    assert plan['blockers']
    assert session.writes == []


@pytest.mark.parametrize(('endpoint', 'status'), [
    ('http://10.0.0.1', {'status': 'offline', 'message': 'Agent 离线'}),
    ('http://10.0.0.1', {'status': 'unavailable', 'message': 'Agent 不支持回流'}),
    ('http://router.local', SAFE_RETURN_STATUS),
])
def test_opt_in_keeps_agent_availability_and_ipv4_upstream_protection(endpoint, status):
    driver, session = router_driver('ikuai')
    plan = plan_for({'type': 'ikuai', 'endpoint': endpoint, 'manage_return_routing': True}, driver, status)
    assert plan['blockers']
    result = execute(plan, driver, NoAgentCalls())
    assert not result['ok'] and session.writes == []


@pytest.mark.parametrize('vendor', ['ikuai', 'routeros'])
def test_turning_off_does_not_clean_up_existing_managed_or_legacy_return_rules(vendor):
    driver, _ = router_driver(vendor)
    status = copy.deepcopy(SAFE_RETURN_STATUS)
    status['managed'] = {'table_present': True, 'spec': {
        'router_ip': '10.0.0.99', 'proxy_dst': ['203.0.113.0/24'],
        'mark': '0x400000', 'table': 104, 'priority': 104}}
    before = copy.deepcopy(status)
    plan = plan_for({'type': vendor, 'endpoint': 'http://10.0.0.1', 'manage_return_routing': False}, driver, status)
    assert return_stage(plan)['ops'] == []
    assert all(not op['kind'].startswith('return_routing') for stage in plan['stages'] for op in stage['ops'])
    assert execute(plan, driver, NoAgentCalls())['ok']
    assert status == before


@pytest.mark.parametrize('vendor', ['ikuai', 'routeros'])
def test_failed_release_does_not_touch_agent_when_return_routing_is_disabled(vendor):
    driver, session = router_driver(vendor)
    session.fail = ('nat_rule', 'edit') if vendor == 'ikuai' else ('PUT', '/ip/firewall/nat', 2)
    plan = plan_for({'type': vendor, 'endpoint': 'http://router.local'}, driver, {'status': 'disabled'})
    result = execute(plan, driver, NoAgentCalls())
    assert result['ok'] is False and result['failed_stage'] == 'nat_bypass'
    assert result['rollback']
    assert not any(item['stage'] == 'return_routing' for item in result['rollback'])


@pytest.mark.parametrize('vendor', ['ikuai', 'routeros'])
def test_noop_preview_fingerprint_changes_when_return_routing_setting_changes(vendor):
    driver, _ = router_driver(vendor)
    router = {'type': vendor, 'endpoint': 'http://10.0.0.1', 'manage_return_routing': True}
    agent = FakeAgent(SAFE_RETURN_STATUS)
    assert execute(plan_for(router, driver, agent.status()), driver, agent)['ok']
    on = plan_for(router, driver, {**agent.status(), 'status': 'ok'})
    off = plan_for({**router, 'manage_return_routing': False}, driver, {'status': 'disabled'})
    assert on['empty'] and off['empty']
    assert return_stage(on)['ops'] == return_stage(off)['ops'] == []
    assert return_stage(on)['enabled'] is True and return_stage(off)['enabled'] is False
    assert on['fingerprint'] != off['fingerprint']


@pytest.mark.parametrize('vendor', ['ikuai', 'routeros'])
def test_default_off_api_never_requests_offline_agents_return_routing(authenticated_app, monkeypatch, vendor):  # noqa: F811
    client, _, agents, admin, _ = authenticated_app
    from backend.routes import routers as routes_module
    driver, session = router_driver(vendor)
    monkeypatch.setitem(routes_module.DRIVERS, vendor, lambda *args, **kwargs: driver)
    monkeypatch.setattr(routes_module, '_agent', lambda agent_id: {**agents[0], 'status': 'offline', 'host': GATEWAY})
    monkeypatch.setattr(routes_module, 'compute_desired', lambda *args, **kwargs: DESIRED)

    def forbidden_status(*args, **kwargs):
        pytest.fail('Disabled integration must not request Agent return-routing status')

    monkeypatch.setattr(routes_module, '_return_routing', forbidden_status)
    monkeypatch.setattr(routes_module, 'AgentReturnRouting', lambda *args: NoAgentCalls())
    created = client.post('/api/routers', headers=admin, json={
        'name': '主路由', 'type': vendor, 'endpoint': 'http://router.local', 'username': 'admin',
        'password': 'pw', 'proxy_gateway_agent_id': agents[0]['id']})
    assert created.status_code == 200, created.get_json()
    router = created.get_json()['router']
    assert router['manage_return_routing'] is False
    report = client.get(f'/api/routers/{router["id"]}/report', headers=admin)
    assert report.status_code == 200, report.get_json()
    assert report.get_json()['report']['return_routing']['status'] == 'disabled'
    preview = client.post(f'/api/routers/{router["id"]}/plan', headers=admin)
    assert preview.status_code == 200, preview.get_json()
    plan = preview.get_json()['plan']
    assert plan['blockers'] == [] and session.writes == []
    applied = client.post(f'/api/routers/{router["id"]}/apply', headers=admin,
                         json={'fingerprint': plan['fingerprint']})
    assert applied.status_code == 200 and applied.get_json()['result']['ok'], applied.get_json()
    assert session.writes


def test_api_setting_persists_and_noop_toggle_rejects_old_preview(authenticated_app, monkeypatch):  # noqa: F811
    client, _, agents, admin, _ = authenticated_app
    from backend.routes import routers as routes_module
    driver, session = router_driver('ikuai')
    agent = FakeAgent(SAFE_RETURN_STATUS)
    initial_router = {'type': 'ikuai', 'endpoint': 'http://10.0.0.1', 'manage_return_routing': True}
    assert execute(plan_for(initial_router, driver, SAFE_RETURN_STATUS), driver, agent)['ok']
    session.writes.clear()
    agent.calls.clear()
    monkeypatch.setitem(routes_module.DRIVERS, 'ikuai', lambda *args, **kwargs: driver)
    monkeypatch.setattr(routes_module, 'compute_desired', lambda *args, **kwargs: DESIRED)
    monkeypatch.setattr(routes_module, '_return_routing', lambda record: {**agent.status(), 'status': 'ok'})
    monkeypatch.setattr(routes_module, 'AgentReturnRouting', lambda *args: agent)
    created = client.post('/api/routers', headers=admin, json={
        'name': '主路由', 'type': 'ikuai', 'endpoint': 'http://10.0.0.1', 'username': 'admin',
        'password': 'pw', 'proxy_gateway_agent_id': agents[0]['id'], 'manage_return_routing': True})
    router_id = created.get_json()['router']['id']
    on = client.post(f'/api/routers/{router_id}/plan', headers=admin).get_json()['plan']
    assert on['empty'] and return_stage(on)['enabled'] is True

    changed = client.put(f'/api/routers/{router_id}', headers=admin, json={'manage_return_routing': False})
    assert changed.status_code == 200 and changed.get_json()['router']['manage_return_routing'] is False
    rejected = client.post(f'/api/routers/{router_id}/apply', headers=admin,
                           json={'fingerprint': on['fingerprint']})
    assert rejected.status_code == 409, rejected.get_json()
    off = rejected.get_json()['plan']
    assert not off['empty'] and off['fingerprint'] != on['fingerprint']
    # 端口分流 needs return routing: switching it off moves the CIDRs back to static routes.
    route_ops = next(stage['ops'] for stage in off['stages'] if stage['key'] == 'routes')
    assert [op['kind'] for op in route_ops].count('policy_route_delete') == 2
    assert session.writes == [] and agent.calls == []

    assert client.put(f'/api/routers/{router_id}', headers=admin,
                      json={'manage_return_routing': True}).status_code == 200
    renamed = client.put(f'/api/routers/{router_id}', headers=admin, json={'name': '新名字'})
    assert renamed.get_json()['router']['manage_return_routing'] is True
    rejected = client.post(f'/api/routers/{router_id}/apply', headers=admin,
                           json={'fingerprint': off['fingerprint']})
    assert rejected.status_code == 409
    stored = routes_module.get_config()['routers']['items'][0]
    assert stored['manage_return_routing'] is True
    assert client.get('/api/routers', headers=admin).get_json()['routers'][0]['manage_return_routing'] is True

    config = routes_module.get_config()
    config['routers']['items'][0].pop('manage_return_routing')
    routes_module.save_config(config)
    assert client.get('/api/routers', headers=admin).get_json()['routers'][0]['manage_return_routing'] is False


@pytest.mark.parametrize('value', ['true', 1, None])
def test_api_rejects_non_boolean_return_routing_setting(authenticated_app, value):  # noqa: F811
    client, _, _, admin, _ = authenticated_app
    payload = {'name': '主路由', 'type': 'ikuai', 'endpoint': 'http://router.local',
               'username': 'admin', 'password': 'pw'}
    assert client.post('/api/routers', headers=admin,
                       json={**payload, 'manage_return_routing': value}).status_code == 400
    created = client.post('/api/routers', headers=admin, json=payload).get_json()['router']
    assert client.put(f'/api/routers/{created["id"]}', headers=admin,
                      json={'manage_return_routing': value}).status_code == 400
    assert client.get('/api/routers', headers=admin).get_json()['routers'][0]['manage_return_routing'] is False


@pytest.mark.parametrize('vendor', ['ikuai', 'routeros'])
@pytest.mark.parametrize('safety', [
    None, {}, {'version': 0, 'ok': True, 'blockers': []},
    {'version': True, 'ok': True, 'blockers': []},
    {'version': 2, 'ok': True, 'blockers': []},
    {'version': 1, 'ok': 'true', 'blockers': []},
    {'version': 1, 'ok': True},
    {'version': 1, 'ok': True, 'blockers': 'unknown'},
    {'version': 1, 'ok': True, 'blockers': [False]},
])
def test_opt_in_blocks_old_or_incomplete_agent_safety_contract(vendor, safety):
    driver, session = router_driver(vendor)
    status = {**SAFE_RETURN_STATUS, 'safety': safety}
    plan = plan_for({'type': vendor, 'endpoint': 'http://10.0.0.1', 'manage_return_routing': True},
                    driver, status)
    assert plan['blockers'] and plan['stages'] == []
    result = execute(plan, driver, NoAgentCalls())
    assert not result['ok'] and session.writes == []


@pytest.mark.parametrize('conflict', [
    '路由表 104 已被其他程序占用', '优先级 104 已被其他规则占用',
    'mark 0x400000 与已有手工规则重叠', '已有回流规则缺少可验证的归属记录',
])
def test_legacy_or_occupied_resources_block_instead_of_being_adopted(conflict):
    driver, session = router_driver('ikuai')
    status = {**SAFE_RETURN_STATUS,
              'return_tables': [{'family': 'inet', 'name': 'my_return', 'marks': ['0x400000']}],
              'fwmark_rules': [{'priority': '104', 'mark': '0x400000/0x400000', 'table': '104'}],
              'safety': {'version': 1, 'ok': False, 'blockers': [conflict]}}
    plan = plan_for({'type': 'ikuai', 'endpoint': 'http://10.0.0.1', 'manage_return_routing': True},
                    driver, status)
    assert plan['blockers'] == [conflict] and plan['stages'] == []
    assert not execute(plan, driver, NoAgentCalls())['ok']
    assert session.writes == []


def test_new_agent_spec_never_adopts_unrelated_manual_tables():
    driver, _ = router_driver('ikuai')
    manual = {'family': 'inet', 'name': 'unrelated_manual', 'marks': ['0x8']}
    status = {**SAFE_RETURN_STATUS, 'return_tables': [manual]}
    agent = FakeAgent(status)
    plan = plan_for({'type': 'ikuai', 'endpoint': 'http://10.0.0.1', 'manage_return_routing': True},
                    driver, status)
    [op] = return_stage(plan)['ops']
    assert op['spec']['safety_version'] == 1
    assert 'adopt_legacy' not in op['spec'] and '接管' not in op['summary']
    assert execute(plan, driver, agent)['ok']
    assert manual in agent.status()['return_tables']


@pytest.mark.parametrize('change', ['old_agent', 'occupied', 'missing_handshake', 'legacy_adoption'])
def test_return_apply_rechecks_capability_and_occupancy_before_any_write(change):
    driver, session = router_driver('ikuai')
    agent = FakeAgent(SAFE_RETURN_STATUS)
    plan = plan_for({'type': 'ikuai', 'endpoint': 'http://10.0.0.1', 'manage_return_routing': True},
                    driver, agent.status())
    if change == 'old_agent':
        agent.state.pop('safety')
    elif change == 'occupied':
        agent.state['safety'] = {'version': 1, 'ok': False, 'blockers': ['路由表 104 已被占用']}
    elif change == 'missing_handshake':
        return_stage(plan)['ops'][0]['spec'].pop('safety_version')
    else:
        return_stage(plan)['ops'][0]['spec']['adopt_legacy'] = [{'family': 'inet', 'name': 'manual'}]
    result = execute(plan, driver, agent)
    assert not result['ok'] and result['failed_stage'] == 'return_routing'
    assert session.writes == [] and agent.calls == []


@pytest.mark.parametrize('change', ['occupied', 'old_agent'])
def test_failed_return_readback_only_cleans_up_through_ownership_aware_agent(change):
    manual = {'family': 'inet', 'name': 'added_by_other_program', 'marks': ['0x400000']}

    class ChangingAgent(FakeAgent):
        def apply(self, spec):
            super().apply(spec)
            self.state['return_tables'].append(manual)
            if change == 'occupied':
                self.state['safety'] = {'version': 1, 'ok': False, 'blockers': ['检测到新的标记占用']}
            else:
                self.state.pop('safety')

    driver, session = router_driver('ikuai')
    agent = ChangingAgent(SAFE_RETURN_STATUS)
    plan = plan_for({'type': 'ikuai', 'endpoint': 'http://10.0.0.1', 'manage_return_routing': True},
                    driver, agent.status())
    result = execute(plan, driver, agent)
    assert not result['ok'] and result['failed_stage'] == 'return_routing'
    assert session.writes == [] and manual in agent.status()['return_tables']
    if change == 'occupied':
        assert agent.calls[-1] == ('remove', False)
        assert result['rollback'][0]['status'] == 'ok'
    else:
        assert [call[0] for call in agent.calls] == ['apply']
        assert result['rollback'][0]['status'] == 'failed'


def test_unchanged_return_stage_rechecks_safety_before_router_writes():
    driver, session = router_driver('ikuai')
    router = {'type': 'ikuai', 'endpoint': 'http://10.0.0.1', 'manage_return_routing': True}
    agent = FakeAgent(SAFE_RETURN_STATUS)
    first = plan_for(router, driver, agent.status())
    agent.apply(return_stage(first)['ops'][0]['spec'])
    plan = plan_for(router, driver, agent.status())
    assert return_stage(plan)['ops'] == [] and not plan['empty']
    agent.calls.clear()
    agent.state['safety'] = {'version': 1, 'ok': False, 'blockers': ['优先级 104 已被占用']}
    result = execute(plan, driver, agent)
    assert not result['ok'] and result['failed_stage'] == 'return_routing'
    assert session.writes == [] and agent.calls == []


@pytest.mark.parametrize(('mark', 'ok'), [
    ('0x00400000/0x00400000', True), ('4194304/4194304', True),
    ('0x400000/0xffffffff', False), ('0x400000', False), ('garbage', False),
])
def test_return_readback_checks_mark_and_mask_semantics(mark, ok):
    class PrintedMarkAgent(FakeAgent):
        def apply(self, spec):
            super().apply(spec)
            self.state['fwmark_rules'][0]['mark'] = mark

    driver, session = router_driver('ikuai')
    agent = PrintedMarkAgent(SAFE_RETURN_STATUS)
    plan = plan_for({'type': 'ikuai', 'endpoint': 'http://10.0.0.1', 'manage_return_routing': True},
                    driver, agent.status())
    result = execute(plan, driver, agent)
    assert result['ok'] is ok, result
    if not ok:
        assert result['failed_stage'] == 'return_routing' and session.writes == []
