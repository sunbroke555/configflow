#!/usr/bin/env python3
"""Generate/run a reproducible loopback smoke test against a real OxiDNS binary.

From the repository: python scripts/verify_oxidns_native.py --prepare /tmp/oxi-test
On a host with PyYAML and OxiDNS: python verify_oxidns_native.py --run /tmp/oxi-test --binary /opt/oxidns/oxidns
Per-rule DNS actions: add --rule-dns SCENARIO to --prepare; --run detects it.
Only generated fixtures are used. No production config, services or RouterOS calls.
"""
import argparse
import collections
import json
import os
from pathlib import Path
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import yaml


def prepare(root, complete=False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from backend.common.oxidns_settings import get_default_oxidns_settings
    from backend.converters.oxidns import generate_oxidns_config, get_oxidns_custom_files
    settings = get_default_oxidns_settings()
    settings.update(prefer_ipv4=False, cache_lazy_ttl=8)
    settings['api'].update(enabled=True, username='smoke', password='test-only')
    settings['query_recorder']['enabled'] = True
    settings['dynamic_domain_set']['enabled'] = True
    settings['dynamic_domain_set']['args']['bootstrap_rules'] = ['full:dynamic.test']
    settings['adguard'].update(enabled=True, content='||ads.test^')
    rules = []
    for identity, kind, value, route in (
        ('direct', 'DOMAIN', 'direct.test', 'direct'),
        ('proxy', 'DOMAIN', 'proxy.test', 'proxy'),
        ('overlap-first', 'DOMAIN', 'overlap.test', 'proxy'),
        ('overlap-last', 'DOMAIN', 'overlap.test', 'direct'),
        ('direct-ip', 'IP-CIDR', '10.0.0.0/8', 'direct'),
        ('proxy-ip', 'IP-CIDR', '203.0.113.0/24', 'proxy'),
    ):
        rules.append({'id': identity, 'itemType': 'rule', 'rule_type': kind, 'value': value})
        settings[route + '_rules'].append(identity)
    config = {'oxidns': settings, 'rule_configs': rules}
    root.mkdir(parents=True, exist_ok=True)
    generated = generate_oxidns_config(config)
    if complete:
        certs = root / 'certs'
        certs.mkdir(exist_ok=True)
        (certs / 'test.cnf').write_text('[req]\ndistinguished_name=dn\nx509_extensions=v3_req\nprompt=no\n[dn]\nCN=localhost\n[v3_req]\nsubjectAltName=DNS:localhost,IP:127.0.0.1\nbasicConstraints=critical,CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n')
        subprocess.run(['openssl', 'req', '-config', str(certs / 'test.cnf'), '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(certs / 'test.key'), '-out', str(certs / 'test.pem'), '-subj', '/CN=localhost', '-days', '1'], check=True, capture_output=True)
        document = yaml.safe_load(generated)
        document['network'] = {'outbound': {'default': 'direct', 'profiles': {'direct': {'resolver': 'system', 'proxy': 'none'}}}}
        document['api']['http']['cors'] = {'allowed_origins': ['http://localhost']}
        document['plugins'].extend([
            {'tag': 'test_dot', 'type': 'tcp_server', 'args': {'listen': '127.0.0.1:18530', 'entry': 'sequence_main', 'cert': 'certs/test.pem', 'key': 'certs/test.key', 'idle_timeout': 10}},
            {'tag': 'test_doh', 'type': 'http_server', 'args': {'listen': '127.0.0.1:18531', 'entries': [{'path': '/dns-query', 'exec': 'sequence_main', 'json_api': True}], 'cert': 'certs/test.pem', 'key': 'certs/test.key', 'enable_http3': True, 'idle_timeout': 30}},
            {'tag': 'test_doq', 'type': 'quic_server', 'args': {'listen': '127.0.0.1:18532', 'entry': 'sequence_main', 'cert': 'certs/test.pem', 'key': 'certs/test.key', 'idle_timeout': 30}},
        ])
        # Include order, binary attachments and full-document preservation pass through the actual converter.
        document['include'] = ['plugins/matchers.yaml']
        settings.update(config_mode='native', native_config=yaml.safe_dump(document, sort_keys=False), additional_files=[
            {'path': 'certs/test.pem', 'encoding': 'text', 'content': (certs / 'test.pem').read_text()},
            {'path': 'certs/test.key', 'encoding': 'text', 'content': (certs / 'test.key').read_text()},
            {'path': 'plugins/matchers.yaml', 'encoding': 'text', 'content': 'plugins:\n  - tag: test_qtype\n    type: qtype\n    args: [1, 28]\n'},
            {'path': 'rules/binary-fixture.dat', 'encoding': 'base64', 'content': 'AP8='},
        ])
        generated = generate_oxidns_config(config)
        actual = yaml.safe_load(generated)
        assert actual['network'] == document['network']
        assert actual['api'] == document['api']
        assert actual['plugins'][0]['tag'] == 'test_qtype'
        assert not actual.get('include')
    (root / 'config.yaml').write_text(generated)
    for item in get_oxidns_custom_files(config):
        destination = root / item['path']
        destination.parent.mkdir(parents=True, exist_ok=True)
        content = item['content']
        if isinstance(content, bytes):
            destination.write_bytes(content)
        elif item.get('encoding') == 'base64':
            import base64
            destination.write_bytes(base64.b64decode(content))
        else:
            destination.write_text(content)
    print(json.dumps({'prepared': str(root)}))


def read_name(data, offset):
    labels = []
    while data[offset]:
        size = data[offset]
        labels.append(data[offset + 1:offset + 1 + size].decode())
        offset += size + 1
    return '.'.join(labels), offset + 1


class MockDNS(socketserver.ThreadingUDPServer):
    daemon_threads = True

    def __init__(self, default_ip):
        self.default_ip = default_ip
        self.answers = {}
        self.counts = collections.Counter()
        self.lock = threading.Lock()
        super().__init__(('127.0.0.1', 0), MockHandler)
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    def set(self, name, ip, ttl=2):
        with self.lock:
            self.answers[name] = (ip, ttl)

    def count(self, name):
        with self.lock:
            return self.counts[name]

    def close(self):
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=2)


class MockHandler(socketserver.BaseRequestHandler):
    def handle(self):
        data, sock = self.request
        name, end = read_name(data, 12)
        qtype, qclass = struct.unpack('!HH', data[end:end + 4])
        with self.server.lock:
            self.server.counts[name] += 1
            ip, ttl = self.server.answers.get(name, (self.server.default_ip, 60))
        answer = b''
        if qtype == 1:
            answer = b'\xc0\x0c' + struct.pack('!HHIH', 1, 1, ttl, 4) + socket.inet_aton(ip)
        response = data[:2] + struct.pack('!HHHHH', 0x8180, 1, int(bool(answer)), 0, 0) + data[12:end + 4] + answer
        sock.sendto(response, self.client_address)


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def query(port, name, tcp=False, qtype=1, timeout=3):
    wire_name = b''.join(bytes([len(label)]) + label.encode() for label in name.split('.')) + b'\0'
    request = struct.pack('!HHHHHH', 0x2345, 0x0100, 1, 0, 0, 0) + wire_name + struct.pack('!HH', qtype, 1)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM if tcp else socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        if tcp:
            sock.connect(('127.0.0.1', port))
            sock.sendall(struct.pack('!H', len(request)) + request)
            size = struct.unpack('!H', sock.recv(2))[0]
            reply = b''
            while len(reply) < size:
                block = sock.recv(size - len(reply))
                if not block:
                    raise AssertionError('truncated TCP response')
                reply += block
        else:
            sock.sendto(request, ('127.0.0.1', port))
            reply = sock.recv(4096)
    header = struct.unpack('!HHHHHH', reply[:12])
    assert header[0] == 0x2345
    _, offset = read_name(reply, 12)
    offset += 4
    ips, ttls = [], []
    for _ in range(header[3]):
        if reply[offset] & 0xc0 == 0xc0:
            offset += 2
        else:
            _, offset = read_name(reply, offset)
        typ, cls, ttl, size = struct.unpack('!HHIH', reply[offset:offset + 10])
        offset += 10
        if typ in (1, 28):
            ips.append(socket.inet_ntop(socket.AF_INET if typ == 1 else socket.AF_INET6, reply[offset:offset + size]))
            ttls.append(ttl)
        offset += size
    return {'rcode': header[1] & 15, 'ips': ips, 'ttls': ttls, 'answers': header[3], 'authority': header[4]}


def run(root, binary):
    import base64
    config = yaml.safe_load((root / 'config.yaml').read_text())
    dns_port, api_port = free_port(), free_port()
    encrypted_ports = {name: free_port() for name in ('test_dot', 'test_doh', 'test_doq')}
    assert dns_port not in (53, 9199) and api_port not in (53, 9199)
    local, remote = MockDNS('10.1.2.3'), MockDNS('198.18.0.1')
    local.set('probe.test', '10.1.2.3')
    local.set('direct.test', '10.2.3.4')
    config['api']['http']['listen'] = f'127.0.0.1:{api_port}'
    for plugin in config['plugins']:
        if plugin['type'] == 'forward':
            mock = remote if plugin['tag'] == 'forward_remote' else local
            plugin['args']['upstreams'] = [{'addr': f'udp://127.0.0.1:{mock.server_address[1]}'}]
        elif plugin['tag'] in encrypted_ports:
            plugin['args']['listen'] = f"127.0.0.1:{encrypted_ports[plugin['tag']]}"
        elif plugin['type'] in ('udp_server', 'tcp_server'):
            plugin['args']['listen'] = f'127.0.0.1:{dns_port}'
    (root / 'state').mkdir(exist_ok=True)
    runtime = root / 'runtime.yaml'
    runtime.write_text(yaml.safe_dump(config, sort_keys=False))
    process = None
    results = {}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    api_url = f'http://127.0.0.1:{api_port}/api/readyz'
    try:
        subprocess.run([binary, 'check', '-c', str(runtime), '-d', str(root)], check=True, capture_output=True, timeout=30)
        with (root / 'runtime.log').open('w') as log:
            process = subprocess.Popen([binary, 'start', '-c', str(runtime), '-d', str(root)], stdout=log, stderr=subprocess.STDOUT)
            auth = 'Basic ' + base64.b64encode(b'smoke:test-only').decode()
            for _ in range(100):
                assert process.poll() is None, (root / 'runtime.log').read_text()[-3000:]
                try:
                    with opener.open(urllib.request.Request(api_url, headers={'Authorization': auth}), timeout=.3) as response:
                        assert response.status == 200
                    break
                except (OSError, urllib.error.URLError):
                    time.sleep(.05)
            else:
                raise AssertionError('temporary OxiDNS API did not become ready')
            try:
                opener.open(api_url, timeout=1)
                raise AssertionError('Basic authentication was not enforced')
            except urllib.error.HTTPError as error:
                assert error.code == 401
            results['api_basic_auth'] = True
            if any(plugin['tag'] == 'test_doh' for plugin in config['plugins']):
                import ssl
                encrypted_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
                with encrypted_opener.open(f"https://127.0.0.1:{encrypted_ports['test_doh']}/dns-query?name=direct.test&type=A", timeout=3) as response:
                    answer = json.load(response)
                    assert answer['Status'] == 0 and answer['Answer'][0]['data'] == '10.2.3.4', answer
                    assert 'h3=' in response.headers.get('Alt-Svc', '')
                results['doh_json_api_and_http3_advertisement'] = True
                for tag, scheme in [('test_dot', 'tls'), ('test_doq', 'quic'), ('test_doh', 'https'), ('test_doh', 'h3')]:
                    address = f"{scheme}://127.0.0.1:{encrypted_ports[tag]}" + ('/dns-query' if scheme in ('https', 'h3') else '')
                    probe = subprocess.run([binary, 'probe', 'upstream', address, '--insecure-skip-verify', '--qname', 'proxy.test', '--serial-samples', '3', '--pipeline-concurrency', '4', '--pipeline-rounds', '2', '--json'], check=True, capture_output=True, text=True, timeout=30)
                    report = json.loads(probe.stdout)
                    assert report['serial']['success_count'] == report['serial']['total_queries'] == 3, report
                    if report['pipeline']['applicable']:
                        assert report['pipeline']['success_count'] == report['pipeline']['total_queries'], report
                    results['test_doh3' if scheme == 'h3' else tag] = {'serial_queries': report['serial']['success_count'], 'concurrent_queries': report['pipeline']['success_count']}
                assert (root / 'rules/binary-fixture.dat').read_bytes() == b'\x00\xff'
                results['native_globals_include_and_binary_attachment'] = True
            for tcp in (False, True):
                for _ in range(25):
                    for name, ip in (('direct.test', '10.2.3.4'), ('proxy.test', '198.18.0.1'),
                                     ('overlap.test', '198.18.0.1'), ('dynamic.test', '10.1.2.3')):
                        reply = query(dns_port, name, tcp)
                        assert reply['ips'] == [ip], (name, tcp, reply)
            results['udp_tcp_routing_queries'] = 200
            assert local.count('direct.test') == 1
            results['direct_cache_hit'] = True
            for _ in range(20):
                assert query(dns_port, 'probe.test')['ips'] == ['198.18.0.1']
            assert local.count('probe.test') == 0
            local.set('probe.test', '203.0.113.10')
            local.set('direct.test', '10.2.3.5')
            time.sleep(2.2)
            assert query(dns_port, 'probe.test')['ips'] == ['198.18.0.1']
            assert local.count('probe.test') == 0
            for _ in range(10):
                assert query(dns_port, 'probe.test')['ips'] == ['198.18.0.1']
            assert local.count('probe.test') == 0
            results['unmatched_domains_ignore_local_ip_answers'] = True
            assert query(dns_port, 'direct.test')['ips'] == ['10.2.3.4']
            for _ in range(50):
                time.sleep(.05)
                if query(dns_port, 'direct.test')['ips'] == ['10.2.3.5']:
                    break
            else:
                raise AssertionError('lazy refresh did not install fresh response')
            results['lazy_stale_and_background_refresh'] = True
            assert query(dns_port, 'ads.test')['rcode'] == 3
            assert local.count('ads.test') == remote.count('ads.test') == 0
            results['adguard_blocks_before_upstream'] = True
            assert query(dns_port, 'type.test', qtype=65)['ips'] == []
            results['blocked_qtype'] = True
            time.sleep(.3)
            assert (root / 'state/queries.db').stat().st_size > 0
            results['query_recorder_database'] = True
    finally:
        if process:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        local.close()
        remote.close()
    (root / 'result.json').write_text(json.dumps(results, indent=2) + '\n')
    print(json.dumps(results, indent=2))


# Per-rule DNS actions on domains, ignored IP rules, and pinned
# upstreams from each source list with and without the china_dns features
# their chains copy. --prepare writes placeholder upstream addresses; --run maps
# each to a loopback stub of the same role, so generated chain tags are kept.
RULE_DNS_STUBS = {  # role: (placeholder address, default A answer)
    'local': ('udp://192.0.2.1:53', '198.51.100.1'),
    'remote': ('udp://192.0.2.2:53', '192.0.2.99'),
    'fallback': ('udp://192.0.2.3:53', '192.0.2.98'),
    'pin_local': ('udp://192.0.2.4:53', '192.0.2.77'),
    'pin_remote': ('udp://192.0.2.5:53', '192.0.2.78'),
    'pin_fallback': ('udp://192.0.2.6:53', '192.0.2.79'),
}
# Local answers that would have selected IP rules in the old probe pipeline.
RULE_DNS_PROBE_ANSWERS = {
    'ipdirect.test': '203.0.113.1', 'ipref.test': '203.0.113.20', 'ipnx.test': '203.0.113.40',
    'ipnodata.test': '203.0.113.50', 'ipbh.test': '203.0.113.70', 'ipup.test': '203.0.113.90',
    'ipupr.test': '203.0.113.100', 'ipproxy.test': '203.0.113.120', 'clsip.test': '198.18.5.5',
}
# id, rule type or ruleset behavior, value or ruleset content, route (pin_*: pinned upstream)
RULE_DNS_RULES = (
    ('direct', 'DOMAIN', 'direct.test', 'direct'),
    ('proxy', 'DOMAIN', 'proxy.test', 'proxy'),
    ('refused', 'DOMAIN-SUFFIX', 'refused.test', 'refused'),
    ('nxdomain', 'DOMAIN-SUFFIX', 'nx.test', 'nxdomain'),
    ('nodata', 'DOMAIN', 'nodata.test', 'nodata'),
    ('blackhole', 'domain', 'DOMAIN-SUFFIX,bh.test', 'blackhole'),
    ('up-local', 'DOMAIN', 'up-local.test', 'pin_local'),
    ('up-local-2', 'DOMAIN', 'up-local2.test', 'pin_local'),
    ('up-remote', 'DOMAIN', 'up-remote.test', 'pin_remote'),
    ('up-fallback', 'DOMAIN-SUFFIX', 'up-fallback.test', 'pin_fallback'),
    ('ip-direct', 'IP-CIDR', '203.0.113.0/28', 'direct'),
    ('ip-refused', 'IP-CIDR', '203.0.113.16/28', 'refused'),
    ('ip-nxdomain', 'IP-CIDR', '203.0.113.32/28', 'nxdomain'),
    ('ip-nodata', 'IP-CIDR', '203.0.113.48/28', 'nodata'),
    ('ip-blackhole', 'ipcidr', '203.0.113.64/28', 'blackhole'),
    ('ip-up-local', 'IP-CIDR', '203.0.113.80/28', 'pin_local'),
    ('ip-up-remote', 'IP-CIDR', '203.0.113.96/28', 'pin_remote'),
    ('ip-proxy', 'IP-CIDR', '203.0.113.112/28', 'proxy'),
)
# Mixed rule sets keep their domain action; their IP content does not affect DNS.
RULE_DNS_CLASSICAL = (('classical', 'classical', 'DOMAIN-SUFFIX,cls.test\nIP-CIDR,198.18.5.0/24', 'nxdomain'),)
_ROUTEROS = {'address': '127.0.0.1:9', 'username': 'smoke', 'password': 'test-only'}
_FEATURES = {'ip_selector': {'enabled': True},
             'ros_address_list': {'enabled': True, 'args': dict(_ROUTEROS)},
             'ros_route': {'enabled': True, 'args': {**_ROUTEROS, 'routing_table': 'main', 'gateway4': '192.0.2.254'}}}
_CACHED = {'cache_enabled': True, 'probe_cache_enabled': True, 'cache_lazy_ttl': 8}
_UNCACHED = {'cache_enabled': False, 'probe_cache_enabled': False}
RULE_DNS_SCENARIOS = {  # name: (config mode, rules, settings)
    'cached-features': ('template', RULE_DNS_RULES, {**_CACHED, **_FEATURES, 'remote_ttl': 60}),
    'uncached-features': ('template', RULE_DNS_RULES, {**_UNCACHED, **_FEATURES, 'remote_ttl': 30}),
    'cached-plain': ('template', RULE_DNS_RULES, {**_CACHED, 'remote_ttl': 0}),
    'uncached-plain': ('template', RULE_DNS_RULES, {**_UNCACHED, 'remote_ttl': 0}),
    # The same routing written into native YAML by the common-settings applier.
    'native': ('native', RULE_DNS_RULES, {**_CACHED, **_FEATURES, 'remote_ttl': 60}),
    'classical': ('template', RULE_DNS_CLASSICAL, {}),
}
RCODES = {0: 'NOERROR', 2: 'SERVFAIL', 3: 'NXDOMAIN', 5: 'REFUSED'}
QTYPES = {'A': 1, 'AAAA': 28, 'MX': 15}


def rule_dns_inputs(scenario):
    """Template settings without rule_actions, rule_configs and rule_actions of a scenario."""
    _, rules, features = RULE_DNS_SCENARIOS[scenario]
    settings = json.loads(json.dumps(features))
    settings.update(listen='127.0.0.1:53', prefer_ipv4=False, metrics_enabled=False, log={'level': 'warn'},
                    local_dns=RULE_DNS_STUBS['local'][0], remote_dns=RULE_DNS_STUBS['remote'][0],
                    fallback_dns=RULE_DNS_STUBS['fallback'][0])
    items, actions = [], {}
    for identity, kind, value, route in rules:
        if kind in ('domain', 'ipcidr', 'classical'):
            items.append({'id': identity, 'itemType': 'ruleset', 'name': identity, 'behavior': kind,
                          'content': value, 'url': f'https://rules.invalid/{identity}.txt'})
            suffix = 'rulesets'
        else:
            items.append({'id': identity, 'itemType': 'rule', 'rule_type': kind, 'value': value, 'policy': 'DIRECT'})
            suffix = 'rules'
        if route in ('direct', 'proxy'):
            settings.setdefault(f'{route}_{suffix}', []).append(identity)
        elif route.startswith('pin_'):
            actions[identity] = {'action': 'upstream', 'source': route[4:],
                                 'upstream': {'addr': RULE_DNS_STUBS[route][0]}}
        else:
            actions[identity] = {'action': route}
    return settings, items, actions


def prepare_rule_dns(root, scenario):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from backend.common.oxidns_common_routing import apply_routing, read_routing
    from backend.common.oxidns_editor import prepare_editor
    from backend.common.oxidns_validation import validate_native_config
    from backend.converters.mosdns import _ruleset_parts, convert_rule_content_for_mosdns
    from backend.converters.oxidns import _rule_path, generate_oxidns_config, ruleset_parts
    settings, rules, actions = rule_dns_inputs(scenario)
    # Rule sets are always downloadable through the rule proxy, which needs a token.
    config = {'rule_configs': rules,
              'system_config': {'server_domain': 'http://config.invalid', 'rule_proxy_token': 'test-only'}}
    if RULE_DNS_SCENARIOS[scenario][0] == 'template':
        config['oxidns'] = {**settings, 'rule_actions': actions}
    else:
        native = prepare_editor(settings, config)
        document = yaml.safe_load(native['native_config'])
        actions = {identity: action for identity, action in actions.items()
                   if not identity.startswith('ip-')}
        document, native = apply_routing(document, native, config, {'rule_actions': actions})
        # The recognizer must read back exactly what the applier wrote.
        assert read_routing(document, native, config)['values']['rule_actions'] == actions
        config['oxidns'] = {**native, 'native_config': yaml.safe_dump(document, allow_unicode=True, sort_keys=False)}
    generated = generate_oxidns_config(config)
    issues = validate_native_config(yaml.safe_load(generated))
    assert not issues, issues
    root.mkdir(parents=True, exist_ok=True)
    (root / 'config.yaml').write_text(generated)
    # Stand-ins for the rule files a deployment downloads through the rule proxy.
    for item in rules:
        if item['itemType'] == 'ruleset':
            for part in ruleset_parts(item, config):
                destination = root / _rule_path(item, part)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(convert_rule_content_for_mosdns(item['content'], part) + '\n')
    (root / 'rule-dns.json').write_text(json.dumps({'scenario': scenario}) + '\n')
    print(json.dumps({'prepared': str(root), 'scenario': scenario}))


def run_rule_dns(root, binary):
    scenario = json.loads((root / 'rule-dns.json').read_text())['scenario']
    _, rules, features = RULE_DNS_SCENARIOS[scenario]
    cached = features.get('cache_enabled', True)
    remote_ttl = features.get('remote_ttl', 1)
    config = yaml.safe_load((root / 'config.yaml').read_text())
    # Each scenario runs independently, including parallel pytest workers.
    if isinstance(config.get('api', {}).get('http'), dict):
        config['api']['http']['listen'] = f'127.0.0.1:{free_port()}'
    stubs = {role: MockDNS(answer) for role, (_, answer) in RULE_DNS_STUBS.items()}
    roles = {address: role for role, (address, _) in RULE_DNS_STUBS.items()}
    for name, ip in RULE_DNS_PROBE_ANSWERS.items():
        stubs['local'].set(name, ip, ttl=300)
    stubs['pin_remote'].set('up-remote.test', '192.0.2.78', ttl=300)
    dns_port = free_port()
    for plugin in config['plugins']:
        if plugin['type'] == 'forward':
            for upstream in plugin['args']['upstreams']:
                upstream['addr'] = f"udp://127.0.0.1:{stubs[roles[upstream['addr']]].server_address[1]}"
        elif plugin['type'] in ('udp_server', 'tcp_server'):
            plugin['args']['listen'] = f'127.0.0.1:{dns_port}'
    main = next(plugin['args'] for plugin in config['plugins'] if plugin['tag'] == 'sequence_main')
    runtime = root / 'runtime.yaml'
    runtime.write_text(yaml.safe_dump(config, sort_keys=False))
    process, closed = None, set()
    results = {'scenario': scenario, 'queries': []}

    def hits(name):
        return {role: stub.count(name) for role, stub in stubs.items() if stub.count(name)}

    def ask(name, qtype='A', timeout=3):
        reply = query(dns_port, name, qtype=QTYPES[qtype], timeout=timeout)
        results['queries'].append(f"{name} {qtype}: {RCODES.get(reply['rcode'], reply['rcode'])} "
                                  f"answers={reply['ips'] or reply['answers']} ttls={reply['ttls']} "
                                  f"authority={reply['authority']} upstreams={hits(name)}")
        return reply

    def expect(name, rcode, ips=(), upstreams=None, qtype='A'):
        reply = ask(name, qtype)
        assert RCODES.get(reply['rcode']) == rcode and reply['ips'] == list(ips), (name, qtype, reply)
        if not ips:
            assert reply['answers'] == 0, (name, qtype, reply)
        if upstreams is not None:
            assert hits(name) == upstreams, (name, qtype, hits(name))
        return reply

    try:
        checked = subprocess.run([binary, 'check', '-c', str(runtime), '-d', str(root)],
                                 capture_output=True, text=True, timeout=30)
        assert checked.returncode == 0, checked.stdout + checked.stderr
        results['check'] = (checked.stdout + checked.stderr).strip().splitlines()[-1]
        with (root / 'runtime.log').open('w') as log:
            process = subprocess.Popen([binary, 'start', '-c', str(runtime), '-d', str(root)],
                                       stdout=log, stderr=subprocess.STDOUT)
            for _ in range(100):
                assert process.poll() is None, (root / 'runtime.log').read_text()[-3000:]
                try:
                    query(dns_port, 'ready.test', timeout=.3)
                    break
                except OSError:
                    time.sleep(.05)
            else:
                raise AssertionError('temporary OxiDNS did not answer')
            if rules is RULE_DNS_CLASSICAL:
                assert {'exec': 'jump local_probe'} not in main
                expect('cls.test', 'NXDOMAIN', upstreams={})
                expect('clsip.test', 'NOERROR', ['192.0.2.99'], {'remote': 1})
                expect('other.test', 'NOERROR', ['192.0.2.99'], {'remote': 1})
                results['classical_domain_only'] = True
            else:
                expect('direct.test', 'NOERROR', ['198.51.100.1'], {'local': 1})
                expect('proxy.test', 'NOERROR', ['192.0.2.99'], {'remote': 1})
                expect('refused.test', 'REFUSED', upstreams={})
                expect('www.refused.test', 'REFUSED', upstreams={})
                expect('nx.test', 'NXDOMAIN', upstreams={})
                expect('nodata.test', 'NOERROR', upstreams={})
                expect('bh.test', 'NOERROR', ['0.0.0.0'], {})
                expect('bh.test', 'NOERROR', ['::'], {}, qtype='AAAA')
                assert expect('bh.test', 'NOERROR', upstreams={}, qtype='MX')['authority'] == 1
                expect('up-local2.test', 'NOERROR', ['192.0.2.77'], {'pin_local': 1})
                expect('up-fallback.test', 'NOERROR', ['192.0.2.79'], {'pin_fallback': 1})
                # Old IP actions are ignored; unknown names use the default resolver.
                for name in ('ipdirect.test', 'ipref.test', 'ipnx.test', 'ipnodata.test',
                             'ipbh.test', 'ipupr.test', 'ipproxy.test', 'other.test'):
                    expect(name, 'NOERROR', ['192.0.2.99'], {'remote': 1})
                results['actions'] = True
                # Each pinned upstream has its own cache, present only with cache_enabled.
                first = expect('up-local.test', 'NOERROR', ['192.0.2.77'], {'pin_local': 1})
                second = expect('up-local.test', 'NOERROR', ['192.0.2.77'], {'pin_local': 1 if cached else 2})
                assert second['ttls'][0] <= first['ttls'][0]
                expect('ipup.test', 'NOERROR', ['192.0.2.99'], {'remote': 1})
                expect('ipup.test', 'NOERROR', ['192.0.2.99'], {'remote': 2})
                ttl = remote_ttl or 300
                first = expect('up-remote.test', 'NOERROR', ['192.0.2.78'], {'pin_remote': 1})
                second = expect('up-remote.test', 'NOERROR', ['192.0.2.78'], {'pin_remote': 1 if cached else 2})
                assert first['ttls'] == [ttl] and second['ttls'][0] <= ttl, (first, second)
                results['pinned_cache'] = 'hit' if cached else 'disabled'
                results['pinned_remote_ttl'] = first['ttls'][0]
                # A failed pinned upstream never falls through to another answer.
                stubs['pin_fallback'].close()
                closed.add('pin_fallback')
                reply = ask('gone.up-fallback.test', timeout=15)
                assert RCODES.get(reply['rcode']) == 'SERVFAIL' and not reply['ips'], reply
                results['pinned_down_servfail'] = True
    finally:
        if process:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for role, stub in stubs.items():
            if role not in closed:
                stub.close()
    (root / 'result.json').write_text(json.dumps(results, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps(results, indent=2, ensure_ascii=False))
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--prepare', type=Path)
    modes.add_argument('--run', type=Path)
    parser.add_argument('--binary', default='oxidns')
    parser.add_argument('--complete', action='store_true', help='Prepare native mode with managed include/certificate/binary files and DoT/DoH/DoQ servers')
    parser.add_argument('--rule-dns', choices=RULE_DNS_SCENARIOS, help='Prepare a per-rule DNS action scenario instead')
    args = parser.parse_args()
    if args.prepare and args.rule_dns:
        prepare_rule_dns(args.prepare.resolve(), args.rule_dns)
    elif args.prepare:
        prepare(args.prepare.resolve(), complete=args.complete)
    elif (args.run / 'rule-dns.json').exists():
        run_rule_dns(args.run.resolve(), args.binary)
    else:
        run(args.run.resolve(), args.binary)
