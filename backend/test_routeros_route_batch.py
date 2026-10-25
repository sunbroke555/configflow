"""Batched static-route writes: fewer requests, the same plan, writes, errors and rollback."""
import copy
import ipaddress
import random
import re

import pytest
import requests

from backend.routers import routeros_batch
from backend.routers.base import RouterError, RouterRollbackError
from backend.routers.plan import build_plan
from backend.routers.release import execute
from backend.routers.report import build_report
from backend.routers.ros_policy import network_overlap
from backend.test_router_rule_sync import environment  # noqa: F401 (pytest fixture)
from backend.test_routeros_release import (
    BROAD_SOURCE_NAT, NAT, ROUTES, RESTResponse, StatefulRouterOS, configured, driver_for,
)

GATEWAY = '10.0.0.4'
ROUTER = {'id': 'r1', 'type': 'routeros', 'endpoint': 'https://10.0.0.1', 'manage_return_routing': False}
# Existing NAT already covers every destination, so only routes are written.
COVERED_NAT = [{'.id': '*A', 'chain': 'srcnat', 'action': 'accept', 'comment': 'mihomo', 'disabled': 'false',
                'src-address': '10.0.0.0/24'}, BROAD_SOURCE_NAT]


def cidrs(count, start=0):
    return [f'{45 + (index >> 16)}.{(index >> 8) & 255}.{index & 255}.0/24' for index in range(start, start + count)]


def unrelated(count):
    return [{'.id': f'*{0xA0000 + index:X}', 'dst-address': f'100.{64 + (index >> 8)}.{index & 255}.0/24', 'blackhole': 'true',
             'comment': 'other', 'routing-table': 'main', 'disabled': 'false', 'dynamic': 'false', 'active': 'true',
             'distance': '1'} for index in range(count)]


def desired(targets):
    return {'fake_ip_range': '198.18.0.0/16', 'proxy_gateway': GATEWAY, 'warnings': [],
            'proxy_cidrs': [{'cidr': cidr, 'sources': ['bench']} for cidr in targets]}


def plan_for(driver, targets):
    state = driver.read_state()
    want = desired(targets)
    plan = build_plan(ROUTER, want, state, build_report(want, state), {'status': 'disabled'}, driver)
    assert not plan['blockers'], plan['blockers']
    return plan


def counted(driver):
    """Requests the driver itself sends (the fake's console adds are not requests)."""
    driver.sent = []
    send = driver._request

    def request(method, path, values=None, **kwargs):
        driver.sent.append((method, path))
        return send(method, path, values, **kwargs)
    driver._request = request
    return driver


def whole_table_reads(session):
    return sum(1 for method, path, _ in session.calls if method == 'GET' and path == ROUTES)


def single_writes(monkeypatch):
    """The one-by-one path every batch must reproduce."""
    monkeypatch.setattr(routeros_batch, 'BATCH_MIN', 10 ** 9)


def outcome(result, session):
    return ({key: result[key] for key in ('ok', 'failed_stage', 'rollback') if key in result},
            [(entry['stage'], entry['summary'], entry['status']) for entry in result['log']],
            configured(session.rows[ROUTES]), copy.deepcopy(session.rows[NAT]))


def run(session_factory, steps, monkeypatch=None):
    session = session_factory()
    driver = driver_for(session)
    results = []
    for targets, prepare in steps:
        plan = plan_for(driver, targets)
        if prepare:
            prepare(session)
        session.calls.clear()
        results.append(outcome(execute(plan, driver, None), session))
    return results, session


def test_publishing_300_routes_reads_the_route_table_a_constant_number_of_times():
    session = StatefulRouterOS(routes=unrelated(50), nats=COVERED_NAT)
    driver = driver_for(session)
    plan = plan_for(driver, cidrs(300))
    session.calls.clear()
    result = execute(plan, counted(driver), None)
    assert result['ok'], result
    # One-by-one: two whole-table reads per route (603 GETs, 121k rows); now
    # one stage read-back and a handful of batch requests.
    assert whole_table_reads(session) <= 2
    assert len(driver.sent) <= 25
    assert sum(1 for method, path, _ in session.calls if (method, path) == ('POST', '/execute')) <= 4
    assert {row['dst-address'] for row in session.rows[ROUTES]} >= set(cidrs(300))


@pytest.fixture
def small_batches(monkeypatch):
    # Several batches without the one-by-one path's slow fake reads.
    monkeypatch.setattr(routeros_batch, 'ROUTE_BATCH', 20)


@pytest.mark.parametrize('scenario', ['publish', 'withdraw', 'rejected_add', 'rejected_first_of_batch',
                                      'nat_failure_rolls_back_adds', 'nat_failure_restores_deletes',
                                      'hidden_new_routes', 'changed_before_delete'])
