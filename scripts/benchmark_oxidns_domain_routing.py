#!/usr/bin/env python3
"""Real-core DNS correctness/latency harness using isolated loopback upstreams.

Export fixtures from both revisions with the same script, then benchmark them:
  python scripts/benchmark_oxidns_domain_routing.py --export /tmp/before --generator-root /tmp/old-checkout
  python scripts/benchmark_oxidns_domain_routing.py --export /tmp/after
  python scripts/benchmark_oxidns_domain_routing.py --binary /path/to/oxidns \
      --before /tmp/before --after /tmp/after --output /tmp/dns-benchmark.json

Run on the test server, never the development Mac. No production DNS or network
interfaces are used. The delays model upstream latency, not Internet performance.
"""
import argparse
import collections
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import socket
import socketserver
import statistics
import struct
import subprocess
import sys
import tempfile
import threading
import time

import yaml


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def read_name(data, offset):
    labels = []
    while data[offset]:
        size = data[offset]
        labels.append(data[offset + 1:offset + 1 + size].decode())
        offset += size + 1
    return '.'.join(labels), offset + 1


class DelayedDNS(socketserver.ThreadingUDPServer):
    daemon_threads = True

    def __init__(self, local, delay_ms=0):
        self.local, self.delay_ms = local, delay_ms
        self.counts = collections.Counter()
        self.fail_names = set()
        self.lock = threading.Lock()
        super().__init__(('127.0.0.1', 0), DNSHandler)
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    def count(self, name=None):
        with self.lock:
            return sum(value for (query_name, _), value in self.counts.items()
                       if name is None or name == query_name)

    def close(self):
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=2)


class DNSHandler(socketserver.BaseRequestHandler):
    def handle(self):
        request, sock = self.request
        name, end = read_name(request, 12)
        qtype, _ = struct.unpack('!HH', request[end:end + 4])
        with self.server.lock:
            self.server.counts[name, qtype] += 1
            fail = name in self.server.fail_names
        if self.server.delay_ms:
            time.sleep(self.server.delay_ms / 1000)
        answer = b''
        if not fail and qtype in (1, 28):
            if self.server.local:
                in_range = name.startswith('poison.') or name.endswith('.direct.test') or name.endswith('.mixed.test')
                ip = ('10.1.2.3' if in_range else '203.0.114.44') if qtype == 1 else (
                    '2001:db8:1::3' if in_range else '2001:db8:2::44')
            else:
                ip = '198.51.100.20' if qtype == 1 else '2001:db8:ffff::20'
            packed = socket.inet_pton(socket.AF_INET if qtype == 1 else socket.AF_INET6, ip)
            answer = b'\xc0\x0c' + struct.pack('!HHIH', qtype, 1, 300, len(packed)) + packed
        response = request[:2] + struct.pack('!5H', 0x8182 if fail else 0x8180, 1, int(bool(answer)), 0, 0)
        response += request[12:end + 4] + answer
        sock.sendto(response, self.client_address)


def query(port, name, *, qtype=1, tcp=False, timeout=3):
    question = b''.join(bytes([len(label)]) + label.encode() for label in name.split('.')) + b'\0'
    request = struct.pack('!6H', 0x3344, 0x0100, 1, 0, 0, 0) + question + struct.pack('!HH', qtype, 1)
    started = time.perf_counter()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM if tcp else socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        if tcp:
            sock.connect(('127.0.0.1', port))
            sock.sendall(struct.pack('!H', len(request)) + request)
            header = sock.recv(2)
            if len(header) != 2:
                raise OSError('truncated DNS TCP length')
            length = struct.unpack('!H', header)[0]
            reply = b''
            while len(reply) < length:
                chunk = sock.recv(length - len(reply))
                if not chunk:
                    raise OSError('truncated DNS TCP reply')
                reply += chunk
        else:
            sock.sendto(request, ('127.0.0.1', port))
            reply = sock.recv(4096)
    elapsed = (time.perf_counter() - started) * 1000
    header = struct.unpack('!6H', reply[:12])
    assert header[0] == 0x3344
    _, offset = read_name(reply, 12)
    offset += 4
    ips = []
    for _ in range(header[3]):
        if reply[offset] & 0xc0 == 0xc0:
            offset += 2
        else:
            _, offset = read_name(reply, offset)
        typ, _, _, size = struct.unpack('!HHIH', reply[offset:offset + 10])
        offset += 10
        if typ in (1, 28):
            ips.append(socket.inet_ntop(socket.AF_INET if typ == 1 else socket.AF_INET6, reply[offset:offset + size]))
        offset += size
    return {'rcode': header[1] & 15, 'ips': ips, 'elapsed_ms': elapsed}


