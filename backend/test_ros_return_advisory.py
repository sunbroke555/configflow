"""Forward-filter advisories must preserve existing plans and publish requests."""
import copy
import json
from pathlib import Path

import pytest

from backend.routers.plan import build_plan
from backend.routers.release import execute
from backend.routers.report import build_report
from backend.test_router_return_routing import NoAgentCalls
from backend.test_router_release import DESIRED
from backend.test_routeros_release import driver_for, release_session

FILTER = '/ip/firewall/filter'
FIXTURE = Path(__file__).with_name('testdata') / 'ros_return_advisory_unchanged.json'
ROUTER = {'type': 'routeros', 'endpoint': 'http://10.0.0.1', 'manage_return_routing': False}
HARMLESS = {
    'empty': [],
    'dns_only': [
        {'.id': '*D1', 'chain': 'forward', 'action': 'drop', 'protocol': 'udp', 'dst-port': '853',
         'in-interface': 'ether1', 'comment': 'cf-dnsguard:dot', 'disabled': 'false'},
        {'.id': '*D2', 'chain': 'forward', 'action': 'reject', 'protocol': 'tcp', 'dst-port': '853',
         'reject-with': 'tcp-reset', 'in-interface': 'ether1', 'comment': 'cf-dnsguard:dot', 'disabled': 'false'},
    ],
    'fasttrack_only': [
        {'.id': '*F', 'chain': 'forward', 'action': 'fasttrack-connection',
         'connection-state': 'established,related'},
        {'.id': '*A', 'chain': 'forward', 'action': 'accept', 'connection-state': 'established,related'},
    ],
    'disabled_and_input': [
        {'.id': '*X', 'chain': 'forward', 'action': 'drop', 'connection-state': 'invalid', 'disabled': 'true'},
        {'.id': '*I', 'chain': 'input', 'action': 'drop', 'connection-state': 'invalid'},
    ],
}


def preview_and_publish(filters):
    session = release_session()
    session.rows[FILTER] = copy.deepcopy(filters)
    driver = driver_for(session)
    state = driver.read_state()
    desired = copy.deepcopy(DESIRED)
    plan = build_plan(ROUTER, desired, state, build_report(desired, state), {'status': 'disabled'}, driver)
    assert not session.writes
    result = execute(plan, driver, NoAgentCalls())
    assert result['ok'], result
    return {'plan': plan, 'requests': session.writes}


@pytest.mark.parametrize('name', HARMLESS)
def test_harmless_filters_preserve_entire_legacy_plan_fingerprint_and_publish_requests(name):
    # Captured on upstream/main before the advisory implementation was added.
    expected = json.loads(FIXTURE.read_text())
    actual = json.loads(json.dumps(preview_and_publish(HARMLESS[name])))
    assert actual == expected


@pytest.mark.parametrize('action', ['drop', 'reject'])
@pytest.mark.parametrize('state', ['invalid', 'invalid,new'])
def test_invalid_advisory_identifies_rule_and_only_changes_warnings(action, state):
    filters = [{'.id': '*B', 'chain': 'forward', 'action': action,
                'connection-state': state, 'comment': '拦截无效连接', 'disabled': 'false'}]
    actual = json.loads(json.dumps(preview_and_publish(filters)))
    expected = json.loads(FIXTURE.read_text())
    [warning] = actual['plan']['warnings']
    assert '*B' in warning and '拦截无效连接' in warning
    assert '约 6 秒' in warning and 'syn-sent' in warning and '建议开启「自动配置回流」' in warning
    actual['plan']['warnings'] = []
    assert actual == expected  # Same operations, fingerprint, blockers, script and writes.