def test_batches_match_one_by_one_writes(monkeypatch, small_batches, scenario):
    first, second = cidrs(50), cidrs(30, 500)
    factory, steps = (lambda: StatefulRouterOS(routes=unrelated(20), nats=COVERED_NAT)), [(first, None)]
    if scenario == 'withdraw':
        steps = [(first, None), (second, None)]
    elif scenario == 'rejected_add':
        factory = lambda: StatefulRouterOS(routes=unrelated(20), nats=COVERED_NAT, fail=('PUT', ROUTES, 30))
    elif scenario == 'rejected_first_of_batch':
        factory = lambda: StatefulRouterOS(routes=unrelated(20), nats=COVERED_NAT, fail=('PUT', ROUTES, 21))
    elif scenario == 'nat_failure_rolls_back_adds':
        factory = lambda: StatefulRouterOS(routes=unrelated(20), nats=[BROAD_SOURCE_NAT], fail=('PUT', NAT, 1))
    elif scenario == 'nat_failure_restores_deletes':
        def failing():
            session = StatefulRouterOS(routes=unrelated(20), nats=[BROAD_SOURCE_NAT])
            return session
        factory = failing
        steps = [(first, None), (second, lambda session: setattr(session, 'fail', ('PUT', NAT, 2)))]
    elif scenario == 'hidden_new_routes':
        factory = lambda: StatefulRouterOS(routes=unrelated(20), nats=COVERED_NAT, hide_new_routes=True)
    elif scenario == 'changed_before_delete':
        def change(session):
            row = next(row for row in session.rows[ROUTES] if row.get('dst-address') == first[7])
            row['dst-address'] = '203.0.113.0/24'
        steps = [(first, None), (second, change)]
    batched, session = run(factory, steps)
    single_writes(monkeypatch)
    one_by_one, _ = run(factory, steps)
    assert batched == one_by_one
    if scenario in ('publish', 'withdraw', 'nat_failure_rolls_back_adds', 'nat_failure_restores_deletes'):
        assert any(path in ('/execute', ROUTES + '/remove') for _, path, _ in session.calls)


def test_rollback_of_a_large_publish_removes_routes_in_batches():
    session = StatefulRouterOS(routes=unrelated(20), nats=[BROAD_SOURCE_NAT], fail=('PUT', NAT, 1))
    driver = driver_for(session)
    before = configured(session.rows[ROUTES])
    plan = plan_for(driver, cidrs(300))
    session.calls.clear()
    result = execute(plan, driver, None)
    assert not result['ok'] and result['failed_stage'] == 'nat_bypass'
    assert len(result['rollback']) == 300 and all(item['status'] == 'ok' for item in result['rollback'])
    assert configured(session.rows[ROUTES]) == before
    removes = [call for call in session.calls if call[:2] == ('POST', ROUTES + '/remove')]
    assert len(removes) == 1 and len(removes[0][2]['.id'].split(',')) == 300
    assert not any(method == 'DELETE' for method, _, _ in session.calls)


def test_lost_batch_reply_keeps_the_unknown_batch_and_rolls_back_proven_ones(small_batches):
    session = StatefulRouterOS(routes=unrelated(20), nats=COVERED_NAT, timeout_after=('PUT', ROUTES, 30))
    driver = driver_for(session)
    plan = plan_for(driver, cidrs(50))
    result = execute(plan, driver, None)
    assert not result['ok'] and result['failed_stage'] == 'routes'
    assert result['rollback'][0]['status'] == 'failed' and '已保留现场' in result['rollback'][0]['summary']
    assert [item['status'] for item in result['rollback'][1:]] == ['ok'] * 20
    # The first batch is undone; the second (whose reply was lost) is left for inspection.
    left = {row['dst-address'] for row in session.rows[ROUTES] if row.get('comment') == 'cf-bench'}
    assert left == set(cidrs(50)[20:30])
    assert len([row for row in session.rows[ROUTES] if row.get('comment') == 'other']) == 20


class ForeignReply(StatefulRouterOS):
    """A batch reply naming an existing route instead of the one it added."""
    def _post(self, path, body):
        response = super()._post(path, body)
        if path == '/execute':
            response.data['ret'] = re.sub(r'cf-id:3:\*[0-9A-F]+', 'cf-id:3:*A0000', response.data['ret'])
        return response


