"""IPv4 same-port forward hint: RouterOS-only, read-only, never part of a release."""
import copy

import pytest

from backend.routers import forward_hint
from backend.routers.plan import build_plan
from backend.routers.report import build_report
from backend.test_router_release import DESIRED, SAFE_RETURN_STATUS
from backend.test_routeros_release import driver_for, release_session

FILTER = '/ip/firewall/filter'
ROUTER = {'type': 'routeros', 'endpoint': 'http://10.0.0.1', 'manage_return_routing': False}
# RouterOS 7.25 default IPv4 forward chain, with the fasttrack counter rule it adds.
DEFCONF = [
    {'.id': '*1', 'chain': 'forward', 'action': 'passthrough', 'dynamic': 'true',
     'comment': 'special dummy rule to show fasttrack counters'},
    {'.id': '*2', 'chain': 'input', 'action': 'drop', 'connection-state': 'invalid', 'comment': 'defconf: drop invalid'},
    {'.id': '*3', 'chain': 'forward', 'action': 'accept', 'ipsec-policy': 'in,ipsec', 'comment': 'defconf: accept in ipsec policy'},
    {'.id': '*4', 'chain': 'forward', 'action': 'accept', 'ipsec-policy': 'out,ipsec', 'comment': 'defconf: accept out ipsec policy'},
    {'.id': '*5', 'chain': 'forward', 'action': 'fasttrack-connection', 'connection-state': 'established,related',
     'hw-offload': 'true', 'comment': 'defconf: fasttrack'},
    {'.id': '*6', 'chain': 'forward', 'action': 'accept', 'connection-state': 'established,related,untracked',
     'comment': 'defconf: accept established,related, untracked'},
    {'.id': '*7', 'chain': 'forward', 'action': 'drop', 'connection-state': 'invalid', 'comment': 'defconf: drop invalid'},
    {'.id': '*8', 'chain': 'forward', 'action': 'drop', 'connection-state': 'new', 'connection-nat-state': '!dstnat',
     'in-interface-list': 'WAN', 'comment': 'defconf: drop all from WAN not DSTNATed'},
]
SAME_PORT = {'.id': '*F', 'chain': 'forward', 'action': 'accept', 'in-interface': 'bridge', 'out-interface': 'bridge'}
COMMAND = ('/ip firewall filter add chain=forward action=accept in-interface=bridge out-interface=bridge '
           'comment="LAN to Mihomo via router" place-before=*7')


def session_with(filters):
    session = release_session()
    session.rows[FILTER] = copy.deepcopy(filters)
    session.rows['/ip/route'].append({'.id': '*LAN', 'dst-address': '10.0.0.0/24', 'gateway': 'bridge',
                                      'immediate-gw': 'bridge', 'connect': 'true', 'active': 'true', 'dynamic': 'true'})
    session.rows['/interface'] = [{'.id': '*I1', 'name': 'bridge'}, {'.id': '*I2', 'name': 'ether1'}]
    session.rows['/interface/list'] = [{'.id': '*L1', 'name': 'LAN'}, {'.id': '*L2', 'name': 'WAN'}]
    session.rows['/interface/list/member'] = [{'.id': '*M1', 'list': 'LAN', 'interface': 'bridge'},
                                              {'.id': '*M2', 'list': 'WAN', 'interface': 'ether1'}]
    return session


def preview(filters, router=ROUTER, desired=DESIRED, session=None):
    session = session or session_with(filters)
    driver = driver_for(session)
    state = driver.read_state()
    desired = copy.deepcopy(desired)
    report = build_report(desired, state)
    hint = forward_hint.ipv4(router, desired, state, report, driver)
    if hint:
        report['routes']['forward_hint'] = hint
    status = SAFE_RETURN_STATUS if router.get('manage_return_routing') else {'status': 'disabled'}
    plan = build_plan(router, desired, state, report, status, driver)
    assert not any(method != 'GET' for method, _, _ in session.writes)  # never writes, firewall included
    return hint, plan, session


def check(filters, targets=('198.18.0.0/16',), lan='bridge'):
    return forward_hint.check(driver_for(session_with(filters)), 4, lan, list(targets))


def test_default_firewall_drops_the_same_port_path_and_gets_one_command():
    hint, plan, _ = preview(DEFCONF)
    assert hint['status'] == 'drop' and hint['fix_command'] == COMMAND
    assert '卡几秒' in hint['warning'] and '重定向' in hint['warning'] and '恢复正常' in hint['warning']
    # Only a hint: the routes are still published, once in the warnings.
    assert not plan['blockers'] and any(stage['ops'] for stage in plan['stages'] if stage['key'] == 'routes')
    assert plan['warnings'].count(hint['warning']) == 1
    assert not any('syn-sent' in warning for warning in plan['warnings'])  # the older advisory says it no more


