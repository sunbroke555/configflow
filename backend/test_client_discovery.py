"""Device discovery for 「客户端设备」: live RouterOS / iKuai reads joined with the saved rows.

RouterOS fixtures are a read-only CLI snapshot of a RouterOS 7.24.5 home router
converted to the REST JSON shape (testdata/routeros_clients_7245.json, MACs and
personal host names anonymized). Ping request/response/error shapes were
captured from a RouterOS 7.24.5 CHR over REST. iKuai replies were captured from
an isolated iKuai 4.0.314 lab (testdata/ikuai_clients_40314.json).
"""
import copy
import json
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import requests

from backend.common import client_discovery
from backend.common.agent_manager import get_agent_manager
from backend.common.oxidns_device_policy import apply_policies
from backend.routers import ikuai_clients, ros_clients
from backend.routers.base import RouterError
from backend.routers.ikuai import IKuaiDriver
from backend.routers.routeros import PingNotPermitted, RouterOSDriver
from backend.test_oxidns_integration import oxidns_app  # noqa: F401

SNAPSHOT = json.loads((Path(__file__).parent / 'testdata' / 'routeros_clients_7245.json').read_text())
IKUAI_LAB = json.loads((Path(__file__).parent / 'testdata' / 'ikuai_clients_40314.json').read_text())
TABLES = {'/ip/arp': SNAPSHOT['arp'], '/ip/dhcp-server/lease': SNAPSHOT['leases'],
          '/ip/dhcp-server': SNAPSHOT['dhcp_servers'], '/ip/address': SNAPSHOT['addresses']}
# Stale LAN entries with no fresh DHCP renewal: these need an active check.
STALE = ['192.168.0.5', '192.168.0.7', '192.168.0.13', '192.168.0.23', '192.168.0.32', '192.168.0.34',
         '192.168.0.36', '192.168.0.42', '192.168.0.45', '192.168.0.56', '192.168.0.72', '192.168.0.80',
         '192.168.0.91', '192.168.0.100', '192.168.0.103', '192.168.0.107', '192.168.0.241']
# Captured on RouterOS 7.24.5 CHR: POST /rest/ping {"address", "count": "2", "interval": "300ms", ...}.
ARP_ANSWER = [{'avg-rtt': '479us', 'host': 'BC:24:11:C0:F5:77', 'max-rtt': '479us', 'min-rtt': '479us',
               'packet-loss': '0', 'received': '1', 'sent': '1', 'seq': '0', 'time': '479us'},
              {'avg-rtt': '441us', 'host': 'BC:24:11:C0:F5:77', 'max-rtt': '479us', 'min-rtt': '403us',
               'packet-loss': '0', 'received': '2', 'sent': '2', 'seq': '1', 'time': '403us'}]
ICMP_ANSWER = [{'avg-rtt': '137us', 'host': '10.97.0.1', 'max-rtt': '137us', 'min-rtt': '137us', 'packet-loss': '0',
                'received': '1', 'sent': '1', 'seq': '0', 'size': '56', 'time': '137us', 'ttl': '64'}]
SILENT = [{'host': '10.97.0.222', 'packet-loss': '100', 'received': '0', 'sent': '1', 'seq': '0', 'status': 'timeout'},
          {'host': '10.97.0.222', 'packet-loss': '100', 'received': '0', 'sent': '2', 'seq': '1', 'status': 'timeout'}]
NO_TEST_POLICY = (500, {'detail': 'not enough permissions (9)', 'error': 500, 'message': 'Internal Server Error'})
NO_INTERFACE = (400, {'detail': 'failure: interface needs to be specified for arp ping', 'error': 400,
                      'message': 'Bad Request'})


def response(status, body):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(body).encode()
    return result


class FakeRouterOS:
    """The REST surface ros_clients uses, behind a real RouterOSDriver session."""

    def __init__(self, tables=None, ping=None, rearp=None):
        self.tables = copy.deepcopy(TABLES if tables is None else tables)
        self.ping = ping or (lambda values: (200, SILENT))
        self.rearp = rearp            # /ip/arp rows served after the first read
        self.pings = []
        self.reads = []
        self.lock = threading.Lock()

    def request(self, method, url, **kwargs):
        path = urlsplit(url).path.removeprefix('/rest')
        with self.lock:
            if method.upper() == 'POST' and path == '/ping':
                self.pings.append(kwargs['json'])
                result = self.ping(kwargs['json'])
                if isinstance(result, Exception):
                    raise result
                return response(*result)
            assert method.upper() == 'GET', (method, path)
            self.reads.append(path)
            if path == '/ip/arp' and self.rearp is not None and self.reads.count('/ip/arp') > 1:
                return response(200, self.rearp)
            if path not in self.tables:
                return response(404, {})
            rows = self.tables[path]
            return response(*rows) if isinstance(rows, tuple) else response(200, rows)

    def driver(self, monkeypatch, user='configflow'):
        driver = RouterOSDriver('http://192.0.2.1', user, 'private-test-password')
        monkeypatch.setattr(driver.session, 'request', self.request)
        return driver


def answers(*ips):
    return lambda values: (200, ARP_ANSWER if values['address'] in ips else SILENT)


def read(fake, monkeypatch, priority=(), deadline=None):
    return ros_clients.read(fake.driver(monkeypatch), priority=priority, deadline=deadline)


# --- RouterOS -----------------------------------------------------------------------------------------------

