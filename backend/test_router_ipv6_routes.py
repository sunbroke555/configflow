"""IPv6 rule-set routes: owned by manifest, batched, read back and compensated."""
import copy

import pytest

from backend.routers import ipv6_routes as routes6
from backend.routers.base import RouterError
from backend.routers.desired import compute_desired
from backend.routers.plan import build_plan
from backend.routers.release import execute
from backend.routers.report import build_report
from backend.routers.rule_sync_rules import classify, ip_loader
from backend.test_routeros_release import IPV6_ROUTES, StatefulRouterOS, driver_for

CONFIG = {'mihomo': {'custom_config': 'ipv6: true\ntun:\n  enable: true\n'}}
AGENT = {'id': 'gateway', 'host': '10.0.0.4', 'service_type': 'mihomo', 'enabled': True}
LLA = 'fe80::4'
HOST = {'interface': 'eth0', 'addresses': [
    {'address': LLA + '/64', 'interface': 'eth0'},
    {'address': 'fdda::4/64', 'interface': 'eth0', 'usable': True},
    {'address': '2408:8000:1:2::4/64', 'interface': 'eth0', 'usable': True},
]}
GW = LLA + '%bridge'
ROUTER = {'id': 'ros', 'type': 'routeros', 'endpoint': 'https://10.0.0.1'}
TELEGRAM6 = ['2001:67c:4e8::/48', '2001:b28:f23c::/48', '2001:b28:f23d::/48', '2001:b28:f23f::/48',
             '2a0a:f280::/32']


def wanted(*cidrs, sources=('Telegram',)):
    return {'ipv6_cidrs': [{'cidr': cidr, 'sources': list(sources)} for cidr in cidrs], 'blocking_warnings': []}


class IPv6ROS(StatefulRouterOS):
    def __init__(self, routes6=None):
        super().__init__(routes=[], nats=[])
        self.rows[IPV6_ROUTES] = copy.deepcopy(routes6 or [
            {'.id': '*D0', 'dst-address': '::/0', 'gateway': 'fe80::1%ether1', 'immediate-gw': 'fe80::1%ether1',
             'routing-table': 'main', 'active': 'true', 'dynamic': 'true', 'distance': '1'}])
        self.rows['/ipv6/address'] = [
            {'.id': '*A1', 'address': '2408:8000:1:2::1/64', 'interface': 'bridge', 'disabled': 'false'},
            {'.id': '*A2', 'address': 'fdda::1/64', 'interface': 'bridge', 'disabled': 'false'}]
        self.rows['/ip/address'] = [{'.id': '*B1', 'address': '10.0.0.1/24', 'interface': 'bridge',
                                     'actual-interface': 'bridge', 'disabled': 'false'}]
        self.rows['/ipv6/firewall/filter'] = []
        self.rows['/ipv6/firewall/address-list'] = []
        self.rows['/interface'] = [{'.id': '*I1', 'name': 'bridge'}, {'.id': '*I2', 'name': 'ether1'}]
        self.rows['/interface/list'] = [{'.id': '*L1', 'name': 'LAN'}, {'.id': '*L2', 'name': 'WAN'}]
        self.rows['/interface/list/member'] = [{'.id': '*M1', 'list': 'LAN', 'interface': 'bridge'},
                                               {'.id': '*M2', 'list': 'WAN', 'interface': 'ether1'}]
        self.inactive = False

    def request(self, method, url, json=None, **kwargs):
        reply = super().request(method, url, json=json, **kwargs)
        if method.upper() == 'PUT' and IPV6_ROUTES in url and reply.status_code == 201:
            row = next(item for item in self.rows[IPV6_ROUTES] if item['.id'] == reply.data['.id'])
            row.update(active='false' if self.inactive else 'true', dynamic='false',
                       **({'immediate-gw': row['gateway']} if '%' in row.get('gateway', '') else {}))
            reply.data = copy.deepcopy(row)
        return reply


