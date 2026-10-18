"""Upstream router integration (read-only P0): drivers, desired state, drift report and API."""
import base64
import hashlib

import pytest

from backend.routers import desired as desired_module
from backend.routers.base import NatBypassEntry, RouteEntry, RouterError, RouterInfo, RouterState
from backend.routers.ikuai import IKuaiDriver
from backend.routers.report import build_report
from backend.routers.routeros import RouterOSDriver
from backend.routers.store import RouterValidationError, normalize, public_view, redact_routers
from backend.test_agent_upgrade_auth import authenticated_app  # noqa: F401  (pytest fixture)

MIHOMO_YAML = 'dns:\n  enhanced-mode: fake-ip\n  fake-ip-range: 198.18.0.1/16\n'
TG_CONTENT = 'IP-CIDR,91.108.4.0/22,no-resolve\nIP-CIDR,149.154.160.0/20\nIP-CIDR,10.0.0.0/8\nDOMAIN,t.me\n'


def profile_config():
    return {
        'mihomo': {'custom_config': MIHOMO_YAML},
        'proxy_groups': [
            {'name': '🎯 全球直连', 'manual_nodes': ['DIRECT'], 'include_groups': []},
            {'name': '📲 Telegram', 'manual_nodes': [], 'include_groups': ['g1']},
        ],
        'rule_configs': [
            {'id': 'r1', 'itemType': 'ruleset', 'name': 'Telegram', 'policy': '📲 Telegram', 'enabled': True},
            {'id': 'r2', 'itemType': 'ruleset', 'name': 'ChinaIP', 'policy': '🎯 全球直连', 'enabled': True},
            {'id': 'r3', 'itemType': 'ruleset', 'name': 'Blocked', 'policy': 'REJECT', 'enabled': True},
            {'id': 'r4', 'itemType': 'ruleset', 'name': 'Disabled', 'policy': '📲 Telegram', 'enabled': False},
            {'id': 'r5', 'itemType': 'ruleset', 'name': 'Uncached', 'policy': '📲 Telegram', 'enabled': True},
        ],
        'rule_library': [],
    }


def loader(item, _library):
    return {'Telegram': TG_CONTENT, 'ChinaIP': 'IP-CIDR,1.0.1.0/24', 'Blocked': 'IP-CIDR,2.2.2.0/24'}.get(item['name'], '')


def test_desired_uses_only_selected_rule_sets():
    result = desired_module.compute_desired(profile_config(), '192.168.1.4', ['r1'], content_loader=loader)
    cidrs = {entry['cidr']: entry['sources'] for entry in result['proxy_cidrs']}
    assert cidrs == {'91.108.4.0/22': ['Telegram'], '149.154.160.0/20': ['Telegram'], '198.18.0.0/16': ['fake-ip']}
    assert result['proxy_gateway'] == '192.168.1.4' and result['warnings'] == []


def test_desired_defaults_to_fake_ip_only():
    result = desired_module.compute_desired(profile_config(), '192.168.1.4', content_loader=loader)
    assert [entry['cidr'] for entry in result['proxy_cidrs']] == ['198.18.0.0/16']


def test_desired_warns_about_questionable_selections():
    result = desired_module.compute_desired(profile_config(), None, ['r2', 'r4', 'r5', 'gone'], content_loader=loader)
    assert [entry['cidr'] for entry in result['proxy_cidrs']] == ['1.0.1.0/24', '198.18.0.0/16']
    joined = '\n'.join(result['warnings'])
    for text in ('ChinaIP', '不走代理', 'Disabled', '已停用', 'Uncached', '没有本地缓存', 'gone', 'mihomo Agent'):
        assert text in joined


def test_desired_defaults_and_missing_gateway():
    result = desired_module.compute_desired({'mihomo': {'custom_config': ''}}, None, content_loader=loader)
    assert [entry['cidr'] for entry in result['proxy_cidrs']] == ['198.18.0.0/16']
    assert any('mihomo Agent' in warning for warning in result['warnings'])


