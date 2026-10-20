"""RouterOS DNS guard: owned objects, safe ordering, Netwatch pauses and rollback."""
import copy
from urllib.parse import unquote, urlsplit

import pytest

from backend.routers import dns_guard
from backend.routers.dns_guard import ADDRESSES, BYPASS_LIST, FILTER, NAT, NETWATCH, owned_entries, snapshot
from backend.routers.plan import build_plan
from backend.routers.release import execute
from backend.routers.report import build_report
from backend.routers.routeros import RouterOSDriver
from backend.routers.store import RouterValidationError, guard_settings, normalize
from backend.common.config_repository import ProfileInUse
from backend.test_router_repository_safety import (  # noqa: F401 (pytest fixture)
    CONNECTION, replace_routers, repository,
)
from backend.routers.rule_sync_store import job_lock

ROUTES = '/ip/route'
GATEWAY = '10.0.0.4'
OXIDNS = '10.0.0.2'
POLICY_PATHS = ('/routing/rule', '/ip/firewall/mangle', '/ip/vrf', '/interface/list', '/interface/list/member')
RUNTIME = {'.id', 'bytes', 'packets', 'dynamic', 'invalid', 'creation-time', 'status', 'since', 'done-tests'}
DESIRED = {'fake_ip_range': '198.18.0.0/16', 'proxy_gateway': GATEWAY, 'warnings': [],
           'proxy_cidrs': [{'cidr': '198.18.0.0/16', 'sources': ['fake-ip']}],
           'dns_server': OXIDNS, 'dns_error': ''}
LEGACY_HIJACK = {'.id': '*L1', 'chain': 'dstnat', 'action': 'dst-nat', 'protocol': 'udp', 'dst-port': '53',
                 'to-addresses': OXIDNS, 'comment': 'cf-dns hijack udp', 'disabled': 'false'}
WAN_MASQUERADE = {'.id': '*M1', 'chain': 'srcnat', 'action': 'masquerade', 'out-interface': 'ether1',
                  'disabled': 'false'}


class Response:
    def __init__(self, data=None, status=200):
        self.data, self.status_code = data, status

    def json(self):
        if self.status_code == 204:
            raise ValueError('no body')
        return copy.deepcopy(self.data)


class RouterOS:
    """REST fake whose reads move counters and probe state like a live device."""

    def __init__(self, nats=None, addresses=None, filters=None, netwatch=None):
        self.rows = {
            ROUTES: [
                {'.id': '*R1', 'dst-address': '10.0.0.0/24', 'gateway': 'bridge', 'immediate-gw': 'bridge',
                 'connect': 'true', 'active': 'true', 'dynamic': 'true', 'distance': '0', 'routing-table': 'main'},
                {'.id': '*R2', 'dst-address': '198.18.0.0/16', 'gateway': GATEWAY, 'comment': 'cf-fake-ip',
                 'immediate-gw': f'{GATEWAY}%bridge', 'active': 'true', 'distance': '1', 'routing-table': 'main',
                 'disabled': 'false', 'dynamic': 'false'},
            ],
            NAT: copy.deepcopy([LEGACY_HIJACK, WAN_MASQUERADE] if nats is None else nats),
            ADDRESSES: copy.deepcopy(addresses or []),
            FILTER: copy.deepcopy(filters or []),
            NETWATCH: copy.deepcopy(netwatch or []),
            **{path: [] for path in POLICY_PATHS},
        }
        self.next_id = 0x100
        self.fail = set()
        self.counts = {}
        self.writes = []

    def close(self):
        pass

    def _collection(self, path):
        return next(key for key in sorted(self.rows, key=len, reverse=True)
                    if path == key or path.startswith(key + '/'))

    def request(self, method, url, json=None, **_):
        path = unquote(urlsplit(url).path).removeprefix('/rest')
        collection = self._collection(path)
        rows = self.rows[collection]
        row_id = path[len(collection) + 1:] or None
        if method == 'GET':
            for row in rows:
                if collection in (NAT, FILTER):
                    row['bytes'] = str(int(row.get('bytes', 0)) + 64)
                    row['packets'] = str(int(row.get('packets', 0)) + 1)
                if collection == NETWATCH:
                    row.update(status='up', since='now', **{'done-tests': str(int(row.get('done-tests', 0)) + 1)})
            if row_id:
                row = next((row for row in rows if row['.id'] == row_id), None)
                return Response(row, 200 if row else 404)
            return Response(rows)
        self.writes.append((method, collection, copy.deepcopy(json)))
        key = (method, collection)
        self.counts[key] = self.counts.get(key, 0) + 1
        if (method, collection, self.counts[key]) in self.fail:
            return Response({'message': 'injected failure'}, 400)
        if method == 'PUT':
            if set(json) & RUNTIME:
                return Response({'message': f'read-only: {sorted(set(json) & RUNTIME)}'}, 400)
            self.next_id += 1
            row = {'.id': f'*{self.next_id:X}', 'disabled': 'false', **copy.deepcopy(json)}
            before = row.pop('place-before', None)
            if collection == ADDRESSES:
                row.update({'creation-time': 'now', 'dynamic': 'false'})
            if collection in (NAT, FILTER):
                row.update(bytes='0', packets='0', invalid='false', dynamic='false')
            if before is not None:
                rows.insert(next(i for i, item in enumerate(rows) if item['.id'] == before), row)
            else:
                rows.append(row)
            return Response(row, 201)
        row = next((row for row in rows if row['.id'] == row_id), None)
        if row is None:
            return Response({'message': 'not found'}, 404)
        if method == 'PATCH':
            row.update(json)
            return Response(row)
        rows.remove(row)
        return Response(status=204)

    def get(self, url, **kwargs):
        return self.request('GET', url, **kwargs)

    def put(self, url, **kwargs):
        return self.request('PUT', url, **kwargs)

    def patch(self, url, **kwargs):
        return self.request('PATCH', url, **kwargs)

    def delete(self, url, **kwargs):
        return self.request('DELETE', url, **kwargs)