def device(routes=None):
    session = IPv6ROS(routes)
    return session, driver_for(session)


def inspect(driver, desired, router=ROUTER, config=CONFIG, host=HOST, agent=AGENT):
    return routes6.inspect(config, router, desired, agent, host, driver)


def stage_plan(part):
    return {'stages': [{'key': 'ipv6_routes', 'title': 'IPv6 规则集路由', 'manual': False,
                        'ops': part['ops'], 'enabled': True, 'skipped': bool(part['skip'])}],
            'checks': {'ipv6_routes': part['check']} if not part['skip'] else {}, 'blockers': []}


def publish(driver, desired, router=ROUTER, **kwargs):
    part = inspect(driver, desired, router, **kwargs)
    assert not part['skip'], part['skip']
    result = execute(stage_plan(part), driver, None)
    assert result['ok'], result
    return {**router, 'ipv6_routes_state': copy.deepcopy(driver.ipv6_routes_manifest)}, part


def own(session):
    return sorted(row['dst-address'] for row in session.rows[IPV6_ROUTES] if row.get('comment') == 'cf-rule6:ros')


def test_desired_lists_public_ipv6_separately_and_auto_sync_keeps_it():
    content = '\n'.join(['IP-CIDR,91.108.4.0/22', 'IP-CIDR6,2001:b28:f23d::/48', 'IP-CIDR6,2001:b28:f23c::/48',
                         'IP-CIDR6,fc00::/7', 'IP-CIDR6,fe80::/10', 'DOMAIN,t.me'])
    config = {'rule_configs': [{'id': 'tg', 'itemType': 'ruleset', 'name': 'Telegram', 'policy': 'PROXY'}]}
    desired = compute_desired(config, '10.0.0.4', ['tg'], content_loader=lambda *_: content)
    assert [item['cidr'] for item in desired['ipv6_cidrs']] == ['2001:b28:f23c::/47']
    assert desired['ipv6_cidrs'][0]['sources'] == ['Telegram']
    assert '91.108.4.0/22' in [item['cidr'] for item in desired['proxy_cidrs']]
    record = classify(content, 'classical')
    assert record['ipv6'] == ['2001:b28:f23c::/48', '2001:b28:f23d::/48'] and record['ip_count'] == 1
    pinned = compute_desired(config, '10.0.0.4', ['tg'], content_loader=ip_loader({'tg': record}))
    assert pinned['ipv6_cidrs'] == desired['ipv6_cidrs'] and pinned['proxy_cidrs'] == desired['proxy_cidrs']


def test_publish_batches_routes_to_the_scoped_link_local_next_hop_and_is_then_in_sync():
    session, driver = device()
    router, part = publish(driver, wanted(*TELEGRAM6))
    assert part['gateway'] == GW and part['status'] == 'missing'
    assert own(session) == sorted(TELEGRAM6)
    # Five adds are one console script, not five REST writes.
    assert [path for method, path, _ in session.calls if method == 'POST' and path == '/execute'] == ['/execute']
    manifest = router['ipv6_routes_state']
    assert manifest['gateway'] == GW and len(manifest['resources']) == 5
    again = inspect(driver, wanted(*TELEGRAM6), router)
    assert again['ops'] == [] and again['status'] == 'synced' and not again['skip']
    assert {item['status'] for item in again['items']} == {'synced'}


def test_ula_is_the_fallback_and_ambiguous_next_hops_skip_only_this_item():
    _, driver = device()
    host = {'interface': 'eth0', 'addresses': [{'address': 'fdda::4/64', 'interface': 'eth0', 'usable': True}]}
    assert inspect(driver, wanted(TELEGRAM6[0]), host=host)['gateway'] == 'fdda::4%bridge'
    two = {'interface': 'eth0', 'addresses': [{'address': 'fe80::4/64', 'interface': 'eth0'},
                                              {'address': 'fe80::5/64', 'interface': 'eth0'}]}
    part = inspect(driver, wanted(TELEGRAM6[0]), host=two)
    assert part['ops'] == [] and part['check'] is None
    assert '下一跳' in part['skip'][0]
    for setup in ({'host': {'error': '设备离线'}}, {'agent': None},
                  {'config': {'mihomo': {'custom_config': 'ipv6: true\n'}}},
                  {'config': {'mihomo': {'custom_config': 'ipv6: "yes"\n'}}}):
        part = inspect(driver, wanted(TELEGRAM6[0]), **setup)
        assert part['skip'] and part['ops'] == [], setup