def test_batch_reply_naming_another_route_never_deletes_it():
    session = ForeignReply(routes=unrelated(5), nats=COVERED_NAT)
    driver = driver_for(session)
    foreign = copy.deepcopy(session.rows[ROUTES][0])
    with pytest.raises(RouterRollbackError, match='无法确认新增对象归属'):
        driver.apply_many([{'kind': 'route_add', 'cidr': cidr, 'gateway': GATEWAY, 'comment': 'cf-x'}
                           for cidr in cidrs(10)], lambda op, undo: None)
    assert session.rows[ROUTES][0] == foreign
    # Routes 0-2 and 4-9 were proven: those after the doubtful one are removed again.
    assert sorted(row['dst-address'] for row in session.rows[ROUTES] if row.get('comment') == 'cf-x') == sorted(
        cidrs(10)[:4])


def test_many_nat_rules_of_one_operation_are_written_in_batches(monkeypatch):
    def apply(batch):
        if not batch:
            single_writes(monkeypatch)
        session = StatefulRouterOS(nats=[BROAD_SOURCE_NAT])
        driver = driver_for(session)
        op = {'kind': 'nat_add', 'src': ['10.0.0.1-10.0.0.3', '10.0.0.5-10.0.0.254'], 'dst': cidrs(40),
              'comment': 'cf-mihomo'}
        undo = counted(driver).apply(op)
        rows = copy.deepcopy(session.rows[NAT])
        requests_made = len(driver.sent)
        driver.apply(undo)
        return rows, requests_made, session.rows[NAT]

    rows, batched_requests, after = apply(True)
    expected, single_requests, single_after = apply(False)
    assert [{key: value for key, value in row.items() if key != '.id'} for row in rows] == [
        {key: value for key, value in row.items() if key != '.id'} for row in expected]
    assert len(rows) == 1 + 40 * 15
    assert after == single_after == [BROAD_SOURCE_NAT]
    assert single_requests >= 3 * 600 and batched_requests <= 25


def test_nat_rule_the_console_rejects_falls_back_to_single_writes():
    session = StatefulRouterOS(nats=[BROAD_SOURCE_NAT], fail=('PUT', NAT, 3))
    driver = driver_for(session)
    op = {'kind': 'nat_add', 'src': ['10.0.0.1-10.0.0.3'], 'dst': cidrs(6), 'comment': 'cf-mihomo'}
    with pytest.raises(RouterError, match='injected write failure'):
        driver.apply(op)
    assert session.rows[NAT] == [BROAD_SOURCE_NAT]


def test_release_reads_state_once_per_routes_verification():
    session = StatefulRouterOS(routes=unrelated(5), nats=COVERED_NAT)
    driver = driver_for(session)
    plan = plan_for(driver, cidrs(2))
    session.calls.clear()
    assert execute(plan, driver, None)['ok']
    # The routes and NAT stage read-backs. Before: two reads per route, and
    # another for stale kernel routes, which RouterOS never reports.
    assert whole_table_reads(session) == 2


def test_network_overlap_matches_pairwise_comparison():
    rng = random.Random(7)
    def networks(count):
        return [ipaddress.IPv4Network((rng.randrange(1 << 32) & ~((1 << (32 - length)) - 1), length))
                for length in (rng.choice((8, 12, 16, 20, 24, 28, 32)) for _ in range(count))]
    for _ in range(30):
        pool, probes = networks(rng.randrange(0, 60)), networks(80)
        overlaps = network_overlap(pool)
        assert [overlaps(str(net)) for net in probes] == [any(net.overlaps(old) for old in pool) for net in probes]


def test_remove_stopping_at_a_missing_route_leaves_the_rest_to_single_deletes():
    session = StatefulRouterOS(routes=unrelated(10), nats=COVERED_NAT)
    driver = driver_for(session)
    ops = [{'kind': 'route_delete', 'id': row['.id'], 'cidr': row['dst-address'], 'summary': row['.id']}
           for row in session.rows[ROUTES]]

    original = session._post
    def remove_one_first(path, body):
        if path == ROUTES + '/remove':
            # Another program deletes the fifth route just before ours runs.
            session.rows[ROUTES] = [row for row in session.rows[ROUTES] if row['.id'] != ops[4]['id']]
        return original(path, body)
    session._post = remove_one_first
    recorded = []
    with pytest.raises(RouterError, match='HTTP 404'):
        driver.apply_many(ops, lambda op, undo: recorded.append(op['id']))
    # Ours and the one deleted meanwhile are recorded (restoring both is the
    # safe side); the rest are untouched and not recorded.
    assert recorded == [op['id'] for op in ops[:5]]
    assert [row['.id'] for row in session.rows[ROUTES]] == [op['id'] for op in ops[5:]]


def unordered(configs):
    return {path: sorted(map(repr, rows)) for path, rows in configs.items()}


def migration_driver(count):
    """Static managed routes inside the policy destination, released one by one before."""
    from backend.test_ros_policy import MemoryROS
    driver = MemoryROS()
    for index in range(count):
        driver.rows[ROUTES].append({'.id': f'*{0xB000 + index:X}', 'dst-address': f'91.108.{index}.0/24',
                                    'gateway': '192.168.0.3', 'routing-table': 'main', 'active': 'true',
                                    'immediate-gw': '192.168.0.3%bridge', 'disabled': 'false',
                                    'comment': 'cf-Telegram', 'distance': '1'})
    return driver