def device(**kwargs):
    session = RouterOS(**kwargs)
    driver = RouterOSDriver('https://10.0.0.1', 'admin', 'pw')
    driver.session = session
    return session, driver


def router(**guard):
    settings = {'enabled': True, 'block_dot': True, 'failover': True, 'bypass': ['10.0.0.50'], **guard}
    return {'id': 'ros', 'type': 'routeros', 'endpoint': 'https://10.0.0.1', 'dns_guard': settings}


def preview(driver, item, desired=DESIRED):
    """Mirror routes.routers._inspect for the RouterOS stages exercised here."""
    if dns_guard.relevant(item):
        driver.dns_guard_manifest = item.get('dns_guard_state')
    state = driver.read_state()
    desired = copy.deepcopy(desired)
    if dns_guard.relevant(item):
        desired['dns_guard'] = {'settings': guard_settings(item), 'previous': item.get('dns_guard_state')}
        state.dns_guard = snapshot(driver)
    report = build_report(desired, state)
    return report, build_plan(item, desired, state, report, {}, driver)


def publish(driver, item):
    report, plan = preview(driver, item)
    assert not plan['blockers'], plan['blockers']
    result = execute(plan, driver, None)
    item = copy.deepcopy(item)
    if driver.dns_guard_manifest:
        item['dns_guard_state'] = driver.dns_guard_manifest
    else:
        item.pop('dns_guard_state', None)
    return result, item


def guard_rows(session, path):
    return [row for row in session.rows[path] if str(row.get('comment', '')).startswith('cf-dnsguard:')]


