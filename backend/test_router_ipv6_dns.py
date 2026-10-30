"""RA DNS updates follow the selected DHCP LAN and preserve other RA settings."""
import copy

import pytest

from backend.routers import ipv6_dns
from backend.routers.base import DhcpDnsEntry, DhcpDnsState, RouterError, RouterRollbackError
from backend.routers.routeros import RouterOSDriver
from backend.test_router_dns_guard import RouterOS

DESIRED = {'dns_agent_id': 'oxidns', 'dns_server': '10.0.0.2', 'proxy_gateway': '10.0.0.4',
           'dns_error': '', 'ipv6_dns_scope': {
               'id': '*D', 'network': '10.0.0.0/24', 'gateway': '10.0.0.4', 'dns': ['10.0.0.2']}}


def nd_row(**extra):
    return {'.id': '*1', 'interface': 'bridge', 'advertise-dns': 'yes', 'dns-servers': '2001:db8::53',
            'disabled': 'false', 'dynamic': 'false', 'invalid': 'false',
            'ra-lifetime': '30m', 'ra-interval': '3m20s-10m', 'ra-preference': 'high',
            'managed-address-configuration': 'false', 'other-configuration': 'true',
            'advertise-mac-address': 'true', 'hop-limit': '64', 'mtu': '1500',
            'reachable-time': 'unspecified', 'retransmit-interval': 'unspecified',
            'comment': 'keep my LAN settings', **extra}


def device(rows=None):
    session = RouterOS(nats=[])
    session.rows[ipv6_dns.IPV4_ADDRESSES] = [
        {'.id': '*A', 'address': '10.0.0.1/24', 'interface': 'bridge'}]
    session.rows[ipv6_dns.ND] = copy.deepcopy([nd_row()] if rows is None else rows)
    session.dhcp = DhcpDnsState(entries=[DhcpDnsEntry('*D', '10.0.0.0/24', ['10.0.0.2'])])
    driver = RouterOSDriver('https://10.0.0.1', 'admin', 'pw')
    driver.session = session
    driver.read_dhcp_dns = lambda: copy.deepcopy(session.dhcp)
    return session, driver


@pytest.mark.parametrize('advertise', ['yes', 'self'])
def test_disable_only_selected_lan_dns_and_restore_exact_setting(advertise):
    other = nd_row(**{'.id': '*2', 'interface': 'guest', 'dynamic': 'true'})
    wan = nd_row(**{'.id': '*3', 'interface': 'pppoe-out'})
    session, driver = device([nd_row(**{'advertise-dns': advertise}), other, wan])
    before = copy.deepcopy(session.rows[ipv6_dns.ND])
    plan = ipv6_dns.inspect(DESIRED, driver)
    assert plan['status'] == 'pending' and not plan['blockers']
    assert plan['interface'] == 'bridge' and plan['target_dns'] == '10.0.0.2'
    assert len(plan['ops']) == 1
    assert plan['items'][0]['dns_servers'] == ['2001:db8::53']
    undo = ipv6_dns.apply(driver, plan['ops'][0])
    ipv6_dns.verify(driver, plan['check'])
    expected = copy.deepcopy(before)
    expected[0]['advertise-dns'] = 'no'
    assert session.rows[ipv6_dns.ND] == expected
    assert session.writes == [('PATCH', ipv6_dns.ND, {'advertise-dns': 'no'})]
    assert undo['kind'] == 'ros_restore'
    driver.apply(undo)
    assert session.rows[ipv6_dns.ND] == before


@pytest.mark.parametrize('rows', [[], [nd_row(**{'advertise-dns': 'no'})],
                                  [nd_row(disabled='true')],
                                  [nd_row(interface='all', **{'advertise-dns': 'no'})]])
def test_no_dns_advertisement_is_idempotent_and_still_verified(rows):
    session, driver = device(rows)
    plan = ipv6_dns.inspect(DESIRED, driver)
    assert plan['status'] == 'synced' and not plan['ops'] and not plan['blockers']
    ipv6_dns.verify(driver, plan['check'])
    assert not session.writes