@pytest.mark.parametrize('policy', ['DIRECT', 'REJECT', 'REJECT-DROP', 'PASS', 'COMPATIBLE', '🎯 全球直连'])
def test_non_proxy_policy_keeps_selected_ips_as_an_advisory(policy):
    config = profile_config()
    config['rule_configs'][1]['policy'] = policy
    result = desired_module.compute_desired(config, '192.168.1.4', ['r1', 'r2'], content_loader=loader)
    assert any(entry['cidr'] == '1.0.1.0/24' for entry in result['proxy_cidrs'])
    assert len(result['warnings']) == 1 and policy in result['warnings'][0]
    assert result['blocking_warnings'] == []


def test_incomplete_selection_is_distinct_from_non_proxy_advice():
    result = desired_module.compute_desired(profile_config(), None, ['r2', 'r4', 'r5', 'gone'], content_loader=loader)
    assert len(result['warnings']) == 5
    assert len(result['blocking_warnings']) == 4
    assert all(warning in result['warnings'] for warning in result['blocking_warnings'])
    assert not any('ChinaIP' in warning for warning in result['blocking_warnings'])


def _desired(gateway='192.168.1.4'):
    return {'fake_ip_range': '198.18.0.0/16', 'proxy_gateway': gateway, 'warnings': [],
            'proxy_cidrs': [{'cidr': '198.18.0.0/16', 'sources': ['fake-ip']},
                            {'cidr': '91.108.4.0/22', 'sources': ['Telegram']},
                            {'cidr': '149.154.160.0/20', 'sources': ['Telegram']},
                            {'cidr': '185.76.151.0/24', 'sources': ['Telegram']}]}


def test_report_statuses_and_nat_coverage():
    state = RouterState(
        routes=[RouteEntry('1', '198.18.0.0/16', '192.168.1.4', True, 'cf:fake-ip'),
                RouteEntry('2', '91.108.4.0/22', '192.168.1.4', True, 'tg'),
                RouteEntry('3', '149.154.160.0/20', '192.168.1.9', True, ''),
                RouteEntry('4', '8.8.8.8/32', '192.168.1.4', True, 'DNS'),
                RouteEntry('5', '203.0.113.0/24', '192.168.1.4', True, 'cf:old')],
        nat_bypass=[NatBypassEntry('n1', ['198.18.0.0/16', '91.108.0.0/16'], ['192.168.1.1-192.168.1.3'], True, 'mihomo'),
                    NatBypassEntry('n2', ['185.76.151.0/24'], [], False, 'old')])
    report = build_report(_desired(), state)
    routes = {item['cidr']: item['status'] for item in report['routes']['items']}
    assert routes == {'198.18.0.0/16': 'synced', '91.108.4.0/22': 'adoptable',
                      '149.154.160.0/20': 'conflict', '185.76.151.0/24': 'missing'}
    assert [item['cidr'] for item in report['routes']['extra']] == ['203.0.113.0/24']
    assert [item['cidr'] for item in report['routes']['unmanaged_other']] == ['8.8.8.8/32']
    nat = {item['cidr']: item['status'] for item in report['nat_bypass']['items']}
    assert nat == {'198.18.0.0/16': 'adoptable', '91.108.4.0/22': 'adoptable',
                   '149.154.160.0/20': 'missing', '185.76.151.0/24': 'missing'}
    assert report['nat_bypass']['disabled_rules'][0]['rule_id'] == 'n2'
    assert report['in_sync'] is False


class FakeResponse:
    def __init__(self, data, status=200):
        self.status_code = status
        self._data = data

    def json(self):
        return self._data


