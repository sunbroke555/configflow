"""IPv6 preparation preserves existing networking and binds changes to fresh checks."""
from copy import deepcopy

import pytest

from backend.agents import ipv6_readiness as bridge
from backend.routers import ipv6_readiness as ros
from backend.routers.base import RouterError
from backend.test_agent_upgrade_auth import authenticated_app  # noqa: F401

YAML = 'ipv6: true\ndns:\n  enable: true\n  ipv6: true\n  enhanced-mode: fake-ip\n  fake-ip-range6: fc00::1/64\ntun:\n  enable: true\n'


def host_status():
    return {'success': True, 'supported': True, 'can_prepare': True, 'interface': 'eth0', 'interface_index': 2,
            'addresses': [{'address': 'fdda::3', 'prefix_length': 64, 'usable': True},
                          {'address': 'fe80::3', 'prefix_length': 64, 'usable': False}],
            'default_routes': [{'interface': 'eth0', 'gateway': 'fe80::1', 'protocol': 'ra', 'expires': 1800}],
            'persistence': {'persisted': False, 'state': 'missing'},
            'checks': [{'id': 'default_route', 'status': 'pass', 'message': 'RA 默认路由正常'}]}


def settings():
    return bridge.configuration({'mihomo': {'custom_config': YAML}})[0]


class Router:
    def __init__(self):
        self.rows = {path: [] for path in ros.TABLES.values()}
        self.rows['/ip/address'] = [{'address': '192.168.0.1/24', 'interface': 'lan4'}]
        self.rows['/ipv6/address'] = [{'address': 'fdda::1/64', 'interface': 'lan4'}]
        self.rows['/ipv6/nd'] = [{'interface': 'lan4', 'advertise-dns': 'false'}]
        self.rows[ros.ROUTES] = [{'.id': '*D', 'dst-address': '::/0', 'gateway': 'pppoe-out1', 'active': 'true'}]
        self.writes = []
        self.fail_path = None
        self.lost_reply = False

    def _list(self, path):
        if path == self.fail_path:
            raise RouterError('read denied')
        return deepcopy(self.rows[path])

    def _get(self, path):
        assert path == '/ip/dns'
        return {'servers': '192.168.0.2', 'dynamic-servers': ''}

    def _request(self, method, path, values=None):
        self.writes.append((method, path, values))
        if method == 'PUT':
            row = {**values, '.id': '*N' + str(len(self.writes)), 'active': 'true'}
            self.rows[ros.ROUTES].append(row)
            if self.lost_reply:
                raise RouterError('lost response')
            return row
        assert method == 'DELETE'
        row_id = path.rsplit('/', 1)[1]
        # CHR 7.25 REST does not interpret %2A as the leading * of an ID.
        assert row_id.startswith('*'), 'RouterOS resource IDs must preserve the literal star'
        self.rows[ros.ROUTES] = [r for r in self.rows[ros.ROUTES] if r['.id'] != row_id]
        return {}

    def close(self):
        pass


ROUTER = {'id': 'r1', 'name': 'ROS', 'type': 'routeros', 'endpoint': 'http://router.test', 'username': 'user', 'password': 'private'}
AGENT = {'id': 'm1', 'host': '192.168.0.3'}


def inspect_router(router, host=None):
    return ros.inspect(router, ROUTER, AGENT, host or host_status(), settings(), '192.168.0.2')


def test_default_and_separate_configuration_flags():
    result, _ = bridge.configuration({'mihomo': {'custom_config': ''}})
    assert result['ipv6'] is True and result['dns_ipv6'] is False
    assert not result['fake_ipv6_enabled']
    result, _ = bridge.configuration({'mihomo': {'custom_config': 'ipv6: true\ndns:\n  ipv6: false\n'}})
    assert result['ipv6'] is True and result['dns_ipv6'] is False


@pytest.mark.parametrize('changed', [{'id': 'm1'}, {'profile_id': 'other'}, {'host': '192.168.0.3'},
                                    {'host': 'client.example'}, {'host': '127.0.0.1'}, {'enabled': False}])
