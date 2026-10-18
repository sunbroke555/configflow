"""RouterOS mode transitions preserve working paths and narrow obsolete ownership."""
import copy
import ipaddress

import pytest

from backend.routers.plan import build_plan
from backend.routers.release import execute
from backend.routers.report import build_report
from backend.test_ros_policy import (
    GATEWAY, MANGLE, NAT, ROUTER, ROUTES, TARGET, MemoryROS,
    configurations, desired_for, inspect, release,
)


def static_release(driver, desired):
    state = driver.read_state()
    report = build_report(desired, state)
    plan = build_plan(ROUTER, desired, state, report, {'status': 'disabled'}, driver)
    assert not plan['blockers'], plan['blockers']
    result = execute(plan, driver, None)
    assert result['ok'], result


def matches_destination(row, ip):
    return ipaddress.ip_address(ip) in ipaddress.ip_network(row.get('dst-address', '0.0.0.0/0'))


def nat_action(driver, source, destination, interface):
    from backend.routers.ros_policy import ADDRESSES
    source, destination = ipaddress.ip_address(source), ipaddress.ip_address(destination)

    def listed(name, address):
        return any(address in ipaddress.ip_network(row['address'])
                   for row in driver.rows[ADDRESSES] if row.get('list') == name)

    for row in driver.rows[NAT]:
        if row.get('chain') != 'srcnat' or row.get('disabled') == 'true':
            continue
        if row.get('out-interface') not in (None, '', interface):
            continue
        if source not in ipaddress.ip_network(row.get('src-address', '0.0.0.0/0')):
            continue
        if destination not in ipaddress.ip_network(row.get('dst-address', '0.0.0.0/0')):
            continue
        if row.get('src-address-list') and not listed(row['src-address-list'], source):
            continue
        if row.get('dst-address-list') and not listed(row['dst-address-list'], destination):
            continue
        return row.get('action')
    return None


def test_static_to_policy_replaces_broad_nat_without_exempting_direct_wan():
    driver = MemoryROS()
    driver.rows[NAT][0].pop('out-interface')
    desired = desired_for(driver, selected=False)
    desired.pop('ros_policy')
    static_release(driver, desired)
    old_ids = {row['.id'] for row in driver.rows[NAT]
               if row.get('action') == 'accept' and row.get('dst-address') == TARGET}
    assert old_ids
    _, plan = release(driver)
    assert not old_ids & {row['.id'] for row in driver.rows[NAT]}
    accepts = [row for row in driver.rows[NAT] if row.get('action') == 'accept']
    assert not any(matches_destination(row, '91.108.4.1') and not row.get('out-interface')
                   and not row.get('dst-address-list') for row in accepts)
    assert any(row.get('out-interface') == 'bridge' and row.get('src-address-list')
               and row.get('dst-address-list') for row in accepts)
    assert any(row.get('dst-address') == '198.18.0.0/16' for row in accepts)
    assert inspect(driver)[1]['empty']


@pytest.mark.parametrize('disabled', ['false', 'true'])
def test_conflicting_static_replacement_cannot_withdraw_working_policy(disabled):
    driver = MemoryROS()
    release(driver)
    driver.rows[ROUTES].append({'.id': '*AB', 'dst-address': TARGET,
                               'gateway': '192.168.0.9', 'routing-table': 'main',
                               'disabled': disabled, 'comment': 'handmade'})
    before = configurations(driver)
    manifest = copy.deepcopy(driver.ros_policy_manifest)
    writes = copy.deepcopy(driver.writes)
    _, plan = inspect(driver, desired_for(driver, selected=False))
    assert any('替代静态路由' in item for item in plan['blockers'])
    assert not execute(plan, driver, None)['ok']
    assert configurations(driver) == before
    assert driver.ros_policy_manifest == manifest
    assert driver.writes == writes


