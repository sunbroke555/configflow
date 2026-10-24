"""Per-rule-set RouterOS policy routing: selection, migration and compensation."""
import copy
import ipaddress
import re
from collections import Counter
from urllib.parse import unquote

import pytest

from backend.routers import ros_address_lists as address_lists
from backend.routers.base import RouterError
from backend.routers.plan import build_plan
from backend.routers.release import execute
from backend.routers.report import build_report
from backend.routers.ros_policy import (
    ADDRESSES, FILTER, MANGLE, NAT, PATHS, RAW, ROUTES, RULES, SETTINGS, TABLES, VRFS,
    config_row, inspect_desired, smart_sources, snapshot,
)
from backend.routers.ros_policy_write import verify
from backend.routers.routeros import MissingResource, RouterOSDriver, UncertainWrite
from backend.test_agent_upgrade_auth import authenticated_app  # noqa: F401

GATEWAY = '192.168.0.3'
TARGET = '91.108.4.0/22'
ROUTER = {'id': 'ros1', 'type': 'routeros', 'endpoint': 'http://192.168.0.1',
          'ip_rule_sets': [], 'ros_policy_rule_sets': ['telegram']}


class MemoryROS(RouterOSDriver):
    """Exercise real driver reads/writes against ordered, ID-based REST state."""
    def __init__(self):
        self.rows = {path: [] for path in (*PATHS, '/interface/list', '/interface/list/member')}
        self.rows[TABLES] = [{'.id': '*0', 'name': 'main', 'fib': 'true'}]
        self.rows[ROUTES] = [
            {'.id': '*1', 'dst-address': '192.168.0.0/24', 'gateway': 'bridge',
             'immediate-gw': 'bridge', 'routing-table': 'main', 'active': 'true',
             'connect': 'true', 'dynamic': 'true'},
            {'.id': '*2', 'dst-address': '198.18.0.0/16', 'gateway': GATEWAY,
             'routing-table': 'main', 'active': 'true', 'immediate-gw': GATEWAY + '%bridge',
             'disabled': 'false', 'comment': 'cf'},
        ]
        self.rows[NAT] = [{'.id': '*3', 'chain': 'srcnat', 'action': 'masquerade',
                           'out-interface': 'pppoe-out1', 'disabled': 'false'}]
        self.writes, self.counts, self.next_id = [], Counter(), 20
        self.fail = self.lost = None
        self.inactive = False
        self.settings = {'policy-rules': 'mangle,vrf-lookup,vrf-unreach,local,user,main'}
        self.resource = {'version': '7.24.5 (stable)'}

    def close(self):
        pass

    def _request(self, method, path, values=None):
        path, _, query = unquote(path).partition('?')
        if path == '/execute':
            return self._execute(values)
        if path == ADDRESSES + '/remove':
            return self._remove(values)
        if query:
            assert method == 'GET' and path == ADDRESSES
            key, _, value = query.partition('=')
            return [copy.deepcopy(row) for row in self.rows[ADDRESSES] if row.get(key) == value]
        if path == SETTINGS:
            assert method == 'GET'
            return copy.deepcopy(self.settings)
        if path == '/system/resource':
            assert method == 'GET'
            if self.resource is None:
                raise RouterError('resource unavailable')
            return copy.deepcopy(self.resource)
        collection = next(key for key in sorted(self.rows, key=len, reverse=True)
                          if path == key or path.startswith(key + '/'))
        row_id = path[len(collection) + 1:]
        rows = self.rows[collection]
        current = next((row for row in rows if row['.id'] == row_id), None)
        if method == 'GET':
            if row_id and current is None:
                raise MissingResource('absent')
            return copy.deepcopy(current if row_id else rows)
        self.writes.append((method, path, copy.deepcopy(values)))
        self.counts[method, collection] += 1
        point = (method, collection, self.counts[method, collection])
        if point == self.fail:
            raise RouterError('injected failure')
        if method == 'PUT':
            assert not row_id
            assert not set(values) & {'.id', 'active', 'dynamic', 'immediate-gw', 'bytes', 'packets'}
            self.next_id += 1
            current = {'.id': f'*{self.next_id:X}', **copy.deepcopy(values)}
            if collection == ADDRESSES:
                current['address'] = current['address'].removesuffix('/32')
            if collection == ROUTES:
                current.update(active='false' if self.inactive else 'true',
                               **{'immediate-gw': GATEWAY + '%bridge'})
            before = current.pop('place-before', None)
            if before:
                rows.insert(next(i for i, row in enumerate(rows) if row['.id'] == before), current)
            else:
                rows.append(current)
        elif method == 'DELETE':
            if current is not None:
                rows.remove(current)
        elif method == 'PATCH':
            assert current is not None
            current.update(copy.deepcopy(values))
        else:
            raise AssertionError(method)
        if point == self.lost:
            raise UncertainWrite('response lost after commit')
        return copy.deepcopy(current or {})

    def _execute(self, values):
        """Run the address-list batch script; each member counts as one PUT."""
        if 'cf-digest:' in values['script']:
            self.counts['DIGEST', ADDRESSES] += 1
            names = [_unescape(name) for name in DIGEST_LIST.findall(values['script'])]
            lines = []
            for index, name in enumerate(names):
                count, value = address_lists.digest([row for row in self.rows[ADDRESSES] if row.get('list') == name])
                lines.append(f'cf-digest:{index}:{count}:{value}')
            return {'ret': '\r\n'.join(lines)}
        if 'cf-names:' in values['script']:
            pattern = re.compile(_unescape(NAMES_PATTERN.search(values['script'])[1]))
            excluded = {_unescape(name) for name in NAMES_EXCLUDED.findall(values['script'])}
            names = sorted({row['list'] for row in self.rows[ADDRESSES]
                            if pattern.search(row.get('list', '')) and row['list'] not in excluded})
            return {'ret': 'cf-names:\r\n' + '\r\n'.join(names)}
        match = BATCH.fullmatch(values['script'])
        assert match and values.get('as-string') == '', values['script']
        fields = {key: {'yes': 'true', 'no': 'false'}[bare] if bare else _unescape(quoted)
                  for key, quoted, bare in FIELD.findall(match['params'])}
        failed, first = 0, ''
        existing = {(row.get('list'), row.get('address')) for row in self.rows[ADDRESSES]}
        for address in (_unescape(item) for item in ITEM.findall(match['items'])):
            self.writes.append(('PUT', ADDRESSES, {**fields, 'address': address}))
            self.counts['PUT', ADDRESSES] += 1
            point = ('PUT', ADDRESSES, self.counts['PUT', ADDRESSES])
            if point == self.fail:
                raise RouterError('injected failure')
            network = ipaddress.ip_network(address, strict=False)
            stored = str(network.network_address) if network.prefixlen == network.max_prefixlen else str(network)
            if (fields['list'], stored) in existing:
                failed += 1
                first = first or f'{address} failure: already have such entry'
                continue
            existing.add((fields['list'], stored))
            self.next_id += 1
            self.rows[ADDRESSES].append({'.id': f'*{self.next_id:X}', **fields, 'address': stored})
            if point == self.lost:
                raise UncertainWrite('response lost after commit')
        return {'ret': f'cf-done:{failed}:{first}'}

    def _remove(self, values):
        """Comma-separated ids stop at the first missing one, like RouterOS."""
        for row_id in values['.id'].split(','):
            self.writes.append(('DELETE', ADDRESSES + '/' + row_id, None))
            self.counts['DELETE', ADDRESSES] += 1
            if ('DELETE', ADDRESSES, self.counts['DELETE', ADDRESSES]) == self.fail:
                raise RouterError('injected failure')
            current = next((row for row in self.rows[ADDRESSES] if row['.id'] == row_id), None)
            if current is None:
                raise RouterError('RouterOS POST /ip/firewall/address-list/remove 返回 HTTP 404')
            self.rows[ADDRESSES].remove(current)
        return []


