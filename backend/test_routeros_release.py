"""RouterOS REST releases preserve matching conditions and compensate failed writes."""
import copy
import ipaddress
from urllib.parse import unquote, urlsplit

import pytest
import requests

from backend.routers.base import RouterError
from backend.routers.plan import build_plan
from backend.routers.release import execute
from backend.routers.report import _networks, build_report
from backend.routers.routeros import RouterOSDriver
from backend.routers.routeros_nat import client_source_networks
from backend.routeros_test_helpers import is_add_script, print_rows, run_add_script, run_list_script, stored_address
from backend.test_agent_upgrade_auth import authenticated_app  # noqa: F401 (pytest fixture)
from backend.test_router_release import DESIRED, FakeAgent, GATEWAY, SAFE_RETURN_STATUS


ROUTES = '/ip/route'
NAT = '/ip/firewall/nat'
ADDRESSES = '/ip/firewall/address-list'
READ_ONLY = {'.id', 'dynamic', 'active', 'invalid', 'bytes', 'packets', 'immediate-gw',
             'belongs-to', 'hw-offloaded', 'debug.fwp-ptr', 'actual-interface'}
ROUTE_WRITABLE = {'dst-address', 'gateway', 'comment', 'disabled', 'distance', 'routing-table',
                  'scope', 'target-scope', 'check-gateway', 'suppress-hw-offload', 'pref-src',
                  'blackhole', 'type'}
NAT_WRITABLE = {'chain', 'action', 'comment', 'disabled', 'src-address', 'src-address-list',
                'dst-address', 'dst-address-list', 'in-interface', 'in-interface-list',
                'out-interface', 'out-interface-list', 'protocol', 'src-port', 'dst-port',
                'connection-state', 'connection-mark', 'connection-nat-state', 'routing-mark',
                'src-mac-address', 'ipsec-policy', 'log', 'log-prefix', 'place-before'}

HOME_ROUTES = [
    {'.id': '*1', 'dst-address': '198.18.0.0/16', 'gateway': GATEWAY, 'comment': 'fake-ip',
     'distance': '3', 'routing-table': 'main', 'disabled': 'false', 'dynamic': 'false',
     'active': 'true', 'immediate-gw': f'{GATEWAY}%bridge'},
    {'.id': '*2', 'dst-address': '203.0.113.0/24', 'gateway': '10.0.0.7', 'comment': 'cf-old',
     'distance': '17', 'routing-table': 'backup', 'disabled': 'true', 'scope': '20',
     'target-scope': '12', 'check-gateway': 'ping', 'suppress-hw-offload': 'true',
     'dynamic': 'false', 'active': 'false'},
]
HOME_NATS = [
    {'.id': '*A', 'chain': 'srcnat', 'action': 'accept', 'comment': 'mihomo', 'disabled': 'false',
     'src-address': '10.0.0.0/24', 'src-address-list': 'limited-clients',
     'dst-address-list': 'proxy-old', 'in-interface-list': 'LAN', 'out-interface': 'ether1',
     'protocol': 'tcp', 'src-port': '1024-65535', 'dst-port': '443',
     'connection-state': 'new,established', 'connection-mark': 'proxy',
     'src-mac-address': 'AA:BB:CC:DD:EE:FF', 'ipsec-policy': 'out,none',
     'log': 'false', 'log-prefix': 'proxy ', 'dynamic': 'false', 'invalid': 'false',
     'bytes': '1234', 'packets': '23'},
]
HOME_ADDRESSES = [
    {'.id': '*B', 'list': 'proxy-old', 'address': '198.18.0.0/16'},
    {'.id': '*C', 'list': 'limited-clients', 'address': '10.0.0.1-10.0.0.3'},
]
BROAD_SOURCE_NAT = {'.id': '*F', 'chain': 'srcnat', 'action': 'masquerade',
                    'disabled': 'false', 'comment': 'source NAT'}
POLICY_PATHS = ('/routing/rule', '/ip/firewall/mangle', '/ip/vrf', '/interface/list', '/interface/list/member',
                '/ip/firewall/filter', '/ip/firewall/raw')
# The only address lists a release may write: ConfigFlow's NAT bypass lists.
OWN_LISTS = ('cf-mihomo-nat-src', 'cf-mihomo-nat-dst')
ADDRESS_WRITABLE = {'list', 'address', 'comment', 'disabled'}