def fixture_config(default='proxy', cache=True):
    rules = [
        {'id': 'direct', 'itemType': 'rule', 'rule_type': 'DOMAIN-SUFFIX', 'value': 'direct.test'},
        {'id': 'proxy', 'itemType': 'rule', 'rule_type': 'DOMAIN-SUFFIX', 'value': 'proxy.test'},
        {'id': 'ip4', 'itemType': 'rule', 'rule_type': 'IP-CIDR', 'value': '10.0.0.0/8'},
        {'id': 'ip6', 'itemType': 'rule', 'rule_type': 'IP-CIDR6', 'value': '2001:db8:1::/48'},
        {'id': 'mixed', 'itemType': 'ruleset', 'library_rule_id': 'mixed-library', 'source_type': 'content',
         'name': 'mixed', 'behavior': 'classical', 'format': 'text',
         'url': '/api/rule-library/content/mixed-library',
         'content': 'DOMAIN-SUFFIX,mixed.test\nIP-CIDR,10.0.0.0/8\nIP-CIDR6,2001:db8:1::/48\n'},
    ]
    # The legacy pipeline also probes when the global default is direct.
    if default == 'direct':
        rules.append({'id': 'proxy-ip', 'itemType': 'rule', 'rule_type': 'IP-CIDR', 'value': '198.51.100.0/24'})
    return {'rule_configs': rules, 'rule_library': [{**rules[-2 if default == 'direct' else -1], 'id': 'mixed-library'}],
            'system_config': {'server_domain': 'http://127.0.0.1:9', 'rule_proxy_token': 'test-only'}, 'oxidns': {
                'direct_rules': ['direct', 'ip4', 'ip6'], 'proxy_rules': ['proxy', *(['proxy-ip'] if default == 'direct' else [])],
                'direct_rulesets': ['mixed'], 'default_forward': 'forward_local' if default == 'direct' else 'forward_remote',
                'local_dns': 'udp://127.0.0.1:9', 'remote_dns': 'udp://127.0.0.1:9', 'fallback_dns': '',
                'cache_enabled': cache, 'cache_lazy_ttl': 0, 'probe_cache_enabled': cache,
                'api': {'enabled': False}, 'query_recorder': {'enabled': False}, 'metrics_enabled': False,
                'prefer_ipv4': False, 'remote_ttl': 0, 'log': {'level': 'warn'}, 'block_qtypes': [],
            }}


def export_fixtures(output, generator_root=None):
    sys.path.insert(0, str(generator_root or Path(__file__).resolve().parents[1]))
    from backend.converters.oxidns import generate_oxidns_config
    output.mkdir(parents=True, exist_ok=True)
    for default in ('proxy', 'direct'):
        for cache in (True, False):
            destination = output / f'{default}-cache-{int(cache)}.yaml'
            destination.write_text(generate_oxidns_config(fixture_config(default, cache)))