def test_partial_aggregate_switch_removes_only_policy_coverage():
    driver = MemoryROS()
    driver.rows[ROUTES].append({'.id': '*AB', 'dst-address': '91.108.4.0/23',
                               'gateway': GATEWAY, 'routing-table': 'main', 'active': 'true',
                               'comment': 'cf-two-sets', 'distance': '7'})
    manual = {'.id': '*AC', 'dst-address': '8.0.0.0/8', 'gateway': GATEWAY,
              'routing-table': 'main', 'comment': 'handmade'}
    driver.rows[ROUTES].append(copy.deepcopy(manual))
    desired = desired_for(driver, cidrs=['91.108.4.0/24'])
    desired['proxy_cidrs'].append({'cidr': '91.108.5.0/24', 'sources': ['still-static']})
    _, plan = inspect(driver, desired)
    assert not plan['blockers'], plan['blockers']
    assert execute(plan, driver, None)['ok']
    main = [row for row in driver.rows[ROUTES] if row.get('routing-table', 'main') == 'main']
    assert not any(matches_destination(row, '91.108.4.1') for row in main)
    assert any(row.get('dst-address') == '91.108.5.0/24' and row['gateway'] == GATEWAY for row in main)
    assert next(row for row in main if row['.id'] == '*AC') == manual
    desired['ros_policy']['previous'] = driver.ros_policy_manifest
    report, next_plan = inspect(driver, desired)
    assert report['in_sync'] and next_plan['empty'] and not next_plan['blockers']


def test_broader_managed_nat_preserves_other_destinations_and_matchers():
    driver = MemoryROS()
    driver.rows[NAT][0].pop('out-interface')
    driver.rows[NAT].insert(0, {'.id': '*AB', 'chain': 'srcnat', 'action': 'accept',
        'dst-address': '91.108.4.0/23', 'src-address': '192.168.0.0/24',
        'protocol': 'tcp', 'comment': 'cf-old', 'disabled': 'false'})
    _, plan = inspect(driver, desired_for(driver, cidrs=['91.108.4.0/24']))
    assert not plan['blockers'], plan['blockers']
    assert execute(plan, driver, None)['ok']
    old_scope = [row for row in driver.rows[NAT] if row.get('comment') == 'cf-old']
    assert [row['dst-address'] for row in old_scope] == ['91.108.5.0/24']
    assert old_scope[0]['src-address'] == '192.168.0.0/24' and old_scope[0]['protocol'] == 'tcp'
    assert driver.rows[NAT].index(old_scope[0]) < next(i for i, row in enumerate(driver.rows[NAT])
                                                    if row.get('action') == 'masquerade')


def test_transition_failure_restores_old_aggregate_nat_and_rule_order():
    driver = MemoryROS()
    driver.rows[NAT][0].pop('out-interface')
    driver.rows[NAT].insert(0, {'.id': '*AB', 'chain': 'srcnat', 'action': 'accept',
        'dst-address': '91.108.4.0/23', 'src-address': '192.168.0.0/24',
        'comment': 'cf-old', 'disabled': 'false'})
    driver.rows[ROUTES].append({'.id': '*AC', 'dst-address': '91.108.4.0/23',
                               'gateway': GATEWAY, 'routing-table': 'main', 'active': 'true',
                               'comment': 'cf-old', 'distance': '7'})
    before = configurations(driver)
    desired = desired_for(driver, cidrs=['91.108.4.0/24'])
    _, plan = inspect(driver, desired)
    assert not plan['blockers'], plan['blockers']
    driver.fail = ('DELETE', NAT, 1)
    result = execute(plan, driver, None)
    assert not result['ok'] and not any(row['status'] == 'failed' for row in result['rollback'])
    assert configurations(driver) == before
    assert not driver.rows[MANGLE]


def test_inactive_static_replacement_keeps_original_policy_until_it_can_forward():
    driver = MemoryROS()
    release(driver)
    before = configurations(driver)
    manifest = copy.deepcopy(driver.ros_policy_manifest)
    _, plan = inspect(driver, desired_for(driver, selected=False))
    assert not plan['blockers'], plan['blockers']
    driver.inactive = True
    result = execute(plan, driver, None)
    assert not result['ok']
    assert configurations(driver) == before
    assert driver.ros_policy_manifest == manifest