BATCH = re.compile(r':local n 0\n:local m ""\n:foreach a in=\{(?P<items>.*)\} do=\{:onerror e in=\{'
                   r'/ip/firewall/address-list/add (?P<params>.*) address=\$a\} do=\{:set n \(\$n\+1\); '
                   r':if \(\$n=1\) do=\{:set m \(\$a \. " " \. \$e\)\}\}\}\n:put \("cf-done:" \. \$n \. ":" \. \$m\)',
                   re.DOTALL)
DIGEST_LIST = re.compile(r'\(\$groups->"((?:[^"\\]|\\.)*)"\)')
NAMES_PATTERN = re.compile(r'find where list~"((?:[^"\\]|\\.)*)"')
NAMES_EXCLUDED = re.compile(r'list!="((?:[^"\\]|\\.)*)"')
FIELD = re.compile(r'([\w-]+)=(?:"((?:[^"\\]|\\.)*)"|(yes|no))')
ITEM = re.compile(r'"((?:[^"\\]|\\.)*)"')


def _unescape(text):
    return re.sub(r'\\(.)', lambda match: {'n': '\n', 'r': '\r', 't': '\t'}.get(match[1], match[1]), text)


def is_write(method, path, values=None):
    """Digest and name scripts run through POST /execute but only read."""
    script = (values or {}).get('script', '') if path == '/execute' else ''
    return method != 'GET' and 'cf-digest:' not in script and 'cf-names:' not in script


def desired_for(driver, selected=True, cidrs=None):
    desired = {'fake_ip_range': '198.18.0.0/16', 'proxy_gateway': GATEWAY, 'warnings': [],
               'proxy_cidrs': [{'cidr': '198.18.0.0/16', 'sources': ['fake-ip']}]}
    if not selected:
        desired['proxy_cidrs'].append({'cidr': TARGET, 'sources': ['Telegram']})
    desired['ros_policy'] = {
        'selected': ['telegram'] if selected else [], 'sources': ['192.168.0.101/32'] if selected else [],
        'cidrs': [{'cidr': cidr, 'sources': ['Telegram']} for cidr in (cidrs or [TARGET])] if selected else [],
        'error': '', 'owner': 'cf-ros-policy-test', 'previous': getattr(driver, 'ros_policy_manifest', None),
    }
    return desired


def inspect(driver, desired=None):
    desired = desired or desired_for(driver)
    state = driver.read_state()
    state.ros_policy = snapshot(driver)
    state.nat_context.update(routing_rules=driver.rows[RULES], vrfs=driver.rows[VRFS])
    if state.nat_context['mangle'] is None:
        state.nat_context['mangle'] = driver.rows[MANGLE]
    report = build_report(desired, state)
    plan = build_plan(ROUTER, desired, state, report, {'status': 'disabled'}, driver)
    return report, plan


def release(driver):
    report, plan = inspect(driver)
    assert not plan['blockers'], plan['blockers']
    result = execute(plan, driver, None)
    assert result['ok'], result
    return report, plan


def configurations(driver):
    return {path: [config_row(row) for row in rows] for path, rows in driver.rows.items()}


def test_policy_view_reads_only_recorded_ids_and_survives_external_changes():
    from backend.routers.ros_policy_view import read_policy_objects
    driver = MemoryROS()
    release(driver)
    manifest = copy.deepcopy(driver.ros_policy_manifest)
    # A matching name is not sufficient to adopt a foreign object.
    driver.rows[ADDRESSES].append({'.id': '*foreign', 'list': 'CF_Source_Proxy_IP_01', 'address': '10.0.0.1'})
    source = manifest['resources'][0]
    driver.rows[source['path']] = [row for row in driver.rows[source['path']] if row['.id'] != source['id']]
    marked = next(row for row in driver.rows[MANGLE] if row['action'] == 'mark-routing')
    marked.update(disabled='true', **{'private-field': 'do-not-export'})
    writes = copy.deepcopy(driver.writes)
    view = read_policy_objects(driver, manifest)
    assert len(view) == len(manifest['resources'])
    assert not any(row['id'] == '*foreign' for row in view)
    assert view[0]['status'] == 'missing' and view[0]['fields']['address'] == source['config']['address']
    changed = next(row for row in view if row['kind'] == 'mangle' and row['id'] == marked['.id'])
    assert changed['status'] == 'changed' and changed['fields']['disabled'] == 'true'
    assert 'private-field' not in changed['fields']
    assert any(row['kind'] == 'routes' and row['fields']['immediate-gw'] == GATEWAY + '%bridge' for row in view)
    assert read_policy_objects(driver, None) == []
    assert driver.writes == writes