def test_policy_client_candidate_requires_independent_saved_address(monkeypatch, changed):
    from backend.common import client_devices
    monkeypatch.setattr(client_devices, 'list_devices', lambda _: {'devices': []})
    source = {'id': 'm1', 'profile_id': 'v6', 'host': '192.168.0.3'}
    client = {'id': 'client1', 'profile_id': 'v6', 'host': '192.168.0.4', **changed}
    assert bridge._policy_context({}, source, client)['clients'] == []


def test_selected_verification_client_enters_policy_as_candidate_only(environment, monkeypatch):
    from backend.common import client_devices
    client, manager, agent, headers, path, *_ = environment
    probe = manager.register_agent({'name': 'Probe', 'host': '192.168.0.4', 'port': 27648,
                                    'service_type': 'mosdns', 'deployment_method': 'shell', 'profile_id': 'v6'})
    def select(agents):
        next(a for a in agents if a['id'] == agent['id'])['ipv6_verification_settings'] = {
            'client_agent_id': probe['id'], 'test_url': ''}
    manager._update_agents(select)
    devices = {'devices': []}
    monkeypatch.setattr(client_devices, 'list_devices', lambda _: devices)
    contexts = []
    def inspect(*a, **kwargs):
        contexts.append(kwargs['policy_context'])
        return {'id': 'r1', 'status': 'ok', 'checks': []}
    monkeypatch.setattr(ros, 'inspect', inspect)
    response = client.get(path, headers=headers)
    assert response.status_code == 200, response.get_json()
    candidate = contexts[0]['clients'][0]
    assert candidate['address'] == '192.168.0.4' and candidate['source_candidate'] is True
    assert '登记地址候选' in candidate['source_note'] and '尚未确认' in candidate['source_note']
    assert devices == {'devices': []}


@pytest.mark.parametrize('raw', ['ipv6: ture', 'ipv6: "true"', 'dns:\n  ipv6: 1', 'dns: []', '[]'])
def test_invalid_ipv6_types_do_not_offer_preparation(raw):
    assert bridge.configuration({'mihomo': {'custom_config': raw}})[0]['errors']


def test_read_only_plan_uses_exact_fake_network_and_prefers_scoped_link_local():
    router = Router()
    result = inspect_router(router)
    assert result['plan'] == {'dst-address': 'fc00::/64', 'gateway': 'fe80::3%lan4',
                              'routing-table': 'main', 'disabled': 'false', 'distance': '1'}
    assert router.writes == []


def test_link_local_next_hop_uses_router_interface_not_linux_interface():
    host = host_status()
    host['addresses'] = [{'address': 'fe80::3', 'usable': False}]
    assert inspect_router(Router(), host)['plan']['gateway'] == 'fe80::3%lan4'
    host['addresses'][0]['dad_failed'] = True
    assert inspect_router(Router(), host)['plan'] is None


def test_new_route_falls_back_to_unique_on_link_ula_without_link_local():
    host = host_status()
    host['addresses'] = [host['addresses'][0], {'address': '2001:db8::3', 'usable': True}]
    assert inspect_router(Router(), host)['plan']['gateway'] == 'fdda::3%lan4'
    host['addresses'] = [{'address': '2001:db8::3', 'usable': True}]
    assert inspect_router(Router(), host)['plan'] is None


@pytest.mark.parametrize('flag', ['temporary', 'deprecated', 'tentative', 'dad_failed', 'invalid'])
def test_unready_link_local_is_excluded_before_ula_fallback(flag):
    host = host_status()
    host['addresses'][1][flag] = True
    assert inspect_router(Router(), host)['plan']['gateway'] == 'fdda::3%lan4'


def test_multiple_link_local_candidates_block_new_route_instead_of_falling_back():
    host = host_status()
    host['addresses'].append({'address': 'fe80::4', 'usable': False})
    assert inspect_router(Router(), host)['plan'] is None


def test_link_local_selection_ignores_other_interface_inventory():
    host = host_status()
    host['interfaces'] = [{'name': 'eth1', 'addresses': [{'address': 'fe80::99'}]}]
    host['addresses'].append({'address': 'fe80::98', 'interface': 'eth1'})
    assert inspect_router(Router(), host)['plan']['gateway'] == 'fe80::3%lan4'