def test_snapshot_only_reads_the_lan_and_decides_presence_from_arp_and_leases(monkeypatch):
    fake = FakeRouterOS(ping=answers(*STALE[:9]))
    reading = read(fake, monkeypatch)
    seen = reading.sightings
    # The WAN carries 252 entries (251 failed); none are clients.
    assert not [ip for ip in seen if ip.startswith('192.168.1.')]
    assert len(seen) == 47 and reading.own == {'192.168.0.1', '192.168.1.2'}
    assert reading.notes == []
    expected = {
        '192.168.0.8': ('online', 'arp'),          # reachable
        '192.168.0.134': ('online', 'arp'),        # delay
        '192.168.0.133': ('online', 'lease'),      # stale ARP, dynamic lease renewed 8m43s ago
        '192.168.0.102': ('online', 'arp'),        # static lease "waiting" while the static-IP VM is reachable
        '192.168.0.96': ('offline', 'arp'),        # failed
    }
    for ip, (presence, basis) in expected.items():
        assert (seen[ip].presence, seen[ip].basis) == (presence, basis), ip
    for ip in STALE[:9]:
        assert (seen[ip].presence, seen[ip].basis) == ('online', 'probe'), ip
    for ip in STALE[9:]:
        assert (seen[ip].presence, seen[ip].basis) == ('offline', 'probe'), ip
    assert seen['192.168.0.133'].public() == {
        'mac': 'E0:BA:AD:94:F2:48', 'hostname': 'C6c_EZVIZ', 'sources': ['arp', 'lease'], 'presence': 'online',
        'basis': 'lease', 'last_seen': 523, 'router_id': ''}
    assert seen['192.168.0.102'].hostname == 'Win11 VM static'     # the lease comment names it
    # One MAC owns two addresses: both are listed.
    assert seen['192.168.0.35'].mac == seen['192.168.0.241'].mac == 'BC:24:11:A2:84:D0'
    # Every stale host got exactly one ARP ping on its own interface.
    assert sorted(item['address'] for item in fake.pings) == sorted(STALE)
    assert fake.pings[0] == {'address': fake.pings[0]['address'], 'count': '2', 'interval': '300ms',
                             'arp-ping': 'yes', 'interface': 'lan4'}
    assert 'private-test-password' not in json.dumps(fake.pings)


def test_saved_addresses_are_confirmed_first_and_the_rest_are_capped(monkeypatch):
    monkeypatch.setattr(ros_clients, 'PROBE_LIMIT', 3)
    fake = FakeRouterOS(ping=answers(*STALE))
    reading = read(fake, monkeypatch, priority={'192.168.0.100', '192.168.0.241'})
    assert sorted(item['address'] for item in fake.pings) == ['192.168.0.100', '192.168.0.241', '192.168.0.5']
    assert reading.sightings['192.168.0.7'].presence == 'recent'
    assert reading.notes == ['待确认的设备较多，只主动确认了 3 台，其余 14 台显示为近期见过']


def test_a_group_without_the_test_policy_stops_pinging_and_keeps_hosts_recent(monkeypatch):
    fake = FakeRouterOS(ping=lambda values: NO_TEST_POLICY)
    reading = read(fake, monkeypatch)
    assert {reading.sightings[ip].presence for ip in STALE} == {'recent'}
    assert {reading.sightings[ip].basis for ip in STALE} == {'stale'}
    assert reading.notes == [ros_clients.NOTE_NO_TEST]
    assert 'test 权限' in ros_clients.NOTE_NO_TEST
    assert len(fake.pings) <= ros_clients.PROBE_WORKERS     # no point trying every host
    assert fake.reads.count('/ip/arp') == 1


def test_icmp_fallback_settles_silent_hosts_from_the_arp_table_read_afterwards(monkeypatch):
    tables = copy.deepcopy(TABLES)
    tables['/ip/arp'] = [row for row in tables['/ip/arp'] if row['address'] in ('192.168.0.5', '192.168.0.7',
                                                                                   '192.168.0.13', '192.168.0.23')]

    def ping(values):
        if 'arp-ping' in values:
            return NO_INTERFACE
        return (200, ICMP_ANSWER) if values['address'] == '192.168.0.5' else (200, SILENT)
    later = {'192.168.0.5': 'reachable', '192.168.0.7': 'reachable', '192.168.0.13': 'failed', '192.168.0.23': 'delay'}
    rearp = [{**row, 'status': later[row['address']]} for row in tables['/ip/arp']]
    fake = FakeRouterOS(tables, ping, rearp)
    seen = read(fake, monkeypatch).sightings
    assert [(seen[ip].presence, seen[ip].basis) for ip in ('192.168.0.5', '192.168.0.7', '192.168.0.13', '192.168.0.23')] == [
        ('online', 'probe'),     # answered ICMP
        ('online', 'probe'),     # dropped ICMP, ARP answered meanwhile
        ('offline', 'probe'),    # ARP resolution failed
        ('recent', 'stale'),     # still in delay: not decided yet
    ]
    assert len([item for item in fake.pings if 'arp-ping' not in item]) == 4
    assert {'address': '192.168.0.5', 'count': '2', 'interval': '300ms'} in fake.pings


def test_timeouts_and_the_deadline_leave_hosts_recent(monkeypatch):
    fake = FakeRouterOS(ping=lambda values: requests.ReadTimeout())
    reading = read(fake, monkeypatch)
    assert {reading.sightings[ip].presence for ip in STALE} == {'recent'}
    assert reading.notes == [f'{len(STALE)} 台设备未能及时确认，显示为近期见过']
    late = FakeRouterOS(ping=answers(*STALE))
    sightings = {ip: ros_clients.Sighting(ip=ip, presence='recent', basis='stale') for ip in STALE}
    ros_clients.confirm(late.driver(monkeypatch), sightings, [(ip, 'lan4') for ip in STALE], {'lan4'},
                        deadline=time.monotonic() + 0.9)
    assert late.pings == [] and sightings[STALE[0]].presence == 'recent'
    with pytest.raises(RouterError, match='读取时间已用完'):
        read(late, monkeypatch, deadline=time.monotonic())


def lease(ip, last_seen, status='bound', dynamic='true', **extra):
    return {'.id': '*1', 'address': ip, 'mac-address': '02:00:00:00:00:01', 'server': 'server1', 'status': status,
            'last-seen': last_seen, 'dynamic': dynamic, 'disabled': 'false', **extra}


def arp(ip, status, interface='lan4'):
    return {'.id': '*1', 'address': ip, 'mac-address': '02:00:00:00:00:02', 'interface': interface,
            'status': status, 'disabled': 'false', 'invalid': 'false', 'dynamic': 'true'}


