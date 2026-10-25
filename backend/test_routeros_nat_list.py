"""RouterOS NAT bypass as one rule over ConfigFlow's two address lists."""
import copy
import ipaddress

import pytest

from backend.routers import ros_policy_write, routeros_nat_list as nat_list
from backend.routers.base import RouterError
from backend.routers.plan import build_plan
from backend.routers.release import execute
from backend.routers.report import build_report
from backend.routers.routeros import UncertainWrite
from backend.routers.routeros_nat import client_source_networks
from backend.test_nat_list_unchanged import legacy_rules
from backend.test_ros_policy import (
    ADDRESSES, GATEWAY, MANGLE, NAT, ROUTER, ROUTES, TARGET, MemoryROS, configurations, desired_for, inspect, release,
)

MASQUERADE = {'.id': '*3', 'chain': 'srcnat', 'action': 'masquerade', 'disabled': 'false'}
FAKE_IP = '198.18.0.0/16'
SRC, DST = nat_list.SRC_LIST, nat_list.DST_LIST


@pytest.fixture(autouse=True)
def no_waits(monkeypatch):
    import time
    monkeypatch.setattr(time, 'sleep', lambda seconds: None)


def device(nats=None):
    driver = MemoryROS()
    driver.rows[NAT] = [copy.deepcopy(MASQUERADE)] if nats is None else copy.deepcopy(nats)
    return driver


def desired(cidrs, gateway=GATEWAY):
    return {'fake_ip_range': FAKE_IP, 'proxy_gateway': gateway, 'warnings': [],
            'proxy_cidrs': [{'cidr': cidr, 'sources': ['test']} for cidr in cidrs]}


def preview(driver, cidrs, gateway=GATEWAY):
    state = driver.read_state()
    want = desired(cidrs, gateway)
    report = build_report(want, state)
    return report, build_plan(ROUTER, want, state, report, {'status': 'disabled'}, driver)


def publish(driver, cidrs, gateway=GATEWAY):
    report, plan = preview(driver, cidrs, gateway)
    assert not plan['blockers'], plan['blockers']
    result = execute(plan, driver, None)
    assert result['ok'], result
    return plan


def nat_ops(plan):
    return next(stage['ops'] for stage in plan['stages'] if stage['key'] == 'nat_bypass')


def list_op(plan):
    return next(op for op in nat_ops(plan) if op['kind'] == 'nat_list_sync')


def members(driver, name, comment=nat_list.COMMENT):
    return {str(ipaddress.ip_network(row['address'])) for row in driver.rows[ADDRESSES]
            if row['list'] == name and (comment is None or row.get('comment') == comment)}


def sources(gateway=GATEWAY):
    return {str(network) for network in client_source_networks(gateway)}


def rule_rows(driver):
    return [row for row in driver.rows[NAT] if row.get('comment') == nat_list.COMMENT]


def cidrs(count, start=0):
    return [f'{45 + (index >> 16)}.{(index >> 8) & 255}.{index & 255}.0/24' for index in range(start, start + count)]


def legacy_for(targets, gateway=GATEWAY):
    rows, number = [], 0x1000
    for cidr in targets:
        for source in client_source_networks(gateway):
            number += 1
            rows.append({'.id': f'*{number:X}', 'chain': 'srcnat', 'action': 'accept', 'disabled': 'false',
                         'dst-address': cidr, 'comment': 'cf-mihomo', 'src-address': str(source)})
    return rows


def recorded(driver):
    """Every request the driver sends, reads included."""
    driver.sent = []
    send = driver._request

    def request(method, path, values=None, **kwargs):
        driver.sent.append((method, path, values))
        return send(method, path, values, **kwargs)
    driver._request = request
    return driver


# --- creation and member updates ---------------------------------------------

