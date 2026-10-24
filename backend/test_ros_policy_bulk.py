"""Address-list members are written in /execute batches and removed by id batches."""
import copy
import ipaddress
from collections import Counter

import pytest

from backend.routers.base import RouterError, RouterRollbackError
from backend.routers.release import execute
from backend.routers.ros_policy import ADDRESSES, MANGLE
from backend.routers import ros_policy_write
from backend.routers.ros_policy_write import (
    ADDRESS_BATCH_MEMBERS, ADDRESS_SCRIPT_BYTES, _create_addresses, _delete_addresses, restore,
)
from backend.routers.routeros import UncertainWrite
from backend.test_ros_policy import MemoryROS, configurations, desired_for, inspect

LIST = 'CF_Destination_Proxy_IP_01'


def cidrs(count, start='100.64.0.0'):
    base = int(ipaddress.IPv4Address(start))
    return [f'{ipaddress.IPv4Address(base + index * 256)}/24' for index in range(count)]


class CountingROS(MemoryROS):
    def __init__(self):
        super().__init__()
        self.requests = Counter()

    def _request(self, method, path, values=None):
        endpoint = path.partition('?')[0]
        if endpoint.startswith(ADDRESSES + '/') and endpoint != ADDRESSES + '/remove':
            endpoint = ADDRESSES + '/<id>'
        if endpoint == '/execute':
            script = values['script']
            endpoint = ('/execute:digest' if 'cf-digest:' in script else
                        '/execute:names' if 'cf-names:' in script else
                        '/execute:fetch' if 'cf-rows:' in script else '/execute')
        self.requests[method, endpoint] += 1
        return super()._request(method, path, values)


def is_read(path, values):
    """A list read after a batch: a filtered GET or a console row fetch."""
    return '?' in path or (path == '/execute' and 'cf-rows:' in values['script'])


def members(driver, name=LIST):
    return [row for row in driver.rows[ADDRESSES] if row['list'] == name]


def test_thousands_of_destinations_cost_batches_not_one_request_per_member():
    driver = CountingROS()
    destinations = cidrs(3000)
    release_desired = desired_for(driver, cidrs=destinations)
    _, plan = inspect(driver, release_desired)
    assert not plan['blockers'], plan['blockers']
    assert execute(plan, driver, None)['ok']
    assert {row['address'] for row in members(driver)} == set(destinations)
    assert driver.requests['PUT', ADDRESSES] == 0
    assert driver.requests['GET', ADDRESSES + '/<id>'] == 0
    assert driver.requests['POST', '/execute'] == 3000 // ADDRESS_BATCH_MEMBERS + 2  # + source and bypass

    # Replacing the group removes the old list with a handful of id batches.
    driver.requests.clear()
    updated = destinations[:2999] + ['198.51.100.0/24']
    _, plan = inspect(driver, desired_for(driver, cidrs=updated))
    assert execute(plan, driver, None)['ok']
    assert not members(driver)
    assert {row['address'] for row in members(driver, 'CF_Destination_Proxy_IP_02')} == set(updated)
    assert driver.requests['DELETE', ADDRESSES + '/<id>'] == 0
    assert driver.requests['POST', ADDRESSES + '/remove'] <= 8


def test_batches_stay_under_the_script_limit_and_escape_values(monkeypatch):
    driver = MemoryROS()
    sent = []
    original = driver._request
    def capture(method, path, values=None):
        if path == '/execute' and 'address-list/add' in values['script']:
            sent.append(values['script'])
        return original(method, path, values)
    monkeypatch.setattr(driver, '_request', capture)
    comment = 'cf-ros-policy-"quoted" $var \\ back\nline'
    entries = [{'list': LIST, 'address': cidr, 'comment': comment, 'disabled': 'false'} for cidr in cidrs(2500)]
    journal, ids = [], {ADDRESSES: set()}
    resources = _create_addresses(driver, entries, journal, ids)
    assert len(sent) == 13 and all(len(script.encode()) <= ADDRESS_SCRIPT_BYTES for script in sent)
    assert max(script.count('";"') + 1 for script in sent) == ADDRESS_BATCH_MEMBERS
    assert all('disabled=no' in script and '\\$var' in script and '\\"quoted\\"' in script for script in sent)
    assert {row['comment'] for row in members(driver)} == {comment}
    assert [item['config']['address'] for item in resources] == [entry['address'] for entry in entries]
    assert [step['id'] for step in journal] == [item['id'] for item in resources]