SERVER = [{'.id': '*1', 'name': 'server1', 'interface': 'lan4', 'lease-time': '30m', 'disabled': 'false'}]


@pytest.mark.parametrize('rows, leases, server, expected, probed', [
    # A dynamic lease that missed its renewal (half of 30m + 5m grace) is gone, even with a stale ARP entry.
    ([arp('10.0.0.2', 'stale')], [lease('10.0.0.2', '25m10s')], SERVER, ('offline', 'lease'), False),
    # With a 1-day lease a 3h-old renewal proves nothing: confirm actively.
    ([arp('10.0.0.2', 'stale')], [lease('10.0.0.2', '3h')], [{**SERVER[0], 'lease-time': '1d'}], ('online', 'probe'), True),
    # A bound lease with no ARP entry is checked on the DHCP server's interface.
    ([], [lease('10.0.0.2', '3h')], [{**SERVER[0], 'lease-time': '1d'}], ('online', 'probe'), True),
    # A static lease nobody has used and no ARP entry: not on the network.
    ([], [lease('10.0.0.2', 'never', 'waiting', 'false')], SERVER, ('offline', 'lease'), False),
    # A fresh renewal wins over a failed ARP entry (the client changed interface or MAC).
    ([arp('10.0.0.2', 'failed')], [lease('10.0.0.2', '1m')], SERVER, ('online', 'lease'), False),
    # A static lease past its renewal on a host using a static IP: not evidence of absence.
    ([arp('10.0.0.2', 'stale')], [lease('10.0.0.2', '2h', 'waiting', 'false')], SERVER, ('online', 'probe'), True),
    # Permanent (static) ARP entries say nothing about liveness either.
    ([arp('10.0.0.2', 'permanent')], [], SERVER, ('online', 'probe'), True),
])
def test_lease_and_arp_rules(monkeypatch, rows, leases, server, expected, probed):
    fake = FakeRouterOS({'/ip/arp': rows, '/ip/dhcp-server/lease': leases, '/ip/dhcp-server': server,
                         '/ip/address': [{'address': '10.0.0.1/24', 'interface': 'lan4', 'dynamic': 'false'}]},
                        answers('10.0.0.2'))
    sighting = read(fake, monkeypatch).sightings['10.0.0.2']
    assert (sighting.presence, sighting.basis) == expected
    assert bool(fake.pings) is probed
    if probed:
        assert fake.pings[0]['interface'] == 'lan4'


def test_without_a_dhcp_server_static_private_interfaces_except_wan_are_the_lan(monkeypatch):
    addresses = [{'address': '192.168.88.1/24', 'interface': 'bridge', 'dynamic': 'false'},
                 {'address': '192.168.1.20/24', 'interface': 'ether1', 'dynamic': 'true'},     # DHCP client WAN
                 {'address': '10.9.0.2/30', 'interface': 'ether2', 'dynamic': 'false'},        # static WAN, in list WAN
                 {'address': '203.0.113.2/24', 'interface': 'ether3', 'dynamic': 'false'}]
    rows = [arp('192.168.88.10', 'reachable', 'bridge'), arp('192.168.1.1', 'reachable', 'ether1'),
            arp('10.9.0.1', 'reachable', 'ether2'), arp('203.0.113.1', 'reachable', 'ether3')]
    fake = FakeRouterOS({'/ip/arp': rows, '/ip/address': addresses, '/ip/dhcp-server': [],
                         '/ip/dhcp-server/lease': [],
                         '/interface/list/member': [{'list': 'WAN', 'interface': 'ether2'},
                                                    {'list': 'LAN', 'interface': 'bridge'}]})
    reading = read(fake, monkeypatch)
    assert list(reading.sightings) == ['192.168.88.10']
    assert reading.notes == ['未发现启用的 DHCP 服务，按带私有地址的接口（bridge）判断局域网']
    assert reading.own == {'192.168.88.1', '192.168.1.20', '10.9.0.2', '203.0.113.2'}
    fake = FakeRouterOS({'/ip/arp': rows, '/ip/address': addresses[1:], '/ip/dhcp-server': [],
                         '/ip/dhcp-server/lease': [], '/interface/list/member': [{'list': 'WAN', 'interface': 'ether2'}]})
    with pytest.raises(RouterError, match='无法确定局域网接口'):
        read(fake, monkeypatch)


def test_unreadable_lease_table_degrades_to_a_note_but_arp_is_required(monkeypatch):
    tables = {**TABLES, '/ip/dhcp-server/lease': (403, {})}
    reading = read(FakeRouterOS(tables, answers()), monkeypatch)
    assert reading.notes[0].startswith('无法读取 DHCP 租约：RouterOS 拒绝访问')
    assert reading.sightings['192.168.0.133'].sources == {'arp'}
    with pytest.raises(RouterError, match='认证失败'):
        read(FakeRouterOS({**TABLES, '/ip/arp': (401, {})}), monkeypatch)


@pytest.mark.parametrize('text, seconds', [('13m39s', 819), ('2d1h36m57s', 178617), ('15s', 15), ('1w', 604800),
                                           ('500ms', 0), ('00:30:00', 1800), ('never', None), ('', None),
                                           ('3x', None)])
def test_routeros_durations(text, seconds):
    assert ros_clients.duration(text) == seconds