class RESTResponse:
    def __init__(self, data=None, status=200):
        self.data = data
        self.status_code = status
        self.text = '' if data is None else str(data)

    def json(self):
        if self.status_code == 204:
            raise ValueError('RouterOS DELETE has no response body')
        return copy.deepcopy(self.data)


class StatefulRouterOS:
    """Stateful REST fake with real verbs, IDs, read-only fields and rule ordering."""

    def __init__(self, routes=None, nats=None, addresses=None, fail=None, error_status=400,
                 error_body=None, hide_new_routes=False, timeout_after=None):
        self.rows = {ROUTES: copy.deepcopy(HOME_ROUTES if routes is None else routes),
                     NAT: copy.deepcopy(HOME_NATS if nats is None else nats),
                     ADDRESSES: copy.deepcopy(HOME_ADDRESSES if addresses is None else addresses),
                     **{path: [] for path in POLICY_PATHS}}
        self.calls = []
        self.writes = []
        self.counts = {}
        self.next_id = 100
        self.fail = fail
        self.error_status = error_status
        self.error_body = error_body if error_body is not None else {
            'error': error_status, 'message': 'Bad Request', 'detail': 'injected write failure'}
        self.hide_new_routes = hide_new_routes
        self.hidden_ids = set()
        self.write_exception = None
        self.timeout_after = timeout_after
        self.read_failures = set()
        self.rejected = []

    def close(self):
        pass

    def request(self, method, url, json=None, **_):
        method = method.upper()
        path = unquote(urlsplit(url).path).removeprefix('/rest')
        query = unquote(urlsplit(url).query)
        self.calls.append((method, path + ('?' + query if query else ''), copy.deepcopy(json)))
        if method == 'POST':
            return self._post(path, json)
        collection = next((key for key in sorted(self.rows, key=len, reverse=True)
                           if path == key or path.startswith(key + '/')), None)
        assert collection is not None, path
        row_id = path[len(collection) + 1:] if path != collection else None
        rows = self.rows[collection]
        if method == 'GET':
            if collection in self.read_failures:
                return RESTResponse({'message': 'Forbidden'}, 403)
            visible = [row for row in rows if row.get('.id') not in self.hidden_ids]
            if row_id:
                row = next((row for row in rows if row.get('.id') == row_id), None)
                return RESTResponse(row, 200 if row is not None else 404)
            if query:
                visible = print_rows(visible, {'.query': [query]})
            return RESTResponse(visible)

        self.writes.append((method, path, copy.deepcopy(json)))
        if self.write_exception is not None:
            raise self.write_exception
        key = (method, collection)
        self.counts[key] = self.counts.get(key, 0) + 1
        failure = (method, collection, self.counts[key])
        if (self.fail == failure or (isinstance(self.fail, (set, frozenset, list)) and failure in self.fail)
                or (method == 'PUT' and (collection, json) in self.rejected)):
            if method == 'PUT':
                # Rejected however it is sent: a batch falling back to single
                # writes meets the same failure.
                self.rejected.append((collection, copy.deepcopy(json)))
            return RESTResponse(self.error_body, self.error_status)
        if collection == ADDRESSES:
            target = json if method == 'PUT' else next((row for row in rows if row['.id'] == row_id), {})
            assert (target or {}).get('list') in OWN_LISTS, 'Releases must not mutate shared address lists'
        if method in ('PUT', 'PATCH'):
            assert isinstance(json, dict)
            allowed = {ROUTES: ROUTE_WRITABLE, ADDRESSES: ADDRESS_WRITABLE}.get(collection, NAT_WRITABLE)
            assert set(json) <= allowed, f'Non-writable properties: {set(json) - allowed}'
            assert not (set(json) & READ_ONLY)
        if method == 'PUT':
            assert row_id is None, 'RouterOS REST creates resources at the collection URL'
            self.next_id += 1
            new_id = f'*{self.next_id:X}'
            row = {'.id': new_id, 'disabled': 'false', **copy.deepcopy(json)}
            if collection == ADDRESSES:
                if any(item.get('list') == row['list'] and item.get('address') == stored_address(row['address'])
                       for item in rows):
                    return RESTResponse({'error': 400, 'message': 'Bad Request',
                                         'detail': 'failure: already have such entry'}, 400)
                row['address'] = stored_address(row['address'])
            before = row.pop('place-before', None)
            if before is not None:
                assert str(before).startswith('*'), 'RouterOS REST place-before needs an internal rule ID'
                index = next(i for i, item in enumerate(rows) if item['.id'] == before)
                rows.insert(index, row)
            else:
                rows.append(row)
            if collection == ROUTES and self.hide_new_routes:
                self.hidden_ids.add(new_id)
            return self._written_response(method, collection, RESTResponse(row, 201))
        row = next((row for row in rows if row['.id'] == row_id), None)
        if row is None:
            return RESTResponse({'error': 404, 'message': 'Not Found'}, 404)
        if method == 'PATCH':
            row.update(copy.deepcopy(json))
            return self._written_response(method, collection, RESTResponse(row))
        if method == 'DELETE':
            rows.remove(row)
            self.hidden_ids.discard(row_id)
            return self._written_response(method, collection, RESTResponse(status=204))
        raise AssertionError(f'Unexpected write verb {method}')

    def _post(self, path, body):
        """Console batches, OR'ed print queries and comma-separated removes."""
        if path == '/execute':
            def add(collection, fields):
                response = self.request('PUT', f'https://10.0.0.1/rest{collection}', json=fields)
                if not 200 <= response.status_code < 300:
                    raise RuntimeError((response.data or {}).get('detail') or 'failure')
                return response.data['.id']
            if is_add_script(body['script']):
                return RESTResponse(run_add_script(body['script'], add, fatal=(requests.RequestException,)))
            return RESTResponse(run_list_script(body['script'], self.rows[ADDRESSES],
                                                lambda fields: add(ADDRESSES, fields)))
        if path == ROUTES + '/print':
            visible = [row for row in self.rows[ROUTES] if row.get('.id') not in self.hidden_ids]
            return RESTResponse(print_rows(visible, body))
        collection = path.removesuffix('/remove')
        assert path.endswith('/remove') and collection in (ROUTES, NAT, ADDRESSES), path
        for row_id in body['.id'].split(','):
            self.writes.append(('DELETE', collection + '/' + row_id, None))
            self.counts[('DELETE', collection)] = self.counts.get(('DELETE', collection), 0) + 1
            if (self.fail == ('DELETE', collection, self.counts[('DELETE', collection)])
                    or (isinstance(self.fail, (set, frozenset, list))
                        and ('DELETE', collection, self.counts[('DELETE', collection)]) in self.fail)):
                return RESTResponse(self.error_body, self.error_status)
            row = next((row for row in self.rows[collection] if row['.id'] == row_id), None)
            if row is None:
                return RESTResponse({'error': 404, 'message': 'Not Found'}, 404)
            if collection == ADDRESSES:
                assert row.get('list') in OWN_LISTS, 'Releases must not mutate shared address lists'
            self.rows[collection].remove(row)
            self.hidden_ids.discard(row_id)
        return RESTResponse([])

    def post(self, url, **kwargs):
        return self.request('POST', url, **kwargs)

    def _written_response(self, method, collection, response):
        if self.timeout_after == (method, collection, self.counts[(method, collection)]):
            self.timeout_after = None
            raise requests.Timeout('Response lost after the router committed the mutation')
        return response

    def get(self, url, **kwargs):
        return self.request('GET', url, **kwargs)

    def put(self, url, **kwargs):
        return self.request('PUT', url, **kwargs)

    def patch(self, url, **kwargs):
        return self.request('PATCH', url, **kwargs)

    def delete(self, url, **kwargs):
        return self.request('DELETE', url, **kwargs)