def test_ipv4_and_ipv6_share_one_explanation_without_a_fixed_delay():
    """VM151 lab (2026-10-31): IPv4 timed out; IPv6 with redirects barely stalled one-by-one."""
    ipv4, ipv6 = forward_hint.drop_warning(4), forward_hint.drop_warning(6)
    assert (ipv4, ipv6) == (forward_hint.IPV4_DROP, forward_hint.IPV6_DROP)
    for text in (ipv4, ipv6):
        assert '可能卡几秒甚至超时' in text and '只能部分缓解' in text and '不能依赖' in text
        assert 'drop invalid' in text and '恢复正常' in text
        assert '7 秒' not in text and '每个新连接' not in text and '偶尔' not in text
    assert 'ICMP 重定向' in ipv4 and 'ICMPv6 重定向' in ipv6
    assert 'IPv6 规则集路由' in ipv6 and 'IPv6' not in ipv4
    # Apart from the family names and the IPv6-only coverage note, the wording is identical.
    assert ipv6.replace('IPv6', 'IPv4').replace('ICMPv6', 'ICMP').replace(
        '一条命令同时覆盖 IPv4 Fake-IP 和 IPv4 规则集路由；', '') == ipv4


def test_hint_changes_no_operation_or_fingerprint():
    _, with_hint, _ = preview(DEFCONF)
    _, without, _ = preview([dict(SAME_PORT)] + DEFCONF)
    assert with_hint['fingerprint'] == without['fingerprint']
    assert with_hint['stages'] == without['stages'] and with_hint['checks'] == without['checks']


def test_same_port_accept_before_the_drop_silences_the_hint():
    rows = copy.deepcopy(DEFCONF)
    rows.insert(6, dict(SAME_PORT))
    hint, plan, _ = preview(rows)
    assert hint is None and not any('同口转发' in warning for warning in plan['warnings'])
    # An accept behind the drop does not help.
    assert check(DEFCONF + [dict(SAME_PORT)])['status'] == 'drop'


def test_place_before_follows_the_rule_that_actually_drops_whatever_its_comment():
    renamed = [dict(row, comment='我的规则') if row['.id'] == '*7' else row for row in DEFCONF]
    assert check(renamed)['rule']['.id'] == '*7'
    # A narrower custom drop of the LAN's invalid packets earlier in the chain is the one to precede.
    custom = {'.id': '*C', 'chain': 'forward', 'action': 'drop', 'connection-state': 'invalid',
              'in-interface-list': 'LAN', 'comment': 'lan invalid'}
    rows = copy.deepcopy(DEFCONF)
    rows.insert(2, custom)
    assert check(rows)['command'].endswith('place-before=*C')
    # Through a jump: precede the jump in forward.
    jumped = [{'.id': '*J', 'chain': 'forward', 'action': 'jump', 'jump-target': 'lan'},
              {'.id': '*K', 'chain': 'lan', 'action': 'drop', 'connection-state': 'invalid'}]
    assert check(jumped)['command'].endswith('place-before=*J')
    # A plain default-deny after the established accept drops it as well.
    strict = [DEFCONF[5], {'.id': '*D', 'chain': 'forward', 'action': 'drop', 'comment': '丢弃其余'}]
    assert check(strict)['command'].endswith('place-before=*D')


@pytest.mark.parametrize('rows', [
    [],
    [dict(DEFCONF[6], disabled='true')],
    [dict(DEFCONF[6], chain='input')],
    [dict(DEFCONF[6], **{'in-interface-list': 'WAN'})],
    [dict(DEFCONF[6], **{'connection-state': 'new'})],
])
def test_no_drop_of_the_same_port_path_means_no_hint(rows):
    assert check(rows)['status'] == 'ok'


def test_unknown_matchers_are_not_reported_as_a_drop():
    # An accept whose match cannot be judged leaves the outcome open: not a daily warning.
    rows = [{'.id': '*X', 'chain': 'forward', 'action': 'accept', 'packet-mark': 'x'}, DEFCONF[6]]
    assert check(rows)['status'] == 'ok'


def test_destination_specific_drop_is_found_on_a_rule_set_route():
    drop = {'.id': '*T', 'chain': 'forward', 'action': 'drop', 'connection-state': 'invalid', 'dst-address': '91.108.0.0/16'}
    assert check([drop])['status'] == 'ok'  # Fake-IP alone is not affected
    result = check([drop], targets=('198.18.0.0/16', '91.108.0.0/16'))
    assert result['status'] == 'drop' and result['command'].endswith('place-before=*T')
    hint, _, _ = preview([drop])
    assert hint['status'] == 'drop'