def test_inactive_partial_aggregate_replacement_keeps_original_static_coverage():
    driver = MemoryROS()
    driver.rows[ROUTES].append({'.id': '*AB', 'dst-address': '91.108.4.0/23',
                               'gateway': GATEWAY, 'routing-table': 'main', 'active': 'true',
                               'comment': 'cf-two-sets', 'distance': '7'})
    before = configurations(driver)
    desired = desired_for(driver, cidrs=['91.108.4.0/24'])
    desired['proxy_cidrs'].append({'cidr': '91.108.5.0/24', 'sources': ['still-static']})
    _, plan = inspect(driver, desired)
    assert not plan['blockers'], plan['blockers']
    driver.inactive = True
    result = execute(plan, driver, None)
    assert not result['ok']
    assert configurations(driver) == before
    assert not driver.rows[MANGLE]


def test_retained_nat_destinations_keep_return_coverage_and_converge_on_first_publish():
    from backend.routers.ros_policy import snapshot
    from backend.test_router_release import FakeAgent, SAFE_RETURN_STATUS
    driver = MemoryROS()
    driver.rows[NAT].insert(0, {'.id': '*AB', 'chain': 'srcnat', 'action': 'accept',
        'dst-address': '91.108.4.0/23', 'src-address': '192.168.0.0/24',
        'comment': 'cf-old', 'disabled': 'false'})
    desired = desired_for(driver, cidrs=['91.108.4.0/24'])
    router = {**ROUTER, 'manage_return_routing': True}
    agent = FakeAgent(SAFE_RETURN_STATUS)

    def with_return():
        state = driver.read_state()
        state.ros_policy = snapshot(driver)
        report = build_report(desired, state)
        return build_plan(router, desired, state, report, agent.status(), driver)

    plan = with_return()
    assert not plan['blockers'], plan['blockers']
    retained = ipaddress.ip_address('91.108.5.1')
    assert any(retained in ipaddress.ip_network(cidr)
               for cidr in plan['checks']['return_routing']['proxy_dst'])
    result = execute(plan, driver, agent)
    assert result['ok'], result
    assert any(row.get('comment') == 'cf-old' and row.get('dst-address') == '91.108.5.0/24'
               for row in driver.rows[NAT])
    assert any(retained in ipaddress.ip_network(cidr)
               for cidr in agent.state['managed']['spec']['proxy_dst'])
    desired['ros_policy']['previous'] = driver.ros_policy_manifest
    next_plan = with_return()
    assert next_plan['empty'] and not next_plan['blockers']


@pytest.mark.parametrize('with_literal', [False, True])
def test_list_backed_nat_is_not_silently_materialized_during_policy_migration(with_literal):
    from backend.routers.ros_policy import ADDRESSES
    driver = MemoryROS()
    driver.rows[ADDRESSES] += [
        {'.id': '*AD', 'list': 'legacy-targets', 'address': '91.108.4.0/24'},
        {'.id': '*AE', 'list': 'legacy-targets', 'address': '91.108.5.0/24'}]
    original = {'.id': '*AB', 'chain': 'srcnat', 'action': 'accept',
                'dst-address-list': 'legacy-targets', 'src-address': '192.168.0.0/24',
                'comment': 'cf-old', 'disabled': 'false'}
    if with_literal:
        original['dst-address'] = '91.108.4.0/23'
    driver.rows[NAT].insert(0, original)
    before = configurations(driver)
    _, plan = inspect(driver, desired_for(driver, cidrs=['91.108.4.0/24']))
    assert any('列表型 NAT' in item for item in plan['blockers'])
    assert not execute(plan, driver, None)['ok']
    assert configurations(driver) == before and not driver.writes


@pytest.mark.parametrize('protected', ['198.18.0.0/16', '198.18.1.0/24'])
def test_skipped_conflict_preserves_existing_return_protection(protected):
    from backend.test_router_release import FakeAgent, SAFE_RETURN_STATUS
    driver = MemoryROS()
    driver.rows[ROUTES][1]['gateway'] = '192.168.0.9'
    agent = FakeAgent(SAFE_RETURN_STATUS)
    allocation = agent.state['safety']['allocation']
    agent.apply({'router_ip': '192.168.0.1', 'proxy_dst': [protected],
                 'table': allocation['table'], 'priority': allocation['priority'],
                 'mark': allocation['mark'], 'safety_version': 1})
    desired = desired_for(driver, selected=False)
    desired.pop('ros_policy')
    state = driver.read_state()
    report = build_report(desired, state)
    plan = build_plan({**ROUTER, 'manage_return_routing': True}, desired, state,
                      report, agent.status(), driver)
    assert report['routes']['items'][0]['status'] == 'conflict'
    assert not plan['blockers'], plan['blockers']
    assert execute(plan, driver, agent)['ok']
    assert any(ipaddress.ip_network(protected).subnet_of(ipaddress.ip_network(cidr))
               for cidr in agent.state['managed']['spec']['proxy_dst'])