def driver_for(session):
    driver = RouterOSDriver('https://10.0.0.1', 'admin', 'pw')
    driver.session = session
    return driver


def release_session(**kwargs):
    """Write/rollback scenarios need source NAT which actually requires a bypass."""
    # Complete coverage for end-to-end releases. The deliberately restricted
    # HOME_NATS fixture remains in the low-level matcher-preservation tests.
    kwargs.setdefault('nats', [{'.id': '*A', 'chain': 'srcnat', 'action': 'accept',
                               'comment': 'mihomo', 'disabled': 'false', 'src-address': '10.0.0.0/24',
                               'dst-address': '198.18.0.0/16'}, BROAD_SOURCE_NAT])
    return StatefulRouterOS(**kwargs)


def release_plan(driver, status=SAFE_RETURN_STATUS, desired=DESIRED):
    state = driver.read_state()
    return build_plan({'type': 'routeros', 'endpoint': 'https://10.0.0.1', 'manage_return_routing': True}, desired, state,
                      build_report(desired, state), status, driver)


def own_members(session, name):
    return {str(ipaddress.ip_network(row['address'])) for row in session.rows[ADDRESSES] if row['list'] == name}


def configured(rows):
    return sorted(({key: value for key, value in row.items() if key not in READ_ONLY} for row in rows),
                  key=lambda row: (row.get('dst-address', ''), row.get('comment', '')))