def test_policy_view_api_is_authenticated_read_only_and_type_scoped(authenticated_app, monkeypatch):
    client, manager, agents, admin, _ = authenticated_app
    from backend.routes import routers as routes_module
    driver = MemoryROS()
    monkeypatch.setitem(routes_module.DRIVERS, 'routeros', lambda *args, **kwargs: driver)
    monkeypatch.setattr(manager, 'get_all_agents', lambda: [
        {'id': 'same', 'name': 'Same profile', 'service_type': 'oxidns', 'profile_id': 'default'},
        {'id': 'legacy', 'name': 'Default profile', 'service_type': 'oxidns'},
        {'id': 'other', 'name': 'Other profile', 'service_type': 'oxidns', 'profile_id': 'other'},
        {'id': 'proxy', 'name': 'Proxy', 'service_type': 'mihomo', 'profile_id': 'default'},
    ])
    created = client.post('/api/routers', headers=admin, json={**ROUTER, 'name': 'ROS',
        'username': 'admin', 'password': 'router-secret'})
    router_id = created.get_json()['router']['id']
    path = f'/api/routers/{router_id}/policy'
    assert client.get(path).status_code == 401
    assert client.post(path, headers=admin).status_code == 405
    assert client.get('/api/routers/missing/policy', headers=admin).status_code == 404
    response = client.get(path, headers=admin)
    assert response.status_code == 200 and response.get_json()['policy']['objects'] == []
    assert {item['id'] for item in response.get_json()['policy']['source_agents']} == {'same', 'legacy'}
    assert response.headers['Cache-Control'] == 'no-store'
    assert 'router-secret' not in response.get_data(as_text=True)
    assert not driver.writes
    release(driver)
    config = routes_module.get_config()
    config['routers']['items'][0]['ros_policy_state'] = driver.ros_policy_manifest
    routes_module.save_config(config)
    writes = copy.deepcopy(driver.writes)
    response = client.get(path, headers=admin)
    assert len(response.get_json()['policy']['objects']) == len(driver.ros_policy_manifest['resources'])
    assert driver.writes == writes
    monkeypatch.setattr(driver, '_list', lambda path: (_ for _ in ()).throw(RouterError('设备无法连接')))
    assert client.get(path, headers=admin).status_code == 502
    client.put(f'/api/routers/{router_id}', headers=admin, json={'ros_policy_rule_sets': []})
    other = client.post('/api/routers', headers=admin, json={'name': 'ikuai', 'type': 'ikuai',
        'endpoint': 'http://192.168.0.1', 'username': 'admin', 'password': 'pw'}).get_json()['router']['id']
    assert client.get(f'/api/routers/{other}/policy', headers=admin).status_code == 400


def test_longest_prefix_sources_exclude_direct_and_gateway():
    ranges = smart_sources([
        {'ip': '192.168.0.0/24', 'mode': 'smart'},
        {'ip': '192.168.0.0/25', 'mode': 'direct'},
        {'ip': '192.168.0.101', 'mode': 'smart'},
        {'ip': '192.168.0.151', 'mode': 'direct'},
        {'ip': 'fd00::/64', 'mode': 'smart'},
    ], GATEWAY)
    contains = lambda ip: any(ipaddress.ip_address(ip) in ipaddress.ip_network(net) for net in ranges)
    assert contains('192.168.0.101') and contains('192.168.0.150')
    assert not contains('192.168.0.51') and not contains(GATEWAY)
    assert not contains('192.168.0.151') and not contains('192.168.1.101')
    assert smart_sources([], GATEWAY) == []


def test_mixed_report_publish_noop_and_clear_to_static():
    driver = MemoryROS()
    report, plan = release(driver)
    assert [item['cidr'] for item in report['routes']['items']] == ['198.18.0.0/16']
    assert report['policy_routing']['items'][0]['cidr'] == TARGET
    assert report['summary']['pending'] == 1
    assert not any(row['dst-address'] == TARGET for row in driver.rows[ROUTES])
    assert driver.rows[MANGLE][-1]['action'] == 'jump'
    assert driver.rows[MANGLE][-2]['new-routing-mark'] == driver.rows[TABLES][-1]['name']
    assert driver.rows[ROUTES][-1]['gateway'] == GATEWAY + '@main'
    assert len(driver.rows[NAT]) == 1  # WAN-only masquerade does not need bypass
    assert not any(path == '/routing/rule' for _, path, _ in driver.writes)
    report, plan = inspect(driver)
    assert report['in_sync'] and plan['empty'] and not plan['blockers']
    before = len(driver.writes)
    verify(driver, plan['checks']['ros_policy'])
    assert len(driver.writes) == before
    report, plan = inspect(driver, desired_for(driver, selected=False))
    assert report['summary']['pending'] == 1
    assert not plan['blockers']
    result = execute(plan, driver, None)
    assert result['ok'], result
    assert driver.ros_policy_manifest is None
    assert not driver.rows[MANGLE] and not driver.rows[ADDRESSES]
    assert len(driver.rows[TABLES]) == 1
    assert any(row['dst-address'] == TARGET and row.get('gateway') == GATEWAY for row in driver.rows[ROUTES])


def test_exact_managed_main_route_migrates_after_activation():
    driver = MemoryROS()
    driver.rows[ROUTES].append({'.id': '*AB', 'dst-address': TARGET, 'gateway': GATEWAY,
                               'routing-table': 'main', 'active': 'true', 'comment': 'cf-Telegram'})
    report, plan = release(driver)
    assert not report['routes']['extra']
    assert not any(row['.id'] == '*AB' for row in driver.rows[ROUTES])
    activation = next(i for i, (_, path, data) in enumerate(driver.writes)
                      if path == MANGLE and data.get('action') == 'jump')
    deletion = next(i for i, (method, path, _) in enumerate(driver.writes)
                    if method == 'DELETE' and path.endswith('/*AB'))
    assert activation < deletion