@pytest.mark.parametrize('fail_withdrawal', [False, True])
def test_policy_withdrawal_preserves_source_nat_protection_at_every_write(fail_withdrawal):
    driver = MemoryROS()
    driver.rows[NAT][0].pop('out-interface')
    release(driver)
    before = configurations(driver)
    if fail_withdrawal:
        driver.fail = ('DELETE', ROUTES, driver.counts['DELETE', ROUTES] + 1)
    original_request = driver._request
    observations = []

    def request(method, path, values=None):
        result = original_request(method, path, values)
        if method == 'GET':
            return result
        action = nat_action(driver, '192.168.0.101', '91.108.4.1', 'bridge')
        observations.append((method, path, action))
        assert action == 'accept', observations
        static = any(row.get('dst-address') == TARGET and row.get('routing-table', 'main') == 'main'
                     for row in driver.rows[ROUTES])
        direct = nat_action(driver, '192.168.0.102', '91.108.4.1',
                            'bridge' if static else 'pppoe-out1')
        assert direct == ('accept' if static else 'masquerade'), (method, path, direct)
        return result

    driver._request = request
    _, plan = inspect(driver, desired_for(driver, selected=False))
    assert not plan['blockers'], plan['blockers']
    result = execute(plan, driver, None)
    assert observations
    assert result['ok'] is not fail_withdrawal
    if fail_withdrawal:
        assert configurations(driver) == before
        assert not any(row['status'] == 'failed' for row in result['rollback'])
    else:
        assert driver.ros_policy_manifest is None
        assert inspect(driver, desired_for(driver, selected=False))[1]['empty']


@pytest.mark.parametrize('condition', ['other-source', 'protocol', 'other-interface', 'late-accept', 'complete'])
def test_withdrawal_requires_full_old_source_destination_nat_proof(condition):
    driver = MemoryROS()
    driver.rows[NAT][0].pop('out-interface')
    release(driver)
    row = {'.id': '*NARROW', 'chain': 'srcnat', 'action': 'accept',
           'dst-address': TARGET, 'src-address': '192.168.0.101/32',
           'out-interface': 'bridge', 'comment': 'cf-existing', 'disabled': 'false'}
    if condition == 'other-source':
        row['src-address'] = '192.168.0.102/32'
    elif condition == 'protocol':
        row['protocol'] = 'tcp'
    elif condition == 'other-interface':
        row['out-interface'] = 'wan'
    elif condition == 'complete':
        row['src-address'] = '192.168.0.0/24'
    driver.rows[NAT].insert(len(driver.rows[NAT]) if condition == 'late-accept' else 0, row)
    before = configurations(driver)
    manifest = copy.deepcopy(driver.ros_policy_manifest)
    original_request = driver._request
    observations = []

    def request(method, path, values=None):
        result = original_request(method, path, values)
        if method != 'GET':
            action = nat_action(driver, '192.168.0.101', '91.108.4.1', 'bridge')
            observations.append(action)
            assert action == 'accept', (method, path, action)
        return result

    driver._request = request
    _, plan = inspect(driver, desired_for(driver, selected=False))
    if condition != 'complete':
        assert plan['blockers']
        assert not execute(plan, driver, None)['ok']
        assert configurations(driver) == before and driver.ros_policy_manifest == manifest
        assert not observations
        return
    assert not plan['blockers'], plan['blockers']
    result = execute(plan, driver, None)
    assert observations
    if condition == 'complete':
        assert result['ok'], result
        assert driver.ros_policy_manifest is None
    else:
        assert not result['ok']
        assert configurations(driver) == before
        assert driver.ros_policy_manifest == manifest
        assert any('来源、目的及出口' in entry['summary'] for entry in result['log'])