def test_host_bits_and_single_addresses_match_router_normalization():
    driver = MemoryROS()
    journal, ids = [], {ADDRESSES: set()}
    resources = _create_addresses(driver, [
        {'list': LIST, 'address': '192.168.0.101/32', 'comment': 'c', 'disabled': 'false'},
        {'list': LIST, 'address': '10.0.0.5/24', 'comment': 'c', 'disabled': 'false'},
    ], journal, ids)
    assert [item['config']['address'] for item in resources] == ['192.168.0.101', '10.0.0.0/24']


def test_reported_failures_stop_and_journal_only_created_members():
    driver = MemoryROS()
    journal, ids = [], {ADDRESSES: set()}
    requested = cidrs(3) + ['100.64.1.0/24']  # RouterOS refuses the duplicate member
    entries = [{'list': LIST, 'address': cidr, 'comment': 'c', 'disabled': 'false'} for cidr in requested]
    with pytest.raises(RouterError, match='1 条写入失败.*already have such entry'):
        _create_addresses(driver, entries, journal, ids)
    assert sorted(step['config']['address'] for step in journal) == sorted(cidrs(3))
    restore(driver, {'steps': list(reversed(journal)), 'previous': None})
    assert not driver.rows[ADDRESSES]


@pytest.mark.parametrize('output', ['syntax error (line 1 column 10)', '', 'cf-done:x:'])
def test_script_without_completion_marker_is_a_failure(monkeypatch, output):
    driver = MemoryROS()
    original = driver._request
    def broken(method, path, values=None):
        if path == '/execute':
            original(method, path, values)
            return {'ret': output}
        return original(method, path, values)
    monkeypatch.setattr(driver, '_request', broken)
    journal = []
    with pytest.raises(RouterError, match='未执行完成'):
        _create_addresses(driver, [{'list': LIST, 'address': '100.64.0.0/24', 'comment': 'c', 'disabled': 'false'}],
                          journal, {ADDRESSES: set()})
    assert len(journal) == 1  # what did get written is still compensated


def test_lost_reply_waits_for_the_batch_to_settle_before_journaling(monkeypatch):
    driver = MemoryROS()
    monkeypatch.setattr(ros_policy_write.time, 'sleep', lambda _: None)
    driver.lost = ('PUT', ADDRESSES, 2)
    journal = []
    with pytest.raises(UncertainWrite):
        _create_addresses(driver, [{'list': LIST, 'address': cidr, 'comment': 'c', 'disabled': 'false'}
                                   for cidr in cidrs(3)], journal, {ADDRESSES: set()})
    assert len(journal) == 2


def test_list_that_keeps_growing_after_a_lost_reply_is_left_for_inspection(monkeypatch):
    driver = MemoryROS()
    monkeypatch.setattr(ros_policy_write.time, 'sleep', lambda _: None)
    driver.lost = ('PUT', ADDRESSES, 1)
    reads = []
    original = driver._request
    def growing(method, path, values=None):
        if is_read(path, values):
            reads.append(path)
            driver.rows[ADDRESSES].append({'.id': f'*G{len(reads)}', 'list': LIST,
                                           'address': f'203.0.113.{len(reads)}', 'comment': 'c', 'disabled': 'false'})
        return original(method, path, values)
    monkeypatch.setattr(driver, '_request', growing)
    journal = []
    with pytest.raises(RouterRollbackError, match='结果不明确'):
        _create_addresses(driver, [{'list': LIST, 'address': '100.64.0.0/24', 'comment': 'c', 'disabled': 'false'}],
                          journal, {ADDRESSES: set()})
    assert [step['action'] for step in journal] == ['sweep']
    monkeypatch.setattr(driver, '_request', original)
    restore(driver, {'steps': journal, 'previous': None})
    assert members(driver) and all(row['.id'].startswith('*G') for row in members(driver))