def test_driver_ping_reads_the_captured_responses(monkeypatch):
    calls = []
    replies = iter([(200, ARP_ANSWER), (200, SILENT), (200, ICMP_ANSWER), NO_TEST_POLICY, NO_INTERFACE,
                    (400, {'detail': 'failure: resolve failed', 'error': 400, 'message': 'Bad Request'})])

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs['json'], kwargs['timeout']))
        return response(*next(replies))
    driver = RouterOSDriver('http://192.0.2.1', 'u', 'p')
    monkeypatch.setattr(driver.session, 'request', request)
    assert driver.ping('10.97.0.200', 'ether1') is True
    assert driver.ping('10.97.0.222', 'ether1') is False
    assert driver.ping('10.97.0.1', timeout=2) is True
    with pytest.raises(PingNotPermitted, match='test 权限'):
        driver.ping('10.97.0.1')
    with pytest.raises(RouterError, match='interface needs to be specified'):
        driver.ping('10.97.0.1', 'nosuch')
    with pytest.raises(RouterError, match='HTTP 400'):
        driver.ping('nope')
    assert calls[0] == ('POST', 'http://192.0.2.1/rest/ping', {'address': '10.97.0.200', 'count': '2',
                        'interval': '300ms', 'arp-ping': 'yes', 'interface': 'ether1'}, 4)
    assert calls[2][2:] == ({'address': '10.97.0.1', 'count': '2', 'interval': '300ms'}, 2)


# --- iKuai --------------------------------------------------------------------------------------------------

def terminal(ip, mac, **extra):
    """A captured 4.0.314 monitor_lanip row for a static-IP client (no hostname key), re-addressed."""
    row = copy.deepcopy(IKUAI_LAB['monitor_lanip']['results']['data'][0])
    assert 'hostname' not in row
    row.update(ip_addr=ip, mac=mac, **extra)
    return row


def lab_rows(func_name):
    return copy.deepcopy(IKUAI_LAB[func_name]['results']['data'])


class FakeIKuai:
    """``show`` lists the way 4.0.314 serves them: limit is "<offset>,<count>", total is the full count."""

    def __init__(self, terminals=(), leases=(), failures=None, lan=()):
        self.data = {'monitor_lanip': list(terminals), 'dhcp_lease': list(leases), 'lan': list(lan)}
        self.failures = failures or {}
        self.calls = []

    def call(self, func_name, param, action='show'):
        self.calls.append((func_name, action, dict(param)))
        if func_name in self.failures:
            raise RouterError(self.failures[func_name])
        start, size = (int(part) for part in param['limit'].split(','))
        rows = self.data[func_name]
        return {'data': rows[start:start + size], 'total': len(rows)}

    def close(self):
        pass


def test_ikuai_lab_capture_reads_terminals_leases_and_own_address():
    fake = FakeIKuai(lab_rows('monitor_lanip'), lab_rows('dhcp_lease'), lan=lab_rows('lan'))
    reading = ikuai_clients.read(fake)
    seen = {ip: sighting.public() for ip, sighting in reading.sightings.items()}
    assert seen == {
        # Static IP (also bound in dhcp_static): no hostname key in the terminal row.
        '192.168.77.3': {'mac': 'BC:24:11:98:C7:BD', 'hostname': '', 'sources': ['online'], 'presence': 'online',
                         'basis': 'terminal', 'last_seen': None, 'router_id': ''},
        # DHCP client: lease end_time - start_time = 7200, timeout 6901 left -> renewed 299 s ago.
        '192.168.77.100': {'mac': 'BC:24:11:71:F6:CC', 'hostname': 'cf-ik-client', 'sources': ['lease', 'online'],
                           'presence': 'online', 'basis': 'terminal', 'last_seen': 299, 'router_id': ''},
        '192.168.77.250': {'mac': '76:58:BD:A9:09:0F', 'hostname': '', 'sources': ['online'], 'presence': 'online',
                           'basis': 'terminal', 'last_seen': None, 'router_id': ''},
    }
    assert reading.own == {'192.168.77.1'}
    assert reading.notes == ['按 2 分钟内经过路由器的流量判断在线']
    assert [call[:2] for call in fake.calls] == [('monitor_lanip', 'show'), ('dhcp_lease', 'show'), ('lan', 'show')]
    assert all(call[2] == {'TYPE': 'data,total', 'limit': '0,1000'} for call in fake.calls)


def test_ikuai_lease_without_traffic_is_offline_with_its_renewal_age():
    reading = ikuai_clients.read(FakeIKuai([], lab_rows('dhcp_lease')))
    assert reading.sightings['192.168.77.100'].public() == {
        'mac': 'BC:24:11:71:F6:CC', 'hostname': 'cf-ik-client', 'sources': ['lease'], 'presence': 'offline',
        'basis': 'absent', 'last_seen': 299, 'router_id': ''}
    assert reading.own == set()


def test_ikuai_names_paging_and_odd_rows(monkeypatch):
    monkeypatch.setattr(ikuai_clients, 'PAGE', 2)
    fake = FakeIKuai(
        [terminal('192.168.9.10', 'aa:bb:cc:00:00:01', comment='客厅%20电视', hostname='tv'),
         terminal('192.168.9.11', 'aa:bb:cc:00:00:02', termname='NAS'),
         terminal('192.168.9.12', 'aa:bb:cc:00:00:03', hostname='iPhone'),
         terminal('fe80::1', 'aa:bb:cc:00:00:04'), {'ip_addr': 'garbage'}],
        [{**lab_rows('dhcp_lease')[0], 'ip_addr': '192.168.9.11', 'mac': 'aa:bb:cc:00:00:02', 'hostname': 'nas-box'},
         {**lab_rows('dhcp_lease')[0], 'ip_addr': '192.168.9.20', 'mac': 'AA-BB-CC-00-00-05', 'hostname': 'printer',
          'timeout': 'x'}])
    reading = ikuai_clients.read(fake)
    seen = {ip: (s.hostname, s.mac, sorted(s.sources), s.presence, s.last_seen) for ip, s in reading.sightings.items()}
    assert seen == {
        '192.168.9.10': ('客厅 电视', 'AA:BB:CC:00:00:01', ['online'], 'online', None),
        '192.168.9.11': ('NAS', 'AA:BB:CC:00:00:02', ['lease', 'online'], 'online', 299),
        '192.168.9.12': ('iPhone', 'AA:BB:CC:00:00:03', ['online'], 'online', None),
        '192.168.9.20': ('printer', 'AA:BB:CC:00:00:05', ['lease'], 'offline', None),
    }
    pages = [call for call in fake.calls if call[0] == 'monitor_lanip']
    assert [call[2] for call in pages] == [{'TYPE': 'data,total', 'limit': f'{start},2'} for start in (0, 2, 4)]
    assert all(call[1] == 'show' for call in fake.calls)