def test_enable_writes_owned_objects_without_a_broken_hijack_window():
    session, driver = device()
    report, plan = preview(driver, router())
    stage = plan['stages'][-1]
    assert stage['key'] == 'dns_guard' and len(stage['ops']) == 1
    assert report['dns_guard']['status'] == 'pending'
    assert any('cf-dns hijack udp' in warning for warning in plan['warnings'])
    result, item = publish(driver, router())
    assert result['ok'], result
    assert [entry['role'] for entry in item['dns_guard_state']['resources']] == [
        'bypass:10.0.0.2/32', 'bypass:10.0.0.4/32', 'bypass:10.0.0.50/32',
        'hairpin-udp', 'hairpin-tcp', 'hijack-udp', 'hijack-tcp', 'dot-tcp', 'dot-udp', 'netwatch']
    nat_writes = [values for method, path, values in session.writes if method == 'PUT' and path == NAT]
    assert [values['comment'] for values in nat_writes] == ['cf-dnsguard:hairpin'] * 2 + ['cf-dnsguard:hijack'] * 2
    comments = [row.get('comment') for row in session.rows[NAT]]
    # Hijack first, ahead of the legacy rule; the hairpin after every other srcnat rule.
    assert comments[:2] == ['cf-dnsguard:hijack'] * 2 and comments[-2:] == ['cf-dnsguard:hairpin'] * 2
    assert session.rows[NAT][2]['.id'] == '*L1'
    assert [row['comment'] for row in session.rows[FILTER]] == ['cf-dnsguard:dot'] * 2
    hijack = session.rows[NAT][0]
    assert (hijack['in-interface'], hijack['to-addresses'], hijack['src-address-list'], hijack['dst-address']) == (
        'bridge', OXIDNS, '!' + BYPASS_LIST, '!' + OXIDNS)
    hairpin = guard_rows(session, NAT)[-1]
    assert (hairpin['src-address'], hairpin['dst-address'], hairpin['out-interface']) == ('10.0.0.0/24', OXIDNS, 'bridge')
    watch = session.rows[NETWATCH][0]
    assert (watch['startup-delay'], watch['timeout'], watch['interval']) == ('30s', '2s', '10s')
    assert watch['down-script'].startswith(':delay 15s; :if ([/tool netwatch get [find name="cf-dnsguard"] status] = "down")')
    assert '/ip firewall nat disable [find comment="cf-dnsguard:hijack"]' in watch['down-script']
    assert f'dst-port=53 and dstnat and reply-src-address={OXIDNS}' in watch['down-script']
    assert watch['up-script'].startswith('/ip firewall nat enable [find comment="cf-dnsguard:hijack"]; ')
    assert session.rows[NAT][3] == WAN_MASQUERADE | {'bytes': session.rows[NAT][3]['bytes'],
                                                     'packets': session.rows[NAT][3]['packets']}

    report, plan = preview(driver, item)
    assert report['dns_guard']['status'] == 'synced' and report['in_sync']
    assert plan['empty'] and not plan['blockers']
    # The owned hairpin shares the proxy egress but never translates proxy traffic.
    assert plan['checks']['nat_not_required'] == [{'cidr': '198.18.0.0/16', 'gateway': GATEWAY}]
    assert all(row.get('comment') != 'cf-dnsguard:hairpin' for row in driver.read_state().nat_context['rules'])


def test_hairpin_still_counts_for_nat_assessment_without_ownership_proof():
    session, driver = device()
    _, item = publish(driver, router())
    session.rows[NAT][-1]['src-address'] = '10.0.0.0/16'
    driver.dns_guard_manifest = item['dns_guard_state']
    rules = driver.read_state().nat_context['rules']
    assert any(row.get('comment') == 'cf-dnsguard:hairpin' for row in rules)
    _, plan = preview(driver, item)
    assert any('外部修改' in blocker for blocker in plan['blockers'])


def test_netwatch_pause_is_not_treated_as_tampering():
    session, driver = device()
    _, item = publish(driver, router())
    for row in guard_rows(session, NAT):
        if row['comment'] == 'cf-dnsguard:hijack':
            row['disabled'] = 'true'
    report, plan = preview(driver, item)
    assert plan['empty'] and not plan['blockers']
    assert report['dns_guard']['status'] == 'synced' and 'Netwatch 已自动暂停' in report['dns_guard']['message']
    assert [entry['paused'] for entry in report['dns_guard']['items'] if entry['role'].startswith('hijack')] == [True, True]


def test_disable_removes_only_verified_objects_and_keeps_manual_rules():
    session, driver = device()
    _, item = publish(driver, router())
    item['dns_guard']['enabled'] = False
    _, plan = preview(driver, item)
    assert plan['stages'][-1]['ops'][0]['spec'] is None
    result, item = publish(driver, item)
    assert result['ok'], result
    assert 'dns_guard_state' not in item
    assert not any(guard_rows(session, path) for path in (NAT, FILTER, ADDRESSES, NETWATCH))
    assert [row['.id'] for row in session.rows[NAT]] == ['*L1', '*M1']
    assert not dns_guard.relevant(item)


def test_settings_change_replaces_objects_and_rolls_back_with_new_ids():
    session, driver = device()
    _, item = publish(driver, router())
    before = {path: [{key: value for key, value in row.items() if key not in RUNTIME} for row in session.rows[path]]
              for path in (NAT, FILTER, ADDRESSES, NETWATCH)}
    changed = copy.deepcopy(item)
    changed['dns_guard']['bypass'] = ['10.0.0.50', '10.0.0.60']
    _, plan = preview(driver, changed)
    session.counts, session.fail = {}, {('PUT', NAT, 2)}  # the replacement's second hairpin
    result = execute(plan, driver, None)
    assert not result['ok'] and result['failed_stage'] == 'dns_guard'
    after = {path: [{key: value for key, value in row.items() if key not in RUNTIME} for row in session.rows[path]]
             for path in (NAT, FILTER, ADDRESSES, NETWATCH)}
    strip = lambda rows: [{key: value for key, value in row.items() if key != '.id'} for row in rows]  # noqa: E731
    assert {path: strip(rows) for path, rows in after.items()} == {path: strip(rows) for path, rows in before.items()}
    restored = driver.dns_guard_manifest
    assert [entry['role'] for entry in restored['resources']] == [
        entry['role'] for entry in item['dns_guard_state']['resources']]
    assert len(owned_entries(restored, snapshot(driver))) == len(restored['resources'])
    item['dns_guard_state'] = restored
    _, plan = preview(driver, item)
    assert plan['empty'] and not plan['blockers']


