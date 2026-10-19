#!/usr/bin/env python3
"""Network fixtures/probes for an explicitly marked disposable Linux VM only.

Never starts a VM, changes the initial namespace, or downloads software.
The shell entrypoint owns creation/removal of the isolated topology.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import http.server
import ipaddress
import json
import os
from pathlib import Path
import re
import select
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import uuid

STATE = Path('/var/lib/configflow-gateway-lab')
MARKER = Path('/root/.configflow-disposable-lab')
NAMESPACES = ('cfgl-gateway', 'cfgl-router', 'cfgl-client', 'cfgl-switch', 'cfgl-origin')
HOSTS = {'direct.lab.invalid': '203.0.113.10', 'selected.lab.invalid': '203.0.113.20', 'proxy.lab.invalid': '203.0.113.30'}
LOG_LOCK = threading.Lock()


def guard(require_setup=True):
    if sys.platform != 'linux' or os.geteuid() != 0:
        raise RuntimeError('Refusing: this harness requires root inside its disposable Linux VM.')
    if socket.gethostname() != 'configflow-gateway-lab':
        raise RuntimeError('Refusing: hostname must be configflow-gateway-lab.')
    if MARKER.is_symlink() or not MARKER.is_file():
        raise RuntimeError('Refusing: disposable VM marker is missing.')
    info = MARKER.stat()
    expected = 'CONFIGFLOW_DISPOSABLE_LINUX_VM=1\nMACHINE_ID=' + Path('/etc/machine-id').read_text().strip() + '\n'
    if info.st_uid != 0 or (info.st_mode & 0o777) != 0o600 or MARKER.read_text() != expected:
        raise RuntimeError('Refusing: marker ownership, mode, or machine identity does not match.')
    found = subprocess.run(['systemd-detect-virt', '--vm'], capture_output=True, text=True)
    if found.returncode != 0 or found.stdout.strip() not in ('qemu', 'kvm', 'apple', 'bochs'):
        raise RuntimeError('Refusing: this is not a recognized disposable QEMU/Apple VM.')
    if require_setup and (not (STATE / 'owner').is_file() or STATE.is_symlink()):
        raise RuntimeError('The isolated lab has not been set up.')


def call(arguments, *, timeout=15, check=True):
    result = subprocess.run(arguments, text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError('Command failed: ' + ' '.join(arguments[:5]) + '\n' + result.stderr[-1200:])
    return result


def ns(namespace, arguments, **kwargs):
    if namespace not in NAMESPACES:
        raise RuntimeError('Unknown lab namespace')
    return call(['ip', 'netns', 'exec', namespace, *arguments], **kwargs)


def log_event(event):
    event['at'] = time.time()
    with LOG_LOCK, (STATE / 'fixtures.jsonl').open('a') as stream:
        stream.write(json.dumps(event, sort_keys=True) + '\n')
        stream.flush()


class HTTPFixture(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def do_GET(self):
        token = self.path.rsplit('/', 1)[-1]
        record = {'event': 'http_request', 'token': token, 'path': self.path,
                  'source_ip': self.client_address[0], 'destination_ip': self.connection.getsockname()[0]}
        log_event(record)
        if self.path.startswith('/hold/'):
            time.sleep(6)
        body = json.dumps(record).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def receive_exact(stream, count):
    output = b''
    while len(output) < count:
        chunk = stream.recv(count - len(output))
        if not chunk:
            raise EOFError('truncated SOCKS request')
        output += chunk
    return output


class SOCKSFixture(socketserver.BaseRequestHandler):
    def handle(self):
        stream = self.request
        stream.settimeout(12)
        upstream = None
        try:
            version, count = receive_exact(stream, 2)
            methods = receive_exact(stream, count)
            if version != 5 or 0 not in methods:
                stream.sendall(b'\x05\xff')
                return
            stream.sendall(b'\x05\x00')
            version, command, _, kind = receive_exact(stream, 4)
            if version != 5 or command != 1:
                raise ValueError('fixture only supports SOCKS5 CONNECT')
            if kind == 1:
                host = socket.inet_ntoa(receive_exact(stream, 4))
            elif kind == 3:
                host = receive_exact(stream, receive_exact(stream, 1)[0]).decode('ascii')
            else:
                raise ValueError('fixture only supports IPv4/domain targets')
            port = struct.unpack('!H', receive_exact(stream, 2))[0]
            destination = HOSTS.get(host, host)
            if destination not in HOSTS.values() or port != 18080:
                raise ValueError('fixture refuses targets outside the isolated test endpoints')
            upstream = socket.create_connection((destination, port), timeout=3)
            stream.sendall(b'\x05\x00\x00\x01' + socket.inet_aton('203.0.113.2') + struct.pack('!H', 1080))
            log_event({'event': 'socks_connect', 'host': host, 'destination_ip': destination, 'port': port})
            stream.setblocking(False)
            upstream.setblocking(False)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                ready, _, _ = select.select([stream, upstream], [], [], 1)
                for current in ready:
                    data = current.recv(65536)
                    if not data:
                        return
                    target = upstream if current is stream else stream
                    target.setblocking(True)
                    target.sendall(data)
                    target.setblocking(False)
        except (OSError, EOFError, ValueError) as error:
            log_event({'event': 'socks_error', 'error': str(error)})
        finally:
            if upstream:
                upstream.close()


class ThreadedTCP(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def dns_reply(data):
    if len(data) < 12:
        return b''
    offset, labels = 12, []
    while offset < len(data) and data[offset]:
        length = data[offset]
        if length > 63 or offset + length + 1 >= len(data):
            return b''
        labels.append(data[offset + 1:offset + length + 1].decode('ascii', errors='replace'))
        offset += length + 1
    if offset + 5 > len(data):
        return b''
    qtype, qclass = struct.unpack('!HH', data[offset + 1:offset + 5])
    question = data[12:offset + 5]
    address = HOSTS.get('.'.join(labels).lower())
    count = 1 if address and qtype == 1 and qclass == 1 else 0
    header = data[:2] + struct.pack('!HHHHH', 0x8180 if address else 0x8183, 1, count, 0, 0)
    answer = b'\xc0\x0c' + struct.pack('!HHIH', 1, 1, 30, 4) + socket.inet_aton(address) if count else b''
    return header + question + answer


class DNSUDP(socketserver.BaseRequestHandler):
    def handle(self):
        data, stream = self.request
        result = dns_reply(data)
        if result:
            stream.sendto(result, self.client_address)


class DNSTCP(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(3)
        try:
            data = receive_exact(self.request, struct.unpack('!H', receive_exact(self.request, 2))[0])
            result = dns_reply(data)
            self.request.sendall(struct.pack('!H', len(result)) + result)
        except (OSError, EOFError):
            return


def serve():
    # Every listener is confined to cfgl-origin; reject a mistaken namespace.
    inode = str(os.stat('/proc/self/ns/net').st_ino)
    if inode != str(os.stat('/run/netns/cfgl-origin').st_ino):
        raise RuntimeError('Fixture must run in cfgl-origin, never the VM initial namespace.')
    servers = [http.server.ThreadingHTTPServer(('0.0.0.0', 18080), HTTPFixture),
               ThreadedTCP(('203.0.113.2', 1080), SOCKSFixture),
               socketserver.ThreadingUDPServer(('203.0.113.2', 5353), DNSUDP),
               ThreadedTCP(('203.0.113.2', 5353), DNSTCP)]
    (STATE / 'fixtures.ready').write_text(str(os.getpid()))
    with ThreadPoolExecutor(max_workers=len(servers)) as pool:
        list(pool.map(lambda server: server.serve_forever(), servers))


def canonical(value):
    if isinstance(value, list):
        return [canonical(item) for item in value]
    if isinstance(value, dict):
        return {key: canonical(item) for key, item in sorted(value.items())
                if key not in ('handle', 'packets', 'bytes', 'expires', 'used', 'age', 'valid_life_time', 'preferred_life_time')}
    return value


def snapshot():
    nft = json.loads(call(['nft', '-j', 'list', 'ruleset']).stdout)
    nft['nftables'] = [item for item in nft.get('nftables', []) if 'metainfo' not in item]
    sysctls = {'net/ipv4/ip_forward': Path('/proc/sys/net/ipv4/ip_forward').read_text().strip()}
    for interface in sorted(Path('/proc/sys/net/ipv4/conf').iterdir()):
        for field in ('forwarding', 'send_redirects', 'accept_redirects', 'rp_filter', 'accept_local'):
            path = interface / field
            if path.is_file():
                sysctls[f'net/ipv4/conf/{interface.name}/{field}'] = path.read_text().strip()
    links = json.loads(call(['ip', '-j', '-4', 'address', 'show']).stdout)
    keep = ('ifname', 'flags', 'mtu', 'link_type', 'address', 'addr_info')
    links = [{key: link[key] for key in keep if key in link} for link in links]
    return canonical({'links': links, 'routes': json.loads(call(['ip', '-j', '-4', 'route', 'show', 'table', 'all']).stdout),
                      'rules': json.loads(call(['ip', '-j', '-4', 'rule', 'show']).stdout), 'nft': nft, 'sysctls': sysctls})


def dns_a(host, server, transport='udp'):
    query_id = int.from_bytes(os.urandom(2), 'big')
    query = struct.pack('!HHHHHH', query_id, 0x0100, 1, 0, 0, 0)
    query += b''.join(bytes([len(label)]) + label.encode('ascii') for label in host.split('.')) + b'\0' + struct.pack('!HH', 1, 1)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM if transport == 'tcp' else socket.SOCK_DGRAM) as stream:
        stream.settimeout(8)
        if transport == 'tcp':
            stream.connect((server, 53))
            stream.sendall(struct.pack('!H', len(query)) + query)
            answer = receive_exact(stream, struct.unpack('!H', receive_exact(stream, 2))[0])
        else:
            stream.sendto(query, (server, 53))
            answer, peer = stream.recvfrom(65535)
            if peer != (server, 53):
                raise RuntimeError('DNS response came from an unexpected server')
    if len(answer) < 12 or struct.unpack('!H', answer[:2])[0] != query_id:
        raise RuntimeError('DNS response ID mismatch')
    offset = 12
    def skip_name(position):
        while answer[position]:
            if answer[position] & 0xc0 == 0xc0:
                return position + 2
            position += answer[position] + 1
        return position + 1
    qd, an = struct.unpack('!HH', answer[4:8])
    for _ in range(qd):
        offset = skip_name(offset) + 4
    addresses = []
    for _ in range(an):
        offset = skip_name(offset)
        kind, cls, _, size = struct.unpack('!HHIH', answer[offset:offset + 10])
        offset += 10
        if kind == 1 and cls == 1 and size == 4:
            addresses.append(socket.inet_ntoa(answer[offset:offset + 4]))
        offset += size
    return addresses


def captured_packets(path):
    """Read tcpdump's classic PCAP, retaining only lab DNS and the IP-DoH flow."""
    raw = path.read_bytes()
    orders = {b'\xd4\xc3\xb2\xa1': '<', b'\xa1\xb2\xc3\xd4': '>',
              b'\x4d\x3c\xb2\xa1': '<', b'\xa1\xb2\x3c\x4d': '>'}
    order = orders.get(raw[:4])
    if not order or len(raw) < 24:
        raise RuntimeError('DNS evidence is not a complete classic PCAP')
    link_type = struct.unpack(order + 'I', raw[20:24])[0]
    offsets = {1: 14, 113: 16, 276: 20, 101: 0, 228: 0}
    if link_type not in offsets:
        raise RuntimeError('Unsupported DNS capture link type: ' + str(link_type))
    records, position = [], 24
    while position + 16 <= len(raw):
        size = struct.unpack(order + 'I', raw[position + 8:position + 12])[0]
        frame = raw[position + 16:position + 16 + size]
        position += 16 + size
        packet = frame[offsets[link_type]:]
        if len(packet) < 20 or packet[0] >> 4 != 4:
            continue
        header_length = (packet[0] & 15) * 4
        total_length = struct.unpack('!H', packet[2:4])[0]
        protocol = packet[9]
        if protocol not in (6, 17) or len(packet) < header_length + 8:
            continue
        source, destination = socket.inet_ntoa(packet[12:16]), socket.inet_ntoa(packet[16:20])
        source_port, destination_port = struct.unpack('!HH', packet[header_length:header_length + 4])
        record = {'transport': 'tcp' if protocol == 6 else 'udp', 'source': source,
                  'destination': destination, 'source_port': source_port, 'destination_port': destination_port}
        if '223.5.5.5' in (source, destination) and 443 in (source_port, destination_port):
            records.append({**record, 'kind': 'ip-doh-tls'})
            continue
        if not {source_port, destination_port} & {53, 1053}:
            continue
        if protocol == 6:
            if len(packet) < header_length + 20:
                continue
            payload = packet[header_length + (packet[header_length + 12] >> 4) * 4:total_length]
            if len(payload) < 2:
                continue
            length = struct.unpack('!H', payload[:2])[0]
            payload = payload[2:2 + length]
            if len(payload) != length:
                continue
        else:
            payload = packet[header_length + 8:total_length]
        if len(payload) < 12:
            continue
        query_id, flags = struct.unpack('!HH', payload[:4])
        offset, labels = 12, []
        while offset < len(payload) and payload[offset]:
            length = payload[offset]
            if length > 63 or offset + length + 1 > len(payload):
                break
            labels.append(payload[offset + 1:offset + length + 1].decode('ascii', errors='replace'))
            offset += length + 1
        else:
            if offset + 5 <= len(payload):
                records.append({**record, 'kind': 'dns', 'id': query_id, 'name': '.'.join(labels).lower(),
                                'response': bool(flags & 0x8000), 'rcode': flags & 15,
                                'qtype': struct.unpack('!H', payload[offset + 1:offset + 3])[0]})
    return records