def test_existing_manual_route_is_reused_without_adopting_or_rewriting():
    router = Router()
    router.rows[ros.ROUTES].append({'.id': '*A', 'dst-address': 'fc00::/64', 'gateway': 'fdda::3', 'immediate-gw': 'fdda::3%lan4',
                                   'active': 'true', 'comment': 'my route'})
    result = inspect_router(router)
    assert result['plan'] is None
    assert next(c for c in result['checks'] if c['id'] == 'fake_ipv6')['status'] == 'ok'
    assert router.writes == []


def test_existing_route_cannot_hide_disabled_tun_or_overlapping_lan():
    router = Router()
    router.rows[ros.ROUTES].append({'.id': '*A', 'dst-address': 'fc00::/64', 'gateway': 'fdda::3%lan4', 'active': 'true'})
    disabled_tun = {**settings(), 'tun_enabled': False}
    result = ros.inspect(router, ROUTER, AGENT, host_status(), disabled_tun)
    assert result['status'] == 'error'
    assert any('尚未开启 TUN' in c['message'] for c in result['checks'])
    router.rows['/ipv6/address'].append({'address': 'fc00::1/64', 'interface': 'lan4'})
    assert inspect_router(router)['status'] == 'error'
    assert not router.writes


def test_existing_unscoped_gateway_must_not_ignore_resolved_wrong_interface():
    router = Router()
    router.rows[ros.ROUTES].append({'.id': '*A', 'dst-address': 'fc00::/64', 'gateway': 'fdda::3',
                                   'immediate-gw': 'fdda::3%other-lan', 'active': 'true'})
    assert inspect_router(router)['status'] == 'error'
    assert not router.writes


@pytest.mark.parametrize('fields', [
    {'gateway': 'fdda::9'}, {'disabled': 'true'}, {'active': 'false'}, {'dst-address': 'fc00::/80'},
])
def test_conflicting_routes_are_never_overwritten(fields):
    router = Router()
    router.rows[ros.ROUTES].append({'.id': '*A', 'dst-address': 'fc00::/64', 'gateway': 'fdda::3', 'active': 'true', **fields})
    assert inspect_router(router)['status'] == 'error'
    assert inspect_router(router)['plan'] is None
    assert not router.writes


def test_unknown_router_reads_or_ambiguous_lan_disable_automatic_route():
    router = Router()
    router.fail_path = '/ipv6/firewall/filter'
    assert inspect_router(router)['plan'] is None
    router.fail_path = None
    router.rows['/ip/address'].append({'address': '192.168.0.9/24', 'interface': 'lan5'})
    assert inspect_router(router)['plan'] is None


def test_peer_dns_and_nat_are_advisories_not_mutations():
    router = Router()
    router.rows['/ipv6/dhcp-client'] = [{'use-peer-dns': 'true'}]
    router.rows['/ipv6/firewall/nat'] = [{'chain': 'srcnat', 'action': 'masquerade'}]
    result = inspect_router(router)
    assert next(c for c in result['checks'] if c['id'] == 'dns')['status'] == 'warning'
    assert next(c for c in result['checks'] if c['id'] == 'nat')['status'] == 'unknown'
    assert not router.writes


def test_same_port_forward_hint_reuses_the_rows_already_read():
    """#452's hint (drop invalid before a same-port accept) without another RouterOS read."""
    router = Router()
    router.rows['/ipv6/firewall/filter'] = [
        {'.id': '*4', 'chain': 'forward', 'action': 'accept', 'connection-state': 'established,related,untracked'},
        {'.id': '*5', 'chain': 'forward', 'action': 'drop', 'connection-state': 'invalid'}]
    reads = []
    original = router._list
    router._list = lambda path: reads.append(path) or original(path)
    result = inspect_router(router)
    hint = next(c for c in result['checks'] if c['id'] == 'forward_hairpin')
    assert hint['status'] == 'warning'
    assert 'drop invalid' in hint['message']
    assert hint['command'] == ('/ipv6 firewall filter add chain=forward action=accept in-interface=lan4 '
                               'out-interface=lan4 comment="LAN to Mihomo via router" place-before=*5')
    assert reads.count('/ipv6/firewall/filter') == 1 and reads.count('/interface') == 1
    assert not router.writes
    # A summary of readiness counts only router errors; the hint stays advisory.
    assert result['status'] == 'warning'