def test_failed_first_enable_leaves_no_objects_behind():
    session, driver = device()
    _, plan = preview(driver, router())
    session.fail = {('PUT', FILTER, 1)}
    result = execute(plan, driver, None)
    assert not result['ok']
    assert not any(guard_rows(session, path) for path in (NAT, FILTER, ADDRESSES, NETWATCH))
    assert driver.dns_guard_manifest is None


@pytest.mark.parametrize('path,row,message', [
    (ADDRESSES, {'.id': '*X', 'list': BYPASS_LIST, 'address': '10.0.0.9'}, '地址列表'),
    (NETWATCH, {'.id': '*Y', 'name': 'cf-dnsguard', 'host': '10.0.0.9'}, 'Netwatch'),
])
def test_foreign_objects_with_reserved_names_block(path, row, message):
    session, driver = device(**{{ADDRESSES: 'addresses', NETWATCH: 'netwatch'}[path]: [row]})
    _, plan = preview(driver, router())
    assert any(message in blocker for blocker in plan['blockers'])


def test_oxidns_outside_a_connected_network_blocks():
    _, driver = device()
    _, plan = preview(driver, router(), {**DESIRED, 'dns_server': '172.16.0.2'})
    assert any('直连网段' in blocker for blocker in plan['blockers'])


def test_dhcp_dns_errors_block_the_guard():
    _, driver = device()
    _, plan = preview(driver, router(), {**DESIRED, 'dns_error': '所选 DNS Agent 离线，无法下发 DHCP DNS'})
    assert any('依赖 DHCP DNS' in blocker for blocker in plan['blockers'])


def test_unused_feature_adds_no_stage_or_report():
    _, driver = device()
    item = {'id': 'ros', 'type': 'routeros', 'endpoint': 'https://10.0.0.1'}
    report, plan = preview(driver, item)
    assert 'dns_guard' not in report
    assert [stage['key'] for stage in plan['stages']] == ['return_routing', 'routes', 'nat_bypass', 'dhcp_dns']


def test_optional_parts_follow_settings():
    session, driver = device()
    result, item = publish(driver, router(block_dot=False, failover=False, bypass=[]))
    assert result['ok']
    assert not session.rows[FILTER] and not session.rows[NETWATCH]
    assert [entry['role'] for entry in item['dns_guard_state']['resources']] == [
        'bypass:10.0.0.2/32', 'bypass:10.0.0.4/32', 'hairpin-udp', 'hairpin-tcp', 'hijack-udp', 'hijack-tcp']


BASE = {'name': 'ROS', 'type': 'routeros', 'endpoint': 'https://10.0.0.1', 'username': 'admin',
        'password': 'pw', 'dns_agent_id': 'oxidns'}


def test_settings_are_validated_and_normalized():
    saved = normalize({**BASE, 'dns_guard': {'enabled': True, 'bypass': ['10.0.0.50', '10.0.1.7/24', '10.0.0.50']}})
    assert saved['dns_guard'] == {'enabled': True, 'block_dot': False, 'failover': True,
                                  'bypass': ['10.0.0.50/32', '10.0.1.0/24']}
    assert 'dns_guard' not in normalize(BASE)
    for payload, message in (
            ({**BASE, 'dns_agent_id': '', 'dns_guard': {'enabled': True}}, 'DHCP DNS'),
            ({**BASE, 'type': 'ikuai', 'dns_guard': {'enabled': True}}, '仅支持 RouterOS'),
            ({**BASE, 'dns_guard': {'enabled': True, 'bypass': ['not-an-ip']}}, '不是有效'),
            ({**BASE, 'dns_guard': {'enabled': 'yes'}}, '布尔值'),
            ({**BASE, 'dns_guard': {'enabled': True, 'unknown': 1}}, '格式无效')):
        with pytest.raises(RouterValidationError, match=message):
            normalize(payload)
    owned = {**normalize(BASE), 'dns_guard_state': {'resources': []}}
    with pytest.raises(RouterValidationError, match='强制 DNS'):
        normalize({'endpoint': 'https://10.0.0.9'}, owned)