class FakeIKuaiSession:
    def __init__(self, version='4.0.308'):
        self.calls = []
        self.version = version
        self.trust_env = True
        self.verify = True

    def close(self):
        pass

    def post(self, url, json=None, **_):
        self.calls.append((url, json))
        if url.endswith('/Action/login'):
            return FakeResponse({'Result': 10000, 'code': 0, 'message': '成功'})
        func = json['func_name']
        rows = {
            'static_rt': [{'id': 10, 'dst_addr': '198.18.0.0', 'netmask': '255.255.0.0', 'gateway': '192.168.1.4',
                           'enabled': 'yes', 'comment': 'fake-ip'}],
            'route_object': [{'group_id': 'IPGP1', 'group_name': 'proxy_cidr',
                              'group_value': [{'ip': '198.18.0.0/16'}, {'ip': '91.108.4.0/22'}]}],
            'nat_rule': [{'id': 1, 'action': 'filter', 'enabled': 'yes', 'comment': 'mihomo',
                          'src_addr': {'custom': ['192.168.1.5-192.168.1.255'], 'object': {}},
                          'dst_addr': {'custom': {}, 'object': [{'gp_name': 'proxy_cidr'}]}},
                         {'id': 2, 'action': 'dnat', 'enabled': 'no', 'comment': 'dns'}],
            'stream_ipport': [{'id': 3, 'enabled': 'yes', 'tagname': 'cf_pr_x', 'comment': 'cf-mihomo', 'type': 1,
                               'nexthop': '192.168.1.4', 'protocol': 'any', 'src_addr_inv': 1, 'dst_addr_inv': 0,
                               'src_addr': {'custom': ['192.168.1.4'], 'object': {}},
                               'dst_addr': {'custom': {}, 'object': [{'gp_name': 'proxy_cidr'}]}}],
            'static_rt_table': [{'id': 1, 'dst_addr': '198.18.0.0', 'netmask': '255.255.0.0', 'gateway': '192.168.1.4',
                                 'interface': 'lan1', 'prio': 1}],
        }
        if func == 'sysstat':
            return FakeResponse({'code': 0, 'results': {'verinfo': {'version': self.version, 'verstring': 'x64'}}})
        return FakeResponse({'code': 0, 'results': {'total': len(rows[func]), 'data': rows[func]}})


def test_ikuai_driver_login_probe_and_state(monkeypatch):
    driver = IKuaiDriver('http://192.168.1.1/', 'admin', 'pw')
    session = FakeIKuaiSession()
    driver.session = session
    info = driver.probe()
    assert info.version == '4.0.308' and set(info.capabilities) == {'routes', 'nat_bypass', 'address_sets',
                                                                     'policy_routes'}
    login = session.calls[0][1]
    assert login == {'username': 'admin', 'passwd': hashlib.md5(b'pw').hexdigest(),
                     'pass': base64.b64encode(b'salt_11pw').decode()}
    state = driver.read_state()
    assert state.routes[0].dst == '198.18.0.0/16' and state.routes[0].enabled
    assert len(state.nat_bypass) == 1
    assert state.nat_bypass[0].dst == ['198.18.0.0/16', '91.108.4.0/22']
    assert [(route.dst, route.gateway) for route in state.active_routes] == [('198.18.0.0/16', '192.168.1.4')]
    [rule] = state.policy_routes
    assert (rule.dst, rule.gateway, rule.src, rule.src_invert, rule.protocol) == \
        (['198.18.0.0/16', '91.108.4.0/22'], '192.168.1.4', ['192.168.1.4'], True, 'any')


def test_ikuai_driver_rejects_3x():
    driver = IKuaiDriver('http://192.168.1.1', 'admin', 'pw')
    driver.session = FakeIKuaiSession(version='3.7.21')
    with pytest.raises(RouterError, match='4.x'):
        driver.probe()