def mihomo_dns_dnat_counters():
    rules = json.loads(ns('cfgl-gateway', ['nft', '-j', 'list', 'ruleset']).stdout)
    counters = {}
    for item in rules.get('nftables', []):
        rule = item.get('rule', {})
        expressions = rule.get('expr', [])
        if rule.get('table') != 'mihomo' or not any('dnat' in expr for expr in expressions):
            continue
        if not any(expr.get('match', {}).get('right') == 53 for expr in expressions):
            continue
        counter = next((expr['counter'] for expr in expressions if 'counter' in expr), None)
        if counter is None:
            raise RuntimeError('Mihomo DNS DNAT has no observable counter')
        key = str(rule.get('family')) + '/' + str(rule.get('chain')) + '/' + str(rule.get('handle'))
        counters[key] = counter
    return counters


def active_dns_probe():
    identifier = 'dns-' + uuid.uuid4().hex
    fresh_proxy_domain = uuid.uuid4().hex + '.proxy.lab.invalid'
    capture_path, log_path = STATE / (identifier + '.pcap'), STATE / (identifier + '.log')
    before = mihomo_dns_dnat_counters()
    answers = {}
    with log_path.open('w') as capture_log:
        capture = subprocess.Popen(['ip', 'netns', 'exec', 'cfgl-gateway', 'tcpdump',
            '--immediate-mode', '-Z', 'root', '-U', '-nn', '-s', '0', '-i', 'any', '-w', str(capture_path),
            'port 53 or port 1053 or (host 223.5.5.5 and port 443)'],
            stdout=subprocess.DEVNULL, stderr=capture_log)
        try:
            for _ in range(40):
                if 'listening on' in log_path.read_text():
                    break
                if capture.poll() is not None:
                    raise RuntimeError('DNS capture could not start: ' + log_path.read_text()[-600:])
                time.sleep(.05)
            else:
                raise RuntimeError('Timed out waiting for DNS packet capture')
            for transport in ('udp', 'tcp'):
                command = [sys.executable, str(Path(__file__).resolve()), 'dns-a', '--name', 'example.com', '--transport', transport]
                values = json.loads(ns('cfgl-client', command).stdout)
                if not values or any(not ipaddress.ip_address(value).is_global for value in values):
                    raise RuntimeError('OxiDNS example.com ' + transport + ' did not return real public IPv4: ' + repr(values))
                answers[transport] = values
            fake_addresses = {}
            # A fresh subdomain guarantees observable forwarding on every lab
            # run even while OxiDNS retains answers from the preceding session.
            # HTTP keeps the fixed domain supported by the origin/SOCKS fixture.
            for name in (fresh_proxy_domain, 'proxy.lab.invalid'):
                fake_values = json.loads(ns('cfgl-client', [sys.executable, str(Path(__file__).resolve()),
                    'dns-a', '--name', name]).stdout)
                address = next((value for value in fake_values if ipaddress.ip_address(value) in ipaddress.ip_network('198.18.0.0/16')), None)
                if not address:
                    raise RuntimeError('OxiDNS did not return fake-IP for ' + name + ': ' + repr(fake_values))
                fake_addresses[name] = address
            fake = fake_addresses['proxy.lab.invalid']
            time.sleep(.2)
        finally:
            capture.send_signal(signal.SIGINT)
            try:
                capture.wait(timeout=4)
            except subprocess.TimeoutExpired:
                capture.kill()
                capture.wait(timeout=2)
    after = mihomo_dns_dnat_counters()
    if before != after:
        raise RuntimeError('Client DNS was intercepted by Mihomo DNAT53: ' + json.dumps({'before': before, 'after': after}))
    packets = captured_packets(capture_path)
    def exchange(name, transport, source, destination, port):
        queries = [item for item in packets if item.get('kind') == 'dns' and item['name'] == name
                   and item['transport'] == transport and not item['response'] and item['qtype'] == 1
                   and item['source'] == source and item['destination'] == destination and item['destination_port'] == port]
        replies = [item for item in packets if item.get('kind') == 'dns' and item['name'] == name
                   and item['transport'] == transport and item['response'] and item['rcode'] == 0
                   and item['source'] == destination and item['destination'] == source and item['source_port'] == port
                   and any(query['id'] == item['id'] and query['source_port'] == item['destination_port'] for query in queries)]
        if not queries or not replies:
            raise RuntimeError('DNS packet evidence lacks request/reply: ' + name + ' ' + transport + ' port ' + str(port))
        return {'queries': queries, 'replies': replies}
    evidence = {transport: exchange('example.com', transport, '192.0.2.100', '192.0.2.2', 53) for transport in ('udp', 'tcp')}
    evidence['oxidns_to_mihomo'] = exchange(fresh_proxy_domain, 'udp', '127.0.0.1', '127.0.0.1', 1053)
    result = {'name': 'dns-through-oxidns', 'direct_domain': 'example.com', 'answers': answers,
              'proxy_domain': 'proxy.lab.invalid', 'fake_ip': fake, 'packet_evidence': evidence,
              'fresh_probe_domain': fresh_proxy_domain, 'fresh_probe_fake_ip': fake_addresses[fresh_proxy_domain],
              'ip_doh_tls_packets': len([item for item in packets if item.get('kind') == 'ip-doh-tls']),
              'mihomo_dns_dnat_before': before, 'mihomo_dns_dnat_after': after, 'pcap': str(capture_path)}
    return result, fake