@pytest.mark.parametrize('accept_new', [False, True])
def test_strict_state_firewall_advisory_names_accept_and_final_drop(accept_new):
    filters = [{'.id': '*A', 'chain': 'forward', 'action': 'accept',
                'connection-state': 'established,related'}]
    if accept_new:
        filters.append({'.id': '*N', 'chain': 'forward', 'action': 'accept', 'connection-state': 'new'})
    filters.append({'.id': '*D', 'chain': 'forward', 'action': 'drop', 'comment': '丢弃其余'})
    actual = preview_and_publish(filters)
    [warning] = actual['plan']['warnings']
    assert '*A' in warning and '*D' in warning and '丢弃其余' in warning
    assert not actual['plan']['blockers']


def test_return_enabled_does_not_read_filter_or_add_the_advisory(monkeypatch):
    session = release_session()
    driver = driver_for(session)
    state = driver.read_state()
    from backend.test_router_release import SAFE_RETURN_STATUS
    from backend.routers import ros_return_advisory
    monkeypatch.setattr(ros_return_advisory, 'warnings_for', lambda *args: pytest.fail('Return routing is enabled'))
    plan = build_plan({**ROUTER, 'manage_return_routing': True}, DESIRED, state,
                      build_report(DESIRED, state), SAFE_RETURN_STATUS, driver)
    assert not any('syn-sent' in warning for warning in plan['warnings'])


INVALID = {'.id': '*B', 'chain': 'forward', 'action': 'drop', 'connection-state': 'invalid'}
ESTABLISHED = {'.id': '*A', 'chain': 'forward', 'action': 'accept', 'connection-state': 'established,related'}


def scoped_warning(filters, address_sets=None):
    from backend.routers.ros_return_advisory import warnings_for
    session = release_session()
    session.rows[FILTER] = copy.deepcopy(filters)
    session.rows['/ip/route'].append({'.id': '*LAN', 'dst-address': '10.0.0.0/24', 'gateway': 'bridge',
                                    'immediate-gw': 'bridge', 'connect': 'true', 'active': 'true', 'dynamic': 'true'})
    session.rows['/interface/list'] = [{'name': 'LAN'}, {'name': 'WAN'}]
    session.rows['/interface/list/member'] = [{'list': 'LAN', 'interface': 'bridge'}, {'list': 'WAN', 'interface': 'ether1'}]
    driver = driver_for(session)
    state = driver.read_state()
    state.address_sets.update(address_sets or {})
    before = copy.deepcopy(state.nat_context)
    result = warnings_for(DESIRED, state, driver)
    assert state.nat_context == before and not session.writes
    assert not any('/ip/firewall/address-list?list=' in path for method, path, _ in session.calls
                   if method == 'GET' and path.endswith('blocklist'))
    return result


@pytest.mark.parametrize('extra', [
    {'disabled': 'true'}, {'disabled': True}, {'disabled': 'yes'}, {'invalid': 'true'},
    {'chain': 'input'}, {'chain': 'custom'}, {'connection-state': '!invalid'},
    {'connection-state': 'new'}, {'connection-state': 'untracked'},
    {'src-address': '172.16.0.0/16'}, {'src-address': '!10.0.0.0/24'},
    {'src-address': '10.0.0.4/32'}, {'dst-address': '8.8.8.8'},
    {'in-interface': 'ether1'}, {'out-interface': 'ether1'},
    {'in-interface-list': 'WAN'}, {'out-interface-list': 'WAN'},
    {'in-interface-list': '!LAN'}, {'out-interface-list': '!LAN'},
    {'src-address-type': 'local'}, {'dst-address-type': 'local'},
])
def test_rules_proved_outside_lan_proxy_scope_do_not_warn(extra):
    assert scoped_warning([{**INVALID, **extra}]) == []


@pytest.mark.parametrize('extra', [
    {'in-interface': 'bridge'}, {'in-interface-list': 'LAN'},
    {'src-address': '10.0.0.0/25'}, {'src-address': '!172.16.0.0/16'},
    {'dst-address': '198.18.0.0/16'}, {'src-address-list': 'unknown-blocklist'},
    {'connection-state': 'new,invalid'}, {'dynamic': 'true'}, {'protocol': 'tcp', 'dst-port': '443'},
])
def test_rules_which_may_match_lan_warn_without_modifying_evidence(extra):
    assert len(scoped_warning([{**INVALID, **extra}])) == 1