def test_ikuai_rows_stop_when_total_is_missing():
    """Sent without TYPE, 4.0.314 answers only ``data`` (monitor_lanip_noparam)."""
    class NoTotal(FakeIKuai):
        def call(self, func_name, param, action='show'):
            self.calls.append(func_name)
            return copy.deepcopy(IKUAI_LAB['monitor_lanip_noparam']['results'])
    fake = NoTotal()
    assert [row['ip_addr'] for row in ikuai_clients.rows(fake, 'monitor_lanip')] == [
        '192.168.77.3', '192.168.77.100', '192.168.77.250']
    assert fake.calls == ['monitor_lanip']


def test_ikuai_lease_and_lan_failures_are_notes_but_the_terminal_monitor_is_required():
    reading = ikuai_clients.read(FakeIKuai([terminal('192.168.9.10', 'aa:bb:cc:00:00:01')],
                                           failures={'dhcp_lease': 'iKuai 账号没有「IPv4」（dhcp_lease）的访问权限（错误码 1001）',
                                                     'lan': 'x'}))
    assert reading.notes[1] == '无法读取 DHCP 租约：iKuai 账号没有「IPv4」（dhcp_lease）的访问权限（错误码 1001）'
    assert list(reading.sightings) == ['192.168.9.10'] and reading.own == set()
    with pytest.raises(RouterError):
        ikuai_clients.read(FakeIKuai(failures={'monitor_lanip': 'x'}))


class LabSession:
    """Replays captured 4.0.314 replies: login first, then one reply per call."""

    def __init__(self, login, *replies):
        self.replies = [login, *replies]
        self.posts = []
        self.trust_env = True
        self.verify = True

    def post(self, url, **_):
        self.posts.append(url.rsplit('/', 1)[-1])
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(self.replies.pop(0)).encode()
        return response

    def close(self):
        pass


def lab_driver(*replies, login=None):
    driver = IKuaiDriver('https://192.0.2.2', 'cfro', 'secret')
    driver.session = LabSession(login or IKUAI_LAB['login_ok'], *replies)
    return driver


def test_ikuai_wrong_password_is_a_login_failure_despite_result_10000():
    driver = lab_driver(login=IKUAI_LAB['login_wrong_password'])
    with pytest.raises(RouterError, match=r'^iKuai 登录失败：账号或密码错误（错误码 1005）$'):
        driver.call('monitor_lanip', {'TYPE': 'data,total'})
    assert driver.session.posts == ['login']


@pytest.mark.parametrize('reply, expected', [
    ('no_read_permission', 'iKuai 账号没有「终端监控IPv4」（monitor_lanip）的访问权限（错误码 1001）。'
                           '请在 iKuai 的用户组中授予读取权限，或改用管理员账号。'),
    ('no_read_permission_with_result', 'iKuai 账号没有「IPv4」（monitor_lanip）的访问权限（错误码 1001）。'
                                       '请在 iKuai 的用户组中授予读取权限，或改用管理员账号。'),
    ('restricted_no_such_func', 'iKuai 账号没有 monitor_lanip 的访问权限（错误码 1001）。'
                                '请在 iKuai 的用户组中授予读取权限，或改用管理员账号。'),
    ('no_write_permission', 'iKuai 账号没有「DHCP服务端」（monitor_lanip）的执行权限（错误码 1002）。'
                            '请在 iKuai 的用户组中授予修改权限，或改用管理员账号。'),
    ('no_such_func', 'iKuai 不提供 monitor_lanip 接口（错误码 1003：Access denied）。当前固件可能不支持这项功能，请确认 iKuai 版本。'),
    ('invalid_action', 'iKuai 调用 monitor_lanip 失败：invalid action（错误码 3001）'),
])
def test_ikuai_call_errors_use_the_codes_seen_on_4_0_314(reply, expected):
    """Many 4.x errors carry "Result": 10000 too; none of them may be reported as 3.x."""
    with pytest.raises(RouterError) as caught:
        lab_driver(IKUAI_LAB['errors'][reply]).call('monitor_lanip', {'TYPE': 'data,total'})
    assert str(caught.value) == expected


def test_ikuai_expired_session_logs_in_again_once():
    driver = lab_driver(IKUAI_LAB['errors']['session_expired'], IKUAI_LAB['login_ok'], IKUAI_LAB['dhcp_lease'])
    assert driver.call('dhcp_lease', {'TYPE': 'data,total'})['total'] == 1
    assert driver.session.posts == ['login', 'call', 'login', 'call']
    expired = IKUAI_LAB['errors']['session_expired']
    driver = lab_driver(expired, IKUAI_LAB['login_ok'], expired)
    with pytest.raises(RouterError, match='错误码 1008'):
        driver.call('dhcp_lease', {'TYPE': 'data,total'})


def test_ikuai_3x_replies_are_still_recognized():
    driver = lab_driver({'Result': 30000, 'ErrMsg': 'Success', 'Data': {}}, login={'Result': 10000, 'ErrMsg': 'Success'})
    with pytest.raises(RouterError, match='iKuai 3.x'):
        driver.call('sysstat', {'TYPE': 'verinfo'})
    with pytest.raises(RouterError, match='登录失败：bad'):
        lab_driver(login={'Result': 10001, 'ErrMsg': 'bad'}).call('sysstat', {})


# --- Profile endpoint ---------------------------------------------------------------------------------------

URL = '/api/client-devices/discovery?profile=office'
ROS = {'id': 'ros1', 'name': '主路由', 'type': 'routeros', 'endpoint': 'https://192.168.0.1', 'username': 'cf',
       'password': 'router-secret', 'ros_policy_rule_sets': ['rs']}
IKUAI = {'id': 'ik1', 'name': '爱快', 'type': 'ikuai', 'endpoint': 'http://192.168.9.1', 'username': 'admin',
         'password': 'router-secret'}