def switch_to_policy(driver):
    from backend.test_ros_policy import desired_for, inspect
    _, plan = inspect(driver, desired_for(driver, cidrs=['91.108.0.0/16']))
    assert not plan['blockers'], plan['blockers']
    return counted(driver), execute(plan, driver, None)


@pytest.mark.parametrize('fail_at', [None, 7])
def test_policy_switch_migrates_managed_routes_in_batches(monkeypatch, fail_at):
    from backend.test_ros_policy import configurations

    def run_switch():
        driver = migration_driver(12)
        before = configurations(driver)
        if fail_at:
            driver.fail = ('DELETE', ROUTES, fail_at)
        driver, result = switch_to_policy(driver)
        return driver, result, before

    driver, result, before = run_switch()
    by_id = [path for method, path in driver.sent if method == 'GET' and path.startswith(ROUTES + '/*')]
    single_writes(monkeypatch)
    single, single_result, _ = run_switch()
    assert {key: result[key] for key in ('ok', 'log', 'rollback')} == {
        key: single_result[key] for key in ('ok', 'log', 'rollback')}
    assert configurations(driver) == configurations(single)
    if fail_at:
        # Recreated routes return in reverse order, with new IDs.
        assert not result['ok'] and unordered(configurations(driver)) == unordered(before)
    else:
        assert result['ok'] and not any(row.get('comment') == 'cf-Telegram' for row in driver.rows[ROUTES])
    # One by one: a guard read per route before writing, another before and after each delete.
    single_by_id = [path for method, path in single.sent if method == 'GET' and path.startswith(ROUTES + '/*')]
    migrated = [path for path in single_by_id if path.startswith(ROUTES + '/*B0')]
    assert len(migrated) >= (12 if fail_at else 24) and not any(path.startswith(ROUTES + '/*B0') for path in by_id)


def test_static_sync_checks_enrolled_routes_from_one_snapshot(environment):
    from backend.routers import rule_sync
    from backend.test_router_rule_sync import publish
    repository, driver = environment
    spaced = [f'45.{index}.0.0/24' for index in range(25)]  # adjacent /24s would be merged
    old = '\n'.join(f'IP-CIDR,{cidr}' for cidr in spaced[:20])
    new = '\n'.join(f'IP-CIDR,{cidr}' for cidr in spaced[1:])
    repository.update_profile_transaction('default', lambda doc: doc['routers']['items'][0].update(
        ros_policy_rule_sets=[], ip_rule_sets=['remote']))
    repository.write_shared_text('rules/Remote.list', old)
    # A NAT rule selecting a foreign list: downloaded once per round, not per read.
    driver.rows[NAT].insert(0, {'.id': '*CN', 'chain': 'srcnat', 'action': 'accept', 'disabled': 'false',
                                'dst-address-list': '!CN', 'comment': 'manual'})
    driver.rows['/ip/firewall/address-list'].append({'.id': '*CN1', 'list': 'CN', 'address': '1.0.1.0/24'})
    publish(environment)
    counted(driver)
    result = rule_sync.sync('default', 'ros', lambda *_: new)
    assert result['status'] == 'synced', result
    assert (result['added'], result['removed']) == (5, 1)
    by_id = [path for method, path in driver.sent if method == 'GET' and path.startswith(ROUTES + '/*')]
    # Read back once per added route and twice for the removed one; none per enrolled route.
    assert len(by_id) == 5 + 2
    # Before: one whole-table read per added route, on top of the round's reads.
    assert sum(1 for method, path in driver.sent if (method, path) == ('GET', ROUTES)) <= 6
    downloads = [path for method, path in driver.sent if method == 'GET' and path.endswith('?list=CN')]
    assert len(downloads) <= 2  # the preview's read, and one for the whole write


def test_dhcp_update_reuses_the_snapshot_the_release_just_checked():
    from backend.test_routeros_dhcp_dns import operation, setup_driver

    def apply(reuse):
        driver, session = setup_driver()
        op = operation(driver)
        if reuse:
            op['checked_dhcp_dns'] = driver.read_dhcp_dns()
        session.calls.clear()
        undo = driver.apply(op)
        return undo, session.writes, [call for call in session.calls if call[0] == 'GET']

    undo, writes, reads = apply(True)
    single_undo, single_writes_, single_reads = apply(False)
    assert (undo, writes) == (single_undo, single_writes_)
    # Without the snapshot the six DHCP tables are read again before the write.
    assert len(single_reads) == len(reads) + 6