def test_unlinked_oxidns_preserves_settings_without_reading_router():
    session, driver = device()
    plan = ipv6_dns.inspect({**DESIRED, 'dns_agent_id': ''}, driver)
    assert plan['status'] == 'disabled' and plan['enabled'] is False
    assert not plan['ops'] and not session.reads
    ipv6_dns.verify(driver, plan['check'])


@pytest.mark.parametrize('advertise', ['yes', 'self'])
@pytest.mark.parametrize('disabled_explicit', [True, False])
def test_shared_all_dns_announcement_blocks_without_an_active_lan_override(advertise, disabled_explicit):
    rows = [nd_row(interface='all', **{'advertise-dns': advertise})]
    if disabled_explicit:
        rows.append(nd_row(**{'.id': '*2', 'disabled': 'true'}))
    session, driver = device(rows)
    plan = ipv6_dns.inspect(DESIRED, driver)
    assert plan['status'] == 'blocked' and 'all' in plan['blockers'][0]
    assert not plan['ops'] and not session.writes
    assert ipv6_dns.report(plan)['fix_command'] == plan['fix_command']
    if disabled_explicit:
        # Adding a second LAN row would only create a duplicate to sort out.
        assert plan['fix_command'] == ''
    else:
        assert plan['fix_command'] == (
            '/ipv6 nd add interface=bridge advertise-dns=no advertise-mac-address=yes '
            'dns-servers=2001:db8::53 hop-limit=64 managed-address-configuration=no mtu=1500 '
            'other-configuration=yes ra-interval=3m20s-10m ra-lifetime=30m ra-preference=high '
            'reachable-time=unspecified retransmit-interval=unspecified')


def test_override_command_quotes_values_and_skips_status_fields():
    rows = [nd_row(interface='all', default='true', **{'dns-servers': '', 'mtu': 'a "b" $c'})]
    session, driver = device(rows)
    session.rows[ipv6_dns.IPV4_ADDRESSES][0]['interface'] = 'lan bridge'
    command = ipv6_dns.inspect(DESIRED, driver)['fix_command']
    assert command.startswith('/ipv6 nd add interface="lan bridge" advertise-dns=no ')
    assert 'mtu="a \\"b\\" \\$c"' in command
    assert 'default=' not in command and 'comment=' not in command and 'disabled=' not in command
    assert 'dns-servers' not in command and 'invalid=' not in command


@pytest.mark.parametrize('shared_advertise', ['yes', 'self'])
@pytest.mark.parametrize('lan_advertise', ['yes', 'self', 'no'])
def test_active_lan_override_preserves_shared_all_dns_for_other_interfaces(shared_advertise, lan_advertise):
    shared = nd_row(**{'.id': '*2', 'interface': 'all', 'advertise-dns': shared_advertise})
    session, driver = device([nd_row(**{'advertise-dns': lan_advertise}), shared])
    plan = ipv6_dns.inspect(DESIRED, driver)
    assert not plan['blockers']
    assert plan['status'] == ('synced' if lan_advertise == 'no' else 'pending')
    assert plan['items'][1]['status'] == 'synced' and '共享' in plan['items'][1]['detail']
    for op in plan['ops']:
        ipv6_dns.apply(driver, op)
    ipv6_dns.verify(driver, plan['check'])
    assert session.rows[ipv6_dns.ND][0]['advertise-dns'] == 'no'
    assert session.rows[ipv6_dns.ND][1] == shared
    assert all(values == {'advertise-dns': 'no'} for _, _, values in session.writes)


def test_disabled_all_row_does_not_block_an_explicit_lan_and_is_preserved():
    shared = nd_row(**{'.id': '*2', 'interface': 'all', 'disabled': 'true'})
    session, driver = device([nd_row(), shared])
    plan = ipv6_dns.inspect(DESIRED, driver)
    ipv6_dns.apply(driver, plan['ops'][0])
    ipv6_dns.verify(driver, plan['check'])
    assert session.rows[ipv6_dns.ND][1] == shared


