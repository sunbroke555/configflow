"""Policy advisories must not block explicit selections or weaken write ownership."""
import copy

import pytest

from backend.routers.desired import compute_desired
from backend.routers.plan import build_plan
from backend.routers.release import execute
from backend.routers.report import build_report
from backend.routers.ros_policy import inspect_desired, snapshot
from backend.test_ros_policy import (
    ADDRESSES, GATEWAY, MANGLE, ROUTER, ROUTES, RULES, TARGET, TABLES, VRFS,
    MemoryROS, configurations,
)

DIRECT_TARGET = '1.1.1.0/24'
SELECTED = ['direct', 'proxy']


def configuration(policy='DIRECT'):
    return {
        'rule_configs': [
            {'id': 'direct', 'itemType': 'ruleset', 'name': 'Direct IP',
             'enabled': True, 'policy': policy},
            {'id': 'proxy', 'itemType': 'ruleset', 'name': 'Proxy IP',
             'enabled': True, 'policy': 'Proxy'},
        ],
        'oxidns': {'device_policies': [
            {'ip': '192.168.0.101', 'mode': 'smart'},
            {'ip': '192.168.0.102', 'mode': 'direct'},
        ]},
    }


def contents():
    return {'direct': f'IP-CIDR,{DIRECT_TARGET},no-resolve\n',
            'proxy': f'IP-CIDR,{TARGET},no-resolve\n'}


def loader(texts):
    def load(item, _library):
        value = texts.get(item['id'], '')
        if isinstance(value, Exception):
            raise value
        return value
    return load


def preview(driver, config, texts, *, selected=SELECTED, static=()):
    router = {**ROUTER, 'ros_policy_rule_sets': list(selected), 'ip_rule_sets': list(static),
              'ros_policy_state': copy.deepcopy(getattr(driver, 'ros_policy_manifest', None))}
    desired = compute_desired(config, GATEWAY, list(static), content_loader=loader(texts))
    desired['ros_policy'] = inspect_desired(config, router, desired, content_loader=loader(texts))
    state = driver.read_state()
    state.ros_policy = snapshot(driver)
    state.nat_context.update(routing_rules=driver.rows[RULES], vrfs=driver.rows[VRFS])
    if state.nat_context['mangle'] is None:
        state.nat_context['mangle'] = driver.rows[MANGLE]
    report = build_report(desired, state)
    plan = build_plan(router, desired, state, report, {'status': 'disabled'}, driver)
    return desired, report, plan


def publish(driver, config, texts):
    desired, report, plan = preview(driver, config, texts)
    assert not plan['blockers'], plan['blockers']
    result = execute(plan, driver, None)
    assert result['ok'], result
    return desired, report, plan


@pytest.mark.parametrize('policy', ['DIRECT', 'REJECT', 'REJECT-DROP', 'PASS', 'COMPATIBLE', 'Only direct'])
def test_non_proxy_advisory_allows_selected_ips_to_publish_without_changing_source_scope(policy):
    driver, config, texts = MemoryROS(), configuration(policy), contents()
    if policy == 'Only direct':
        config['proxy_groups'] = [{'name': policy, 'manual_nodes': ['DIRECT', 'REJECT']}]
    before = copy.deepcopy(driver.rows)
    desired, report, plan = preview(driver, config, texts)
    assert driver.rows == before and not driver.writes  # Preview is read-only.
    assert not plan['blockers'], plan['blockers']
    assert {entry['cidr'] for entry in report['policy_routing']['items']} == {DIRECT_TARGET, TARGET}
    assert desired['ros_policy']['sources'] == ['192.168.0.101/32']
    warnings = report['policy_routing']['warnings']
    assert len(warnings) == 1 and policy in warnings[0]
    assert plan['warnings'].count(warnings[0]) == 1

    result = execute(plan, driver, None)
    assert result['ok'], result
    manifest = driver.ros_policy_manifest
    assert set(manifest['spec']['destinations']) == {DIRECT_TARGET, TARGET}
    assert manifest['spec']['sources'] == ['192.168.0.101/32']
    destination_lists = [row for row in driver.rows[ADDRESSES]
                         if row['list'].startswith('CF_Destination_Proxy_IP_')]
    assert {row['address'] for row in destination_lists} == {DIRECT_TARGET, TARGET}
    assert not any(row.get('dst-address') in {DIRECT_TARGET, TARGET} for row in driver.rows[ROUTES])
    jump = next(row for row in driver.rows[MANGLE] if row['action'] == 'jump')
    assert jump['src-address-list'] and jump['dst-address-list'] and jump['dst-address-type'] == '!local'
    assert driver.rows[ROUTES][-1]['gateway'] == GATEWAY + '@main'
    start = len(driver.writes)
    _, current_report, current_plan = preview(driver, config, texts)
    assert current_report['in_sync'] and current_plan['empty'] and not current_plan['blockers']
    assert len(driver.writes) == start
    assert current_plan['warnings'].count(warnings[0]) == 1