class FakeROSSession:
    def __init__(self, version='7.15.3 (stable)'):
        self.version = version
        self.trust_env = True
        self.verify = True
        self.auth = None

    def close(self):
        pass

    def get(self, url, **_):
        path = url.split('/rest', 1)[1]
        data = {
            '/system/resource': {'version': self.version, 'board-name': 'CHR'},
            '/ip/route': [{'.id': '*1', 'dst-address': '198.18.0.0/16', 'gateway': '192.168.1.4',
                           'disabled': 'false', 'comment': 'cf:fake-ip'},
                          {'.id': '*2', 'dst-address': '0.0.0.0/0', 'gateway': 'pppoe-out1', 'disabled': 'false'}],
            '/ip/firewall/nat': [{'.id': '*3', 'chain': 'srcnat', 'action': 'accept', 'dst-address-list': 'cf-proxy',
                                  'disabled': 'false', 'comment': 'cf:return'},
                                 {'.id': '*4', 'chain': 'srcnat', 'action': 'masquerade', 'disabled': 'false'}],
            '/ip/firewall/address-list': [{'list': 'cf-proxy', 'address': '198.18.0.0/16', 'disabled': 'false'}],
            '/routing/rule': [], '/ip/firewall/mangle': [], '/ip/vrf': [],
            '/interface/list': [], '/interface/list/member': [],
        }[path]
        return FakeResponse(data)


def test_routeros_driver_probe_and_state():
    driver = RouterOSDriver('https://192.168.88.1', 'cf', 'pw')
    driver.session = FakeROSSession()
    assert driver.probe().version.startswith('7.15')
    state = driver.read_state()
    assert [route.dst for route in state.routes] == ['198.18.0.0/16', '0.0.0.0/0']
    assert state.nat_bypass[0].dst == ['198.18.0.0/16']
    assert state.nat_context is not None
    assert any(rule['action'] == 'masquerade' for rule in state.nat_context['rules'])


def test_routeros_requires_7_1():
    driver = RouterOSDriver('https://192.168.88.1', 'cf', 'pw')
    driver.session = FakeROSSession(version='6.49.10 (long-term)')
    with pytest.raises(RouterError, match='7.1'):
        driver.probe()


@pytest.mark.parametrize('router_type', ['ikuai', 'routeros'])
@pytest.mark.parametrize('scheme', ['http', 'https'])
def test_store_validation_password_and_redaction(router_type, scheme):
    payload = {'name': '主路由', 'type': router_type, 'endpoint': f'{scheme}://192.168.1.1/', 'username': 'admin', 'password': 'secret',
               'ip_rule_sets': ['r1', 'r1']}
    router = normalize(payload)
    assert router['ip_rule_sets'] == ['r1']
    with pytest.raises(RouterValidationError):
        normalize({**payload, 'ip_rule_sets': 'r1'})
    assert router['endpoint'] == f'{scheme}://192.168.1.1' and router['id'].startswith('router_')
    assert 'password' not in public_view(router) and public_view(router)['has_password'] is True
    kept = normalize({**payload, 'password': ''}, router)
    assert kept['password'] == 'secret'
    with pytest.raises(RouterValidationError):
        normalize({**payload, 'endpoint': 'ftp://x'})
    redacted = redact_routers({'items': [router]})['items'][0]
    assert 'password' not in redacted and redacted['username'] == '[REDACTED]' and redacted['endpoint'] == '[REDACTED]'