def test_a_needed_exemption_is_one_rule_over_two_lists_first_in_the_table():
    driver = device()
    plan = publish(driver, [FAKE_IP, TARGET])
    op = list_op(plan)
    assert op['summary'].startswith('新增 NAT 免转换规则「cf-mihomo-nat」：源地址列表 cf-mihomo-nat-src'
                                    '（192.168.0.3 所在 /24 除 192.168.0.3 外，14 段）→ 目的地址列表 cf-mihomo-nat-dst（2 个网段）')
    [rule] = rule_rows(driver)
    assert driver.rows[NAT].index(rule) == 0
    assert {key: value for key, value in rule.items() if key != '.id'} == {
        'chain': 'srcnat', 'action': 'accept', 'src-address-list': SRC, 'dst-address-list': DST,
        'comment': 'cf-mihomo-nat', 'disabled': 'false'}
    assert members(driver, SRC) == sources() and members(driver, DST) == {FAKE_IP, TARGET}
    assert len(driver.rows[NAT]) == 2
    report, again = preview(driver, [FAKE_IP, TARGET])
    assert report['in_sync'] and again['empty'] and not again['blockers']
    assert {item['status'] for item in report['nat_bypass']['items']} == {'synced'}


@pytest.mark.parametrize(('gateway', 'count'), [('192.168.0.2', 14), ('192.168.0.3', 14), ('192.168.0.254', 13),
                                                ('192.168.0.85', 19), ('192.168.0.100', 19), ('192.168.0.171', 19)])
def test_source_list_is_the_assessed_client_range(gateway, count):
    driver = device()
    driver.rows[ROUTES] = [driver.rows[ROUTES][0]]
    _, plan = preview(driver, [TARGET], gateway)
    op = list_op(plan)
    assert len(op['src_add']) == count == len(client_source_networks(gateway))
    assert set(op['src_add']) == sources(gateway) and gateway + '/32' not in op['src_add']
    assert op['sources'] == plan['checks']['nat_list']['sources']


def test_destination_changes_write_members_only():
    driver = device()
    publish(driver, [FAKE_IP, TARGET])
    rule, nats = copy.deepcopy(rule_rows(driver)), copy.deepcopy(driver.rows[NAT])
    added = '149.154.160.0/20'
    _, plan = preview(driver, [FAKE_IP, added])
    op = list_op(plan)
    assert op['dst_add'] == [added] and [item['config']['address'] for item in op['dst_remove']] == [TARGET]
    assert op['summary'] == ('NAT 免转换目的列表 cf-mihomo-nat-dst：新增 1 个网段（149.154.160.0/20）、'
                             '移除 1 个不再代理的网段（91.108.4.0/22）')
    driver.writes.clear()
    assert execute(plan, driver, None)['ok']
    assert driver.rows[NAT] == nats and rule_rows(driver) == rule
    assert not any(path.startswith(NAT) for _, path, _ in driver.writes)
    assert members(driver, DST) == {FAKE_IP, added}
    assert preview(driver, [FAKE_IP, added])[1]['empty']


def test_summary_lists_five_destinations_then_the_count():
    driver = device()
    publish(driver, [FAKE_IP])
    _, plan = preview(driver, [FAKE_IP] + cidrs(7))
    assert list_op(plan)['summary'] == ('NAT 免转换目的列表 cf-mihomo-nat-dst：新增 7 个网段（'
                                        + '、'.join(cidrs(5)) + ' 等 7 个）')


def test_not_required_destinations_keep_their_members_and_all_not_required_withdraws():
    driver = device()
    publish(driver, [FAKE_IP, TARGET])
    driver.rows[NAT][-1]['out-interface'] = 'pppoe-out1'
    report, plan = preview(driver, [FAKE_IP, TARGET])
    assert {item['status'] for item in report['nat_bypass']['items']} == {'not_required'}
    op = list_op(plan)
    assert op['withdraw'] and op['summary'] == '删除 NAT 免转换规则「cf-mihomo-nat」及其地址列表：已没有需要免转换的代理网段'
    assert execute(plan, driver, None)['ok']
    assert not rule_rows(driver) and not members(driver, SRC) and not members(driver, DST)
    assert preview(driver, [FAKE_IP, TARGET])[1]['empty']