def test_advisory_remains_visible_when_withdrawing_policy_to_selected_static_routes():
    driver, config, texts = MemoryROS(), configuration(), contents()
    publish(driver, config, texts)
    _, report, plan = preview(driver, config, texts, selected=(), static=SELECTED)
    assert not plan['blockers'], plan['blockers']
    assert any('DIRECT' in warning for warning in plan['warnings'])
    assert not report['policy_routing']['enabled']
    result = execute(plan, driver, None)
    assert result['ok'], result
    assert driver.ros_policy_manifest is None
    assert not driver.rows[MANGLE] and not driver.rows[ADDRESSES]
    assert [row['name'] for row in driver.rows[TABLES]] == ['main']
    replacements = {row['dst-address']: row for row in driver.rows[ROUTES]
                    if row['dst-address'] in {DIRECT_TARGET, TARGET}}
    assert set(replacements) == {DIRECT_TARGET, TARGET}
    assert all(row['gateway'] == GATEWAY and row['active'] == 'true' for row in replacements.values())


def test_rule_name_does_not_determine_whether_a_warning_blocks_publish():
    driver, config, texts = MemoryROS(), configuration(), contents()
    config['rule_configs'][0]['name'] = '已停用，没有本地缓存，未计入'
    desired, _, _ = publish(driver, config, texts)
    assert not desired['ros_policy']['error']
    assert set(driver.ros_policy_manifest['spec']['destinations']) == {DIRECT_TARGET, TARGET}


@pytest.mark.parametrize('point', [('PUT', ADDRESSES, 2), ('PUT', TABLES, 1), ('PUT', MANGLE, 3)])
@pytest.mark.parametrize('committed', [False, True])
def test_mixed_selection_create_failures_still_restore_previous_router_configuration(point, committed):
    driver, config, texts = MemoryROS(), configuration(), contents()
    before = configurations(driver)
    setattr(driver, 'lost' if committed else 'fail', point)
    _, _, plan = preview(driver, config, texts)
    assert not plan['blockers'], plan['blockers']
    result = execute(plan, driver, None)
    assert not result['ok'], result
    assert not any(entry['status'] == 'failed' for entry in result['rollback']), result
    assert configurations(driver) == before
    assert not getattr(driver, 'ros_policy_manifest', None)


def test_mixed_selection_replacement_failure_restores_the_working_bundle():
    driver, config, texts = MemoryROS(), configuration(), contents()
    publish(driver, config, texts)
    before = configurations(driver)
    old_spec = copy.deepcopy(driver.ros_policy_manifest['spec'])
    texts['direct'] += 'IP-CIDR,8.8.8.0/24,no-resolve\n'
    driver.fail = ('DELETE', ADDRESSES, 1)
    _, _, plan = preview(driver, config, texts)
    assert not plan['blockers'], plan['blockers']
    result = execute(plan, driver, None)
    assert not result['ok'], result
    assert not any(entry['status'] == 'failed' for entry in result['rollback']), result
    assert configurations(driver) == before
    assert driver.ros_policy_manifest['spec'] == old_spec
    _, _, restored = preview(driver, config, contents())
    assert restored['empty'] and not restored['blockers']


def test_failed_readback_does_not_delete_a_new_route_edited_by_another_writer():
    driver, config, texts = MemoryROS(), configuration(), contents()
    original_request = driver._request
    edited = []

    def request(method, path, values=None):
        result = original_request(method, path, values)
        if method == 'PUT' and path == ROUTES and values.get('routing-table') != 'main':
            # WinBox changes the newly created route between its write and readback.
            row = next(row for row in driver.rows[ROUTES] if row['.id'] == result['.id'])
            row['comment'] = 'keep the operator edit'
            edited.append(copy.deepcopy(row))
        return result

    driver._request = request
    _, _, plan = preview(driver, config, texts)
    assert not plan['blockers'], plan['blockers']
    result = execute(plan, driver, None)
    assert not result['ok'], result
    assert len(edited) == 1
    assert next(row for row in driver.rows[ROUTES] if row['.id'] == edited[0]['.id']) == edited[0]
    assert not any(method == 'DELETE' and path == ROUTES + '/' + edited[0]['.id']
                   for method, path, _ in driver.writes)