def test_same_port_forward_hint_is_silent_without_a_drop_and_unknown_when_unread():
    router = Router()
    assert not any(c['id'] == 'forward_hairpin' for c in inspect_router(router)['checks'])
    router.fail_path = '/ipv6/firewall/filter'
    hint = next(c for c in inspect_router(router)['checks'] if c['id'] == 'forward_hairpin')
    assert hint['status'] == 'unknown' and 'command' not in hint


def test_same_port_forward_hint_covers_rule_set_routes_without_fake_ip():
    router = Router()
    router.rows['/ipv6/firewall/filter'] = [{'.id': '*5', 'chain': 'forward', 'action': 'drop', 'connection-state': 'invalid'}]
    redir = bridge.configuration({'mihomo': {'custom_config': YAML.replace('fake-ip\n', 'redir-host\n')}})[0]
    result = ros.inspect(router, ROUTER, AGENT, host_status(), redir, '192.168.0.2')
    assert next(c for c in result['checks'] if c['id'] == 'forward_hairpin')['command'].endswith('place-before=*5')
    off = bridge.configuration({'mihomo': {'custom_config': YAML.replace('ipv6: true\ndns', 'ipv6: false\ndns')}})[0]
    result = ros.inspect(router, ROUTER, AGENT, host_status(), off, '192.168.0.2')
    assert not any(c['id'] == 'forward_hairpin' for c in result['checks'])


def test_route_add_reads_back_and_owned_undo_preserves_others():
    router = Router()
    initial = deepcopy(router.rows[ros.ROUTES])
    receipt = ros.add_missing_route(router, inspect_router(router)['plan'])
    assert len(router.rows[ros.ROUTES]) == 2
    ros.undo_route(router, receipt)
    assert router.rows[ros.ROUTES] == initial


def test_lost_write_reply_is_compensated_and_foreign_edit_is_not_removed():
    router = Router()
    plan = inspect_router(router)['plan']
    router.lost_reply = True
    with pytest.raises(RouterError):
        ros.add_missing_route(router, plan)
    assert len(router.rows[ros.ROUTES]) == 1
    router.lost_reply = False
    receipt = ros.add_missing_route(router, plan)
    router.rows[ros.ROUTES][-1]['gateway'] = 'fdda::99'
    with pytest.raises(RouterError, match='其他操作修改'):
        ros.undo_route(router, receipt)
    assert len(router.rows[ros.ROUTES]) == 2


@pytest.fixture
def environment(authenticated_app, monkeypatch):
    client, manager, _, admin, _ = authenticated_app
    manager.repository.create_profile({'id': 'v6', 'name': 'IPv6'})
    manager.repository.create_profile({'id': 'other', 'name': 'Other'})
    agent = manager.register_agent({'name': 'Mihomo', 'host': '192.168.0.3', 'port': 27647, 'service_type': 'mihomo',
                                    'deployment_method': 'shell', 'version': '1.8.50-go', 'profile_id': 'v6'})
    manager.repository.save_profile('v6', {'mihomo': {'custom_config': YAML},
                                         'routers': {'items': [{**ROUTER, 'proxy_gateway_agent_id': agent['id']}]}})
    router = Router()
    monkeypatch.setattr(bridge, '_driver', lambda _: router)
    calls = []
    fail = []

    def request(agent, operation='', payload=None):
        calls.append((operation, payload))
        if operation == 'prepare' and fail:
            from backend.agents.oxidns_monitor import MonitorError
            raise MonitorError('FAILED', 502, 'host failed')
        if operation == 'probe':
            return {'success': True, 'connectivity': {'status': 'ok', 'scope': 'node_transport'}}
        return host_status()

    monkeypatch.setattr(bridge, '_request', request)
    headers = {**admin, 'X-ConfigFlow-Profile': 'v6'}
    path = '/api/agents/' + agent['id'] + '/ipv6-readiness'
    return client, manager, agent, headers, path, router, calls, fail