def test_skipped_item_warns_without_blocking_the_rest_of_the_release():
    session, driver = device()
    state = driver.read_state()
    desired = {'fake_ip_range': '198.18.0.0/16', 'proxy_gateway': '10.0.0.4', 'warnings': [],
               'proxy_cidrs': [{'cidr': '198.18.0.0/16', 'sources': ['fake-ip']}], 'dns_server': '', 'dns_error': ''}
    state.ipv6_routes_plan = inspect(driver, wanted(TELEGRAM6[0]), host={'error': '设备离线'})
    part = state.ipv6_routes_plan
    plan = build_plan({**ROUTER, 'manage_return_routing': False}, desired, state, build_report(desired, state),
                      {'status': 'disabled'}, driver)
    assert not any('IPv6' in blocker for blocker in plan['blockers'])
    assert any(warning.startswith('IPv6 规则集路由本次跳过') for warning in plan['warnings'])
    stage = next(stage for stage in plan['stages'] if stage['key'] == 'ipv6_routes')
    assert stage['skipped'] and stage['ops'] == [] and 'ipv6_routes' not in plan['checks']
    assert [stage['key'] for stage in plan['stages']].index('ipv6_routes') < \
        [stage['key'] for stage in plan['stages']].index('nat_bypass')
    result = execute(stage_plan(part), driver, None)
    assert result['ok'], result
    assert any(entry['stage'] == 'ipv6_routes' and entry['status'] == 'warning' for entry in result['log'])
    assert own(session) == []


def test_turning_mihomo_ipv6_off_withdraws_only_owned_routes_in_one_remove():
    foreign = {'.id': '*F1', 'dst-address': '2001:db9::/32', 'gateway': GW, 'immediate-gw': GW,
               'routing-table': 'main', 'active': 'true', 'dynamic': 'false', 'disabled': 'false', 'comment': 'mine'}
    session, driver = device()
    session.rows[IPV6_ROUTES].append(foreign)
    router, _ = publish(driver, wanted(*TELEGRAM6))
    off = {'mihomo': {'custom_config': 'ipv6: false\n'}}
    assert routes6.relevant(off, router, wanted(*TELEGRAM6))
    assert not routes6.needs_host(off, router, wanted(*TELEGRAM6))
    part = inspect(driver, wanted(*TELEGRAM6), router, config=off, host={}, agent=None)
    assert part['enabled'] is False and part['auto'] and {item['status'] for item in part['items']} == {'pending'}
    session.calls.clear()
    result = execute(stage_plan(part), driver, None)
    assert result['ok'], result
    assert own(session) == [] and driver.ipv6_routes_manifest is None
    assert foreign in session.rows[IPV6_ROUTES]
    assert [path for method, path, _ in session.calls if method == 'POST'].count(IPV6_ROUTES + '/remove') == 1