@pytest.mark.parametrize('needs_nat', [False, True])
def test_readable_names_share_one_group_and_all_references_resolve(needs_nat):
    driver = MemoryROS()
    if needs_nat:
        driver.rows[NAT][0].pop('out-interface')
    _, plan = release(driver)
    assert {row['list'] for row in driver.rows[ADDRESSES]} == {
        'CF_Source_Proxy_IP_01', 'CF_Destination_Proxy_IP_01', 'CF_Bypass_IP_01'}
    assert driver.rows[TABLES][-1]['name'] == 'CF_Proxy_Routing_01'
    assert driver.rows[ROUTES][-1]['routing-table'] == 'CF_Proxy_Routing_01'
    bypass, mark, entry = driver.rows[MANGLE]
    assert bypass['chain'] == mark['chain'] == entry['jump-target'] == 'CF_Proxy_Routing_01'
    assert bypass['src-address-list'] == 'CF_Bypass_IP_01'
    assert mark['new-routing-mark'] == 'CF_Proxy_Routing_01'
    assert entry['src-address-list'] == 'CF_Source_Proxy_IP_01'
    assert entry['dst-address-list'] == 'CF_Destination_Proxy_IP_01'
    if needs_nat:
        policy_nat = next(row for row in driver.rows[NAT] if row.get('comment') == 'cf-ros-policy-test')
        assert policy_nat['src-address-list'] == entry['src-address-list']
        assert policy_nat['dst-address-list'] == entry['dst-address-list']
    verify(driver, plan['checks']['ros_policy'])
    assert inspect(driver)[1]['empty']


@pytest.mark.parametrize('path,fields', [
    (ADDRESSES, {'list': 'CF_Source_Proxy_IP_01', 'address': '203.0.113.1'}),
    (ADDRESSES, {'list': 'CF_Destination_Proxy_IP_01', 'address': '203.0.113.1'}),
    (ADDRESSES, {'list': 'CF_Bypass_IP_01', 'address': '203.0.113.1'}),
    (TABLES, {'name': 'CF_Proxy_Routing_01', 'fib': 'true'}),
    (MANGLE, {'chain': 'CF_Proxy_Routing_01', 'action': 'return', 'disabled': 'true'}),
    (FILTER, {'chain': 'forward', 'action': 'accept', 'src-address-list': '!CF_Source_Proxy_IP_01'}),
    (RAW, {'chain': 'prerouting', 'action': 'accept', 'dst-address-list': 'CF_Destination_Proxy_IP_01'}),
    (ROUTES, {'dst-address': '203.0.113.0/24', 'gateway': '192.168.0.254@CF_Proxy_Routing_01',
              'routing-table': 'another', 'disabled': 'true'}),
])
def test_names_occupied_by_definitions_or_references_reserve_the_whole_group(path, fields):
    driver = MemoryROS()
    foreign = {'.id': '*FOREIGN', **fields}
    driver.rows[path].append(copy.deepcopy(foreign))
    release(driver)
    assert driver.rows[TABLES][-1]['name'] == 'CF_Proxy_Routing_02'
    assert {row['list'] for row in driver.rows[ADDRESSES] if row['.id'] != '*FOREIGN'} == {
        'CF_Source_Proxy_IP_02', 'CF_Destination_Proxy_IP_02', 'CF_Bypass_IP_02'}
    _, withdrawal = inspect(driver, desired_for(driver, selected=False))
    assert execute(withdrawal, driver, None)['ok']
    assert next(row for row in driver.rows[path] if row['.id'] == '*FOREIGN') == foreign
    assert not any(url.endswith('/*FOREIGN') for _, url, _ in driver.writes)


def test_updates_keep_old_group_until_activation_and_reuse_only_freed_numbers():
    driver = MemoryROS()
    release(driver)
    start = len(driver.writes)
    desired = desired_for(driver, cidrs=[TARGET, '149.154.160.0/20'])
    _, update = inspect(driver, desired)
    assert execute(update, driver, None)['ok']
    writes = driver.writes[start:]
    activation = next(i for i, (method, path, values) in enumerate(writes)
                      if method == 'PUT' and path == MANGLE and values.get('action') == 'jump')
    first_deletion = next(i for i, (method, _, _) in enumerate(writes) if method == 'DELETE')
    assert activation < first_deletion
    assert driver.rows[TABLES][-1]['name'] == 'CF_Proxy_Routing_02'
    assert all(row['list'].endswith('_02') for row in driver.rows[ADDRESSES])
    assert inspect(driver, desired_for(driver, cidrs=[TARGET, '149.154.160.0/20']))[1]['empty']
    release(driver)
    assert driver.rows[TABLES][-1]['name'] == 'CF_Proxy_Routing_01'
    assert all(row['list'].endswith('_01') for row in driver.rows[ADDRESSES])


def test_exhausted_readable_groups_block_without_mutating_foreign_objects():
    driver = MemoryROS()
    driver.rows[ADDRESSES] = [{'.id': f'*F{i}', 'list': f'CF_Bypass_IP_{i:02d}',
                              'address': '203.0.113.1', 'disabled': 'true'} for i in range(1, 257)]
    before = configurations(driver)
    _, plan = inspect(driver)
    assert any('没有可用的策略资源名称' in message for message in plan['blockers'])
    assert not execute(plan, driver, None)['ok']
    assert not driver.writes and configurations(driver) == before