def test_api_requires_auth_profile_and_binding(environment):
    client, _, _, headers, path, *_ = environment
    assert client.get(path).status_code == 401
    assert client.get(path, headers={k: v for k, v in headers.items() if k != 'X-ConfigFlow-Profile'}).status_code == 400
    assert client.get(path, headers={**headers, 'X-ConfigFlow-Profile': 'other'}).status_code == 409
    assert client.post(path + '/probe', headers=headers, json={'profile_id': 'other'}).status_code == 400


def test_disabled_agent_is_rejected_before_network_calls(environment):
    client, manager, agent, headers, path, router, calls, _ = environment
    manager.update_agent(agent['id'], {'enabled': False})
    response = client.get(path, headers=headers)
    assert response.status_code == 409
    assert response.get_json()['code'] == 'AGENT_DISABLED'
    assert not calls and not router.writes


@pytest.mark.parametrize('core, differing', [
    ({'ipv6': True, 'dns.ipv6': True, 'dns.fake-ip-range6': 'fc00::abcd/64'}, False),
    ({'ipv6': False, 'dns.ipv6': True, 'dns.fake-ip-range6': 'fc00::1/64'}, True),
    ({'ipv6': True, 'dns.ipv6': False, 'dns.fake-ip-range6': 'fc00::1/64'}, True),
    ({'ipv6': True, 'dns.ipv6': True, 'dns.fake-ip-range6': 'fc01::/64'}, True),
    ({'tun.enable': False}, True),
])
def test_readiness_compares_deployed_flags_and_normalized_fake_network(environment, monkeypatch, core, differing):
    client, _, _, headers, path, router, _, _ = environment
    deployed = {'ipv6': True, 'dns.ipv6': True, 'tun.enable': True, 'dns.fake-ip-range6': 'fc00::1/64', **core}
    monkeypatch.setattr(bridge, '_request', lambda *_: {**host_status(), 'core': deployed})
    response = client.get(path, headers=headers)
    assert response.status_code == 200, response.get_json()
    checks = response.get_json()['checks']
    deployed = [item for item in checks if item['id'] == 'deployed_config']
    assert bool(deployed) is differing
    if differing:
        assert deployed[0]['status'] == 'warning'
        assert '保存并发布' in deployed[0]['message']
    assert router.writes == []


def test_disabled_saved_ipv6_is_warning_and_cannot_prepare(environment):
    client, manager, _, headers, path, router, calls, _ = environment
    manager.repository.save_profile('v6', {'mihomo': {'custom_config': 'ipv6: false\ndns:\n  ipv6: true\n'}})
    response = client.get(path, headers=headers)
    assert response.status_code == 200, response.get_json()
    report = response.get_json()
    assert report['configuration']['ipv6'] is False
    assert report['configuration']['dns_ipv6'] is True
    setting = next(item for item in report['checks'] if item['id'] == 'configuration')
    assert setting['status'] == 'warning'
    assert 'Mihomo IPv6：关闭' in setting['message']
    assert 'DNS IPv6 答案：开启' in setting['message']
    assert report['can_prepare'] is False
    assert not router.writes and not any(operation for operation, _ in calls)


def test_prepare_retains_tun_recovery_warning_after_fresh_inspection(environment, monkeypatch):
    client, _, _, headers, path, _, _, _ = environment
    warning = 'TUN IPv6 恢复尚未完成，请稍后重新检查。'

    def request(_agent, operation='', payload=None):
        status = host_status()
        if operation == 'prepare':
            status['checks'].append({'id': 'tun_recovery', 'status': 'warn', 'message': warning})
        return status

    monkeypatch.setattr(bridge, '_request', request)
    report = client.get(path, headers=headers).get_json()
    response = client.post(path + '/prepare', headers=headers, json={
        'profile_id': 'v6', 'interface': report['prepare_interface'], 'check_token': report['check_token']})
    assert response.status_code == 200, response.get_json()
    recovery = next(item for item in response.get_json()['checks'] if item['id'] == 'prepare_tun_recovery')
    assert recovery['status'] == 'warning' and recovery['message'] == warning