def test_rows_nobody_requested_are_never_journaled(monkeypatch):
    driver = MemoryROS()
    original = driver._request
    def concurrent(method, path, values=None):
        result = original(method, path, values)
        if path == '/execute':
            driver.rows[ADDRESSES].append({'.id': '*OTHER', 'list': LIST, 'address': '203.0.113.9',
                                           'comment': 'someone else', 'disabled': 'false'})
        return result
    monkeypatch.setattr(driver, '_request', concurrent)
    journal = []
    with pytest.raises(RouterError, match='未提交的条目'):
        _create_addresses(driver, [{'list': LIST, 'address': '100.64.0.0/24', 'comment': 'c', 'disabled': 'false'}],
                          journal, {ADDRESSES: set()})
    assert [step['config']['address'] for step in journal] == ['100.64.0.0/24']


def test_bulk_delete_guards_every_member_before_removing_any():
    driver = MemoryROS()
    journal, ids = [], {ADDRESSES: set()}
    created = _create_addresses(driver, [{'list': LIST, 'address': cidr, 'comment': 'c', 'disabled': 'false'}
                                         for cidr in cidrs(4)], journal, ids)
    next(row for row in driver.rows[ADDRESSES] if row['.id'] == created[2]['id'])['comment'] = 'edited'
    before = copy.deepcopy(driver.rows[ADDRESSES])
    with pytest.raises(RouterError, match='发生变化'):
        _delete_addresses(driver, created, [])
    assert driver.rows[ADDRESSES] == before


def test_partial_bulk_delete_recreates_removed_members_and_remaps_the_manifest():
    driver = MemoryROS()
    destinations = cidrs(5)
    release_desired = desired_for(driver, cidrs=destinations)
    _, plan = inspect(driver, release_desired)
    assert execute(plan, driver, None)['ok']
    before = configurations(driver)
    old_ids = {row['.id'] for row in members(driver)}
    # Source, bypass and the first three destinations are removed before the failure.
    driver.fail = ('DELETE', ADDRESSES, driver.counts['DELETE', ADDRESSES] + 6)
    _, plan = inspect(driver, desired_for(driver, cidrs=destinations[:4]))
    result = execute(plan, driver, None)
    assert not result['ok'] and not any(item['status'] == 'failed' for item in result['rollback']), result
    assert configurations(driver) == before
    assert {row['.id'] for row in members(driver)} != old_ids
    manifest = driver.ros_policy_manifest
    live = {row['.id']: row for row in driver.rows[ADDRESSES]}
    assert all(item['id'] in live for item in manifest['resources'] if item['path'] == ADDRESSES)
    driver.fail = None
    _, again = inspect(driver, desired_for(driver, cidrs=destinations))
    assert again['empty'] and not again['blockers']


def test_withdrawal_removes_members_in_batches_after_the_entry_rule():
    driver = CountingROS()
    _, plan = inspect(driver, desired_for(driver, cidrs=cidrs(1200)))
    assert execute(plan, driver, None)['ok']
    start = len(driver.writes)
    _, withdrawal = inspect(driver, desired_for(driver, selected=False))
    assert execute(withdrawal, driver, None)['ok']
    assert not driver.rows[ADDRESSES] and not driver.rows[MANGLE]
    writes = driver.writes[start:]
    entry = next(i for i, (method, path, _) in enumerate(writes) if method == 'DELETE' and path.startswith(MANGLE))
    first_member = next(i for i, (_, path, _) in enumerate(writes) if path.startswith(ADDRESSES))
    assert entry < first_member
    assert driver.requests['POST', ADDRESSES + '/remove'] == 3  # 1202 members, 500 per batch


def _pairwise_subtract(networks, exclusions):
    for excluded in exclusions:
        remaining = []
        for network in networks:
            if not network.overlaps(excluded):
                remaining.append(network)
            elif not network.subnet_of(excluded):
                remaining.extend(network.address_exclude(excluded))
        networks = remaining
    return list(ipaddress.collapse_addresses(networks))


@pytest.mark.parametrize('seed', range(40))
def test_sweep_subtract_matches_pairwise_subtraction(seed):
    import random
    from backend.routers.ros_policy import subtract
    rng = random.Random(seed)
    def sample(count):
        return [ipaddress.IPv4Network((rng.randrange(0, 1 << 32), rng.choice((0, 4, 8, 12, 16, 20, 23, 24, 28, 31, 32))),
                                      strict=False) for _ in range(count)]
    networks, exclusions = sample(rng.randrange(0, 60)), sample(rng.randrange(0, 60))
    assert subtract(networks, exclusions) == _pairwise_subtract(networks, exclusions)