def test_router_api_crud_probe_report_and_export(authenticated_app, monkeypatch):  # noqa: F811
    client, manager, agents, admin, _ = authenticated_app
    agent = agents[0]
    created = client.post('/api/routers', headers=admin, json={
        'name': '主路由', 'type': 'ikuai', 'endpoint': 'http://192.168.1.1', 'username': 'admin',
        'password': 'router-secret', 'proxy_gateway_agent_id': agent['id'], 'manage_return_routing': True})
    assert created.status_code == 200, created.get_json()
    router_id = created.get_json()['router']['id']

    listed = client.get('/api/routers', headers=admin).get_json()['routers']
    assert listed[0]['has_password'] is True and 'password' not in listed[0]
    assert 'router-secret' not in client.get('/api/routers', headers=admin).get_data(as_text=True)

    updated = client.put(f'/api/routers/{router_id}', headers=admin, json={'name': '新名字', 'password': ''})
    assert updated.status_code == 200 and updated.get_json()['router']['name'] == '新名字'

    class StubDriver:
        def __init__(self, endpoint, username, password, verify_tls=True):
            assert password == 'router-secret'

        def probe(self):
            return RouterInfo(vendor='ikuai', version='4.0.308', capabilities=['routes'])

        def read_state(self):
            return RouterState(routes=[RouteEntry('1', '198.18.0.0/16', '127.0.0.1', True, 'fake-ip')])

        def close(self):
            pass

    from backend.routes import routers as routes_module
    monkeypatch.setitem(routes_module.DRIVERS, 'ikuai', StubDriver)
    probe = client.post(f'/api/routers/{router_id}/probe', headers=admin)
    assert probe.status_code == 200 and probe.get_json()['probe']['version'] == '4.0.308'
    assert client.get('/api/routers', headers=admin).get_json()['routers'][0]['last_probe']['ok'] is True

    monkeypatch.setattr(routes_module, '_return_routing', lambda agent_record: {'status': 'ok', 'return_tables': []})
    report = client.get(f'/api/routers/{router_id}/report', headers=admin)
    assert report.status_code == 200, report.get_json()
    body = report.get_json()['report']
    assert body['desired']['proxy_gateway'] == '127.0.0.1'
    item = body['routes']['items'][0]
    assert {key: item[key] for key in ('cidr', 'sources', 'status', 'route_ids')} == {
        'cidr': '198.18.0.0/16', 'sources': ['fake-ip'], 'status': 'adoptable', 'route_ids': ['1']}
    assert body['return_routing']['status'] == 'ok'

    exported = client.get('/api/config/export?desensitize=true', headers=admin).get_data(as_text=True)
    assert 'router-secret' not in exported

    assert client.delete(f'/api/routers/{router_id}', headers=admin).status_code == 200
    assert client.get('/api/routers', headers=admin).get_json()['routers'] == []


def test_router_api_validation_and_not_found(authenticated_app):  # noqa: F811
    client, _, _, admin, _ = authenticated_app
    bad = client.post('/api/routers', headers=admin, json={'name': '', 'type': 'ikuai'})
    assert bad.status_code == 400 and bad.get_json()['message']
    assert client.post('/api/routers/missing/probe', headers=admin).status_code == 404
    assert client.get('/api/routers/missing/report', headers=admin).status_code == 404


def test_return_routing_setting_defaults_off_and_is_preserved_on_partial_update():
    payload = {'name': '主路由', 'type': 'ikuai', 'endpoint': 'http://router.local',
               'username': 'admin', 'password': 'pw'}
    router = normalize(payload)
    assert router['manage_return_routing'] is False
    assert public_view({key: value for key, value in router.items() if key != 'manage_return_routing'})[
        'manage_return_routing'] is False
    enabled = normalize({'manage_return_routing': True}, router)
    assert enabled['manage_return_routing'] is True
    assert normalize({'name': '新名字'}, enabled)['manage_return_routing'] is True
    assert normalize({'manage_return_routing': False}, enabled)['manage_return_routing'] is False


@pytest.mark.parametrize('value', ['true', 'false', 1, 0, None, {}, []])
def test_return_routing_setting_rejects_non_boolean_values(value):
    payload = {'name': '主路由', 'type': 'ikuai', 'endpoint': 'http://router.local',
               'username': 'admin', 'password': 'pw'}
    with pytest.raises(RouterValidationError):
        normalize({**payload, 'manage_return_routing': value})
    with pytest.raises(RouterValidationError):
        normalize({'manage_return_routing': value}, normalize(payload))