def test_api_preserves_draft_and_ipv4_and_repeated_prepare_is_idempotent(environment):
    client, manager, agent, headers, path, router, calls, _ = environment
    before = deepcopy(manager.repository.get_profile('v6'))
    response = client.get(path, headers=headers)
    assert response.status_code == 200, response.get_json()
    assert response.headers['Cache-Control'] == 'no-store'
    report = response.get_json()
    assert report['can_prepare'] and not router.writes
    for _ in range(2):
        response = client.post(path + '/prepare', headers=headers, json={
            'profile_id': 'v6', 'interface': report['prepare_interface'], 'check_token': report['check_token']})
        assert response.status_code == 200, response.get_json()
        report = response.get_json()
    assert router.writes == []
    assert report['changed_routes'] == 0
    assert '上级路由差异报告' in report['message']
    assert manager.repository.get_profile('v6') == before
    assert all(w[1].startswith('/ipv6/route') for w in router.writes)


def test_changed_route_or_interface_blocks_before_any_write(environment):
    client, _, _, headers, path, router, calls, _ = environment
    report = client.get(path, headers=headers).get_json()
    payload = {'interface': 'eth1', 'check_token': report['check_token']}
    assert client.post(path + '/prepare', headers=headers, json=payload).status_code == 409
    router.rows[ros.ROUTES].append({'.id': '*X', 'dst-address': 'fc00::/64', 'gateway': 'fdda::9', 'active': 'true'})
    payload['interface'] = 'eth0'
    assert client.post(path + '/prepare', headers=headers, json=payload).status_code == 409
    assert not router.writes and not any(c[0] == 'prepare' for c in calls)


def test_failed_host_prepare_never_writes_or_removes_router_routes(environment):
    client, _, _, headers, path, router, _, fail = environment
    report = client.get(path, headers=headers).get_json()
    fail.append(True)
    response = client.post(path + '/prepare', headers=headers, json={'interface': 'eth0', 'check_token': report['check_token']})
    assert response.status_code == 502
    assert len(router.rows[ros.ROUTES]) == 1
    assert router.writes == []


def test_probe_is_explicit_saved_tcp_endpoint_not_arbitrary_destination(environment):
    client, manager, _, headers, path, _, calls, _ = environment
    assert client.post(path + '/probe', headers=headers, json={'host': 'example.test', 'port': 80}).status_code == 400
    manager.repository.update_shared_transaction(lambda shared: shared.update(nodes=[
        {'id': 'tcp-node', 'name': 'TCP IPv6', 'type': 'socks5', 'server': '2001:db8::2', 'port': 1080},
        {'id': 'udp-node', 'name': 'UDP IPv6', 'type': 'hysteria2', 'server': '2001:db8::3', 'port': 443},
        {'id': 'unused-node', 'name': 'Other profile', 'type': 'http', 'server': '2001:db8::4', 'port': 1080}]))
    manager.repository.save_profile('v6', {'proxy_groups': [
        {'id': 'ipv6-group', 'name': 'IPv6', 'type': 'select', 'manual_nodes': ['tcp-node', 'udp-node']}]})
    response = client.post(path + '/probe', headers=headers, json={})
    assert response.status_code == 200, response.get_json()
    assert response.get_json()['connectivity']['status'] == 'ok'
    assert ('probe', {'host': '2001:db8::2', 'port': 1080}) in calls
    assert not any(c[1] and c[1].get('host') == '2001:db8::3' for c in calls)
    assert not any(c[1] and c[1].get('host') == '2001:db8::4' for c in calls)


def test_default_yaml_and_invalid_boolean_save(environment):
    import yaml
    client, _, _, headers, *_ = environment
    data = client.get('/api/custom-config/mihomo', headers=headers).get_json()
    assert yaml.safe_load(data['default_config'])['dns']['enable'] is True
    for raw in ('ipv6: ture', 'dns:\n  ipv6: "true"'):
        response = client.post('/api/custom-config/mihomo', headers=headers, json={'config': raw})
        assert response.status_code == 400