def controller(token_file):
    headers = {}
    if token_file:
        headers['Authorization'] = 'Bearer ' + Path(token_file).read_text().strip()
    request = urllib.request.Request('http://192.0.2.2:9090/connections', headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=4) as response:
        return json.load(response)


def fixture_log(since=0):
    path = STATE / 'fixtures.jsonl'
    if not path.exists():
        return []
    result = []
    for line in path.read_text().splitlines():
        try:
            event = json.loads(line)
            if event.get('at', 0) >= since:
                result.append(event)
        except ValueError:
            pass
    return result


def request_probe(label, address, expected_proxy, *, domain=None, controller_token_file=None, inspect=True):
    token = label + '-' + uuid.uuid4().hex
    start = time.time()
    arguments = ['curl', '--noproxy', '*', '-fsS', '--connect-timeout', '3', '--max-time', '15']
    host = domain or address
    if domain:
        arguments += ['--resolve', f'{domain}:18080:{address}']
    arguments += [f'http://{host}:18080/hold/{token}']
    child = subprocess.Popen(['ip', 'netns', 'exec', 'cfgl-client', *arguments], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    matched_connections = []
    try:
        for _ in range(25):
            if any(item.get('token') == token for item in fixture_log(start)):
                break
            if child.poll() is not None:
                break
            time.sleep(.1)
        if inspect:
            command = [sys.executable, str(Path(__file__).resolve()), 'controller']
            if controller_token_file:
                command += ['--controller-token-file', controller_token_file]
            connections = json.loads(ns('cfgl-router', command).stdout).get('connections') or []
            for connection in connections:
                metadata = connection.get('metadata') or {}
                if (metadata.get('destinationIP') == address or domain and metadata.get('host') == domain) and str(metadata.get('destinationPort')) == '18080':
                    matched_connections.append({'metadata': metadata, 'chains': connection.get('chains'), 'rule': connection.get('rule')})
        stdout, stderr = child.communicate(timeout=18)
        if child.returncode:
            raise RuntimeError(label + ' HTTP probe failed: ' + stderr[-700:])
        response = json.loads(stdout)
        events = fixture_log(start)
        destination = HOSTS.get(domain, address)
        if response.get('token') != token or response.get('destination_ip') != destination:
            raise RuntimeError(label + ' reached the wrong fixture endpoint')
        if not expected_proxy and response.get('source_ip') != '192.0.2.2':
            raise RuntimeError(label + ' did not use the gateway SNAT address 192.0.2.2')
        proxy_events = [event for event in events if event.get('event') == 'socks_connect' and event.get('destination_ip') == destination]
        if expected_proxy:
            if not proxy_events or inspect and not matched_connections:
                raise RuntimeError(label + ' was reachable but lacked BOTH SOCKS and Mihomo connection evidence')
        elif proxy_events or matched_connections:
            raise RuntimeError(label + ' unexpectedly entered Mihomo/proxy instead of kernel forwarding')
        return {'name': label, 'http': response, 'mihomo_connections': matched_connections, 'socks_connections': proxy_events}
    finally:
        if child.poll() is None:
            child.terminate()
            child.communicate(timeout=3)


def probe(mode, token_file, baseline=None):
    results = []
    if mode == 'active':
        results.append(request_probe('direct', '203.0.113.10', False, controller_token_file=token_file))
        results.append(request_probe('selected-ip', '203.0.113.20', True, controller_token_file=token_file))
        dns_result, fake = active_dns_probe()
        results.append(dns_result)
        results.append(request_probe('fake-ip', fake, True, domain='proxy.lab.invalid', controller_token_file=token_file))
    elif mode == 'direct':
        results.append(request_probe('direct-without-core', '203.0.113.10', False, inspect=False))
    elif mode in ('ordinary', 'removed'):
        current = json.loads(ns('cfgl-gateway', [sys.executable, str(Path(__file__).resolve()), 'snapshot']).stdout)
        tables = [item['table'].get('name') for item in current['nft']['nftables'] if 'table' in item]
        if 'configflow_gateway' in tables:
            raise RuntimeError(mode + ': managed gateway table must be absent')
        expected_forwarding = '0'
        if baseline:
            if not re.fullmatch(r'[A-Za-z0-9_-]+', baseline):
                raise RuntimeError('Invalid snapshot baseline label')
            prior = json.loads((STATE / 'snapshots' / (baseline + '.gateway.json')).read_text())
            if current['sysctls'] != prior['sysctls']:
                raise RuntimeError(mode + ': sysctls did not restore the exact supplied baseline')
            expected_forwarding = prior['sysctls']['net/ipv4/ip_forward']
        if current['sysctls']['net/ipv4/ip_forward'] != expected_forwarding:
            raise RuntimeError(mode + ': expected original ip_forward=' + expected_forwarding)
        result = {'name': mode, 'managed_table_absent': True, 'ip_forward': expected_forwarding, 'baseline': baseline}
        if expected_forwarding == '0':
            response = ns('cfgl-client', ['curl', '--noproxy', '*', '-fsS', '--connect-timeout', '1', '--max-time', '2', 'http://203.0.113.10:18080/ordinary-should-not-forward'], check=False, timeout=4)
            if response.returncode == 0:
                raise RuntimeError(mode + ': unselected real IP was forwarded without gateway enablement')
            result['unselected_connection_rejected'] = True
        results.append(result)
    output = {'success': True, 'mode': mode, 'probes': results}
    (STATE / 'last-probe.json').write_text(json.dumps(output, indent=2) + '\n')
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('serve', 'snapshot', 'dns-a', 'controller', 'probe'))
    parser.add_argument('--mode', choices=('active', 'ordinary', 'removed', 'direct'), default='active')
    parser.add_argument('--controller-token-file')
    parser.add_argument('--name', default='proxy.lab.invalid')
    parser.add_argument('--transport', choices=('udp', 'tcp'), default='udp')
    parser.add_argument('--baseline', help='Snapshot label whose exact sysctl values removal must restore')
    args = parser.parse_args()
    guard(require_setup=args.command != 'snapshot')
    if args.command == 'serve':
        serve()
    else:
        if args.command == 'snapshot': output = snapshot()
        elif args.command == 'dns-a': output = dns_a(args.name, '192.0.2.2', args.transport)
        elif args.command == 'controller': output = controller(args.controller_token_file)
        else: output = probe(args.mode, args.controller_token_file, args.baseline)
        print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        print('LAB ERROR: ' + str(error), file=sys.stderr)
        sys.exit(1)
