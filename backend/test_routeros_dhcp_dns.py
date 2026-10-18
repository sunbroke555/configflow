"""RouterOS DHCP DNS patches preserve unrelated configuration and compensate failure."""
import copy
from urllib.parse import unquote, urlsplit

import pytest
import requests

from backend.routers.base import RouterError, RouterRollbackError, as_dict
from backend.routers.routeros import (
    DHCP_LEASES, DHCP_MATCHERS, DHCP_NETWORKS, DHCP_OPTIONS, DHCP_OPTION_SETS,
    DHCP_SERVERS, RouterOSDriver,
)


NETWORK = {'.id': '*A', 'address': '192.168.0.0/24', 'gateway': '192.168.0.1',
           'netmask': '24', 'dns-server': '1.1.1.1,8.8.8.8', 'domain': 'home',
           'ntp-server': '192.168.0.1', 'comment': 'LAN'}
OTHER_NETWORK = {'.id': '*B', 'address': '192.168.10.0/24', 'gateway': '192.168.10.1',
                 'dns-server': '9.9.9.9'}
SERVER = {'.id': '*S', 'name': 'dhcp1', 'interface': 'lan4', 'address-pool': 'poolLAN',
          'lease-time': '3h', 'disabled': 'false', 'use-radius': 'no'}


class Response:
    status_code = 200

    def __init__(self, value):
        self.value = value

    def json(self):
        return copy.deepcopy(self.value)


class DHCPREST:
    """Fake only the DHCP REST menus; fail on writes outside the two DNS fields."""

    def __init__(self):
        self.rows = {DHCP_NETWORKS: [copy.deepcopy(NETWORK), copy.deepcopy(OTHER_NETWORK)],
                     DHCP_SERVERS: [copy.deepcopy(SERVER)], DHCP_LEASES: [], DHCP_OPTIONS: [],
                     DHCP_OPTION_SETS: [], DHCP_MATCHERS: []}
        self.calls, self.writes = [], []
        self.fail_get = None
        self.fail_get_after_write = False
        self.lose_first_reply = False
        self.ignore_first_write = False

    def get(self, url, **_):
        path = unquote(urlsplit(url).path).removeprefix('/rest')
        self.calls.append(('GET', path))
        if path == self.fail_get or self.fail_get_after_write and self.writes:
            raise requests.Timeout('unavailable evidence')
        assert path in self.rows
        return Response(self.rows[path])

    def patch(self, url, json, **_):
        path = unquote(urlsplit(url).path).removeprefix('/rest')
        assert path.startswith(DHCP_NETWORKS + '/')
        row_id = path.removeprefix(DHCP_NETWORKS + '/')
        row = next(row for row in self.rows[DHCP_NETWORKS] if row['.id'] == row_id)
        assert set(json) <= {'dns-server', 'dns-none'}, 'Only DHCP DNS may be changed'
        assert json.get('dns-none', 'false') in ('true', 'false', 'yes', 'no'), 'Empty bool is invalid'
        self.writes.append((row_id, copy.deepcopy(json)))
        if not (self.ignore_first_write and len(self.writes) == 1):
            for key, value in json.items():
                # RouterOS may omit unset/empty/default fields when read back.
                if value == '' or key == 'dns-none' and value == 'false':
                    row.pop(key, None)
                else:
                    row[key] = value
        if self.lose_first_reply and len(self.writes) == 1:
            raise requests.Timeout('reply lost after mutation')
        return Response(row)

    def close(self):
        pass


def setup_driver():
    session = DHCPREST()
    driver = RouterOSDriver('http://192.168.0.1', 'admin', 'password')
    driver.session = session
    return driver, session


def operation(driver, dns=None):
    entry = driver.read_dhcp_dns().entries[0]
    return {'kind': 'dhcp_dns_update', 'id': entry.id, 'dns': dns or ['192.168.0.4'],
            'scope': {'id': entry.id, 'network': entry.network, 'gateway': '192.168.0.3'},
            'expected': entry.attributes, 'summary': '更新 DHCP DNS'}


