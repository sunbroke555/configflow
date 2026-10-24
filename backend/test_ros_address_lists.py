"""Scoped address-list reads: only referenced lists, known lists proven by digest."""
import copy
import hashlib

import pytest

from backend.routers import ros_address_lists as address_lists
from backend.routers.release import execute
from backend.routers.ros_policy import ADDRESSES, NAT, snapshot
from backend.test_ros_policy import MemoryROS, desired_for, inspect, release
from backend.test_ros_policy_bulk import cidrs

DESTINATIONS = 'CF_Destination_Proxy_IP_01'


class ReadLog(MemoryROS):
    def __init__(self):
        super().__init__()
        self.address_reads = []

    def _request(self, method, path, values=None):
        if method == 'GET' and path.startswith(ADDRESSES):
            self.address_reads.append(path)
        return super()._request(method, path, values)


def row(index, **fields):
    return {'.id': f'*{index:X}', 'list': 'L', 'address': f'10.0.{index // 256}.{index % 256}',
            'disabled': 'false', 'dynamic': 'false', **fields}


@pytest.mark.parametrize('count', [0, 1, 499, 500, 501, 1001])
def test_digest_ignores_order_and_runtime_fields_but_not_content(count):
    rows = [row(index, comment=f'c{index}') for index in range(count)]
    expected = address_lists.digest(rows)
    shuffled = [{**item, 'creation-time': '2026-10-25 00:00:00'} for item in reversed(rows)]
    assert address_lists.digest(shuffled) == expected
    assert expected[0] == count
    if count:
        changed = copy.deepcopy(rows)
        changed[-1]['comment'] = 'edited'
        assert address_lists.digest(changed) != expected
        assert address_lists.digest(rows[:-1]) != expected


def test_encoding_matches_the_router_tostr_format_and_counts_fields():
    # Verified on RouterOS 7.24.5: [:len $r] . ":" . [:tostr $r] after dropping runtime keys.
    member = {'.id': '*1', 'comment': '中文;x=1', 'creation-time': '2026-10-25 00:00:00', 'list': 'L'}
    assert address_lists._encode(member) == '3:.id=*1;comment=中文;x=1;list=L\n'.encode()
    assert address_lists._encode({**member, 'disabled': 'false'}) != address_lists._encode(member)
    assert address_lists._encode({key: value for key, value in member.items() if key != 'comment'}) \
        != address_lists._encode(member)


def test_empty_digest_matches_the_router_formula():
    assert address_lists.digest([]) == (0, hashlib.md5(hashlib.md5(b'').hexdigest().encode()).hexdigest())


def test_published_lists_are_proven_by_digest_instead_of_downloaded():
    driver = ReadLog()
    _, plan = inspect(driver, desired_for(driver, cidrs=cidrs(600)))
    assert execute(plan, driver, None)['ok']
    driver.address_reads.clear()
    _, again = inspect(driver, desired_for(driver, cidrs=cidrs(600)))
    assert again['empty'] and not again['blockers']
    assert not any(DESTINATIONS in path for path in driver.address_reads)
    assert driver.counts['DIGEST', ADDRESSES] >= 1


@pytest.mark.parametrize('change', ['edit', 'foreign_member'])
def test_any_difference_from_the_manifest_falls_back_to_a_full_read_and_blocks(change):
    driver = ReadLog()
    _, plan = inspect(driver, desired_for(driver, cidrs=cidrs(20)))
    assert execute(plan, driver, None)['ok']
    if change == 'edit':
        next(item for item in driver.rows[ADDRESSES] if item['list'] == DESTINATIONS)['comment'] = 'WinBox'
    else:
        driver.rows[ADDRESSES].append({'.id': '*HAND', 'list': DESTINATIONS, 'address': '203.0.113.1',
                                       'disabled': 'false', 'dynamic': 'false'})
    driver.address_reads.clear()
    _, plan = inspect(driver, desired_for(driver, cidrs=cidrs(20)))
    assert plan['blockers']
    assert any(DESTINATIONS in path for path in driver.address_reads)


def test_state_reads_only_lists_that_nat_or_mangle_select_on():
    driver = ReadLog()
    driver.rows[ADDRESSES] += [
        {'.id': '*B1', 'list': 'blocklist', 'address': '203.0.113.1', 'disabled': 'false'},
        {'.id': '*N1', 'list': 'nat-targets', 'address': '198.51.100.0/24', 'disabled': 'false'},
        {'.id': '*N2', 'list': 'nat-targets', 'address': '198.51.100.9', 'disabled': 'true'},
    ]
    driver.rows[NAT].append({'.id': '*9', 'chain': 'srcnat', 'action': 'accept',
                             'dst-address-list': '!nat-targets', 'disabled': 'false'})
    state = driver.read_state()
    assert state.address_sets == {'nat-targets': ['198.51.100.0/24']}
    assert all('?' in path for path in driver.address_reads)
    assert not any('blocklist' in path for path in driver.address_reads)