@pytest.mark.parametrize('outcome', ['success', 'rollback', 'external_edit', 'external_reference'])
def test_legacy_hashed_names_migrate_through_guarded_release(outcome):
    driver = MemoryROS()
    _, legacy = inspect(driver)
    op = legacy['stages'][1]['ops'][0]
    # Seed the old, previously deployed payload and manifest format.
    old_name = 'cfp_0123456789abcdef'
    rename = {'CF_Source_Proxy_IP_01': old_name + '_src',
              'CF_Destination_Proxy_IP_01': old_name + '_dst',
              'CF_Bypass_IP_01': old_name + '_bypass', 'CF_Proxy_Routing_01': old_name}
    for item in op['bundle']:
        item['values'] = {key: rename.get(value, value) for key, value in item['values'].items()}
    op['spec'].pop('naming_version')
    legacy['checks']['ros_policy']['spec'].pop('naming_version', None)
    assert execute(legacy, driver, None)['ok']
    before = configurations(driver)
    if outcome == 'external_edit':
        driver.rows[MANGLE][-1]['comment'] = 'changed outside ConfigFlow'
    elif outcome == 'external_reference':
        driver.rows[RAW].append({'.id': '*FOREIGN', 'chain': 'prerouting', 'action': 'accept',
                                 'src-address-list': old_name + '_src'})
    start = len(driver.writes)
    _, plan = inspect(driver)
    if outcome.startswith('external_'):
        assert plan['blockers']
        assert not execute(plan, driver, None)['ok'] and len(driver.writes) == start
        return
    assert not plan['blockers'] and not plan['empty']
    if outcome == 'rollback':
        driver.fail = ('DELETE', ADDRESSES, 1)
    result = execute(plan, driver, None)
    if outcome == 'rollback':
        assert not result['ok'] and not any(item['status'] == 'failed' for item in result['rollback'])
        assert configurations(driver) == before
        assert 'naming_version' not in driver.ros_policy_manifest['spec']
        verify(driver, legacy['checks']['ros_policy'])
    else:
        assert result['ok']
        assert driver.rows[TABLES][-1]['name'] == 'CF_Proxy_Routing_01'
        assert all(not str(value).startswith('cfp_') for rows in driver.rows.values()
                   for row in rows for value in row.values())
        assert inspect(driver)[1]['empty']


@pytest.mark.parametrize('case', ['fasttrack', 'mangle', 'vrf', 'routing_rule'])
def test_unsafe_plans_never_write(case):
    driver = MemoryROS()
    desired = desired_for(driver)
    if case == 'fasttrack':
        driver.rows[FILTER].append({'.id': '*F', 'action': 'fasttrack-connection', 'chain': 'forward'})
    elif case == 'mangle':
        driver.rows[MANGLE].append({'.id': '*F', 'chain': 'prerouting', 'action': 'mark-routing'})
    elif case == 'vrf':
        driver.rows[VRFS].append({'.id': '*F', 'name': 'another'})
    else:
        driver.rows[RULES].append({'.id': '*F', 'action': 'lookup', 'table': 'another'})
    _, plan = inspect(driver, desired)
    assert plan['blockers']
    assert not execute(plan, driver, None)['ok']
    assert driver.writes == []


@pytest.mark.parametrize('method,path,nth', [
    ('PUT', ADDRESSES, 2), ('PUT', TABLES, 1), ('PUT', ROUTES, 1),
    ('PUT', MANGLE, 2), ('PUT', MANGLE, 3),
])
@pytest.mark.parametrize('after_commit', [False, True])
def test_partial_create_rolls_back(method, path, nth, after_commit):
    driver = MemoryROS()
    before = configurations(driver)
    setattr(driver, 'lost' if after_commit else 'fail', (method, path, nth))
    _, plan = inspect(driver)
    assert not plan['blockers']
    result = execute(plan, driver, None)
    assert not result['ok'] and not any(item['status'] == 'failed' for item in result['rollback']), result
    assert configurations(driver) == before


def test_failure_removing_old_bundle_restores_ids_and_order():
    driver = MemoryROS()
    driver.rows[NAT][0].pop('out-interface')  # policy-scoped exemption is required
    release(driver)
    before = configurations(driver)
    driver.fail = ('DELETE', ADDRESSES, 1)
    desired = desired_for(driver, cidrs=[TARGET, '149.154.160.0/20'])
    _, plan = inspect(driver, desired)
    result = execute(plan, driver, None)
    assert not result['ok'] and not any(item['status'] == 'failed' for item in result['rollback']), result
    assert configurations(driver) == before
    _, restored_plan = inspect(driver)
    assert restored_plan['empty'] and not restored_plan['blockers']


@pytest.mark.parametrize('reference_path', [FILTER, RAW, NAT])
def test_external_edit_reference_and_name_collision_are_preserved(reference_path):
    driver = MemoryROS()
    _, initial = inspect(driver)
    bundle = initial['stages'][1]['ops'][0]['bundle']
    name = next(item['values']['name'] for item in bundle if item['path'] == TABLES)
    driver.rows[TABLES].append({'.id': '*FOREIGN', 'name': name, 'fib': 'true'})
    release(driver)
    assert driver.rows[TABLES][-1]['name'] != name
    own = driver.rows[MANGLE][-1]
    own['comment'] = 'externally edited'
    _, plan = inspect(driver)
    assert any('外部修改' in item for item in plan['blockers'])
    own['comment'] = 'cf-ros-policy-test'
    driver.rows[reference_path].append({'.id': '*F', 'chain': 'forward', 'action': 'accept',
                                'src-address-list': own['src-address-list']})
    _, plan = inspect(driver, desired_for(driver, selected=False))
    assert any('其他配置正在使用' in item for item in plan['blockers'])
    assert next(row for row in driver.rows[TABLES] if row['.id'] == '*FOREIGN')['name'] == name


def test_changed_preview_and_inactive_next_hop_never_activate():
    driver = MemoryROS()
    _, plan = inspect(driver)
    driver.rows[FILTER].append({'.id': '*F', 'chain': 'forward', 'action': 'drop', 'src-address-list': 'foreign'})
    result = execute(plan, driver, None)
    assert not result['ok'] and not driver.writes
    driver = MemoryROS()
    driver.inactive = True
    _, plan = inspect(driver)
    result = execute(plan, driver, None)
    assert not result['ok']
    assert not any(path == MANGLE for _, path, _ in driver.writes)