def test_route_operations_round_trip_preserves_original_configuration():
    session = StatefulRouterOS()
    driver = driver_for(session)
    before = configured(session.rows[ROUTES])

    undo = driver.apply({'kind': 'route_add', 'cidr': '91.108.0.0/16', 'gateway': GATEWAY, 'comment': 'cf-Telegram'})
    assert any(row['dst-address'] == '91.108.0.0/16' for row in session.rows[ROUTES])
    driver.apply(undo)
    assert configured(session.rows[ROUTES]) == before

    undo = driver.apply({'kind': 'route_comment', 'id': '*1', 'comment': 'cf-fake-ip'})
    assert session.rows[ROUTES][0]['comment'] == 'cf-fake-ip'
    driver.apply(undo)
    assert configured(session.rows[ROUTES]) == before

    undo = driver.apply({'kind': 'route_delete', 'id': '*2'})
    assert len(session.rows[ROUTES]) == 1
    driver.apply(undo)
    assert configured(session.rows[ROUTES]) == before
    assert {method for method, _, _ in session.writes} == {'PUT', 'PATCH', 'DELETE'}


def test_nat_update_retains_all_source_and_match_restrictions_and_can_undo():
    session = StatefulRouterOS()
    driver = driver_for(session)
    before = copy.deepcopy(session.rows[NAT])
    targets = ['91.108.0.0/16', '149.154.160.0/20']
    undo = driver.apply({'kind': 'nat_update', 'id': '*A', 'add_dst': targets, 'comment': 'cf-mihomo'})

    original = next(row for row in session.rows[NAT] if row['.id'] == '*A')
    assert original['comment'] == 'cf-mihomo'
    clones = [row for row in session.rows[NAT] if row['.id'] != '*A']
    assert {row['dst-address'] for row in clones} == set(targets)
    inherited = {key: value for key, value in HOME_NATS[0].items()
                 if key not in READ_ONLY | {'comment'}}
    for row in clones:
        assert all(row.get(key) == value for key, value in inherited.items())
        assert row['dst-address-list'] == HOME_NATS[0]['dst-address-list']
        assert row['comment'] == 'cf-mihomo'
        assert session.rows[NAT].index(row) < session.rows[NAT].index(original)
    assert session.rows[ADDRESSES] == HOME_ADDRESSES

    driver.apply(undo)
    assert session.rows[NAT] == before


def test_new_nat_rules_limit_every_source_destination_pair_without_shared_lists():
    session = StatefulRouterOS(nats=[])
    driver = driver_for(session)
    sources = ['10.0.0.1-10.0.0.3', '10.0.0.5-10.0.0.254']
    targets = ['198.18.0.0/16', '91.108.0.0/16']
    undo = driver.apply({'kind': 'nat_add', 'src': sources, 'dst': targets, 'comment': 'cf-mihomo'})
    assert {row['dst-address'] for row in session.rows[NAT]} == set(targets)
    expected_sources = list(ipaddress.collapse_addresses(_networks(sources)))
    for target in targets:
        actual = [row['src-address'] for row in session.rows[NAT] if row['dst-address'] == target]
        assert list(ipaddress.collapse_addresses(_networks(actual))) == expected_sources
        assert not any(ipaddress.ip_address(GATEWAY) in network for network in _networks(actual))
    assert all(row['chain'] == 'srcnat' and row['action'] == 'accept' for row in session.rows[NAT])
    assert all('src-address-list' not in row for row in session.rows[NAT])
    assert session.rows[ADDRESSES] == HOME_ADDRESSES
    driver.apply(undo)
    assert session.rows[NAT] == []