def setup_profile(repo, routers, rows):
    def update(config):
        config['oxidns'] = apply_policies(config['oxidns'], config, [row for row in rows if row['mode']])
        config['device_notes'] = {row['ip']: row['name'] for row in rows if row['name']}
        config['routers'] = {'items': copy.deepcopy(routers)}
    repo.update_profile_transaction('office', update)


ROWS = [{'ip': '192.168.0.133', 'name': '摄像头', 'mode': 'direct'},
        {'ip': '192.168.0.5', 'name': '旧电脑', 'mode': 'smart'},
        {'ip': '192.168.0.199', 'name': '已拆除', 'mode': 'smart'},
        {'ip': '192.168.0.0/24', 'name': '家里', 'mode': 'smart'},
        {'ip': '192.168.0.96/28', 'name': '实验', 'mode': 'direct'},
        {'ip': '2001:db8::5', 'name': 'v6', 'mode': 'smart'}]


def test_profile_without_ros_or_ikuai_is_unsupported(oxidns_app):
    app, repo = oxidns_app
    setup_profile(repo, [], ROWS)
    result = app.test_client().get(URL)
    body = result.get_json()
    assert result.status_code == 200 and result.headers['Cache-Control'] == 'no-store'
    assert body['state'] == 'unsupported' and body['message'] == '需要在当前配置接入 RouterOS 或 iKuai 才能发现设备'
    assert body['sources'] == [] and body['discovered'] == []
    assert {row['ip']: row['presence'] for row in body['devices']} == {
        '192.168.0.133': 'unknown', '192.168.0.5': 'unknown', '192.168.0.199': 'unknown',
        '192.168.0.0/24': 'network', '192.168.0.96/28': 'network', '2001:db8::5': 'unknown'}


def test_discovery_joins_both_router_types_with_saved_rows(oxidns_app, monkeypatch):
    app, repo = oxidns_app
    setup_profile(repo, [ROS, IKUAI], ROWS)
    get_agent_manager().register_agent({'name': 'dns', 'host': '192.168.0.8', 'port': 18000,
                                        'service_type': 'oxidns', 'profile_id': 'office'})
    get_agent_manager().register_agent({'name': 'other', 'host': '192.168.0.2', 'port': 18000,
                                        'service_type': 'mihomo', 'profile_id': 'default'})
    ros = FakeRouterOS(ping=answers('192.168.0.5'))
    ikuai = FakeIKuai([terminal('192.168.9.10', 'aa:bb:cc:00:00:01', comment='电视'),
                       terminal('192.168.9.1', 'aa:bb:cc:00:00:09')])
    opened = []

    def open_driver(router):
        opened.append((router['id'], router['password']))
        return ros.driver(monkeypatch) if router['type'] == 'routeros' else ikuai
    monkeypatch.setattr(client_discovery, 'open_driver', open_driver)
    body = app.test_client().get(URL).get_json()
    assert sorted(opened) == [('ik1', 'router-secret'), ('ros1', 'router-secret')]
    assert 'router-secret' not in json.dumps(body)
    assert body['success'] and body['profile_id'] == 'office' and body['state'] == 'ok' and body['message'] == ''
    assert body['sources'] == [
        {'id': 'ros1', 'name': '主路由', 'type': 'routeros', 'ok': True, 'error': '', 'notes': [], 'policy_routing': True},
        {'id': 'ik1', 'name': '爱快', 'type': 'ikuai', 'ok': True, 'error': '', 'notes': ['按 2 分钟内经过路由器的流量判断在线']}]
    devices = {row['ip']: row for row in body['devices']}
    assert devices['192.168.0.133'] == {**ROWS[0], 'mac': 'E0:BA:AD:94:F2:48', 'hostname': 'C6c_EZVIZ',
                                        'sources': ['arp', 'lease'], 'presence': 'online', 'basis': 'lease',
                                        'last_seen': 523, 'router_id': 'ros1'}
    # Saved addresses are confirmed first.
    assert ros.pings[0]['address'] == '192.168.0.5'
    assert (devices['192.168.0.5']['presence'], devices['192.168.0.5']['basis']) == ('online', 'probe')
    assert (devices['192.168.0.199']['presence'], devices['192.168.0.199']['basis']) == ('offline', 'absent')
    assert devices['192.168.0.0/24']['presence'] == 'network' and devices['2001:db8::5']['presence'] == 'unknown'
    discovered = {row['ip']: row for row in body['discovered']}
    # Saved rows, the routers' own addresses, the management endpoint and this profile's Agent hosts are excluded.
    for ip in ('192.168.0.133', '192.168.0.5', '192.168.0.1', '192.168.9.1', '192.168.0.8'):
        assert ip not in discovered
    assert '192.168.0.2' in discovered          # an Agent of another profile is just a device here
    assert not [ip for ip in discovered if ip.startswith('192.168.1.')]
    assert discovered['192.168.9.10'] == {'ip': '192.168.9.10', 'mac': 'AA:BB:CC:00:00:01', 'hostname': '电视',
                                          'sources': ['online'], 'presence': 'online', 'basis': 'terminal',
                                          'last_seen': None, 'router_id': 'ik1', 'network': None}
    # The most specific saved network covers an address.
    assert discovered['192.168.0.100']['network'] == {'ip': '192.168.0.96/28', 'name': '实验', 'mode': 'direct'}
    assert discovered['192.168.0.147']['network'] == {'ip': '192.168.0.0/24', 'name': '家里', 'mode': 'smart'}
    assert discovered['192.168.0.96']['presence'] == 'offline'
    assert list(discovered) == sorted(discovered, key=lambda ip: tuple(int(part) for part in ip.split('.')))