@pytest.mark.parametrize('setting', [
    {'dns': '2001:db8::53'}, {'dns': ['2001:db8::53', '2001:db8::54']},
    {'dns': '2001:db8::53', 'dns-servers': '2001:db8::54'},
])
def test_current_and_legacy_dns_field_names_are_read_but_preserved(setting):
    row = nd_row(**setting)
    if 'dns-servers' not in setting:
        del row['dns-servers']
    session, driver = device([row])
    plan = ipv6_dns.inspect(DESIRED, driver)
    expected = ['2001:db8::53'] if setting == {'dns': '2001:db8::53'} else ['2001:db8::53', '2001:db8::54']
    assert plan['items'][0]['dns_servers'] == expected
    ipv6_dns.apply(driver, plan['ops'][0])
    ipv6_dns.verify(driver, plan['check'])
    assert session.rows[ipv6_dns.ND][0] == {**row, 'advertise-dns': 'no'}
    assert session.writes == [('PATCH', ipv6_dns.ND, {'advertise-dns': 'no'})]


@pytest.mark.parametrize('extra', [
    {'advertise-dns': 'auto'}, {'advertise-dns': None}, {'dynamic': 'true'}, {'invalid': 'true'},
    {'disabled': 'unknown'}, {'dynamic': 'unknown'}, {'invalid': 'unknown'},
    {'dns-servers': 'unknown'}, {'.id': ''}, {'interface': ''},
])
def test_unknown_or_noneditable_nd_state_blocks(extra):
    session, driver = device([nd_row(**extra)])
    plan = ipv6_dns.inspect(DESIRED, driver)
    assert plan['status'] == 'blocked' and plan['blockers'] and not plan['ops']
    assert not session.writes


def test_duplicate_lan_entries_are_not_silently_selected():
    _, driver = device([nd_row(), nd_row(**{'.id': '*2'})])
    assert ipv6_dns.inspect(DESIRED, driver)['status'] == 'blocked'


def test_actual_interface_is_used_and_two_lan_candidates_block():
    session, driver = device()
    session.rows[ipv6_dns.IPV4_ADDRESSES][0].update(interface='ether2', **{'actual-interface': 'bridge'})
    assert ipv6_dns.inspect(DESIRED, driver)['interface'] == 'bridge'
    session.rows[ipv6_dns.IPV4_ADDRESSES].append(
        {'.id': '*B', 'address': '10.0.0.8/24', 'interface': 'guest'})
    plan = ipv6_dns.inspect(DESIRED, driver)
    assert plan['status'] == 'blocked' and plan['interface'] == '' and not plan['ops']


@pytest.mark.parametrize('extra', [
    {'invalid': 'true'}, {'invalid': 'unknown'}, {'disabled': 'unknown'},
    {'address': 'not-an-address'}, {'interface': ['bridge']},
])
def test_invalid_or_unknown_lan_address_cannot_authorize_an_nd_write(extra):
    session, driver = device()
    session.rows[ipv6_dns.IPV4_ADDRESSES][0].update(extra)
    plan = ipv6_dns.inspect(DESIRED, driver)
    assert plan['status'] == 'blocked' and plan['interface'] == ''
    assert not plan['ops'] and not session.writes


@pytest.mark.parametrize('change', [
    {'dns_server': '::1'}, {'dns_server': ''}, {'dns_error': 'agent offline'},
    {'ipv6_dns_scope': {}}, {'proxy_gateway': '10.1.0.4'},
])
def test_invalid_dns_selection_or_scope_is_not_accepted(change):
    session, driver = device()
    plan = ipv6_dns.inspect({**DESIRED, **change}, driver)
    assert plan['status'] == 'blocked' and not plan['ops'] and not session.writes


def test_unreadable_nd_is_reported_without_a_write(monkeypatch):
    session, driver = device()
    original = driver._list

    def read(path):
        if path == ipv6_dns.ND:
            raise RouterError('读取 IPv6 ND 被拒绝')
        return original(path)

    monkeypatch.setattr(driver, '_list', read)
    plan = ipv6_dns.inspect(DESIRED, driver)
    assert plan['status'] == 'blocked' and '被拒绝' in plan['message']
    assert not session.writes