def test_existing_routes_are_borrowed_or_left_alone_and_router_prefixes_are_never_routed_away():
    good = {'.id': '*G', 'dst-address': TELEGRAM6[0], 'gateway': GW, 'immediate-gw': GW, 'routing-table': 'main',
            'active': 'true', 'dynamic': 'false', 'disabled': 'false', 'comment': 'hand'}
    wrong = {'.id': '*W', 'dst-address': TELEGRAM6[1], 'gateway': 'fe80::9%bridge', 'immediate-gw': 'fe80::9%bridge',
             'routing-table': 'main', 'active': 'true', 'dynamic': 'false', 'disabled': 'false'}
    session, driver = device()
    session.rows[IPV6_ROUTES] += [copy.deepcopy(good), copy.deepcopy(wrong)]
    part = inspect(driver, wanted(TELEGRAM6[0], TELEGRAM6[1], '2408:8000::/20'))
    status = {item['cidr']: item['status'] for item in part['items']}
    assert status == {TELEGRAM6[0]: 'borrowed', TELEGRAM6[1]: 'conflict', '2408:8000::/20': 'conflict'}
    assert part['ops'] == []
    publish(driver, wanted(TELEGRAM6[0]))
    assert good in session.rows[IPV6_ROUTES] and wrong in session.rows[IPV6_ROUTES] and own(session) == []


def test_removed_destinations_are_withdrawn_unless_the_rule_input_is_incomplete():
    session, driver = device()
    router, _ = publish(driver, wanted(*TELEGRAM6))
    incomplete = {**wanted(*TELEGRAM6[:2]), 'blocking_warnings': ['规则集「Telegram」没有本地缓存']}
    part = inspect(driver, incomplete, router)
    assert part['ops'] == [] and part['warnings']
    part = inspect(driver, wanted(*TELEGRAM6[:2]), router)
    assert part['auto'] == '' and len(part['ops'][0]['deletes']) == 3
    execute(stage_plan(part), driver, None)
    assert own(session) == sorted(TELEGRAM6[:2])
    assert len(driver.ipv6_routes_manifest['resources']) == 2


def test_new_next_hop_replaces_routes_and_requires_a_manual_release():
    session, driver = device()
    router, _ = publish(driver, wanted(*TELEGRAM6[:2]))
    host = {'interface': 'eth0', 'addresses': [{'address': 'fe80::7/64', 'interface': 'eth0'}]}
    part = inspect(driver, wanted(*TELEGRAM6[:2]), router, host=host)
    assert part['auto'] and {item['status'] for item in part['items']} == {'pending'}
    execute(stage_plan(part), driver, None)
    assert {row['gateway'] for row in session.rows[IPV6_ROUTES] if row.get('comment') == 'cf-rule6:ros'} == {'fe80::7%bridge'}
    assert len(own(session)) == 2


def test_outside_changes_skip_the_item_or_reject_a_stale_preview():
    session, driver = device()
    router, _ = publish(driver, wanted(*TELEGRAM6))
    row = next(row for row in session.rows[IPV6_ROUTES] if row.get('comment') == 'cf-rule6:ros')
    row['comment'] = 'edited'
    part = inspect(driver, wanted(*TELEGRAM6), router)
    assert '被外部修改' in part['skip'][0] and part['ops'] == []
    row['comment'] = 'cf-rule6:ros'
    part = inspect(driver, wanted(*TELEGRAM6[:3]), router)
    session.rows[IPV6_ROUTES].append({'.id': '*N', 'dst-address': TELEGRAM6[4], 'gateway': 'ether9',
                                      'routing-table': 'main', 'active': 'true', 'disabled': 'false'})
    with pytest.raises(RouterError, match='预览后变化'):
        routes6.apply(driver, part['ops'][0])
    assert len(own(session)) == 5


def test_inactive_new_route_rolls_back_every_write_and_keeps_the_old_manifest():
    session, driver = device()
    router, _ = publish(driver, wanted(*TELEGRAM6[:2]))
    before = copy.deepcopy(session.rows[IPV6_ROUTES])
    part = inspect(driver, wanted(*TELEGRAM6[2:]), router)
    session.inactive = True
    result = execute(stage_plan(part), driver, None)
    assert not result['ok'] and '尚未生效' in result['log'][-1]['summary']
    assert sorted(row['dst-address'] for row in session.rows[IPV6_ROUTES]) == \
        sorted(row['dst-address'] for row in before)
    # The removed routes came back under new IDs and stay owned.
    manifest = driver.ipv6_routes_manifest
    assert [entry['config'] for entry in manifest['resources']] == \
        [entry['config'] for entry in router['ipv6_routes_state']['resources']]
    assert own(session) == sorted(TELEGRAM6[:2])