@pytest.mark.parametrize('kind', ['nat_add', 'nat_update'])
def test_compound_nat_failure_cleans_up_created_rules_and_restores_comment(kind):
    session = StatefulRouterOS(fail=('PUT', NAT, 2))
    driver = driver_for(session)
    before = copy.deepcopy(session.rows[NAT])
    op = {'kind': kind, 'comment': 'cf-mihomo'}
    if kind == 'nat_update':
        op.update(id='*A', add_dst=['91.108.0.0/16', '149.154.160.0/20'])
    else:
        op.update(src=['10.0.0.1-10.0.0.3'], dst=['91.108.0.0/16', '149.154.160.0/20'])
    with pytest.raises(RouterError, match='injected write failure'):
        driver.apply(op)
    assert session.rows[NAT] == before
    assert any(method == 'DELETE' and path.startswith(NAT + '/') for method, path, _ in session.writes)


def test_release_writes_and_verifies_all_stages_then_has_no_remaining_changes():
    session = release_session()
    driver = driver_for(session)
    agent = FakeAgent(SAFE_RETURN_STATUS)
    plan = release_plan(driver)
    assert not session.writes and not agent.calls
    assert not any(stage['manual'] for stage in plan['stages'])
    result = execute(plan, driver, agent)
    assert result['ok'], result
    assert [entry['stage'] for entry in result['log'] if entry['status'] == 'verified'] == [
        'return_routing', 'routes', 'nat_bypass']
    assert not result['rollback']
    assert release_plan(driver, status={**agent.status(), 'status': 'ok'})['empty']
    # Shared lists stay untouched; the two new CIDRs join ConfigFlow's NAT list.
    assert [row for row in session.rows[ADDRESSES] if row['list'] not in OWN_LISTS] == HOME_ADDRESSES
    assert own_members(session, 'cf-mihomo-nat-dst') == {'91.108.0.0/16', '149.154.160.0/20'}
    assert session.rows[NAT][0]['comment'] == 'cf-mihomo-nat'
    assert next(row for row in session.rows[NAT] if row['.id'] == '*A')['comment'] == 'cf-mihomo'
    assert next(row for row in session.rows[ROUTES] if row['.id'] == '*2') == HOME_ROUTES[1]
    assert not any(path == ROUTES + '/*2' for _, path, _ in session.writes)


def test_later_release_adds_only_one_list_member_for_a_new_destination():
    session = release_session(routes=[], nats=[BROAD_SOURCE_NAT])
    driver = driver_for(session)
    agent = FakeAgent(SAFE_RETURN_STATUS)
    initial = execute(release_plan(driver), driver, agent)
    assert initial['ok'], initial
    # Every client source fragment is one member of the source list.
    assert own_members(session, 'cf-mihomo-nat-src') == {str(network) for network in client_source_networks(GATEWAY)}
    accepts = [row for row in session.rows[NAT] if row.get('action') == 'accept']
    assert len(accepts) == 1 and session.rows[NAT].index(accepts[0]) == 0
    nats_before = copy.deepcopy(session.rows[NAT])

    expanded = copy.deepcopy(DESIRED)
    target = '185.76.151.0/24'
    expanded['proxy_cidrs'].append({'cidr': target, 'sources': ['Telegram']})
    plan = release_plan(driver, status={**agent.status(), 'status': 'ok'}, desired=expanded)
    session.writes.clear()
    result = execute(plan, driver, agent)
    assert result['ok'], result
    assert session.rows[NAT] == nats_before
    assert [(method, path, body['address']) for method, path, body in session.writes if path.startswith(ADDRESSES)] == [
        ('PUT', ADDRESSES, target)]
    assert target in own_members(session, 'cf-mihomo-nat-dst')
    assert release_plan(driver, status={**agent.status(), 'status': 'ok'}, desired=expanded)['empty']


@pytest.mark.parametrize(('field', 'value'), [('src-address', '10.0.0.0/25'),
                                            ('src-address-list', 'different-clients')])