@pytest.mark.parametrize('action', ['replace', 'withdraw', 'edit-after-preview'])
def test_mixed_selection_does_not_overwrite_external_edits(action):
    driver, config, texts = MemoryROS(), configuration(), contents()
    publish(driver, config, texts)
    texts['direct'] += 'IP-CIDR,8.8.8.0/24,no-resolve\n'
    selection = {'selected': (), 'static': SELECTED} if action == 'withdraw' else {}
    if action == 'edit-after-preview':
        _, _, plan = preview(driver, config, texts, **selection)
        assert not plan['blockers'], plan['blockers']
    driver.rows[MANGLE][-1]['comment'] = 'operator changed this rule in WinBox'
    before, start = copy.deepcopy(driver.rows), len(driver.writes)
    manifest = copy.deepcopy(driver.ros_policy_manifest)
    if action != 'edit-after-preview':
        _, _, plan = preview(driver, config, texts, **selection)
        assert plan['blockers']
    result = execute(plan, driver, None)
    assert not result['ok'], result
    assert driver.rows == before and len(driver.writes) == start
    assert driver.ros_policy_manifest == manifest


@pytest.mark.parametrize('problem', ['cache-missing', 'cache-read-error', 'disabled', 'deleted'])
def test_incomplete_selection_stays_blocked_and_does_not_withdraw_existing_bundle(problem):
    driver, config, texts = MemoryROS(), configuration('Proxy'), contents()
    publish(driver, config, texts)
    # The selected DIRECT rule is just an advisory; the other selected source is incomplete.
    config['rule_configs'][0]['policy'] = 'DIRECT'
    if problem == 'cache-missing':
        texts['proxy'] = ''
    elif problem == 'cache-read-error':
        texts['proxy'] = OSError('cache cannot be read')
    elif problem == 'disabled':
        config['rule_configs'][1]['enabled'] = False
    else:
        config['rule_configs'].pop()
    before, start = copy.deepcopy(driver.rows), len(driver.writes)
    manifest = copy.deepcopy(driver.ros_policy_manifest)
    desired, report, plan = preview(driver, config, texts)
    computation = compute_desired(config, GATEWAY, SELECTED, content_loader=loader(texts))
    assert len(computation['warnings']) == 2
    assert len(computation['blocking_warnings']) == 1
    blocker = computation['blocking_warnings'][0]
    advisory = next(warning for warning in computation['warnings'] if warning != blocker)
    assert blocker in desired['ros_policy']['error']
    assert advisory not in desired['ros_policy']['error']
    assert report['policy_routing']['warnings'] == [advisory]
    assert plan['warnings'].count(advisory) == 1
    assert blocker not in plan['warnings']
    assert plan['blockers'] and not any(
        op['kind'] == 'ros_policy_replace' for stage in plan['stages'] for op in stage['ops'])
    assert not execute(plan, driver, None)['ok']
    assert driver.rows == before and len(driver.writes) == start
    assert driver.ros_policy_manifest == manifest


@pytest.mark.parametrize('problem', ['none', 'cache-missing', 'disabled', 'deleted'])
def test_static_route_selection_retains_its_existing_warning_only_behavior(problem):
    driver, config, texts = MemoryROS(), configuration(), contents()
    if problem == 'cache-missing':
        texts['proxy'] = ''
    elif problem == 'disabled':
        config['rule_configs'][1]['enabled'] = False
    elif problem == 'deleted':
        config['rule_configs'].pop()
    desired, _, plan = preview(driver, config, texts, selected=(), static=SELECTED)
    assert desired['warnings'] and not plan['blockers'], plan['blockers']
    assert plan['warnings'] == desired['warnings']
    assert any(entry['cidr'] == DIRECT_TARGET for entry in desired['proxy_cidrs'])
    result = execute(plan, driver, None)
    assert result['ok'], result
    assert any(row['dst-address'] == DIRECT_TARGET and row['gateway'] == GATEWAY
               for row in driver.rows[ROUTES])
    assert not driver.rows[MANGLE]