def test_preview_with_thousands_of_previous_and_current_destinations_is_fast():
    import time
    from backend.routers.ros_policy import subtract
    previous, current = _networks_of(cidrs(5000)), _networks_of(cidrs(5000, '100.65.0.0'))
    started = time.monotonic()
    assert len(subtract(previous, current)) < len(previous)
    assert time.monotonic() - started < 10  # pairwise subtraction took minutes


def _networks_of(values):
    return [ipaddress.IPv4Network(value) for value in values]


@pytest.mark.parametrize('stage', ['publish', 'rule_sync_shape'])
def test_failed_readback_after_a_batch_still_rolls_back_every_written_member(monkeypatch, stage):
    """A transient read failure after /execute must not orphan the batch."""
    driver = MemoryROS()
    monkeypatch.setattr(ros_policy_write.time, 'sleep', lambda _: None)
    if stage == 'rule_sync_shape':
        driver.rows[ADDRESSES].append({'.id': '*OLD', 'list': LIST, 'address': '198.51.100.0/24',
                                       'comment': 'c', 'disabled': 'false'})
    original = driver._request
    state = {'executed': False, 'failures': 0}
    def flaky(method, path, values=None):
        if path == '/execute' and 'address-list/add' in values['script']:
            state['executed'] = True
        elif is_read(path, values) and state['executed'] and state['failures'] < 3:
            state['failures'] += 1
            raise RouterError('无法连接 RouterOS：ReadTimeout')
        return original(method, path, values)
    monkeypatch.setattr(driver, '_request', flaky)
    journal = []
    with pytest.raises(RouterError, match='ReadTimeout'):
        _create_addresses(driver, [{'list': LIST, 'address': cidr, 'comment': 'c', 'disabled': 'false'}
                                   for cidr in cidrs(450)], journal, {ADDRESSES: {'*OLD'}})
    assert [step['action'] for step in journal] == ['sweep']
    assert len(members(driver)) == 450 + (stage == 'rule_sync_shape')
    restore(driver, {'steps': list(reversed(journal)), 'previous': None})
    assert [row['.id'] for row in members(driver)] == (['*OLD'] if stage == 'rule_sync_shape' else [])


def test_one_transient_read_failure_is_retried(monkeypatch):
    driver = MemoryROS()
    monkeypatch.setattr(ros_policy_write.time, 'sleep', lambda _: None)
    original = driver._request
    state = {'executed': False, 'failed': False}
    def flaky(method, path, values=None):
        if path == '/execute' and 'address-list/add' in values['script']:
            state['executed'] = True
        elif is_read(path, values) and state['executed'] and not state['failed']:
            state['failed'] = True
            raise RouterError('无法连接 RouterOS：ReadTimeout')
        return original(method, path, values)
    monkeypatch.setattr(driver, '_request', flaky)
    journal = []
    resources = _create_addresses(driver, [{'list': LIST, 'address': cidr, 'comment': 'c', 'disabled': 'false'}
                                           for cidr in cidrs(3)], journal, {ADDRESSES: set()})
    assert state['failed'] and len(resources) == 3
    assert [step['action'] for step in journal] == ['delete'] * 3


def test_rollback_keeps_new_ids_of_members_recreated_before_a_failure():
    driver = MemoryROS()
    journal, ids = [], {ADDRESSES: set()}
    created = _create_addresses(driver, [{'list': LIST, 'address': cidr, 'comment': 'c', 'disabled': 'false'}
                                         for cidr in cidrs(4)], [], ids)
    _delete_addresses(driver, created, journal)
    # Someone re-adds one member by hand, so recreating it is refused.
    driver.rows[ADDRESSES].append({'.id': '*HAND', 'list': LIST, 'address': created[3]['config']['address'],
                                   'comment': 'c', 'disabled': 'false'})
    previous = {'spec': {}, 'resources': copy.deepcopy(created)}
    with pytest.raises(RouterRollbackError, match='1 条写入失败'):
        restore(driver, {'steps': list(reversed(journal)), 'previous': previous})
    manifest = driver.ros_policy_manifest
    live = {row['.id'] for row in members(driver)}
    remapped = [item['id'] for item in manifest['resources'][:3]]
    assert all(item_id in live and item_id not in {item['id'] for item in created} for item_id in remapped)