def test_failed_readback_restores_removed_routes_under_new_ids_and_remaps_ownership(monkeypatch):
    session, driver = device()
    router, _ = publish(driver, wanted(*TELEGRAM6))
    part = inspect(driver, wanted(TELEGRAM6[0]), router)
    count = session.counts.get(('PUT', IPV6_ROUTES), 0)

    def broken(remote, check):
        raise RouterError('injected readback failure')
    monkeypatch.setattr(routes6, 'verify', broken)
    result = execute(stage_plan(part), driver, None)
    monkeypatch.undo()
    assert not result['ok'] and all(item['status'] == 'ok' for item in result['rollback'])
    assert own(session) == sorted(TELEGRAM6)
    assert session.counts[('PUT', IPV6_ROUTES)] > count  # recreated, so under new IDs
    manifest = driver.ipv6_routes_manifest
    assert len(manifest['resources']) == 5
    assert {entry['id'] for entry in manifest['resources']} <= {row['.id'] for row in session.rows[IPV6_ROUTES]}
    assert inspect(driver, wanted(*TELEGRAM6), {**router, 'ipv6_routes_state': manifest})['ops'] == []


def test_invalid_manifest_is_never_trusted():
    _, driver = device()
    for state in ({'version': 9}, {'version': 1, 'gateway': GW, 'resources': [{'id': '*1'}]}):
        part = inspect(driver, wanted(TELEGRAM6[0]), {**ROUTER, 'ipv6_routes_state': state})
        assert part['skip'] and part['ops'] == []


def test_settings_follow_mihomo_default_and_explicit_switch():
    assert routes6.settings({})['enabled'] is True
    assert routes6.settings({'mihomo': {'custom_config': 'ipv6: false'}})['enabled'] is False
    assert routes6.settings({'mihomo': {'custom_config': '[1]'}})['error']
    assert not routes6.relevant(CONFIG, {'type': 'ikuai'}, wanted(TELEGRAM6[0]))
    assert not routes6.relevant(CONFIG, ROUTER, wanted())


def test_ikuai_reports_unhandled_ipv6_and_routeros_asks_the_agent_once(monkeypatch):
    from types import SimpleNamespace
    from backend.routers import ipv6_fakeip
    from backend.routers.base import RouterState
    from backend.routes import routers as routes_module
    calls = []
    monkeypatch.setattr(routes_module, '_agent', lambda agent_id: AGENT if agent_id else None)
    monkeypatch.setattr(routes_module, 'compute_desired', lambda *args, **kwargs: {**wanted(*TELEGRAM6), 'warnings': []})
    monkeypatch.setattr(routes_module, '_dns_settings', lambda *_: {'dns_error': ''})
    monkeypatch.setattr(routes_module, '_return_cleanup', lambda *_: None)
    monkeypatch.setattr(routes_module, 'build_report', lambda *_: {})
    monkeypatch.setattr(routes_module, '_ipv6_host', lambda config, agent: calls.append(agent) or HOST)
    monkeypatch.setattr(ipv6_fakeip, 'inspect', lambda *args: {'ops': [], 'blockers': []})
    monkeypatch.setattr(routes6, 'inspect', lambda config, router, desired, agent, host, driver: {'host': host})
    driver = SimpleNamespace(read_state=lambda: RouterState())
    _, desired, state, _ = routes_module._inspect_state(CONFIG, {'id': 'ik', 'type': 'ikuai',
                                                                 'proxy_gateway_agent_id': 'gateway'}, driver)
    assert state.ipv6_routes_plan is None and calls == []
    assert any('iKuai 只发布 IPv4' in warning for warning in desired['warnings'])
    both = {'mihomo': {'custom_config': 'ipv6: true\ntun: {enable: true}\ndns: {enable: true, ipv6: true, '
                                        'enhanced-mode: fake-ip, fake-ip-range6: "fc00::1/64"}\n'}}
    _, _, state, _ = routes_module._inspect_state(both, {**ROUTER, 'proxy_gateway_agent_id': 'gateway'}, driver)
    assert state.ipv6_routes_plan == {'host': HOST} and calls == [AGENT]