def test_reads_dhcp_dns_and_only_patches_selected_network_dns_fields():
    driver, session = setup_driver()
    before = copy.deepcopy(session.rows)
    state = driver.read_dhcp_dns()
    assert [(entry.network, entry.dns) for entry in state.entries] == [
        ('192.168.0.0/24', ['1.1.1.1', '8.8.8.8']), ('192.168.10.0/24', ['9.9.9.9'])]
    assert all(entry.enabled and not entry.blockers for entry in state.entries)
    assert not session.writes

    undo = driver.apply(operation(driver))
    assert session.writes == [('*A', {'dns-server': '192.168.0.4', 'dns-none': 'false'})]
    assert driver.read_dhcp_dns().entries[0].dns == ['192.168.0.4']
    changed = session.rows[DHCP_NETWORKS][0]
    assert {key: value for key, value in changed.items() if key != 'dns-server'} == {
        key: value for key, value in NETWORK.items() if key != 'dns-server'}
    assert session.rows[DHCP_NETWORKS][1] == OTHER_NETWORK
    assert session.rows[DHCP_SERVERS] == before[DHCP_SERVERS]
    driver.apply(undo)
    assert session.rows == before


def test_rollback_restores_unset_dns_and_default_boolean_without_invalid_empty_bool():
    driver, session = setup_driver()
    session.rows[DHCP_NETWORKS][0].pop('dns-server')
    before = copy.deepcopy(session.rows)
    undo = driver.apply(operation(driver))
    assert driver.read_dhcp_dns().entries[0].dns == ['192.168.0.4']
    driver.apply(undo)
    assert session.writes[-1][1] == {'dns-server': '', 'dns-none': 'false'}
    assert session.rows == before
    assert driver.read_dhcp_dns().entries[0].dns == []


def test_dns_none_suppresses_explicit_dns_then_is_cleared_and_restored():
    driver, session = setup_driver()
    session.rows[DHCP_NETWORKS][0].update({'dns-server': '192.168.0.4', 'dns-none': 'true'})
    before = copy.deepcopy(session.rows)
    entry = driver.read_dhcp_dns().entries[0]
    assert entry.dns == [] and entry.attributes['dns-server'] == '192.168.0.4'
    assert not entry.blockers
    undo = driver.apply(operation(driver))
    assert driver.read_dhcp_dns().entries[0].dns == ['192.168.0.4']
    driver.apply(undo)
    assert session.rows == before


@pytest.mark.parametrize('failure', ['lose_first_reply', 'ignore_first_write'])
def test_failed_or_unverified_patch_restores_original_dns(failure):
    driver, session = setup_driver()
    before = copy.deepcopy(session.rows)
    op = operation(driver)
    setattr(session, failure, True)
    with pytest.raises(RouterError):
        driver.apply(op)
    assert session.rows == before


def test_failed_reads_after_patch_preserve_unverified_dns_and_report_incomplete_rollback():
    driver, session = setup_driver()
    session.rows[DHCP_NETWORKS][0]['dns-none'] = 'true'
    before = copy.deepcopy(session.rows)
    op = operation(driver)
    session.fail_get_after_write = True
    with pytest.raises(RouterRollbackError, match='回滚未完成'):
        driver.apply(op)
    assert session.writes == [
        ('*A', {'dns-server': '192.168.0.4', 'dns-none': 'false'}),
    ]
    assert session.rows != before
    assert session.rows[DHCP_NETWORKS][0]['dns-server'] == '192.168.0.4'