def test_snapshot_reserves_occupied_names_without_reading_their_members():
    driver = ReadLog()
    driver.rows[ADDRESSES] += [{'.id': f'*S{i}', 'list': 'CF_Source_Proxy_IP_01', 'address': f'192.0.2.{i}',
                                'disabled': 'false'} for i in range(5)]
    live = snapshot(driver)
    assert [item['occupied-list'] for item in live[address_lists.ADDRESSES + '#names']] == ['CF_Source_Proxy_IP_01']
    assert not any('CF_Source_Proxy_IP_01' in path for path in driver.address_reads)
    release(driver)
    assert {item['list'] for item in driver.rows[ADDRESSES] if item['.id'].startswith('*S')} == {'CF_Source_Proxy_IP_01'}
    assert {item['list'] for item in driver.rows[ADDRESSES] if item.get('comment') == 'cf-ros-policy-test'} == {
        'CF_Source_Proxy_IP_02', 'CF_Destination_Proxy_IP_02', 'CF_Bypass_IP_02'}


def test_dns_guard_snapshot_reads_only_its_bypass_list():
    from backend.routers import dns_guard
    driver = ReadLog()
    driver.rows[ADDRESSES] += [{'.id': '*B1', 'list': 'blocklist', 'address': '203.0.113.1', 'disabled': 'false'},
                               {'.id': '*G1', 'list': dns_guard.BYPASS_LIST, 'address': '192.168.0.2',
                                'disabled': 'false'}]
    driver.rows.setdefault(dns_guard.NETWATCH, [])
    live = dns_guard.snapshot(driver)
    assert [item['.id'] for item in live[ADDRESSES]] == ['*G1']
    assert driver.address_reads and all('?' in path for path in driver.address_reads)


def test_proofs_are_reused_only_inside_a_scope_and_until_a_write():
    from backend.routers.ros_policy_write import _remove_addresses
    driver = ReadLog()
    _, plan = inspect(driver, desired_for(driver, cidrs=cidrs(30)))
    assert execute(plan, driver, None)['ok']
    names = [DESTINATIONS]
    digests = lambda: driver.counts['DIGEST', ADDRESSES]
    start = digests()
    with address_lists.reuse(driver):
        address_lists.read(driver, names)
        address_lists.read(driver, names)
        assert digests() == start + 1
        member = address_lists.read(driver, names)[DESTINATIONS][0]
        _remove_addresses(driver, [member['.id']])  # any list write drops the proofs
        rows = address_lists.read(driver, names)[DESTINATIONS]
        assert member['.id'] not in {item['.id'] for item in rows}
    outside = digests()
    address_lists.read(driver, names)
    address_lists.read(driver, names)
    assert digests() == outside + 2  # no reuse outside a scope


def test_console_fetch_matches_rest_rows_and_pages_large_lists():
    driver = ReadLog()
    driver.rows[ADDRESSES] = [row(index, list=DESTINATIONS, comment='cf-ros-policy-test',
                                  **{'creation-time': '2026-10-25 00:00:00'}) for index in range(4500)]
    rows = address_lists.fetch(driver, DESTINATIONS)
    expected = [{key: value for key, value in item.items() if key != 'creation-time'} for item in driver.rows[ADDRESSES]]
    assert rows == expected
    assert driver.counts['FETCH', ADDRESSES] == 3 and not driver.address_reads


def test_console_fetch_falls_back_to_rest_when_values_contain_separators():
    driver = ReadLog()
    driver.rows[ADDRESSES] = [row(1, list=DESTINATIONS, comment='a;b'), row(2, list=DESTINATIONS)]
    assert address_lists.fetch(driver, DESTINATIONS) == driver.rows[ADDRESSES]
    assert driver.address_reads == [ADDRESSES + '?list=' + DESTINATIONS]


def test_console_fetch_rejects_pages_taken_while_the_list_changed():
    driver = ReadLog()
    driver.rows[ADDRESSES] = [row(index, list=DESTINATIONS) for index in range(2500)]
    original = driver._execute
    def shrinking(values):
        result = original(values)
        if 'cf-rows:' in values['script'] and driver.counts['FETCH', ADDRESSES] == 1:
            del driver.rows[ADDRESSES][:100]  # someone removes early members between pages
        return result
    driver._execute = shrinking
    rows = address_lists.fetch(driver, DESTINATIONS)
    assert rows == driver.rows[ADDRESSES] and driver.address_reads == [ADDRESSES + '?list=' + DESTINATIONS]


def test_member_moved_to_an_unread_list_is_reported_as_edited_not_deleted():
    driver = ReadLog()
    release(driver)
    member = next(item for item in driver.rows[ADDRESSES] if item['list'] == 'CF_Source_Proxy_IP_01')
    member['list'] = 'my-list'
    driver.address_list_cache = None
    _, plan = inspect(driver)
    assert any('外部修改' in message for message in plan['blockers'])


def test_withdrawal_rechecks_members_instead_of_trusting_the_proof_from_its_start():
    driver = ReadLog()
    release(driver)
    _, withdrawal = inspect(driver, desired_for(driver, selected=False))
    original = driver._request
    def edit_after_entry_rule(method, path, values=None):
        result = original(method, path, values)
        if method == 'DELETE' and path.startswith('/ip/firewall/mangle'):
            next(item for item in driver.rows[ADDRESSES] if item['list'] == DESTINATIONS)['comment'] = 'WinBox'
        return result
    driver._request = edit_after_entry_rule
    result = execute(withdrawal, driver, None)
    assert not result['ok']
    assert any(item['comment'] == 'WinBox' for item in driver.rows[ADDRESSES])