@contextmanager
def running_core(binary, document, root, local, remote):
    document = deepcopy(document)
    root.mkdir(parents=True, exist_ok=True)
    port = free_port()
    for plugin in document['plugins']:
        if plugin['tag'] in ('forward_local', 'forward_remote'):
            upstream = local if plugin['tag'] == 'forward_local' else remote
            plugin['args']['upstreams'] = [{'addr': f'udp://127.0.0.1:{upstream.server_address[1]}'}]
        if plugin['type'] in ('udp_server', 'tcp_server'):
            plugin['args']['listen'] = f'127.0.0.1:{port}'
        if plugin['type'] in ('domain_set', 'ip_set'):
            for filename in plugin['args'].get('files', []):
                path = root / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('domain:mixed.test\n' if plugin['type'] == 'domain_set'
                                else '10.0.0.0/8\n2001:db8:1::/48\n')
    runtime = root / 'runtime.yaml'
    runtime.write_text(yaml.safe_dump(document, sort_keys=False))
    check = subprocess.run([str(binary), 'check', '-c', str(runtime), '-d', str(root)],
                           capture_output=True, text=True, timeout=20)
    assert check.returncode == 0, check.stdout + check.stderr
    log_path = root / 'core.log'
    with log_path.open('w') as log:
        process = subprocess.Popen([str(binary), 'start', '-c', str(runtime), '-d', str(root)], stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                assert process.poll() is None, log_path.read_text()
                try:
                    query(port, 'ready.direct.test', timeout=.3)
                    break
                except OSError:
                    time.sleep(.03)
            else:
                raise AssertionError('temporary OxiDNS did not start: ' + log_path.read_text())
            yield port
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def measure(port, names, local, remote, *, qtype=1, workers=1, expected_ip=None):
    counts = local.count(), remote.count()
    started = time.perf_counter()
    def one(name):
        try:
            return query(port, name, qtype=qtype)
        except (OSError, AssertionError) as exc:
            return {'error': str(exc)}
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            replies = list(pool.map(one, names))
    else:
        replies = [one(name) for name in names]
    elapsed = time.perf_counter() - started
    durations = sorted(reply['elapsed_ms'] for reply in replies if 'elapsed_ms' in reply)
    failures = sum(bool(reply.get('error')) or reply.get('rcode') != 0 for reply in replies)
    return {'samples': len(names), 'median_ms': round(statistics.median(durations), 3) if durations else None,
            'p95_ms': round(durations[max(0, math.ceil(len(durations) * .95) - 1)], 3) if durations else None,
            'error_rate': failures / len(names),
            'answer_mismatch_rate': sum(reply.get('ips') != [expected_ip] for reply in replies) / len(names)
                                    if expected_ip else None,
            'requests_per_second': round(len(names) / elapsed, 2),
            'local_queries': local.count() - counts[0], 'proxy_queries': remote.count() - counts[1],
            'answers': dict(collections.Counter(','.join(reply.get('ips', [])) for reply in replies))}


def benchmark(binary, before, after, output, samples=100):
    def load_snapshot():
        return {'load_average': list(os.getloadavg()), 'logical_cpus': os.cpu_count(),
                'top_processes': subprocess.check_output(
                    ['ps', '-eo', 'pid,comm,pcpu,etime', '--sort=-pcpu'], text=True).splitlines()[:16]}
    report = {'environment': {'binary': str(binary), 'version': subprocess.check_output([str(binary), '--version'], text=True).strip(),
                             'local_delay_ms': 20, 'proxy_delay_ms': 40, 'samples': samples,
                             'load_before': load_snapshot(),
                             'scope': 'loopback controlled-delay benchmark; not public Internet latency'}, 'results': []}
    with tempfile.TemporaryDirectory(prefix='cf-dns-benchmark-') as temporary:
        for cache in (False, True):
            for revision, fixtures in (('before', before), ('after', after)):
                local, remote = DelayedDNS(True, 20), DelayedDNS(False, 40)
                try:
                    document = yaml.safe_load((fixtures / f'proxy-cache-{int(cache)}.yaml').read_text())
                    root = Path(temporary) / f'{revision}-{cache}'
                    with running_core(binary, document, root, local, remote) as port:
                        scenarios = [
                            ('unmatched_cold', [f'cold-{i}.unknown.test' for i in range(samples)], 1, 1),
                            ('unmatched_poisoned_local', [f'poison.{i}.unknown.test' for i in range(samples)], 1, 1),
                            ('matched_direct_cold', [f'cold-{i}.direct.test' for i in range(samples)], 1, 1),
                            ('matched_proxy_cold', [f'cold-{i}.proxy.test' for i in range(samples)], 1, 1),
                            ('unmatched_aaaa', [f'v6-{i}.unknown.test' for i in range(samples)], 28, 1),
                            ('unmatched_concurrent', [f'parallel-{i}.unknown.test' for i in range(samples)], 1, 16),
                        ]
                        if cache:
                            # Warm exactly the names measured below; cold scenarios above remain unique.
                            query(port, 'warm.unknown.test')
                            query(port, 'warm.direct.test')
                            scenarios += [('unmatched_probe_cache_warm', ['warm.unknown.test'] * samples, 1, 1),
                                          ('matched_direct_cache_warm', ['warm.direct.test'] * samples, 1, 1)]
                        for scenario, names, qtype, workers in scenarios:
                            expected = '10.1.2.3' if scenario.startswith('matched_direct') else (
                                '198.51.100.20' if qtype == 1 else '2001:db8:ffff::20')
                            result = measure(port, names, local, remote, qtype=qtype, workers=workers, expected_ip=expected)
                            report['results'].append({'revision': revision, 'cache': cache, 'scenario': scenario, **result})
                finally:
                    local.close()
                    remote.close()
    report['environment']['load_after'] = load_snapshot()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--export', type=Path)
    parser.add_argument('--generator-root', type=Path)
    parser.add_argument('--binary', type=Path)
    parser.add_argument('--before', type=Path)
    parser.add_argument('--after', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--samples', type=int, default=100)
    options = parser.parse_args()
    if options.export:
        export_fixtures(options.export, options.generator_root)
    else:
        if not all((options.binary, options.before, options.after, options.output)):
            parser.error('--binary, --before, --after and --output are required for a benchmark')
        benchmark(options.binary, options.before, options.after, options.output, options.samples)