def test_nat_source_drift_changes_fingerprint_and_rejects_stale_operation(field, value):
    session = release_session()
    driver = driver_for(session)
    preview = release_plan(driver)
    op = next(op for stage in preview['stages'] for op in stage['ops'] if op['kind'] == 'nat_update')
    assert op['expected'] == driver._payload(NAT, session.rows[NAT][0])
    session.rows[NAT][0][field] = value
    assert release_plan(driver)['fingerprint'] != preview['fingerprint']
    with pytest.raises(RouterError, match='预览'):
        driver.apply(op)
    assert session.writes == []


def test_release_nat_failure_rolls_back_routes_and_agent_in_reverse_order():
    session = release_session(fail=('PUT', NAT, 1))
    driver = driver_for(session)
    agent = FakeAgent(SAFE_RETURN_STATUS)
    routes_before = configured(session.rows[ROUTES])
    nats_before = copy.deepcopy(session.rows[NAT])
    plan = release_plan(driver)
    route_ops = next(stage['ops'] for stage in plan['stages'] if stage['key'] == 'routes')
    adopt = next(stage['ops'][0] for stage in plan['stages'] if stage['key'] == 'nat_bypass')
    result = execute(plan, driver, agent)
    assert result['ok'] is False and result['failed_stage'] == 'nat_bypass'
    # The list rule failed first and undid its own members; the adoption and
    # everything before it are undone in reverse order.
    assert [item['stage'] for item in result['rollback']] == ['nat_bypass'] + ['routes'] * len(route_ops) + ['return_routing']
    assert [item['summary'] for item in result['rollback'][:-1]] == [
        f'已撤销：{op["summary"]}' for op in [adopt] + list(reversed(route_ops))]
    assert all(item['status'] == 'ok' for item in result['rollback'])
    assert configured(session.rows[ROUTES]) == routes_before
    assert session.rows[NAT] == nats_before
    assert session.rows[ADDRESSES] == HOME_ADDRESSES
    assert agent.calls[-1] == ('remove', False)


def test_failed_local_nat_rollback_is_reported_with_current_stage():
    # The first destination member fails; removing the one written after it fails too.
    sources = len(client_source_networks(GATEWAY))
    session = release_session(fail={('PUT', ADDRESSES, sources + 1), ('DELETE', ADDRESSES, 1)})
    driver = driver_for(session)
    agent = FakeAgent(SAFE_RETURN_STATUS)
    routes_before = configured(session.rows[ROUTES])
    result = execute(release_plan(driver), driver, agent)
    assert result['ok'] is False and result['failed_stage'] == 'nat_bypass'
    assert any(item['stage'] == 'nat_bypass' and item['status'] == 'failed' for item in result['rollback'])
    # One remove carries every member written; it stopped at the failure, so
    # they are kept for inspection. No rule uses them.
    assert own_members(session, 'cf-mihomo-nat-dst') == {'149.154.160.0/20'}
    assert len(own_members(session, 'cf-mihomo-nat-src')) == len(client_source_networks(GATEWAY))
    assert not any(row.get('comment') == 'cf-mihomo-nat' for row in session.rows[NAT])
    assert next(row for row in session.rows[NAT] if row['.id'] == '*A')['comment'] == 'mihomo'
    assert configured(session.rows[ROUTES]) == routes_before
    assert agent.calls[-1] == ('remove', False)


def test_successful_write_with_missing_readback_rolls_back_before_nat():
    session = StatefulRouterOS(routes=[], nats=[], hide_new_routes=True)
    driver = driver_for(session)
    agent = FakeAgent(SAFE_RETURN_STATUS)
    result = execute(release_plan(driver), driver, agent)
    assert result['ok'] is False and result['failed_stage'] == 'routes'
    assert any(entry['status'] == 'failed' for entry in result['log'])
    assert all(item['status'] == 'ok' for item in result['rollback'])
    assert session.rows[ROUTES] == [] and session.rows[NAT] == []
    assert not any(path.startswith(NAT) for _, path, _ in session.writes)
    assert agent.calls[-1] == ('remove', False)


@pytest.mark.parametrize('status', [400, 401, 403, 404, 500])
def test_rest_write_errors_are_reported_as_router_errors(status):
    session = StatefulRouterOS(fail=('PUT', ROUTES, 1), error_status=status)
    driver = driver_for(session)
    before = copy.deepcopy(session.rows[ROUTES])
    with pytest.raises(RouterError):
        driver.apply({'kind': 'route_add', 'cidr': '91.108.0.0/16', 'gateway': GATEWAY, 'comment': 'cf'})
    assert session.rows[ROUTES] == before