def test_address_lists_used_by_forward_rules_are_read_scoped():
    session = session_with([{'.id': '*L', 'chain': 'forward', 'action': 'drop', 'connection-state': 'invalid',
                             'dst-address-list': 'proxied'}])
    session.rows['/ip/firewall/address-list'] = [{'.id': '*A1', 'list': 'proxied', 'address': '198.18.0.0/15'},
                                                 {'.id': '*A2', 'list': 'other', 'address': '1.1.1.1'}]
    result = forward_hint.check(driver_for(session), 4, 'bridge', ['198.18.0.0/16'])
    assert result['status'] == 'drop' and result['command'].endswith('place-before=*L')
    assert any('list=proxied' in path for _, path, _ in session.calls)
    assert not any('list=other' in path for _, path, _ in session.calls)


def test_interface_names_are_quoted_for_the_terminal():
    command = check([DEFCONF[6]], lan='LAN bridge')['command']
    assert 'in-interface="LAN bridge" out-interface="LAN bridge"' in command


def test_unreadable_firewall_is_shown_as_unconfirmed_and_does_not_block():
    session = session_with(DEFCONF)
    session.read_failures.add(FILTER)
    hint, plan, _ = preview(DEFCONF, session=session)
    assert hint['status'] == 'unknown' and not hint['fix_command'] and '未能确认' in hint['warning']
    assert hint['warning'] in plan['warnings'] and not plan['blockers']


def test_unknown_lan_port_is_unconfirmed():
    session = release_session()  # no connected route to the gateway
    session.rows[FILTER] = copy.deepcopy(DEFCONF)
    hint, plan, _ = preview(DEFCONF, session=session)
    assert hint['status'] == 'unknown' and 'LAN 口' in hint['warning'] and not plan['blockers']


def test_only_routeros_routes_to_mihomo_without_return_routing_are_checked():
    session = session_with(DEFCONF)
    driver = driver_for(session)
    state = driver.read_state()
    report = build_report(copy.deepcopy(DESIRED), state)
    assert forward_hint.ipv4({**ROUTER, 'type': 'ikuai'}, DESIRED, state, report, driver) is None
    # Replies come back through the router: the path is symmetric.
    assert forward_hint.ipv4({**ROUTER, 'manage_return_routing': True}, DESIRED, state, report, driver) is None
    # No route to Mihomo will exist (all conflict): nothing to check.
    for item in report['routes']['items']:
        item['status'] = 'conflict'
    calls = len(session.calls)
    assert forward_hint.ipv4(ROUTER, DESIRED, state, report, driver) is None
    assert len(session.calls) == calls


def test_ipsec_policy_matcher_only_judges_packets_that_state_it():
    from backend.routers.ipv6_policy import _rule_match
    row = {'ipsec-policy': 'in,ipsec'}
    assert _rule_match({}, 4, row, {'ipsec-policy': {'in': 'none', 'out': 'none'}}) is False
    assert _rule_match({}, 4, {'ipsec-policy': 'out,none'}, {'ipsec-policy': {'in': 'none', 'out': 'none'}}) is True
    assert _rule_match({}, 4, row, {}) is None


# Integration: the real preview path (_inspect) and automatic rule sync.
from backend.routers import rule_sync  # noqa: E402
from backend.test_router_rule_sync import NEW, environment, publish  # noqa: E402,F401


def test_preview_reports_the_hint_and_auto_sync_ignores_it(environment):
    from backend.routes.routers import _inspect
    _, driver = environment
    # Policy routing refuses FastTrack on its own; the hint does not depend on it.
    driver.rows[FILTER] = [copy.deepcopy(row) for row in DEFCONF if row['action'] != 'fasttrack-connection']
    driver.rows['/interface'] = [{'.id': '*I1', 'name': 'bridge'}]
    config = rule_sync.config_for('default')
    router = config['routers']['items'][0]
    _, desired, state, report = _inspect(config, router, driver)
    hint = report['routes']['forward_hint']
    assert hint == {'status': 'drop', 'warning': forward_hint.IPV4_DROP, 'fix_command': COMMAND}
    plan = build_plan(router, desired, state, report, report['return_routing'], driver)
    assert hint['warning'] in plan['warnings'] and not plan['blockers']
    publish(environment)  # the release itself goes ahead
    before = copy.deepcopy(driver.rows[FILTER])
    result = rule_sync.sync('default', 'ros', lambda *_: NEW)
    assert result['status'] == 'synced', result
    assert not any(path.startswith(FILTER) for _, path, _ in driver.writes)
    assert driver.rows[FILTER] == before
