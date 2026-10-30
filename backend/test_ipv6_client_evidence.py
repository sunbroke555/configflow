from copy import deepcopy
import threading

import pytest

from backend.agents import ipv6_client_evidence as evidence


@pytest.fixture
def path(monkeypatch):
    config = {'routers': {'items': [{'id': 'ros', 'type': 'routeros', 'proxy_gateway_agent_id': 'gw'}]}}
    request = {'family': 'ipv4', 'status': 'ok', 'source_ip': '192.0.2.100', 'source_port': 45678, 'address': '198.18.0.5'}
    match = {'source_ip': request['source_ip'], 'source_port': 45678, 'host': 'test.example',
             'chains': ['node'], 'ipv6_transport_verified': True, 'proxy_name': 'node', 'proxy_type': 'Socks5',
             'network': 'tcp', 'destination_port': 443}
    rows = [{'protocol': 'tcp', 'src-address': request['source_ip'], 'src-port': '45678',
             'dst-address': '198.18.0.5', 'dst-port': '443', 'orig-packets': '5'}]
    class Driver:
        def _list(self, path):
            assert '/firewall/connection?' in path and 'src-address=' in path
            return rows
        def close(self):
            pass
    monkeypatch.setattr(evidence.readiness, '_driver', lambda _: Driver())
    report = {'target_host': 'test.example', 'requests': [request]}
    trace = {'matches': [match], 'target_host': 'test.example'}
    def evaluate():
        return {c['id']: c['status'] for c in evidence.evaluate(config, {'id': 'gw'}, report, trace, 'https://test.example/')}
    return report, trace, rows, evaluate


def test_all_three_hops_require_correlated_evidence(path):
    _, _, _, evaluate = path
    assert set(evaluate().values()) == {'ok'}


@pytest.mark.parametrize('field,value', [('source_port', 45679), ('source_ip', '192.0.2.101'),
                                        ('host', 'another.example'), ('chains', ['DIRECT']),
                                        ('network', 'udp'), ('destination_port', 80), ('proxy_name', '')])
def test_unrelated_mihomo_flow_never_proves_proxy_path(path, field, value):
    _, trace, _, evaluate = path
    trace['matches'][0][field] = value
    assert evaluate()['client_mihomo_path'] == 'unknown'
    assert evaluate()['client_ipv6_transport'] == 'unknown'


def test_node_socket_alone_does_not_prove_client_ipv6(path):
    _, trace, _, evaluate = path
    trace['matches'][0].pop('ipv6_transport_verified')
    trace['checks'] = [{'id': 'node_ipv6_transport', 'status': 'ok'}]
    assert evaluate()['client_ipv6_transport'] == 'unknown'


def test_ros_requires_destination_and_packets_not_only_default_gateway(path):
    report, _, rows, evaluate = path
    report['default_routes'] = [{'gateway': '192.0.2.1'}]
    rows[0]['dst-address'] = '198.18.0.6'
    assert evaluate()['client_ros_path'] == 'unknown'
    rows[0]['dst-address'] = '198.18.0.5'
    rows[0]['orig-packets'] = '0'
    assert evaluate()['client_ros_path'] == 'unknown'


def test_both_address_families_need_matching_flows(path):
    report, _, _, evaluate = path
    extra = deepcopy(report['requests'][0])
    extra.update(family='ipv6', source_ip='2001:db8::2', address='fc00::4')
    report['requests'].append(extra)
    assert set(evaluate().values()) == {'unknown'}


def test_legacy_routeros_combined_address_port(path):
    _, _, rows, evaluate = path
    rows[0].update({'src-address': '192.0.2.100:45678', 'dst-address': '198.18.0.5:443'})
    rows[0].pop('src-port'); rows[0].pop('dst-port')
    assert evaluate()['client_ros_path'] == 'ok'


def test_observer_keeps_new_packets_before_short_conntrack_entries_expire(monkeypatch):
    config = {'routers': {'items': [{'id': 'ros', 'type': 'routeros', 'proxy_gateway_agent_id': 'gw'}]}}
    calls = {}
    stop, ready = threading.Event(), threading.Event()
    fresh = {'.id': '*2', 'protocol': 'tcp', 'src-address': '192.0.2.100', 'src-port': '45678',
             'dst-address': '198.18.0.5', 'dst-port': '443', 'orig-packets': '3'}
    class Driver:
        closed = False
        def _list(self, path, timeout=None):
            assert timeout == 2 and 'dst-port=443' in path and '.proplist=' in path
            calls[path] = calls.get(path, 0) + 1
            if calls[path] == 1:
                return [{**fresh, '.id': '*1'}]
            if path.startswith('/ipv6'):
                stop.set()
                return []
            return [{**fresh, '.id': '*1'}, fresh]
        def close(self):
            self.closed = True
    driver = Driver()
    monkeypatch.setattr(evidence.readiness, '_driver', lambda _: driver)
    observed = evidence.observe(config, {'id': 'gw'}, 'https://test.example/', stop, ready)
    assert ready.is_set() and driver.closed
    assert observed == {'ros': [fresh]}
    class Expired:
        def _list(self, path):
            return []
    request = {'source_ip': '192.0.2.100', 'source_port': 45678, 'address': '198.18.0.5'}
    assert evidence._tracked(Expired(), request, 443, observed['ros'])
    request['source_port'] = 45679
    assert not evidence._tracked(Expired(), request, 443, observed['ros'])