def test_only_selected_real_ip_sets_and_saved_device_policies_are_read(monkeypatch):
    calls = []
    def compute(config, gateway, selected):
        calls.append(selected)
        return {'proxy_cidrs': [{'cidr': '198.18.0.0/16', 'sources': ['fake-ip']},
                               {'cidr': TARGET, 'sources': ['Telegram']}], 'warnings': []}
    monkeypatch.setattr('backend.routers.desired.compute_desired', compute)
    monkeypatch.setattr('backend.common.oxidns_device_policy.read_policies', lambda settings, config: {
        'editable': True, 'policies': [{'ip': '192.168.0.101', 'mode': 'smart'}]})
    desired = inspect_desired({'oxidns': {}}, ROUTER, {'proxy_gateway': GATEWAY})
    assert calls == [['telegram']]
    assert desired['cidrs'] == [{'cidr': TARGET, 'sources': ['Telegram']}]
    assert desired['sources'] == ['192.168.0.101/32']
    assert not desired['error']


def test_store_accepts_independent_modes_rejects_duplicates_and_keeps_manifest_private():
    from backend.routers.store import normalize, public_view, RouterValidationError
    payload = {**ROUTER, 'name': 'ROS', 'username': 'admin', 'password': 'pw'}
    normalized = normalize(payload)
    assert normalized['ros_policy_rule_sets'] == ['telegram']
    assert normalize({**payload, 'ros_policy_rule_sets': []})['ros_policy_rule_sets'] == []
    assert normalized['ip_rule_sets'] == []
    assert normalize({**payload, 'ip_rule_sets': ['other']})['ip_rule_sets'] == ['other']
    with pytest.raises(RouterValidationError, match='一种方式'):
        normalize({**payload, 'ip_rule_sets': ['telegram']})
    with pytest.raises(RouterValidationError, match='ID 列表'):
        normalize({**payload, 'ros_policy_rule_sets': 'telegram'})
    with pytest.raises(RouterValidationError, match='RouterOS'):
        normalize({**payload, 'type': 'ikuai'})
    old = {**normalized, 'ros_policy_state': {'resources': [{'private': True}]}}
    assert 'ros_policy_state' not in public_view(old)
    assert normalize({'name': 'Renamed', 'ros_policy_state': {}}, old)['ros_policy_state'] == old['ros_policy_state']
    with pytest.raises(RouterValidationError, match='管理地址'):
        normalize({'endpoint': 'http://192.168.0.9'}, old)


def test_legacy_selection_reads_and_edits_keep_effective_modes_without_mutating_original():
    from backend.routers.store import normalize, public_view, static_rule_sets
    old = {**ROUTER, 'name': 'ROS', 'username': 'admin', 'password': 'pw',
           'ip_rule_sets': ['telegram', 'other']}
    before = copy.deepcopy(old)
    assert public_view(old)['ip_rule_sets'] == static_rule_sets(old) == ['other']
    renamed = normalize({'name': 'Renamed'}, old)
    assert renamed['ip_rule_sets'] == ['other'] and renamed['ros_policy_rule_sets'] == ['telegram']
    cleared = normalize({'ros_policy_rule_sets': []}, old)
    assert cleared['ip_rule_sets'] == ['other'] and cleared['ros_policy_rule_sets'] == []
    switched = normalize({'ros_policy_rule_sets': [], 'ip_rule_sets': ['telegram', 'other']}, old)
    assert switched['ip_rule_sets'] == ['telegram', 'other']
    assert old == before


@pytest.mark.parametrize('change', ['inactive', 'order', 'nat', 'new_nat', 'next_hop', 'priority'])
def test_live_drift_cannot_be_reported_as_synchronized(change):
    driver = MemoryROS()
    if change == 'nat':
        driver.rows[NAT][0].pop('out-interface')
    _, plan = release(driver)
    if change == 'inactive':
        driver.rows[ROUTES][-1]['active'] = 'false'
    elif change == 'order':
        driver.rows[MANGLE][:2] = reversed(driver.rows[MANGLE][:2])
    elif change == 'nat':
        driver.rows[NAT].reverse()
    elif change == 'new_nat':
        driver.rows[NAT].append({'.id': '*F', 'chain': 'srcnat', 'action': 'masquerade'})
    elif change == 'next_hop':
        driver.rows[ROUTES][-1]['immediate-gw'] = GATEWAY + '%wan'
    else:
        driver.settings['policy-rules'] = 'main,mangle'
    with pytest.raises(RouterError):
        verify(driver, plan['checks']['ros_policy'])
    report, _ = inspect(driver)
    assert not report['in_sync']


def test_later_nat_failure_undoes_complete_policy_bundle():
    driver = MemoryROS()
    driver.rows[NAT][0].pop('out-interface')
    before = configurations(driver)
    # First NAT is in the policy bundle; the next is the ordinary fake-IP bypass.
    driver.fail = ('PUT', NAT, 2)
    _, plan = inspect(driver)
    result = execute(plan, driver, None)
    assert not result['ok'] and result['failed_stage'] == 'nat_bypass'
    assert not any(item['status'] == 'failed' for item in result['rollback']), result
    assert configurations(driver) == before and driver.ros_policy_manifest is None