def test_preview_drift_blocks_before_writing_and_option_drift_is_rechecked():
    driver, session = setup_driver()
    op = operation(driver)
    session.rows[DHCP_NETWORKS][0]['gateway'] = '192.168.0.99'
    with pytest.raises(RouterError, match='预览后发生变化'):
        driver.apply(op)
    assert session.writes == []
    op = operation(driver)
    session.rows[DHCP_SERVERS][0]['dhcp-option-set'] = 'custom-dns'
    session.rows[DHCP_OPTION_SETS] = [{'name': 'custom-dns', 'options': 'dns-six'}]
    session.rows[DHCP_OPTIONS] = [{'name': 'dns-six', 'code': '6', 'value': '0x01010101'}]
    with pytest.raises(RouterError, match='选项 6'):
        driver.apply(op)
    assert session.writes == []


def test_new_overlapping_network_blocks_write_even_when_target_attributes_are_unchanged():
    driver, session = setup_driver()
    op = operation(driver)
    session.rows[DHCP_NETWORKS].append({'.id': '*C', 'address': '192.168.0.0/25'})
    assert driver.read_dhcp_dns().entries[0].attributes == op['expected']
    with pytest.raises(RouterError, match='目标网段发生变化'):
        driver.apply(op)
    assert session.writes == []


@pytest.mark.parametrize('scope', [None, {}, {'id': '*A'},
                                  {'id': '*A', 'network': '192.168.0.0/24', 'gateway': 123},
                                  {'id': '*B', 'network': '192.168.0.0/24', 'gateway': '192.168.0.3'}])
def test_missing_or_inconsistent_scope_blocks_before_writing(scope):
    driver, session = setup_driver()
    op = operation(driver)
    if scope is None:
        op.pop('scope')
    else:
        op['scope'] = scope
    with pytest.raises(RouterError, match='目标范围'):
        driver.apply(op)
    assert session.writes == []


@pytest.mark.parametrize('level', ['network', 'server', 'lease', 'matcher'])
def test_custom_dns_option_at_every_supported_level_blocks_network_only_write(level):
    driver, session = setup_driver()
    session.rows[DHCP_OPTIONS] = [{'name': 'dns-six', 'code': '6', 'value': '0x01010101'}]
    session.rows[DHCP_OPTION_SETS] = [{'name': 'custom-dns', 'options': 'dns-six'}]
    if level == 'network':
        session.rows[DHCP_NETWORKS][0]['dhcp-option'] = 'dns-six'
    elif level == 'server':
        session.rows[DHCP_SERVERS][0]['dhcp-option-set'] = 'custom-dns'
    elif level == 'lease':
        session.rows[DHCP_LEASES] = [{'address': '192.168.0.10', 'server': 'dhcp1',
                                     'dhcp-option-set': 'custom-dns'}]
    else:
        session.rows[DHCP_MATCHERS] = [{'name': 'special-client', 'server': 'dhcp1',
                                       'option-set': 'custom-dns'}]
    assert any('选项 6' in message for message in driver.read_dhcp_dns().entries[0].blockers)
    with pytest.raises(RouterError, match='选项 6'):
        driver.apply(operation(driver))
    assert not session.writes


def test_unrelated_fixed_lease_and_non_dns_options_do_not_block_selected_network():
    driver, session = setup_driver()
    session.rows[DHCP_OPTIONS] = [{'name': 'dns-six', 'code': '6'}, {'name': 'ntp', 'code': '42'}]
    session.rows[DHCP_OPTION_SETS] = [{'name': 'ntp-set', 'options': 'ntp'}]
    session.rows[DHCP_SERVERS][0]['dhcp-option-set'] = 'ntp-set'
    session.rows[DHCP_LEASES] = [{'address': '192.168.10.10', 'dhcp-option': 'dns-six'}]
    entries = driver.read_dhcp_dns().entries
    assert not entries[0].blockers and entries[1].blockers
    driver.apply(operation(driver))
    assert session.writes


@pytest.mark.parametrize('radius', ['yes', True, 'unexpected'])
def test_radius_or_unknown_radius_state_cannot_claim_network_dns_wins(radius):
    driver, session = setup_driver()
    session.rows[DHCP_SERVERS][0]['use-radius'] = radius
    assert any('RADIUS' in message for message in driver.read_dhcp_dns().entries[0].blockers)