def test_a_failed_router_makes_missing_saved_rows_unknown(oxidns_app, monkeypatch):
    app, repo = oxidns_app
    setup_profile(repo, [ROS, IKUAI], ROWS)
    ikuai = FakeIKuai([terminal('192.168.9.10', 'aa:bb:cc:00:00:01')])
    monkeypatch.setattr(client_discovery, 'open_driver', lambda router: (
        FakeRouterOS({'/ip/arp': (401, {})}).driver(monkeypatch) if router['type'] == 'routeros' else ikuai))
    body = app.test_client().get(URL).get_json()
    assert body['state'] == 'partial' and body['message'] == '部分路由器读取失败，未出现的设备显示为未知'
    assert body['sources'][0]['ok'] is False and '认证失败' in body['sources'][0]['error']
    assert {row['ip']: row['presence'] for row in body['devices']}['192.168.0.199'] == 'unknown'
    assert [row['ip'] for row in body['discovered']] == ['192.168.9.10']


def test_a_slow_router_times_out_without_holding_the_others(oxidns_app, monkeypatch):
    app, repo = oxidns_app
    setup_profile(repo, [ROS, IKUAI], [])
    release = threading.Event()

    class Stuck(FakeIKuai):
        def call(self, func_name, param, action='show'):
            release.wait(5)
            raise RouterError('closed')
    monkeypatch.setattr(client_discovery, 'open_driver', lambda router: (
        Stuck() if router['type'] == 'ikuai' else FakeRouterOS(ping=answers()).driver(monkeypatch)))
    # Readers stop 2 s before the router timeout; RouterOS needs at least 1 s left to start a read.
    monkeypatch.setattr(client_discovery, 'ROUTER_TIMEOUT', 4)
    started = time.monotonic()
    body = app.test_client().get(URL).get_json()
    release.set()
    assert time.monotonic() - started < 5.5
    assert body['state'] == 'partial'
    assert body['sources'][1] == {'id': 'ik1', 'name': '爱快', 'type': 'ikuai', 'ok': False,
                                  'error': '读取超时（4 秒），请检查路由器负载和网络', 'notes': []}
    assert body['sources'][0]['ok'] and body['discovered']


def test_an_unexpected_reader_bug_is_contained(oxidns_app, monkeypatch):
    app, repo = oxidns_app
    setup_profile(repo, [IKUAI], [{'ip': '192.168.9.10', 'name': '电视', 'mode': ''}])

    class Broken(FakeIKuai):
        def call(self, func_name, param, action='show'):
            return {'total': 'x', 'data': [{'ip_addr': '192.168.9.10', 'mac': {'odd': 1}}, 'junk']}
    monkeypatch.setattr(client_discovery, 'open_driver', lambda router: Broken())
    monkeypatch.setattr(ikuai_clients, 'parse_terminal', lambda row: 1 / 0)
    body = app.test_client().get(URL).get_json()
    assert body['state'] == 'failed' and body['message'] == '路由器读取失败，暂时无法发现设备'
    assert body['sources'][0]['error'] == '读取失败：ZeroDivisionError'
    assert body['devices'][0]['presence'] == 'unknown'


def test_the_agent_entry_point_reads_that_agents_profile(oxidns_app, monkeypatch):
    app, repo = oxidns_app
    setup_profile(repo, [], [])
    agent_id = get_agent_manager().register_agent({'name': 'dns', 'host': '127.0.0.1', 'port': 18000,
                                                   'service_type': 'oxidns', 'profile_id': 'office'})['id']
    client = app.test_client()
    assert client.get(f'/api/client-devices/discovery?agent={agent_id}').get_json()['profile_id'] == 'office'
    missing = client.get('/api/client-devices/discovery?agent=nope')
    assert missing.status_code == 404 and 'Agent' in missing.get_json()['message']


# --- Review regressions -------------------------------------------------------------------------------------

def ros_only(app, repo, monkeypatch, rows, tables=None, ping=None):
    setup_profile(repo, [ROS], rows)
    fake = FakeRouterOS(tables, ping or answers())
    monkeypatch.setattr(client_discovery, 'open_driver', lambda router: fake.driver(monkeypatch))
    body = app.test_client().get(URL).get_json()
    return body, {row['ip']: row for row in body['devices']}


def test_a_saved_router_address_is_online_not_absent(oxidns_app, monkeypatch):
    """Clients forwarding DNS through the router make OxiDNS see only the router's address."""
    app, repo = oxidns_app
    body, devices = ros_only(app, repo, monkeypatch, [{'ip': '192.168.0.1', 'name': '主路由', 'mode': 'smart'},
                                                      {'ip': '192.168.1.2', 'name': 'WAN', 'mode': ''}])
    assert body['state'] == 'ok'
    for ip in ('192.168.0.1', '192.168.1.2'):
        assert (devices[ip]['presence'], devices[ip]['basis'], devices[ip]['router_id']) == ('online', 'router', 'ros1')


def test_saved_addresses_outside_the_scanned_lan_are_unknown(oxidns_app, monkeypatch):
    app, repo = oxidns_app
    tables = copy.deepcopy(TABLES)
    # DHCP only on lan4; vlan20 is a static-only server VLAN the reader does not scan.
    tables['/ip/address'] = tables['/ip/address'] + [
        {'address': '192.168.20.1/24', 'interface': 'vlan20', 'dynamic': 'false', 'disabled': 'false'}]
    tables['/ip/arp'] = tables['/ip/arp'] + [arp('192.168.20.5', 'reachable', 'vlan20')]
    rows = [{'ip': '10.8.0.2', 'name': 'WireGuard 手机', 'mode': 'smart'},
            {'ip': '192.168.20.5', 'name': 'NAS', 'mode': 'direct'},
            {'ip': '192.168.0.199', 'name': '已拆除', 'mode': 'smart'}]
    _, devices = ros_only(app, repo, monkeypatch, rows, tables)
    assert (devices['10.8.0.2']['presence'], devices['10.8.0.2']['basis']) == ('unknown', 'outside')
    assert (devices['192.168.20.5']['presence'], devices['192.168.20.5']['basis']) == ('unknown', 'outside')
    # Inside the scanned LAN, absence is still evidence.
    assert (devices['192.168.0.199']['presence'], devices['192.168.0.199']['basis']) == ('offline', 'absent')