@pytest.mark.parametrize('next_mode', ['remove', 'static'])
def test_api_persists_independent_modes_and_selector_changes_invalidate_preview(authenticated_app, monkeypatch, next_mode):
    client, _, agents, admin, _ = authenticated_app
    from backend.routes import routers as routes_module
    driver = MemoryROS()
    monkeypatch.setitem(routes_module.DRIVERS, 'routeros', lambda *args, **kwargs: driver)
    monkeypatch.setattr(routes_module, '_agent', lambda agent_id: {'host': GATEWAY})
    def compute(config, gateway, selected):
        entries = [{'cidr': '198.18.0.0/16', 'sources': ['fake-ip']}]
        if 'telegram' in selected:
            entries.append({'cidr': TARGET, 'sources': ['Telegram']})
        if 'other' in selected:
            entries.append({'cidr': '8.8.8.8/32', 'sources': ['Other']})
        return {'fake_ip_range': '198.18.0.0/16', 'proxy_gateway': gateway, 'proxy_cidrs': entries, 'warnings': []}
    monkeypatch.setattr(routes_module, 'compute_desired', compute)
    monkeypatch.setattr('backend.routers.desired.compute_desired', compute)
    monkeypatch.setattr('backend.common.oxidns_device_policy.read_policies', lambda settings, config: {
        'editable': True, 'policies': [{'ip': '192.168.0.101', 'mode': 'smart'}]})
    created = client.post('/api/routers', headers=admin, json={**ROUTER, 'name': 'ROS', 'ip_rule_sets': ['other'],
        'username': 'admin', 'password': 'pw', 'proxy_gateway_agent_id': agents[0]['id']})
    assert created.status_code == 200, created.get_json()
    router_id = created.get_json()['router']['id']
    path = f'/api/routers/{router_id}'
    report = client.get(path + '/report', headers=admin).get_json()['report']
    assert {row['cidr'] for row in report['routes']['items']} == {'198.18.0.0/16', '8.8.8.8/32'}
    assert {row['cidr'] for row in report['policy_routing']['items']} == {TARGET}
    rejected = client.put(path, headers=admin, json={'ip_rule_sets': ['telegram']})
    assert rejected.status_code == 400 and not driver.writes
    preview = client.post(path + '/plan', headers=admin)
    assert preview.status_code == 200, preview.get_json()
    plan = preview.get_json()['plan']
    assert not plan['blockers'] and not driver.writes
    applied = client.post(path + '/apply', headers=admin, json={'fingerprint': plan['fingerprint']})
    assert applied.status_code == 200, applied.get_json()
    saved = next(row for row in routes_module.get_config()['routers']['items'] if row['id'] == router_id)
    assert saved['ros_policy_state']['resources']
    listed = client.get('/api/routers', headers=admin).get_json()
    assert 'ros_policy_state' not in listed['routers'][0]
    synced = client.post(path + '/plan', headers=admin).get_json()['plan']
    assert synced['empty'] and not synced['blockers']
    edited = client.put(path, headers=admin, json={'ros_policy_rule_sets': [],
        'ip_rule_sets': ['other', 'telegram'] if next_mode == 'static' else ['other']})
    assert edited.status_code == 200, edited.get_json()
    before = len(driver.writes)
    stale = client.post(path + '/apply', headers=admin, json={'fingerprint': synced['fingerprint']})
    assert stale.status_code == 409 and len(driver.writes) == before
    restored = client.post(path + '/apply', headers=admin,
                           json={'fingerprint': stale.get_json()['plan']['fingerprint']})
    assert restored.status_code == 200, restored.get_json()
    saved = next(row for row in routes_module.get_config()['routers']['items'] if row['id'] == router_id)
    assert 'ros_policy_state' not in saved and not driver.rows[MANGLE]
    assert any(row.get('dst-address') == TARGET for row in driver.rows[ROUTES]) is (next_mode == 'static')
    assert any(row.get('dst-address') == '8.8.8.8/32' for row in driver.rows[ROUTES])


@pytest.mark.parametrize('cidr', [TARGET, '91.108.4.0/24', '91.108.4.1/32', '91.0.0.0/8', '0.0.0.0/0'])
@pytest.mark.parametrize('gateway', [GATEWAY, '192.168.0.9'])
def test_manual_main_routes_coexist_and_survive_publish_verify_and_withdraw(cidr, gateway):
    driver = MemoryROS()
    row = {'.id': '*F', 'dst-address': cidr, 'gateway': gateway, 'routing-table': 'main',
           'active': 'true', 'disabled': 'false', 'immediate-gw': gateway + '%wan', 'comment': 'handmade'}
    driver.rows[ROUTES].append(copy.deepcopy(row))
    report, plan = release(driver)
    assert not plan['checks']['ros_policy']['migrations']
    assert not plan['checks']['ros_policy']['spec']['nat_interface']
    verify(driver, plan['checks']['ros_policy'])
    assert inspect(driver)[1]['empty']
    cleared = desired_for(driver, selected=False)
    cleared['proxy_cidrs'] = cleared['proxy_cidrs'][:1]
    _, removal = inspect(driver, cleared)
    assert not removal['blockers']
    assert execute(removal, driver, None)['ok']
    assert next(r for r in driver.rows[ROUTES] if r['.id'] == '*F') == row
    assert not any(path.endswith('/*F') for _, path, _ in driver.writes)


def test_aggregate_policy_preserves_the_reported_eight_manual_routes():
    driver = MemoryROS()
    cidrs = ['37.27.209.0/24', '45.150.242.0/24', '117.55.203.0/24', '152.69.226.0/24',
             '154.26.178.0/24', '159.195.54.201/32', '159.195.63.0/24', '185.203.56.12/32']
    rows = [{'.id': f'*F{i}', 'dst-address': cidr, 'gateway': GATEWAY, 'routing-table': 'main',
             'active': 'true', 'disabled': 'false', 'immediate-gw': GATEWAY + '%bridge',
             'comment': '大妞子服务器' if i == 7 else ''} for i, cidr in enumerate(cidrs)]
    driver.rows[ROUTES].extend(copy.deepcopy(rows))
    desired = desired_for(driver, cidrs=[c if c != '154.26.178.0/24' else '154.26.178.0/23' for c in cidrs])
    report, plan = inspect(driver, desired)
    assert not plan['blockers'], plan['blockers']
    assert any('保留 8 条' in w for w in report['policy_routing']['warnings'])
    assert execute(plan, driver, None)['ok']
    verify(driver, plan['checks']['ros_policy'])
    assert [r for r in driver.rows[ROUTES] if r['.id'].startswith('*F')] == rows
    assert inspect(driver, desired)[1]['empty']