def test_radius_accounting_only_is_not_a_dns_override():
    driver, session = setup_driver()
    session.rows[DHCP_SERVERS][0]['use-radius'] = 'accounting'
    assert not driver.read_dhcp_dns().entries[0].blockers


@pytest.mark.parametrize('kind', ['missing-option', 'unknown-code', 'missing-set', 'incomplete-set'])
def test_unresolvable_option_evidence_blocks_instead_of_assuming_safe(kind):
    driver, session = setup_driver()
    if kind in ('missing-option', 'unknown-code'):
        session.rows[DHCP_NETWORKS][0]['dhcp-option'] = 'custom'
        if kind == 'unknown-code':
            session.rows[DHCP_OPTIONS] = [{'name': 'custom', 'code': 'future-option'}]
    else:
        session.rows[DHCP_NETWORKS][0]['dhcp-option-set'] = 'custom'
        if kind == 'incomplete-set':
            session.rows[DHCP_OPTION_SETS] = [{'name': 'custom'}]
    assert driver.read_dhcp_dns().entries[0].blockers
    with pytest.raises(RouterError):
        driver.apply(operation(driver))
    assert not session.writes


@pytest.mark.parametrize('code', [True, False, 42.0, 42.5, float('inf'), float('-inf'),
                                 '42.0', '4.2e1', 'future-option'])
def test_non_integer_option_codes_are_unknown_and_block_before_writing(code):
    driver, session = setup_driver()
    session.rows[DHCP_NETWORKS][0]['dhcp-option'] = 'custom'
    session.rows[DHCP_OPTIONS] = [{'name': 'custom', 'code': code}]
    assert any('编号未知' in message for message in driver.read_dhcp_dns().entries[0].blockers)
    with pytest.raises(RouterError, match='编号未知'):
        driver.apply(operation(driver))
    assert session.writes == []


@pytest.mark.parametrize('path', [DHCP_SERVERS, DHCP_LEASES, DHCP_OPTIONS, DHCP_OPTION_SETS, DHCP_MATCHERS])
def test_unavailable_override_evidence_fails_closed(path):
    driver, session = setup_driver()
    session.fail_get = path
    with pytest.raises(RouterError):
        driver.read_dhcp_dns()
    assert not session.writes


def test_lease_renewals_and_ordinary_dynamic_leases_do_not_change_dns_evidence():
    driver, session = setup_driver()
    session.rows[DHCP_LEASES] = [{'address': '192.168.0.10', 'dynamic': 'true',
                                 'expires-after': '1h', 'last-seen': '1m'}]
    before = as_dict(driver.read_dhcp_dns())
    session.rows[DHCP_LEASES][0].update({'expires-after': '59m', 'last-seen': '2m'})
    session.rows[DHCP_LEASES].append({'address': '192.168.0.12', 'dynamic': 'true', 'expires-after': '3h'})
    assert as_dict(driver.read_dhcp_dns()) == before


@pytest.mark.parametrize('field', ['dynamic', 'invalid'])
def test_uneditable_network_is_reported_and_not_written(field):
    driver, session = setup_driver()
    session.rows[DHCP_NETWORKS][0][field] = 'true'
    assert driver.read_dhcp_dns().entries[0].blockers
    with pytest.raises(RouterError):
        driver.apply(operation(driver))
    assert not session.writes


@pytest.mark.parametrize('change', [{'.id': ''}, {'address': 'not-a-network'}, {'dns-none': 'unknown'},
                                  {'dns-none': 1}])
def test_malformed_network_evidence_is_not_silently_ignored(change):
    driver, session = setup_driver()
    session.rows[DHCP_NETWORKS][0].update(change)
    with pytest.raises(RouterError):
        driver.read_dhcp_dns()