def test_ikuai_absence_counts_only_inside_its_lan_addresses(oxidns_app, monkeypatch):
    app, repo = oxidns_app
    rows = [{'ip': '192.168.77.50', 'name': '打印机', 'mode': ''}, {'ip': '10.8.0.2', 'name': 'VPN', 'mode': ''}]
    setup_profile(repo, [IKUAI], rows)
    fake = FakeIKuai(lab_rows('monitor_lanip'), lab_rows('dhcp_lease'), lan=lab_rows('lan'))
    monkeypatch.setattr(client_discovery, 'open_driver', lambda router: fake)
    devices = {row['ip']: row for row in app.test_client().get(URL).get_json()['devices']}
    assert (devices['192.168.77.50']['presence'], devices['192.168.77.50']['basis']) == ('offline', 'absent')
    assert devices['10.8.0.2']['presence'] == 'unknown'
    # Without the router's LAN addresses nothing proves absence.
    fake = FakeIKuai(lab_rows('monitor_lanip'), lab_rows('dhcp_lease'), failures={'lan': 'x'})
    body = app.test_client().get(URL).get_json()
    assert {row['ip']: row['presence'] for row in body['devices']} == {'192.168.77.50': 'unknown', '10.8.0.2': 'unknown'}
    assert any('局域网地址' in note for note in body['sources'][0]['notes'])


def test_the_covering_network_is_the_policy_that_applies(oxidns_app, monkeypatch):
    """A name-only network row (mode '') sets no policy; OxiDNS and smart_sources use the most specific policy row."""
    app, repo = oxidns_app
    rows = [{'ip': '192.168.0.0/24', 'name': '家里', 'mode': 'smart'},
            {'ip': '192.168.0.96/28', 'name': '实验', 'mode': ''},
            {'ip': '192.168.0.128/25', 'name': '访客', 'mode': 'direct'}]
    body, _ = ros_only(app, repo, monkeypatch, rows)
    discovered = {row['ip']: row for row in body['discovered']}
    assert discovered['192.168.0.100']['network'] == {'ip': '192.168.0.0/24', 'name': '家里', 'mode': 'smart'}
    assert discovered['192.168.0.147']['network'] == {'ip': '192.168.0.128/25', 'name': '访客', 'mode': 'direct'}


def test_an_unreadable_lease_table_makes_absent_saved_rows_unknown(oxidns_app, monkeypatch):
    app, repo = oxidns_app
    tables = {**TABLES, '/ip/dhcp-server/lease': (403, {})}
    body, devices = ros_only(app, repo, monkeypatch, [{'ip': '192.168.0.199', 'name': '打印机', 'mode': ''}], tables)
    assert body['sources'][0]['ok'] is True
    assert devices['192.168.0.199']['presence'] == 'unknown'
    assert any(note.startswith('无法读取 DHCP 租约') for note in body['sources'][0]['notes'])


class SlowPings:
    """A RouterOS driver whose pings use up their whole timeout."""

    def __init__(self, answer, arp_delay=0.0):
        self.answer = answer
        self.arp_delay = arp_delay
        self.pings = []
        self.arp_reads = []

    def ping(self, address, interface='', timeout=4):
        self.pings.append((address, interface, timeout))
        time.sleep(timeout)
        if self.answer is None:
            raise RouterError('RouterOS ping 未完成：ReadTimeout')
        return self.answer

    def _list(self, path, timeout=None):
        self.arp_reads.append(timeout)
        time.sleep(min(self.arp_delay, timeout or self.arp_delay))
        return []


def test_the_active_check_never_runs_past_its_deadline():
    sightings = {'10.0.0.2': ros_clients.Sighting(ip='10.0.0.2', presence='recent', basis='stale')}
    # A timed-out ARP ping must not spend the same budget again on ICMP.
    driver = SlowPings(None)
    started = time.monotonic()
    notes = ros_clients.confirm(driver, sightings, [('10.0.0.2', 'lan4')], {'lan4'}, deadline=started + 1.5)
    assert time.monotonic() - started < 1.8
    assert [item[1] for item in driver.pings] == ['lan4']
    assert sightings['10.0.0.2'].presence == 'recent' and notes == ['1 台设备未能及时确认，显示为近期见过']
    # A silent ICMP ping that ends near the deadline skips the ARP re-read instead of overrunning.
    driver = SlowPings(False, arp_delay=3)
    started = time.monotonic()
    ros_clients.confirm(driver, sightings, [('10.0.0.2', '')], {'lan4'}, deadline=started + 1.5)
    assert time.monotonic() - started < 1.8
    assert sightings['10.0.0.2'].presence == 'recent'
    # With time left the re-read is bounded by what remains.
    driver = SlowPings(False, arp_delay=10)
    driver.ping = lambda address, interface='', timeout=4: False
    started = time.monotonic()
    ros_clients.confirm(driver, sightings, [('10.0.0.2', '')], {'lan4'}, deadline=started + 2)
    assert time.monotonic() - started < 2.3
    assert driver.arp_reads and 0 < driver.arp_reads[0] <= 2


class SlowRouterOS(SlowPings):
    """Every table read takes 0.5 s and every ping times out."""

    def __init__(self):
        super().__init__(None)
        self.reads = []

    def _list(self, path, timeout=None):
        self.reads.append((path, timeout))
        time.sleep(min(0.5, timeout))
        return copy.deepcopy(TABLES[path])


def test_a_slow_router_still_returns_its_tables_within_the_deadline():
    driver = SlowRouterOS()
    started = time.monotonic()
    reading = ros_clients.read(driver, deadline=started + 3)
    assert time.monotonic() - started < 3.3
    assert [path for path, _ in driver.reads] == ['/ip/arp', '/ip/address', '/ip/dhcp-server', '/ip/dhcp-server/lease']
    assert all(timeout <= 3 for _, timeout in driver.reads)
    assert reading.complete and len(reading.sightings) == 47
    assert {reading.sightings[ip].presence for ip in STALE} == {'recent'}
    assert all(interface == 'lan4' for _, interface, _ in driver.pings)     # no ICMP retry after a timeout