def test_owned_ipv6_routes_guard_router_edits_and_are_persisted_with_the_release(tmp_path):
    from backend.common.config_repository import ProfileRepository
    from backend.routers import rule_sync_store
    from backend.routers.store import RouterValidationError, normalize, redact_routers
    existing = {**ROUTER, 'name': 'ROS', 'username': 'admin', 'password': 'pw', 'endpoint': 'https://10.0.0.1',
                'ipv6_routes_state': {'version': 1, 'gateway': GW, 'resources': []}}
    with pytest.raises(RouterValidationError, match='IPv6 规则集路由'):
        normalize({'endpoint': 'https://10.0.0.2'}, existing)
    assert 'ipv6_routes_state' not in redact_routers({'items': [existing]})['items'][0]
    repository = ProfileRepository(tmp_path)
    router = {key: value for key, value in existing.items() if key != 'ipv6_routes_state'}
    repository.update_profile_transaction('default', lambda config: config.update(routers={'items': [router]}))
    manifest = {'version': 1, 'gateway': GW, 'resources': [{'path': IPV6_ROUTES, 'id': '*9', 'config': {}}]}
    with rule_sync_store.job_lock('default', 'ros', router) as acquired:
        assert acquired
        repository.save_router_publication('default', router, ipv6_routes=manifest)
    assert repository.get_profile('default')['routers']['items'][0]['ipv6_routes_state'] == manifest


DEFCONF = [
    {'.id': '*10', 'chain': 'forward', 'action': 'accept', 'connection-state': 'established,related,untracked',
     'comment': 'defconf: accept established,related,untracked'},
    {'.id': '*11', 'chain': 'forward', 'action': 'drop', 'connection-state': 'invalid', 'comment': 'defconf: drop invalid'},
    {'.id': '*12', 'chain': 'forward', 'action': 'drop', 'src-address-list': 'bad_ipv6'},
    {'.id': '*13', 'chain': 'forward', 'action': 'accept', 'protocol': 'icmpv6'},
    {'.id': '*14', 'chain': 'forward', 'action': 'drop', 'in-interface-list': '!LAN'},
]


def test_drop_invalid_before_a_same_port_accept_is_reported_with_a_copyable_fix():
    session, driver = device()
    session.rows['/ipv6/firewall/filter'] = copy.deepcopy(DEFCONF)
    part = inspect(driver, wanted(*TELEGRAM6))
    assert any('drop invalid' in warning and '7 秒' in warning for warning in part['warnings'])
    assert part['fix_command'] == ('/ipv6 firewall filter add chain=forward action=accept in-interface=bridge '
                                   'out-interface=bridge comment="LAN to Mihomo via router" place-before=*11')
    assert part['ops'] and not part['skip']  # a hint: the routes are still published
    assert routes6.report(part)['fix_command'] == part['fix_command']
    session.rows['/ipv6/firewall/filter'].insert(1, {'.id': '*F', 'chain': 'forward', 'action': 'accept',
                                                     'in-interface': 'bridge', 'out-interface': 'bridge'})
    fixed = inspect(driver, wanted(*TELEGRAM6))
    assert fixed['fix_command'] == '' and not fixed['warnings']
    session.rows['/ipv6/firewall/filter'] = [dict(DEFCONF[1], disabled='true')]
    assert inspect(driver, wanted(*TELEGRAM6))['fix_command'] == ''
    session.read_failures.add('/ipv6/firewall/filter')
    unread = inspect(driver, wanted(*TELEGRAM6))
    assert unread['fix_command'] == '' and any('未能读取' in warning for warning in unread['warnings'])