@pytest.mark.parametrize('values,negate,expected', [([], False, False), ([], True, True),
    (['172.16.0.0/16'], False, False), (['10.0.0.0/24'], True, False),
    (['10.0.0.0/24'], False, True), (['not-an-ip'], False, True)])
def test_known_address_list_scope(values, negate, expected):
    rules = [{**INVALID, 'src-address-list': ('!' if negate else '') + 'clients'}]
    assert bool(scoped_warning(rules, {'clients': values})) is expected


@pytest.mark.parametrize('prefix', [
    [{'chain': 'forward', 'action': 'accept'}],
    [{'chain': 'forward', 'action': 'accept', 'src-address': '10.0.0.0/24'}],
    [{'chain': 'forward', 'action': 'accept', 'in-interface-list': 'LAN'}],
])
def test_prior_complete_accept_shadows_invalid_filter(prefix):
    assert scoped_warning(prefix + [INVALID]) == []


@pytest.mark.parametrize('filters', [
    [INVALID, ESTABLISHED],  # Explicit invalid matches independently of order.
    [ESTABLISHED, {'chain': 'forward', 'action': 'drop', 'connection-state': '!established,related'}],
    [ESTABLISHED, {'chain': 'forward', 'action': 'reject', 'protocol': 'tcp'}],
])
def test_ordered_state_filter_variants(filters):
    assert len(scoped_warning(filters)) == 1


@pytest.mark.parametrize('filters', [
    [{'chain': 'forward', 'action': 'drop'}, ESTABLISHED],
    [{**ESTABLISHED, 'disabled': 'true'}, {'chain': 'forward', 'action': 'drop'}],
    [ESTABLISHED, {'chain': 'forward', 'action': 'drop', 'protocol': 'tcp', 'dst-port': '853'}],
    [ESTABLISHED, {'chain': 'forward', 'action': 'drop', 'in-interface-list': 'WAN'}],
    [ESTABLISHED, {'chain': 'forward', 'action': 'drop', 'connection-state': 'new'}],
])
def test_not_a_lan_default_deny_state_pattern(filters):
    assert scoped_warning(filters) == []


def test_unreadable_filter_does_not_block_plan_or_change_fingerprint():
    session = release_session()
    session.read_failures.add(FILTER)
    driver = driver_for(session)
    state = driver.read_state()
    plan = build_plan(ROUTER, DESIRED, state, build_report(DESIRED, state), {'status': 'disabled'}, driver)
    assert plan == json.loads(FIXTURE.read_text())['plan']


def test_policy_snapshot_is_reused_without_an_extra_filter_request():
    from backend.routers.ros_return_advisory import warnings_for
    session = release_session()
    driver = driver_for(session)
    state = driver.read_state()
    state.ros_policy = {FILTER: [copy.deepcopy(INVALID)]}
    before = copy.deepcopy(state.ros_policy)
    assert len(warnings_for(DESIRED, state, driver)) == 1
    assert state.ros_policy == before
    assert not any(method == 'GET' and path == FILTER for method, path, _ in session.calls)


def test_filter_cache_is_reused_within_preview_and_reset_by_next_read():
    from backend.routers.ros_return_advisory import warnings_for
    session = release_session()
    session.rows[FILTER] = [copy.deepcopy(INVALID)]
    driver = driver_for(session)
    state = driver.read_state()
    driver._list(FILTER)  # NAT ownership/DNS guard may already need this table.
    calls = len(session.calls)
    assert len(warnings_for(DESIRED, state, driver)) == 1
    assert len(session.calls) == calls
    session.rows[FILTER] = []
    state = driver.read_state()
    assert warnings_for(DESIRED, state, driver) == []
    assert session.calls[-1] == ('GET', FILTER, None)


if __name__ == '__main__':
    FIXTURE.write_text(json.dumps(preview_and_publish([]), ensure_ascii=False, indent=2) + '\n')