@pytest.mark.parametrize('during_recheck', [False, True])
def test_unrelated_agent_runtime_does_not_invalidate_prepare(environment, monkeypatch, during_recheck):
    client, manager, _, headers, path, router, calls, _ = environment
    other = manager.register_agent({'name': 'Other', 'host': '192.168.0.4', 'port': 27647,
                                    'service_type': 'mosdns', 'profile_id': 'other'})
    report = client.get(path, headers=headers).get_json()

    def change_runtime():
        def update(agents):
            row = next(a for a in agents if a['id'] == other['id'])
            row.update(config_version='background-publication', last_heartbeat='2026-10-30T23:59:00')
        manager._update_agents(update)

    if during_recheck:
        original = bridge._request
        def request(agent, operation='', payload=None):
            if not operation:
                change_runtime()
            return original(agent, operation, payload)
        monkeypatch.setattr(bridge, '_request', request)
    else:
        change_runtime()
    response = client.post(path + '/prepare', headers=headers, json={
        'interface': report['prepare_interface'], 'check_token': report['check_token']})
    assert response.status_code == 200, response.get_json()
    assert any(operation == 'prepare' for operation, _ in calls)
    assert not router.writes


def test_identical_saved_draft_keeps_preparation_valid(environment):
    client, manager, _, headers, path, router, calls, _ = environment
    report = client.get(path, headers=headers).get_json()
    # Saving identical content advances profile metadata/revision only.
    manager.repository.save_profile('v6', {'mihomo': {'custom_config': YAML}})
    response = client.post(path + '/prepare', headers=headers, json={
        'interface': report['prepare_interface'], 'check_token': report['check_token']})
    assert response.status_code == 200, response.get_json()
    assert any(operation == 'prepare' for operation, _ in calls)
    assert not router.writes


@pytest.mark.parametrize('during_recheck', [False, True])
def test_real_draft_edit_still_blocks_prepare(environment, monkeypatch, during_recheck):
    client, manager, _, headers, path, router, calls, _ = environment
    report = client.get(path, headers=headers).get_json()
    def change_draft():
        manager.repository.save_profile('v6', {'mihomo': {'custom_config': YAML.replace('fc00::1/64', 'fc01::1/64')}})
    if during_recheck:
        original = bridge._request
        def request(agent, operation='', payload=None):
            if not operation:
                change_draft()
            return original(agent, operation, payload)
        monkeypatch.setattr(bridge, '_request', request)
    else:
        change_draft()
    response = client.post(path + '/prepare', headers=headers, json={
        'interface': report['prepare_interface'], 'check_token': report['check_token']})
    assert response.status_code == 409, response.get_json()
    assert response.get_json()['code'] == 'CHECK_CHANGED'
    assert not router.writes and not any(operation == 'prepare' for operation, _ in calls)


def test_router_route_order_and_lifetime_changes_keep_prepare_valid(environment):
    client, _, _, headers, path, router, calls, _ = environment
    router.rows[ros.ROUTES].append({'.id': '*A', 'dst-address': '2001:db8::/64', 'gateway': 'pppoe-out1',
                                   'active': 'true', 'expires-after': '5m'})
    report = client.get(path, headers=headers).get_json()
    router.rows[ros.ROUTES][-1]['expires-after'] = '4m59s'
    router.rows[ros.ROUTES].reverse()
    fresh = client.get(path, headers=headers).get_json()
    assert fresh['check_token'] == report['check_token']
    response = client.post(path + '/prepare', headers=headers, json={
        'interface': report['prepare_interface'], 'check_token': report['check_token']})
    assert response.status_code == 200, response.get_json()
    assert any(operation == 'prepare' for operation, _ in calls)
    assert not router.writes


@pytest.mark.parametrize('changed', [{'host': '192.168.0.5'}, {'port': 27648}, {'token': 'replaced'},
                                    {'gateway_installation_id': 'reinstalled'}])
def test_real_target_change_still_blocks_prepare(environment, changed):
    client, manager, agent, headers, path, router, calls, _ = environment
    report = client.get(path, headers=headers).get_json()
    manager._update_agents(lambda agents: next(a for a in agents if a['id'] == agent['id']).update(changed))
    response = client.post(path + '/prepare', headers=headers, json={
        'interface': report['prepare_interface'], 'check_token': report['check_token']})
    assert response.status_code == 409, response.get_json()
    assert not router.writes and not any(operation == 'prepare' for operation, _ in calls)