@pytest.mark.parametrize('writer', ['profile-import', 'fields', 'save', 'transaction'])
@pytest.mark.parametrize('change', ['remove-manifest', 'replace-manifest', 'endpoint'])
def test_config_writers_cannot_drop_guard_ownership(repository, writer, change):  # noqa: F811
    router_record = repository.get_profile('live')['routers']['items'][0]
    with job_lock('live', router_record['id'], router_record) as acquired:
        assert acquired
        repository.save_router_publication('live', router_record, dns_guard={'resources': [{'id': '*1'}]})
    before = repository.export_all()
    routers = copy.deepcopy(before['profiles']['live']['routers'])
    item = routers['items'][0]
    if change == 'remove-manifest':
        item.pop('dns_guard_state')
    elif change == 'replace-manifest':
        item['dns_guard_state'] = {'resources': []}
    else:
        item['endpoint'] = 'http://192.168.0.9'
    with pytest.raises(ProfileInUse, match='强制 DNS'):
        replace_routers(repository, writer, routers)
    assert repository.export_all() == before
    assert CONNECTION['id'] == item['id']


def test_rule_sync_baseline_is_unchanged_until_the_guard_is_used(monkeypatch):
    from backend.routers import rule_sync
    monkeypatch.setattr('backend.routes.routers._agent', lambda agent_id: None)
    monkeypatch.setattr(rule_sync, 'selected_sources', lambda config, item: {})
    config = {}
    plain = {'id': 'ros', 'type': 'routeros', 'endpoint': 'https://10.0.0.1'}
    unused = {**plain, 'dns_guard': {'enabled': False, 'block_dot': False, 'failover': True, 'bypass': []}}
    assert rule_sync.controls(config, plain) == rule_sync.controls(config, unused)
    assert rule_sync.controls(config, plain) != rule_sync.controls(config, router())


def test_failed_disable_restores_every_object_in_place():
    session, driver = device()
    _, item = publish(driver, router())
    order = {path: [row.get('comment') for row in session.rows[path]] for path in (NAT, FILTER)}
    item['dns_guard']['enabled'] = False
    _, plan = preview(driver, item)
    session.counts, session.fail = {}, {('DELETE', ADDRESSES, 2)}
    result = execute(plan, driver, None)
    assert not result['ok'] and not any(entry['status'] == 'failed' for entry in result['rollback'])
    assert {path: [row.get('comment') for row in session.rows[path]] for path in (NAT, FILTER)} == order
    restored = driver.dns_guard_manifest
    assert len(owned_entries(restored, snapshot(driver))) == len(restored['resources']) == 10


def test_rule_sync_baseline_survives_publishing_the_guard(monkeypatch):
    from backend.routers import rule_sync
    monkeypatch.setattr('backend.routes.routers._agent', lambda agent_id: None)
    monkeypatch.setattr(rule_sync, 'selected_sources', lambda config, item: {})
    enabled = router()
    assert rule_sync.controls({}, enabled) == rule_sync.controls({}, {**enabled, 'dns_guard_state': {'resources': []}})
    disabled = {**enabled, 'dns_guard': {**enabled['dns_guard'], 'enabled': False}, 'dns_guard_state': {'resources': []}}
    withdrawn = {key: value for key, value in disabled.items() if key != 'dns_guard_state'}
    assert rule_sync.controls({}, disabled) == rule_sync.controls({}, withdrawn)


def test_manual_disable_without_netwatch_is_reported_as_an_outside_edit():
    session, driver = device()
    _, item = publish(driver, router(failover=False))
    next(row for row in session.rows[NAT] if row.get('comment') == 'cf-dnsguard:hijack')['disabled'] = 'true'
    report, plan = preview(driver, item)
    assert any('外部修改' in blocker for blocker in plan['blockers'])
    assert report['dns_guard']['status'] == 'unknown'


def test_resume_flush_keeps_exempt_hosts_and_tolerates_expired_flows():
    spec = {'agent': OXIDNS, 'interface': 'bridge', 'network': '10.0.0.0/24', 'block_dot': False, 'failover': True,
            'bypass': ['10.0.0.2/32', '10.0.0.4/32', '10.0.0.48/28']}
    script = next(item for item in dns_guard.resources(spec) if item['role'] == 'netwatch')['values']['up-script']
    assert 'dst-port=53 and !dstnat and dst-address!=10.0.0.2 and src-address!=10.0.0.2 and src-address!=10.0.0.4]' in script
    assert '10.0.0.48' not in script  # networks cannot be matched by a plain address comparison
    assert ':do {/ip firewall connection remove $c} on-error={}' in script