def test_rest_write_connection_failure_is_reported_as_router_error():
    session = StatefulRouterOS()
    session.write_exception = requests.Timeout('timed out')
    with pytest.raises(RouterError):
        driver_for(session).apply({'kind': 'route_add', 'cidr': '91.108.0.0/16',
                                   'gateway': GATEWAY, 'comment': 'cf'})


@pytest.mark.parametrize(('method', 'op'), [
    ('PUT', {'kind': 'route_add', 'cidr': '91.108.0.0/16', 'gateway': GATEWAY, 'comment': 'cf-Telegram'}),
    ('PATCH', {'kind': 'route_comment', 'id': '*1', 'comment': 'cf-fake-ip'}),
    ('DELETE', {'kind': 'route_delete', 'id': '*2'}),
])
def test_lost_response_after_mutation_restores_original_configuration(method, op):
    session = StatefulRouterOS(timeout_after=(method, ROUTES, 1))
    driver = driver_for(session)
    before = configured(session.rows[ROUTES])
    with pytest.raises(RouterError):
        driver.apply(op)
    if method == 'PUT':
        assert len(session.rows[ROUTES]) == len(before) + 1
        assert not any(verb == 'DELETE' for verb, _, _ in session.writes)
    else:
        assert configured(session.rows[ROUTES]) == before


def test_routeros_preview_and_publish_api(authenticated_app, monkeypatch):  # noqa: F811
    client, _, agents, admin, _ = authenticated_app
    created = client.post('/api/routers', headers=admin, json={
        'name': 'ROS 主路由', 'type': 'routeros', 'endpoint': 'https://10.0.0.1',
        'username': 'admin', 'password': 'pw', 'proxy_gateway_agent_id': agents[0]['id'],
        'manage_return_routing': True})
    assert created.status_code == 200, created.get_json()
    router_id = created.get_json()['router']['id']

    from backend.routes import routers as routes_module
    session = release_session()
    agent = FakeAgent(SAFE_RETURN_STATUS)
    monkeypatch.setitem(routes_module.DRIVERS, 'routeros', lambda *args, **kwargs: driver_for(session))
    monkeypatch.setattr(routes_module, 'compute_desired', lambda *args, **kwargs: DESIRED)
    monkeypatch.setattr(routes_module, '_return_routing', lambda record: {**agent.status(), 'status': 'ok'})
    monkeypatch.setattr(routes_module, 'AgentReturnRouting', lambda manager, record: agent)

    preview = client.post(f'/api/routers/{router_id}/plan', headers=admin)
    assert preview.status_code == 200, preview.get_json()
    original = preview.get_json()['plan']
    assert original['script'] == '' and not any(stage['manual'] for stage in original['stages'])
    assert session.writes == [] and agent.calls == []

    # An externally widened selector still changes the preview fingerprint.
    session.rows[NAT][0]['src-address'] = '10.0.0.0/16'
    rejected = client.post(f'/api/routers/{router_id}/apply', headers=admin,
                           json={'fingerprint': original['fingerprint']})
    assert rejected.status_code == 409, rejected.get_json()
    assert session.writes == [] and agent.calls == []
    plan = rejected.get_json()['plan']
    assert plan['fingerprint'] != original['fingerprint']

    applied = client.post(f'/api/routers/{router_id}/apply', headers=admin,
                         json={'fingerprint': plan['fingerprint']})
    assert applied.status_code == 200, applied.get_json()
    body = applied.get_json()
    assert body['success'] and body['result']['ok']
    assert [entry['stage'] for entry in body['result']['log'] if entry['status'] == 'verified'] == [
        'return_routing', 'routes', 'nat_bypass']
    saved = next(router for router in client.get('/api/routers', headers=admin).get_json()['routers']
                 if router['id'] == router_id)
    assert saved['last_release']['ok'] is True
    assert saved['last_release']['fingerprint'] == plan['fingerprint']
    assert saved['last_release']['at']
    again = client.post(f'/api/routers/{router_id}/plan', headers=admin)
    assert again.status_code == 200 and again.get_json()['plan']['empty']
