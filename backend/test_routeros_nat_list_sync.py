"""Auto-sync may change only destination members of the enrolled NAT list rule."""
import copy

import pytest

from backend.routers import routeros_nat_list as nat_list
from backend.test_router_rule_sync import NEW, environment, publish as sync_publish  # noqa: F401
from backend.test_ros_policy import ADDRESSES, NAT, ROUTES, configurations
from backend.test_routeros_nat_list import DST, FAKE_IP, SRC, legacy_for, members, rule_rows


def static_sync(environment):
    repo, driver = environment
    repo.update_profile_transaction('default', lambda doc: doc['routers']['items'][0].update(
        ros_policy_rule_sets=[], ip_rule_sets=['remote']))
    driver.rows[NAT][0].pop('out-interface')
    receipt = sync_publish(environment)
    assert members(driver, DST) == {FAKE_IP, '1.1.1.0/24'}
    return repo, driver, receipt


def test_auto_sync_writes_destination_members_after_routes_and_removes_them_before(environment):
    from backend.routers import rule_sync, rule_sync_store as store
    _, driver, receipt = static_sync(environment)
    enrolled = receipt['baseline']['nat_list']
    assert enrolled['rule']['config']['comment'] == nat_list.COMMENT and len(enrolled['src']) == 14
    rule = copy.deepcopy(rule_rows(driver))
    result = rule_sync.sync('default', 'ros', lambda *_: NEW)
    assert result['status'] == 'synced', result
    assert (result['added'], result['removed']) == (2, 2)
    steps = [(method, path.split('/*')[0], (values or {}).get('dst-address') or (values or {}).get('address'))
             for method, path, values in driver.writes]
    route_add = steps.index(('PUT', ROUTES, '8.8.8.0/24'))
    member_add = steps.index(('PUT', ADDRESSES, '8.8.8.0/24'))
    member_remove = steps.index(('DELETE', ADDRESSES, None))
    route_delete = steps.index(('DELETE', ROUTES, None))
    assert route_add < member_add < member_remove < route_delete
    assert not any(path.startswith(NAT) for _, path, _ in driver.writes)
    assert rule_rows(driver) == rule and members(driver, DST) == {FAKE_IP, '8.8.8.0/24'}
    assert store.read('default', 'ros')['baseline']['nat_list'] == enrolled


@pytest.mark.parametrize('change', ['old-baseline', 'source-member', 'rule-egress', 'legacy'])
def test_auto_sync_leaves_every_other_nat_change_to_a_manual_release(environment, change):
    from backend.routers import rule_sync, rule_sync_store as store
    _, driver, receipt = static_sync(environment)
    if change == 'old-baseline':
        receipt['baseline'].pop('nat_list')
        store.write('default', 'ros', receipt)
    elif change == 'source-member':
        driver.rows[ADDRESSES].remove(next(row for row in driver.rows[ADDRESSES] if row['list'] == SRC))
    elif change == 'rule-egress':
        rule_rows(driver)[0]['out-interface'] = 'bridge'
    else:
        driver.rows[NAT][1:1] = legacy_for(['8.8.8.0/24'])
    before = configurations(driver)
    result = rule_sync.sync('default', 'ros', lambda *_: NEW)
    assert result['status'] == 'paused', result
    assert '主路由 NAT 免转换' in result['message'] or '手动预览' in result['message'], result
    assert not driver.writes and configurations(driver) == before


def test_auto_sync_failure_restores_routes_and_members_and_pauses(environment):
    from backend.routers import rule_sync
    _, driver, _ = static_sync(environment)
    before = configurations(driver)
    driver.fail = ('DELETE', ROUTES, driver.counts['DELETE', ROUTES] + 1)
    result = rule_sync.sync('default', 'ros', lambda *_: NEW)
    assert result['status'] == 'paused', result
    # A recreated member gets a new ID at the end; list members have no order.
    after = configurations(driver)
    key = lambda row: sorted(row.items())  # noqa: E731
    assert sorted(after.pop(ADDRESSES), key=key) == sorted(before.pop(ADDRESSES), key=key)
    assert after == before


@pytest.mark.parametrize('when', ['before-preview', 'after-preview'])
def test_auto_sync_leaves_lists_another_firewall_rule_selects(environment, when):
    from backend.routers import rule_sync
    from backend.test_ros_policy import FILTER
    _, driver, _ = static_sync(environment)
    reference = {'.id': '*F1', 'chain': 'forward', 'action': 'drop', 'dst-address-list': DST, 'disabled': 'false'}
    if when == 'before-preview':
        driver.rows[FILTER].append(reference)
    else:
        read_state = driver.read_state

        def read_then_reference():
            state = read_state()
            if reference not in driver.rows[FILTER]:
                driver.rows[FILTER].append(copy.deepcopy(reference))
            return state
        driver.read_state = read_then_reference
    before = configurations(driver)
    result = rule_sync.sync('default', 'ros', lambda *_: NEW)
    assert result['status'] == 'paused', result
    assert f'{FILTER}/*F1' in result['message'] and DST in result['message'], result
    after = configurations(driver)
    assert after.pop(FILTER) == [{key: value for key, value in reference.items() if key != '.id'}]
    before.pop(FILTER)
    assert not driver.writes and after == before


def legacy_sync(environment):
    """A device enrolled for auto-sync while an earlier release kept per-destination rules."""
    repo, driver = environment
    repo.update_profile_transaction('default', lambda doc: doc['routers']['items'][0].update(
        ros_policy_rule_sets=[], ip_rule_sets=['remote']))
    # No exemption needed at enrollment (WAN-limited masquerade): the baseline has no list rule.
    receipt = sync_publish(environment)
    assert receipt['baseline']['nat_list'] is None and not rule_rows(driver)
    driver.rows[NAT][0].pop('out-interface')
    driver.rows[NAT][0:0] = legacy_for([FAKE_IP, '1.1.1.0/24'])
    return repo, driver


@pytest.mark.parametrize('content', ['unchanged', 'changed'])
def test_auto_sync_never_migrates_old_rules_and_pauses_without_writing(environment, content):
    from backend.routers import rule_sync
    from backend.test_router_rule_sync import OLD
    _, driver = legacy_sync(environment)
    before = configurations(driver)
    result = rule_sync.sync('default', 'ros', lambda *_: OLD if content == 'unchanged' else NEW)
    assert result['status'] == 'paused', result
    # A CIDR the update drops leaves its old rules stale; they are not removed either.
    detail = '28 条待合并为 1 条列表规则' if content == 'unchanged' else '14 条待合并为 1 条列表规则，14 条的目的网段已不再代理'
    assert result['message'] == (f'路由器上还有旧的逐条 NAT 免转换规则（{detail}），'
                                 '自动同步不会迁移，请手动预览并发布一次完成迁移'), result
    assert not driver.writes and configurations(driver) == before


def test_auto_sync_never_removes_stale_old_rules(environment):
    from backend.routers import rule_sync
    _, driver, _ = static_sync(environment)
    driver.rows[NAT][1:1] = legacy_for(['9.9.9.0/24'])
    before = configurations(driver)
    result = rule_sync.sync('default', 'ros', lambda *_: NEW)
    assert result['status'] == 'paused', result
    assert '（14 条的目的网段已不再代理）' in result['message'] and '请手动预览并发布一次完成迁移' in result['message']
    assert not driver.writes and configurations(driver) == before