@pytest.mark.parametrize('cidr', [TARGET, '91.108.4.1/32'])
def test_selected_static_destination_is_preserved_when_it_overlaps_policy(cidr):
    driver = MemoryROS()
    row = {'.id': '*F', 'dst-address': cidr, 'gateway': GATEWAY, 'routing-table': 'main',
           'active': 'true', 'disabled': 'false', 'immediate-gw': GATEWAY + '%bridge', 'comment': 'cf'}
    driver.rows[ROUTES].append(copy.deepcopy(row))
    desired = desired_for(driver)
    desired['proxy_cidrs'].append({'cidr': cidr, 'sources': ['another-static-set']})
    _, plan = inspect(driver, desired)
    assert not plan['blockers'] and not plan['checks']['ros_policy']['migrations']
    assert execute(plan, driver, None)['ok']
    verify(driver, plan['checks']['ros_policy'])
    assert next(r for r in driver.rows[ROUTES] if r['.id'] == '*F') == row
    assert inspect(driver, desired)[1]['empty']


def test_main_route_added_after_publish_does_not_invalidate_policy():
    driver = MemoryROS()
    _, plan = release(driver)
    driver.rows[ROUTES].append({'.id': '*F', 'dst-address': TARGET, 'gateway': GATEWAY})
    verify(driver, plan['checks']['ros_policy'])
    report, plan = inspect(driver)
    assert report['in_sync'] and plan['empty'] and not plan['blockers']


def test_policy_nat_uses_its_gateway_egress_instead_of_main_destination_egress():
    driver = MemoryROS()
    driver.rows[ROUTES].append({'.id': '*F', 'dst-address': TARGET, 'gateway': '192.168.0.9',
                               'active': 'true', 'immediate-gw': '192.168.0.9%wan'})
    driver.rows[NAT][0]['out-interface'] = 'bridge'
    _, plan = release(driver)
    spec = plan['checks']['ros_policy']['spec']
    assert spec['nat_interface'] == spec['egress_interface'] == 'bridge'
    assert any(r.get('action') == 'accept' and r.get('out-interface') == 'bridge' for r in driver.rows[NAT])


@pytest.mark.parametrize('change', ['missing', 'inactive', 'exception', 'ambiguous'])
def test_policy_gateway_lookup_still_requires_an_unambiguous_connected_route(change):
    driver = MemoryROS()
    if change == 'missing':
        driver.rows[ROUTES].pop(0)
    elif change == 'inactive':
        driver.rows[ROUTES][0]['active'] = 'false'
    elif change == 'exception':
        driver.rows[ROUTES].append({'.id': '*F', 'dst-address': GATEWAY + '/32', 'gateway': '192.168.0.9'})
    else:
        driver.rows[ROUTES].append({**driver.rows[ROUTES][0], '.id': '*F', 'gateway': 'wan'})
    _, plan = inspect(driver)
    assert plan['blockers']
    assert not execute(plan, driver, None)['ok'] and not driver.writes


@pytest.mark.parametrize('order', [None, 'main,mangle', 'user,mangle,main', 'mangle,mangle,main', 'mangle,unknown,main'])
def test_unknown_or_lower_mangle_priority_blocks_without_writes(order):
    driver = MemoryROS()
    driver.settings['policy-rules'] = order
    _, plan = inspect(driver)
    assert any('优先级' in b for b in plan['blockers'])
    assert not execute(plan, driver, None)['ok'] and not driver.writes


@pytest.mark.parametrize('resource,message', [
    ({'version': '7.21.2 (stable)'}, '策略路由需要 RouterOS 7.22 或更高版本（当前 7.21.2），请升级 RouterOS 后再发布'),
    (None, '策略路由需要 RouterOS 7.22 或更高版本（当前 版本未知），请升级 RouterOS 后再发布'),
    ({'version': '7.24.5 (stable)'}, 'RouterOS 7.24.5 未返回路由查找顺序（/routing/settings policy-rules），无法确认 Mangle 优先级'),
])
def test_missing_policy_rules_names_required_version_without_writes(resource, message):
    driver = MemoryROS()
    del driver.settings['policy-rules']
    driver.resource = resource
    _, plan = inspect(driver)
    assert message in plan['blockers']
    assert not execute(plan, driver, None)['ok'] and not driver.writes


def test_priority_changes_invalidate_preview():
    driver = MemoryROS()
    _, plan = inspect(driver)
    driver.settings['policy-rules'] = 'main,mangle'
    assert not execute(plan, driver, None)['ok'] and not driver.writes


def test_actual_policy_egress_is_checked_before_mangle_activation(monkeypatch):
    driver = MemoryROS()
    original = driver._request
    before = configurations(driver)
    def request(method, path, values=None):
        result = original(method, path, values)
        if method == 'PUT' and path == ROUTES:
            driver.rows[ROUTES][-1]['immediate-gw'] = GATEWAY + '%unexpected'
        return result
    monkeypatch.setattr(driver, '_request', request)
    _, plan = inspect(driver)
    result = execute(plan, driver, None)
    assert not result['ok']
    assert not any(path == MANGLE for _, path, _ in driver.writes)
    assert configurations(driver) == before


def test_unrelated_and_dynamic_list_members_neither_load_nor_stale_the_preview():
    driver = MemoryROS()
    driver.rows[ADDRESSES] += [{'.id': f'*B{i}', 'list': 'blocklist', 'address': f'203.0.113.{i}', 'disabled': 'false'}
                               for i in range(50)]
    driver.rows[ADDRESSES].append({'.id': '*D1', 'list': 'port-scanners', 'address': '198.51.100.7',
                                   'dynamic': 'true', 'timeout': '59m59s', 'disabled': 'false'})
    driver.rows[FILTER].append({'.id': '*F', 'chain': 'input', 'action': 'drop', 'src-address-list': 'port-scanners'})
    reads = []
    original = driver._request
    def request(method, path, values=None):
        if method == 'GET' and path.startswith(ADDRESSES):
            reads.append(path)
        return original(method, path, values)
    driver._request = request
    _, plan = inspect(driver)
    driver.rows[ADDRESSES][-1]['timeout'] = '59m41s'
    driver.rows[ADDRESSES].append({'.id': '*B99', 'list': 'blocklist', 'address': '203.0.113.99', 'disabled': 'false'})
    result = execute(plan, driver, None)
    assert result['ok'], result
    assert all('?' in path for path in reads)
    assert not any('blocklist' in path or 'port-scanners' in path for path in reads)
    _, again = inspect(driver)
    assert again['empty'] and not again['blockers']