def test_publishing_200_destinations_writes_one_rule_and_never_reads_a_whole_list():
    driver = recorded(device())
    targets = cidrs(200)
    publish(driver, targets)
    puts = [path for method, path, _ in driver.sent if method == 'PUT' and path.startswith(NAT)]
    assert len(puts) == 1
    assert not [path for method, path, _ in driver.sent if method == 'GET' and path == ADDRESSES]
    member_scripts = [values for method, path, values in driver.sent
                      if path == '/execute' and '/ip/firewall/address-list/add' in values['script']]
    assert len(member_scripts) == 2  # sources, then 200 destinations in one batch
    driver.sent.clear()
    publish(driver, targets + cidrs(200, 200))
    assert not [path for method, path, _ in driver.sent if path.startswith(NAT) and method != 'GET']
    assert not [path for method, path, _ in driver.sent if method == 'GET' and path == ADDRESSES]
    assert len(driver.rows[NAT]) == 2 and len(members(driver, DST)) == 400


# --- migration of per-destination rules ------------------------------------

# Previewing S×N old rules is itself quadratic (the cost this migration ends):
# 40 destinations (560 rules, two remove batches) keep the test fast.
@pytest.mark.parametrize('count', [2, 20, 40])
def test_old_per_destination_rules_move_into_the_lists(count):
    targets = cidrs(count)
    driver = recorded(device(legacy_for(targets) + [MASQUERADE]))
    _, plan = preview(driver, targets + ['149.154.160.0/20'])
    op = list_op(plan)
    assert len(op['legacy']) == 14 * count
    assert f'迁移 {14 * count} 条旧的逐条免转换规则（备注 cf-mihomo）' in op['summary']
    driver.sent.clear()
    assert execute(plan, driver, None)['ok']
    assert [row.get('comment') for row in driver.rows[NAT]] == ['cf-mihomo-nat', None]
    assert members(driver, DST) == set(targets) | {'149.154.160.0/20'} and members(driver, SRC) == sources()
    removes = [values for method, path, values in driver.sent if path == NAT + '/remove']
    assert len(removes) == -(-14 * count // 500)
    assert len([1 for method, path, _ in driver.sent if method == 'PUT' and path == NAT]) == 1
    assert preview(driver, targets + ['149.154.160.0/20'])[1]['empty']


def test_migration_keeps_every_client_protected_at_every_write():
    targets = [FAKE_IP, TARGET]
    driver = device(legacy_rules() + [MASQUERADE])
    _, plan = preview(driver, targets + ['149.154.160.0/20'])
    send = driver._request
    checked = []

    def protected(source, destination):
        source, destination = ipaddress.ip_address(source), ipaddress.ip_address(destination)
        def listed(name, address):
            return any(address in ipaddress.ip_network(row['address']) for row in driver.rows[ADDRESSES]
                       if row['list'] == name)
        for row in driver.rows[NAT]:
            if row.get('src-address') and source not in ipaddress.ip_network(row['src-address']):
                continue
            if row.get('dst-address') and destination not in ipaddress.ip_network(row['dst-address']):
                continue
            if row.get('src-address-list') and not listed(row['src-address-list'], source):
                continue
            if row.get('dst-address-list') and not listed(row['dst-address-list'], destination):
                continue
            return row['action'] == 'accept'
        return False

    def request(method, path, values=None, **kwargs):
        result = send(method, path, values, **kwargs)
        if method != 'GET':
            checked.append(path)
            for client in ('192.168.0.1', '192.168.0.2', '192.168.0.4', '192.168.0.200', '192.168.0.254'):
                for target in ('198.18.0.1', '91.108.4.1'):
                    assert protected(client, target), (method, path, client, target)
        return result
    driver._request = request
    assert execute(plan, driver, None)['ok']
    assert checked


@pytest.mark.parametrize('variant', ['hand-made', 'other-matcher', 'incomplete', 'not-desired', 'old-gateway'])
def test_rules_that_are_not_the_old_writer_shape_are_never_deleted_or_widened(variant):
    rows = legacy_for([TARGET])
    if variant == 'hand-made':
        for row in rows:
            row['comment'] = 'cf-tg'
    elif variant == 'other-matcher':
        rows[0]['protocol'] = 'tcp'
    elif variant == 'incomplete':
        rows = rows[1:]
    elif variant == 'not-desired':
        rows = legacy_for(['203.0.113.0/24'])
    else:
        rows = legacy_for([TARGET], '192.168.0.5')
    driver = device(rows + [MASQUERADE])
    before = copy.deepcopy(rows)
    report, plan = preview(driver, [TARGET, '149.154.160.0/20'])
    if plan['blockers']:
        # A partial or restricted exemption of a desired CIDR stays unknown.
        assert variant in ('other-matcher', 'incomplete', 'old-gateway')
        assert not execute(plan, driver, None)['ok']
    else:
        assert not list_op(plan)['legacy']
        assert execute(plan, driver, None)['ok']
    assert [row for row in driver.rows[NAT] if row.get('comment') != nat_list.COMMENT][:-1] == before
    assert bool(rule_rows(driver)) == (variant in ('hand-made', 'not-desired'))


def test_adopted_hand_made_rule_is_only_recommented():
    manual = {'.id': '*A', 'chain': 'srcnat', 'action': 'accept', 'comment': 'tg', 'src-address': '192.168.0.0/24',
              'dst-address': TARGET, 'disabled': 'false'}
    driver = device([manual, MASQUERADE])
    _, plan = preview(driver, [TARGET, '149.154.160.0/20'])
    adopt, op = nat_ops(plan)
    assert adopt['kind'] == 'nat_update' and adopt['add_dst'] == [] and adopt['comment'] == 'cf-tg'
    assert op['dst_add'] == ['149.154.160.0/20']
    assert execute(plan, driver, None)['ok']
    assert next(row for row in driver.rows[NAT] if row['.id'] == '*A') == {**manual, 'comment': 'cf-tg'}
    assert sum(row.get('dst-address') == TARGET for row in driver.rows[NAT]) == 1


# --- rollback ----------------------------------------------------------------

def migrating_device():
    return device(legacy_rules() + [MASQUERADE])


TARGETS = [FAKE_IP, TARGET, '149.154.160.0/20']


def fail_after(driver, method, path, after=0, error=None):
    """Raise once on the matching request, after it ran when ``error`` says so."""
    send = driver._request
    state = {'seen': 0}

    def request(verb, target, values=None, **kwargs):
        match = verb == method and (target == path or target.startswith(path + '/'))
        if match:
            state['seen'] += 1
        if match and state['seen'] == after + 1:
            result = send(verb, target, values, **kwargs)
            raise (error or RouterError)('injected failure')
        return send(verb, target, values, **kwargs)
    driver._request = request


@pytest.mark.parametrize('inject', [
    'source-batch', 'destination-batch', 'destination-lost', 'rule-put', 'rule-lost', 'remove-second',
    'remove-lost-then-readback', 'readback', 'invalid-rule'])
def test_every_failure_restores_the_exact_table_and_lists(inject, monkeypatch):
    driver = migrating_device()
    _, plan = preview(driver, TARGETS)
    before = configurations(driver)
    if inject == 'source-batch':
        driver.fail = ('PUT', ADDRESSES, 3)
    elif inject == 'destination-batch':
        driver.fail = ('PUT', ADDRESSES, 14 + 1)
    elif inject == 'destination-lost':
        driver.lost = ('PUT', ADDRESSES, 14 + 1)
    elif inject == 'rule-put':
        driver.fail = ('PUT', NAT, 1)
    elif inject == 'rule-lost':
        driver.lost = ('PUT', NAT, 1)
    elif inject == 'remove-second':
        driver.fail = ('DELETE', NAT, 2)
    elif inject == 'invalid-rule':
        read = ros_policy_write._read
        monkeypatch.setattr(ros_policy_write, '_read', lambda drv, path, row_id: (
            {**row, 'invalid': 'true'} if (row := read(drv, path, row_id)) and row.get('comment') == nat_list.COMMENT
            else row))
    else:
        if inject == 'remove-lost-then-readback':
            fail_after(driver, 'POST', NAT + '/remove', error=UncertainWrite)
        monkeypatch.setattr(nat_list, 'verify', lambda check, state: (_ for _ in ()).throw(
            RouterError('injected readback failure')))
    result = execute(plan, driver, None)
    assert not result['ok'] and result['failed_stage'] == 'nat_bypass', result
    assert not any(item['status'] == 'failed' for item in result['rollback']), result['rollback']
    after = configurations(driver)
    assert after[NAT] == before[NAT]
    assert after[ADDRESSES] == before[ADDRESSES]
    assert configurations(driver) == before


def test_coverage_proof_runs_before_old_rules_go(monkeypatch):
    driver = migrating_device()
    _, plan = preview(driver, TARGETS)
    before = configurations(driver)
    send = driver._request

    def request(method, path, values=None, **kwargs):
        result = send(method, path, values, **kwargs)
        if method == 'PUT' and path == NAT:
            # Another program puts a masquerade ahead of the new rule.
            driver.rows[NAT].insert(0, {'.id': '*EVIL', 'chain': 'srcnat', 'action': 'masquerade',
                                        'disabled': 'false'})
        return result
    driver._request = request
    result = execute(plan, driver, None)
    assert not result['ok'] and '无法证明覆盖' in result['log'][-1]['summary']
    assert not any(item['status'] == 'failed' for item in result['rollback'])
    assert driver.rows[NAT][0]['.id'] == '*EVIL'
    driver.rows[NAT].pop(0)
    assert configurations(driver) == before


def test_lost_rule_reply_with_two_matching_rules_keeps_everything_for_inspection():
    driver = migrating_device()
    _, plan = preview(driver, TARGETS)
    send = driver._request

    def request(method, path, values=None, **kwargs):
        result = send(method, path, values, **kwargs)
        if method == 'PUT' and path == NAT:
            twin = {key: value for key, value in result.items() if key != '.id'}
            driver.rows[NAT].insert(0, {'.id': '*TWIN', **twin})
            raise UncertainWrite('response lost after commit')
        return result
    driver._request = request
    result = execute(plan, driver, None)
    assert not result['ok']
    assert any(item['status'] == 'failed' for item in result['rollback'])
    assert len(rule_rows(driver)) == 2
    assert len([row for row in driver.rows[NAT] if row.get('comment') == 'cf-mihomo']) == 28


def test_hundreds_of_restored_rules_keep_their_order_in_console_batches(monkeypatch):
    targets = cidrs(40)
    driver = recorded(device(legacy_for(targets) + [MASQUERADE, {'.id': '*Z', 'chain': 'dstnat', 'action': 'accept'}]))
    _, plan = preview(driver, targets + ['149.154.160.0/20'])
    before = configurations(driver)
    monkeypatch.setattr(nat_list, 'verify', lambda check, state: (_ for _ in ()).throw(RouterError('injected')))
    driver.sent.clear()
    result = execute(plan, driver, None)
    assert not result['ok'] and not any(item['status'] == 'failed' for item in result['rollback'])
    assert configurations(driver) == before
    scripts = [values for method, path, values in driver.sent
               if path == '/execute' and '/ip/firewall/nat/add' in values['script']]
    assert len(scripts) == 6  # 560 rules, 100 per console batch


def test_restore_refuses_when_an_anchor_rule_disappeared(monkeypatch):
    driver = migrating_device()
    _, plan = preview(driver, TARGETS)

    def broken(check, state):
        driver.rows[NAT] = [row for row in driver.rows[NAT] if row['.id'] != '*3']
        raise RouterError('injected')
    monkeypatch.setattr(nat_list, 'verify', broken)
    result = execute(plan, driver, None)
    assert not result['ok']
    assert any(item['status'] == 'failed' and '原规则位置已改变' in item['summary'] for item in result['rollback'])
    assert not any(row.get('comment') == 'cf-mihomo' for row in driver.rows[NAT])  # kept for inspection


def test_stale_preview_is_refused_before_any_write():
    driver = migrating_device()
    _, plan = preview(driver, TARGETS)
    op = list_op(plan)
    driver.rows[NAT][3]['comment'] = 'cf-changed'
    assert preview(driver, TARGETS)[1]['fingerprint'] != plan['fingerprint']
    driver.writes.clear()
    with pytest.raises(RouterError, match='预览后发生变化'):
        driver.apply(op)
    assert not driver.writes


@pytest.mark.parametrize('change', ['member', 'foreign-member', 'rule'])
def test_list_and_rule_changes_invalidate_the_preview(change):
    driver = device()
    publish(driver, [FAKE_IP])
    _, plan = preview(driver, [FAKE_IP, TARGET])
    if change == 'member':
        next(row for row in driver.rows[ADDRESSES] if row['list'] == SRC)['address'] = '192.168.0.9'
    elif change == 'foreign-member':
        driver.rows[ADDRESSES].append({'.id': '*F', 'list': DST, 'address': '10.9.0.0/16', 'disabled': 'false'})
    else:
        rule_rows(driver)[0]['out-interface'] = 'bridge'
    assert preview(driver, [FAKE_IP, TARGET])[1]['fingerprint'] != plan['fingerprint']


# --- ownership -----------------------------------------------------------------

@pytest.mark.parametrize(('change', 'message'), [
    ({'protocol': 'tcp'}, '被修改（protocol）'),
    ({'src-address': '192.168.0.0/24'}, '被修改（src-address）'),
    ({'log': 'true'}, '被修改（log）'),
    ({'out-interface': '!bridge'}, '被修改（out-interface）'),
    ({'disabled': 'true'}, '已停用'),
    ({'invalid': 'true'}, '动态或无效'),
    ('after-masquerade', '位于源 NAT 转换规则 *3 之后'),
    ('after-jump', '位于源 NAT 转换规则 *J 之后'),
    ('duplicate', '发现多条'),
    ('nat-reference', '其他规则 /ip/firewall/nat/*R 正在引用'),
    ('mangle-reference', '其他规则 /ip/firewall/mangle/*M 正在引用'),
])
def test_a_changed_or_shadowed_rule_blocks_without_writes(change, message):
    driver = device()
    publish(driver, [FAKE_IP])
    rule = rule_rows(driver)[0]
    if change == 'after-masquerade':
        driver.rows[NAT].remove(rule)
        driver.rows[NAT].append(rule)
    elif change == 'after-jump':
        driver.rows[NAT].insert(0, {'.id': '*J', 'chain': 'srcnat', 'action': 'jump', 'jump-target': 'x',
                                    'disabled': 'false'})
    elif change == 'duplicate':
        driver.rows[NAT].append({**rule, '.id': '*D'})
    elif change == 'nat-reference':
        driver.rows[NAT].append({'.id': '*R', 'chain': 'dstnat', 'action': 'accept', 'src-address-list': '!' + SRC})
    elif change == 'mangle-reference':
        driver.rows[MANGLE].append({'.id': '*M', 'chain': 'forward', 'action': 'accept', 'dst-address-list': DST})
    else:
        rule.update(change)
    before = configurations(driver)
    driver.writes.clear()
    _, plan = preview(driver, [FAKE_IP, TARGET])
    assert any(message in item for item in plan['blockers']), plan['blockers']
    assert not execute(plan, driver, None)['ok']
    assert configurations(driver) == before
    assert not any(path.startswith((NAT, ADDRESSES)) for _, path, _ in driver.writes)


def test_foreign_members_are_kept_and_warned_about():
    driver = device()
    publish(driver, [FAKE_IP, TARGET])
    foreign = {'.id': '*F', 'list': DST, 'address': '149.154.160.0/20', 'disabled': 'false', 'comment': 'mine'}
    driver.rows[ADDRESSES].append(copy.deepcopy(foreign))
    _, plan = preview(driver, [FAKE_IP])
    assert '地址列表 cf-mihomo-nat-dst 中有 1 条不是 ConfigFlow 写入的条目，保留不动' in plan['warnings']
    assert execute(plan, driver, None)['ok']
    assert foreign in driver.rows[ADDRESSES] and members(driver, DST) == {FAKE_IP}
    # A desired CIDR a foreign member already holds is not written twice.
    _, plan = preview(driver, [FAKE_IP, '149.154.160.0/20'])
    assert nat_ops(plan) == [] and plan['checks']['nat_list']['destinations'] == [FAKE_IP, '149.154.160.0/20']


@pytest.mark.parametrize(('member', 'message'), [
    ({'comment': 'mine'}, '已存在非 ConfigFlow 条目'),
    ({'comment': nat_list.COMMENT, 'dynamic': 'true'}, '含动态条目'),
    ({'comment': nat_list.COMMENT, 'disabled': 'true'}, '含停用条目'),
])
def test_unusable_lists_block_creating_the_rule(member, message):
    driver = device()
    driver.rows[ADDRESSES].append({'.id': '*F', 'list': DST, 'address': '10.9.0.0/16', 'disabled': 'false', **member})
    before = configurations(driver)
    _, plan = preview(driver, [FAKE_IP])
    assert any(message in item for item in plan['blockers']), plan['blockers']
    assert not execute(plan, driver, None)['ok'] and configurations(driver) == before


def test_members_left_by_a_deleted_rule_are_reused():
    driver = device()
    publish(driver, [FAKE_IP, TARGET])
    driver.rows[NAT] = [row for row in driver.rows[NAT] if row.get('comment') != nat_list.COMMENT]
    kept = copy.deepcopy(driver.rows[ADDRESSES])
    _, plan = preview(driver, [FAKE_IP])
    op = list_op(plan)
    assert op['rule'] is None and op['src_add'] == [] and op['dst_add'] == []
    assert [item['config']['address'] for item in op['dst_remove']] == [TARGET]
    assert execute(plan, driver, None)['ok']
    assert [row for row in driver.rows[ADDRESSES] if row['address'] != TARGET] == [
        row for row in kept if row['address'] != TARGET]
    assert len(rule_rows(driver)) == 1


def test_gateway_move_rewrites_only_the_source_list():
    driver = device()
    driver.rows[ROUTES] = [driver.rows[ROUTES][0]]
    publish(driver, [TARGET])
    # The user removed the routes to the old gateway; the NAT list still names it.
    driver.rows[ROUTES] = [driver.rows[ROUTES][0]]
    report, plan = preview(driver, [TARGET], '192.168.0.5')
    assert report['nat_bypass']['items'][0]['status'] == 'missing'
    op = list_op(plan)
    assert op['dst_add'] == [] and op['dst_remove'] == [] and op['rule'] is not None
    assert set(op['src_add']) == sources('192.168.0.5') - sources()
    assert {item['config']['address'] for item in op['src_remove']} == {
        str(ipaddress.ip_network(value).network_address) if value.endswith('/32') else value
        for value in sources() - sources('192.168.0.5')}
    assert '源列表随网关 192.168.0.5 更新' in op['summary']
    assert execute(plan, driver, None)['ok']
    assert members(driver, SRC) == sources('192.168.0.5')
    report, again = preview(driver, [TARGET], '192.168.0.5')
    assert report['nat_bypass']['items'][0]['status'] == 'synced' and again['empty']


def test_gateway_move_without_the_list_rule_stays_unknown():
    driver = device(legacy_for([TARGET]) + [MASQUERADE])
    driver.rows[ROUTES] = [driver.rows[ROUTES][0]]
    report, plan = preview(driver, [TARGET], '192.168.0.5')
    assert report['nat_bypass']['items'][0]['status'] == 'unknown' and plan['blockers']


# --- source policy -------------------------------------------------------------

def policy_device():
    driver = MemoryROS()
    driver.rows[NAT][0].pop('out-interface')
    return driver


def test_withdrawing_policy_narrows_the_list_rule_to_the_lan_egress():
    driver = policy_device()
    release(driver)
    [old] = rule_rows(driver)
    assert 'out-interface' not in old
    _, plan = inspect(driver, desired_for(driver, selected=False))
    op = next(op for stage in plan['stages'] for op in stage['ops'] for op in op.get('prepare_static', [op])
              if op['kind'] == 'nat_list_sync')
    assert op['replace_rule'] and op['out_interface'] == 'bridge' and op['dst_add'] == [TARGET]
    assert '限定出口 bridge' in op['summary']
    assert execute(plan, driver, None)['ok']
    [rule] = rule_rows(driver)
    assert rule['.id'] != old['.id'] and rule['out-interface'] == 'bridge'
    assert members(driver, DST) == {FAKE_IP, TARGET}
    assert inspect(driver, desired_for(driver, selected=False))[1]['empty']


def test_policy_round_trip_keeps_the_narrowed_rule_and_converges():
    driver = policy_device()
    release(driver)
    assert execute(inspect(driver, desired_for(driver, selected=False))[1], driver, None)['ok']
    [narrowed] = rule_rows(driver)
    _, plan = release(driver)
    assert rule_rows(driver) == [narrowed] and members(driver, DST) == {FAKE_IP}
    driver.writes.clear()
    report, plan = inspect(driver, desired_for(driver, selected=False))
    assert not plan['blockers'], plan['blockers']
    assert execute(plan, driver, None)['ok']
    # Same egress: only the member is written, the rule stays.
    assert rule_rows(driver) == [narrowed]
    assert not any(path.startswith(NAT) and method == 'PUT' and (values or {}).get('comment') == nat_list.COMMENT
                   for method, path, values in driver.writes)
    report, plan = inspect(driver, desired_for(driver, selected=False))
    assert plan['empty'] and report['in_sync']


def test_withdrawing_policy_with_a_rule_on_another_egress_blocks():
    driver = policy_device()
    release(driver)
    rule_rows(driver)[0]['out-interface'] = 'ether9'
    before = configurations(driver)
    _, plan = inspect(driver, desired_for(driver, selected=False))
    assert plan['blockers']
    assert not execute(plan, driver, None)['ok'] and configurations(driver) == before


@pytest.mark.parametrize('point', [('PUT', NAT, 1), ('DELETE', NAT, 1), ('PUT', ADDRESSES, 1), ('PUT', ROUTES, 1),
                                   ('DELETE', NAT, 2)])
def test_failed_withdrawal_restores_the_original_rule_and_policy(point):
    driver = policy_device()
    release(driver)
    before = configurations(driver)
    manifest = copy.deepcopy(driver.ros_policy_manifest)
    driver.fail = (point[0], point[1], driver.counts[point[0], point[1]] + point[2])
    _, plan = inspect(driver, desired_for(driver, selected=False))
    result = execute(plan, driver, None)
    assert not result['ok']
    assert not any(item['status'] == 'failed' for item in result['rollback']), result
    # Policy objects deleted before the failure come back under new IDs.
    assert configurations(driver) == before and driver.ros_policy_manifest['spec'] == manifest['spec']


# --- verification ----------------------------------------------------------------

def test_readback_rejects_members_that_differ_from_the_plan():
    from backend.routers.release import verify_nat
    driver = device()
    plan = publish(driver, [FAKE_IP, TARGET])
    verify_nat(plan['checks'], driver.read_state())
    driver.rows[ADDRESSES].append({'.id': '*X', 'list': DST, 'address': '10.0.0.0/8', 'comment': nat_list.COMMENT,
                                   'disabled': 'false'})
    with pytest.raises(RouterError, match='ConfigFlow 条目与计划不一致'):
        verify_nat(plan['checks'], driver.read_state())


def test_withdrawal_readback_needs_rule_and_members_gone():
    driver = device()
    publish(driver, [FAKE_IP])
    driver.rows[NAT][-1]['out-interface'] = 'pppoe-out1'
    _, plan = preview(driver, [FAKE_IP])
    check = plan['checks']['nat_list']
    assert check['withdraw']
    with pytest.raises(RouterError, match='仍然存在'):
        nat_list.verify(check, driver.read_state())


def test_restrict_egress_cases():
    base = {'withdraw': False, 'rule': None, 'out_interface': '', 'replace_rule': False, 'summary': 's'}
    check = {'out_interface': '', 'removed': []}
    op = copy.deepcopy(base)
    assert nat_list.restrict_egress(op, check, 'bridge') == '' and op['out_interface'] == 'bridge'
    op = {**copy.deepcopy(base), 'rule': {'id': '*R'}}
    check = {'out_interface': '', 'removed': []}
    assert nat_list.restrict_egress(op, check, 'bridge') == '' and op['replace_rule'] and check['removed'] == ['*R']
    op = {**copy.deepcopy(base), 'rule': {'id': '*R'}, 'out_interface': 'ether9'}
    assert '与当前 mihomo 出口 bridge 不一致' in nat_list.restrict_egress(op, check, 'bridge')



def test_failed_member_removal_puts_the_new_member_back_out():
    driver = device()
    publish(driver, [FAKE_IP, TARGET])
    _, plan = preview(driver, [FAKE_IP, '149.154.160.0/20'])
    before = configurations(driver)
    driver.fail = ('DELETE', ADDRESSES, driver.counts['DELETE', ADDRESSES] + 1)
    result = execute(plan, driver, None)
    assert not result['ok'] and result['failed_stage'] == 'nat_bypass'
    assert not any(item['status'] == 'failed' for item in result['rollback'])
    assert configurations(driver) == before and members(driver, DST) == {FAKE_IP, TARGET}