def test_preview_allows_pending_dhcp_but_write_requires_dns_already_applied():
    session, driver = device()
    session.dhcp.entries[0].dns = ['10.0.0.1']
    plan = ipv6_dns.inspect(DESIRED, driver)
    assert plan['status'] == 'pending'
    with pytest.raises(RouterError, match='DHCP DNS'):
        ipv6_dns.apply(driver, plan['ops'][0])
    assert not session.writes
    session.dhcp.entries[0].dns = ['10.0.0.2']
    ipv6_dns.apply(driver, plan['ops'][0])
    ipv6_dns.verify(driver, plan['check'])


def test_dhcp_target_changes_after_preview_block_the_nd_write():
    session, driver = device()
    plan = ipv6_dns.inspect(DESIRED, driver)
    session.dhcp.entries.append(DhcpDnsEntry('*D2', '10.0.0.0/25', ['10.0.0.2']))
    with pytest.raises(RouterError, match='目标网段'):
        ipv6_dns.apply(driver, plan['ops'][0])
    assert not session.writes


@pytest.mark.parametrize('change', ['lan', 'lifetime', 'new_all'])
def test_preview_is_invalidated_when_lan_or_nd_configuration_changes(change):
    session, driver = device()
    plan = ipv6_dns.inspect(DESIRED, driver)
    if change == 'lan':
        session.rows[ipv6_dns.IPV4_ADDRESSES][0]['interface'] = 'ether9'
    elif change == 'lifetime':
        session.rows[ipv6_dns.ND][0]['ra-lifetime'] = '1h'
    else:
        session.rows[ipv6_dns.ND].append(nd_row(**{'.id': '*2', 'interface': 'all'}))
    with pytest.raises(RouterError):
        ipv6_dns.apply(driver, plan['ops'][0])
    assert not session.writes


def test_last_moment_nd_change_is_protected_by_transaction_expected_snapshot(monkeypatch):
    session, driver = device()
    plan = ipv6_dns.inspect(DESIRED, driver)
    transaction = driver._transaction

    def race(changes):
        session.rows[ipv6_dns.ND][0]['ra-lifetime'] = '1h'
        return transaction(changes)

    monkeypatch.setattr(driver, '_transaction', race)
    with pytest.raises(RouterError, match='外部变化'):
        ipv6_dns.apply(driver, plan['ops'][0])
    assert session.rows[ipv6_dns.ND][0]['advertise-dns'] == 'yes'
    assert session.rows[ipv6_dns.ND][0]['ra-lifetime'] == '1h'
    assert not session.writes


def test_verify_detects_non_dns_ra_changes_after_a_successful_write():
    session, driver = device()
    plan = ipv6_dns.inspect(DESIRED, driver)
    ipv6_dns.apply(driver, plan['ops'][0])
    session.rows[ipv6_dns.ND][0]['ra-preference'] = 'low'
    with pytest.raises(RouterError, match='其他通告设置'):
        ipv6_dns.verify(driver, plan['check'])


def test_noop_verification_detects_new_dns_announcement():
    session, driver = device([])
    plan = ipv6_dns.inspect(DESIRED, driver)
    session.rows[ipv6_dns.ND].append(nd_row())
    with pytest.raises(RouterError):
        ipv6_dns.verify(driver, plan['check'])


def test_rollback_does_not_overwrite_an_external_non_dns_change():
    session, driver = device()
    plan = ipv6_dns.inspect(DESIRED, driver)
    undo = ipv6_dns.apply(driver, plan['ops'][0])
    session.rows[ipv6_dns.ND][0]['ra-lifetime'] = '1h'
    with pytest.raises(RouterRollbackError, match='外部变化'):
        driver.apply(undo)
    assert session.rows[ipv6_dns.ND][0]['ra-lifetime'] == '1h'
    assert session.rows[ipv6_dns.ND][0]['advertise-dns'] == 'no'


def test_report_does_not_expose_write_operations_or_internal_snapshots():
    _, driver = device()
    plan = ipv6_dns.inspect(DESIRED, driver)
    report = ipv6_dns.report(plan)
    assert not {'ops', 'check', 'evidence'} & report.keys()
    report['items'][0]['dns_servers'].append('::1')
    assert plan['items'][0]['dns_servers'] == ['2001:db8::53']
