#!/usr/bin/env python3
"""Real OxiDNS + Go Agent acceptance, restricted to the isolated .32 preview.

Copy to ~/previews/configflow-node-dns-lab/runner.py on the test server, with
current Go Agent and OxiDNS binaries in bin/. Run `prepare`, then `test`.
For a separate empty upstream-selection instance, set
CONFIGFLOW_NODE_DNS_LAB_VARIANT=upstreams and use the fixed upstreams-check/lab
directories documented in docs/testing/node-test-dns-real-core.md. Its
backend-start action creates independent credentials and a loopback backend.
`stop` stops only PIDs whose recorded /proc start time and command still match.
No Docker daemon, native installer, system service, production DNS or proxy is
used. deployment_method=docker disables the Agent's native installer migration;
service_manager=command controls a real OxiDNS process in this private directory.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import http.server
import ipaddress
import json
import os
from pathlib import Path
import select
import secrets
import shutil
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

import requests
import yaml

HOST = '192.168.0.32'
VARIANT = os.environ.get('CONFIGFLOW_NODE_DNS_LAB_VARIANT', 'preview')
if VARIANT == 'preview':
    ROOT = Path.home() / 'previews/configflow-node-dns-lab'
    PREVIEW = Path.home() / 'previews/configflow-node-test-dns'
    API = 'http://127.0.0.1:15801'
    PROFILE = 'node-dns-lab'
    PORTS = (18081, 15354, 15353, 15355, 15356, 18082, 18083, 19199)
elif VARIANT == 'upstreams':
    ROOT = Path.home() / 'previews/configflow-node-dns-upstreams-lab'
    PREVIEW = Path.home() / 'previews/configflow-node-dns-upstreams-check'
    API = 'http://127.0.0.1:15901'
    PROFILE = 'node-dns-upstreams-lab'
    PORTS = (18091, 15364, 15363, 15365, 15366, 18092, 18093, 19209)
else:
    raise RuntimeError('Only the fixed preview/upstreams test-server variants are allowed')
AGENT, MAIN, MANAGED, REAL, FAKE, PROXY, TARGET, CORE_API = PORTS
SUBSCRIPTION = PROFILE + '-subscription'
LOCK = threading.Lock()


def guard():
    if sys.platform != 'linux' or not (ROOT / '.lab-marker').is_file():
        raise RuntimeError('Run only in the marked .32 preview laboratory')
    interfaces = json.loads(subprocess.check_output(['ip', '-j', '-4', 'addr', 'show'], text=True))
    addresses = [entry.get('local') for interface in interfaces for entry in interface.get('addr_info', [])]
    if HOST not in addresses:
        raise RuntimeError('This runner is limited to test server 192.168.0.32')


def private_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    path.chmod(0o600)


def event(kind, **values):
    with LOCK, (ROOT / 'network-events.jsonl').open('a') as stream:
        stream.write(json.dumps({'time': datetime.now(timezone.utc).isoformat(), 'kind': kind, **values}) + '\n')


def api(method, path, body=None, expected=(200, 201, 202)):
    with requests.Session() as session:
        session.trust_env = False
        response = session.request(method, API + path, json=body, timeout=(3, 180))
    if response.status_code not in expected:
        raise RuntimeError(f'{method} {path}: HTTP {response.status_code}: {response.text[:1200]}')
    return response.json() if response.content else {}


def process_record(pid):
    stat = Path(f'/proc/{pid}/stat').read_text().split()
    return {'pid': pid, 'start': stat[21], 'command': Path(f'/proc/{pid}/cmdline').read_bytes().decode().replace('\0', ' ')}


def alive(name):
    path = ROOT / (name + '.pid.json')
    if not path.exists():
        return False
    saved = json.loads(path.read_text())
    try:
        current = process_record(saved['pid'])
        state = Path(f'/proc/{saved["pid"]}/stat').read_text().split()[2]
        return current == saved and state != 'Z'
    except (OSError, ProcessLookupError):
        return False


def start(name, command, cwd=None):
    if alive(name):
        return
    environment = {k: v for k, v in os.environ.items() if k.lower() not in ('http_proxy', 'https_proxy', 'all_proxy')}
    with (ROOT / (name + '.log')).open('ab') as output:
        proc = subprocess.Popen(command, cwd=cwd or ROOT, stdin=subprocess.DEVNULL,
            stdout=output, stderr=output, start_new_session=True, env=environment)
    time.sleep(.15)
    if proc.poll() is not None:
        raise RuntimeError(f'{name} failed to start; inspect its private log')
    private_json(ROOT / (name + '.pid.json'), process_record(proc.pid))


def stop(name):
    if alive(name):
        pid = json.loads((ROOT / (name + '.pid.json')).read_text())['pid']
        os.kill(pid, signal.SIGTERM)
        for _ in range(100):
            if not alive(name):
                break
            time.sleep(.05)
        if alive(name):
            os.kill(pid, signal.SIGKILL)
    (ROOT / (name + '.pid.json')).unlink(missing_ok=True)


def question(data):
    offset, labels = 12, []
    while data[offset]:
        size = data[offset]
        if size > 63:
            raise ValueError('compressed question unsupported by fixture')
        labels.append(data[offset + 1:offset + 1 + size].decode())
        offset += size + 1
    return '.'.join(labels), offset + 1


class DNS(socketserver.BaseRequestHandler):
    def handle(self):
        data, stream = self.request
        name, end = question(data)
        qtype, _ = struct.unpack('!HH', data[end:end + 4])
        real = self.server.server_address[1] == REAL
        # No external lookups: fixtures answer only the two test names.
        supported = name.lower().rstrip('.') in ('proxy.lab.invalid', 'example.com')
        answer = b''
        ip = HOST if real else '198.18.0.99'
        if supported and qtype == 1:
            answer = b'\xc0\x0c' + struct.pack('!HHIH', 1, 1, 1, 4) + socket.inet_aton(ip)
        response = data[:2] + struct.pack('!5H', 0x8180 if supported else 0x8183, 1, bool(answer), 0, 0)
        stream.sendto(response + data[12:end + 4] + answer, self.client_address)
        event('dns-real' if real else 'dns-fake', name=name, qtype=qtype, answer=ip if answer else None)


class Target(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def do_HEAD(self):
        if self.path != '/generate_204':
            self.send_error(404)
            return
        event('origin-204', client=self.client_address[0], method='HEAD', status=204)
        self.send_response(204); self.send_header('Content-Length', '0'); self.end_headers()

    def do_GET(self):
        if self.path == '/subscription.yaml':
            body = yaml.safe_dump({'proxies': [{'name': 'LAB subscription HTTP', 'type': 'http',
                'server': 'proxy.lab.invalid', 'port': PROXY}]}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/yaml')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers(); self.wfile.write(body)
        elif self.path == '/generate_204':
            event('origin-204', client=self.client_address[0], method='GET', status=204)
            self.send_response(204); self.send_header('Content-Length', '0'); self.end_headers()
        else:
            self.send_error(404)

    def log_message(self, *_):
        pass


class Proxy(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(8)
        data = b''
        while b'\r\n\r\n' not in data and len(data) < 16384:
            block = self.request.recv(4096)
            if not block:
                return
            data += block
        first = data.split(b'\r\n')[0].decode()
        method, target, version = first.split(' ', 2)
        authority = target if method == 'CONNECT' else urlsplit(target).netloc
        if authority != f'127.0.0.1:{TARGET}':
            self.request.sendall(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n')
            return
        with socket.create_connection(('127.0.0.1', TARGET), timeout=3) as upstream:
            event('proxy-connect', target=authority, method=method)
            if method == 'CONNECT':
                self.request.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            else:
                path = urlsplit(target).path or '/'
                upstream.sendall(data.replace(first.encode(), f'{method} {path} {version}'.encode(), 1))
            for stream in (self.request, upstream):
                stream.setblocking(False)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                ready, _, _ = select.select([self.request, upstream], [], [], .5)
                for source in ready:
                    block = source.recv(65536)
                    if not block:
                        return
                    destination = upstream if source is self.request else self.request
                    destination.setblocking(True); destination.sendall(block); destination.setblocking(False)


class TCP(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def fixtures():
    servers = [socketserver.ThreadingUDPServer(('127.0.0.1', port), DNS) for port in (REAL, FAKE)]
    servers += [TCP((HOST, PROXY), Proxy), http.server.ThreadingHTTPServer(('127.0.0.1', TARGET), Target)]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    while True:
        time.sleep(10)


def dns_query(port, *, host=HOST, tcp=False):
    name = 'proxy.lab.invalid'
    encoded = b''.join(bytes([len(s)]) + s.encode() for s in name.split('.')) + b'\0'
    data = struct.pack('!6H', 31415, 256, 1, 0, 0, 0) + encoded + struct.pack('!HH', 1, 1)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM if tcp else socket.SOCK_DGRAM) as stream:
        stream.settimeout(2)
        if tcp:
            stream.connect((host, port)); stream.sendall(struct.pack('!H', len(data)) + data)
            size = struct.unpack('!H', stream.recv(2))[0]
            response = b''
            while len(response) < size:
                response += stream.recv(size - len(response))
        else:
            stream.sendto(data, (host, port)); response = stream.recv(4096)
    assert struct.unpack('!H', response[6:8])[0] == 1, 'expected one A answer'
    return socket.inet_ntoa(response[-4:])


def base_document():
    return {'runtime': {'worker_threads': 2}, 'log': {'level': 'info'},
        'api': {'http': {'listen': f'127.0.0.1:{CORE_API}'}}, 'plugins': [
        {'tag': 'kept', 'type': 'domain_set', 'args': {'files': ['rules/kept.txt']}},
        {'tag': 'lab_fake', 'type': 'forward', 'args': {'upstreams': [{'addr': f'udp://127.0.0.1:{FAKE}'}]}},
        {'tag': 'lab_real_forward', 'type': 'forward', 'args': {'concurrent': 1, 'upstreams': [
            {'addr': f'udp://127.0.0.1:{REAL}', 'timeout': '2s', 'idle_timeout': '5s'}]}},
        {'tag': 'main', 'type': 'sequence', 'args': [{'exec': '$lab_fake'}, {'exec': 'accept'}]},
        *[{'tag': 'server_' + protocol, 'type': protocol + '_server', 'args': {
            'listen': f'0.0.0.0:{MAIN}', 'entry': 'main'}} for protocol in ('udp', 'tcp')]]}


def prepare():
    if (ROOT / 'identity.json').exists():
        raise RuntimeError('Lab already prepared; use test or explicit stop, never overwrite identities')
    for port in PORTS:
        for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
            with socket.socket(socket.AF_INET, kind) as stream:
                stream.bind(('0.0.0.0', port))
    (ROOT / 'core/rules').mkdir(parents=True, exist_ok=True)
    (ROOT / 'core/rules/kept.txt').write_text('full:unchanged.lab.invalid\n')
    (ROOT / 'core/config.yaml').write_text(yaml.safe_dump(base_document(), sort_keys=False))
    api('POST', '/api/profiles', {'id': PROFILE, 'name': 'Node DNS laboratory'})
    credentials = json.loads((PREVIEW / 'credentials.json').read_text())
    registration = api('POST', '/api/agents/register', {'name': 'LAB real OxiDNS 1.6.1', 'host': HOST,
        'port': AGENT, 'service_type': 'oxidns', 'deployment_method': 'docker', 'profile_id': PROFILE,
        'version': '1.8.46-go', 'registration_key': credentials['registration_key']})
    ident = registration.get('data', registration)
    agent_id, token = ident['id'], ident['token']
    private_json(ROOT / 'identity.json', {'agent_id': agent_id, 'profile_id': PROFILE})
    runner = str(ROOT / 'runner.py')
    python = sys.executable
    command = f"env CONFIGFLOW_NODE_DNS_LAB_VARIANT={VARIANT} '{python}' '{runner}'"
    private_json(ROOT / 'agent.json', {'agent_id': agent_id, 'token': token, 'server_url': API,
        'agent_name': 'LAB real OxiDNS 1.6.1', 'agent_host': HOST, 'agent_ip': HOST, 'agent_port': AGENT,
        'service_type': 'oxidns', 'deployment_method': 'docker', 'service_name': 'lab-oxidns',
        'service_manager': 'command', 'service_binary': str(ROOT / 'bin/oxidns'),
        'config_path': str(ROOT / 'core/config.yaml'), 'start_command': command + ' core-start',
        'stop_command': command + ' core-stop', 'status_command': command + ' core-status',
        'heartbeat_interval': 15, 'enable_metrics': False, 'health_dns_address': f'127.0.0.1:{MAIN}',
        'deployment_health_timeout': 20})
    start('fixtures', [python, runner, 'fixtures'])
    core_start()
    start('agent', [str(ROOT / 'bin/configflow-agent'), '-config', str(ROOT / 'agent.json')])
    time.sleep(1)
    api('POST', f'/api/profiles/{PROFILE}/oxidns/settings', {'config_mode': 'native',
        'native_config': yaml.safe_dump(base_document(), sort_keys=False), 'additional_files': [
        {'path': 'rules/kept.txt', 'encoding': 'text', 'content': 'full:unchanged.lab.invalid\n'}]})
    api('POST', '/api/subscriptions', {'id': SUBSCRIPTION, 'name': 'LAB isolated HTTP subscription',
        'url': f'http://127.0.0.1:{TARGET}/subscription.yaml', 'enabled': True, 'interval': 0})
    api('POST', f'/api/subscriptions/{SUBSCRIPTION}/fetch', {'preview': True})
    print(json.dumps({'prepared': True, 'agent_id': agent_id, 'profile_id': PROFILE}))


def core_start():
    start('core', [str(ROOT / 'bin/oxidns'), 'start', '-c', str(ROOT / 'core/config.yaml'), '-d', str(ROOT / 'core')], ROOT / 'core')


def backend_serve():
    """Empty secondary instance: never load the shared preview's data/secrets."""
    if VARIANT != 'upstreams' or not (PREVIEW / 'source/backend/app.py').is_file():
        raise RuntimeError('Backend helper is only for the separately copied upstreams laboratory')
    credentials_path = PREVIEW / 'credentials.json'
    if not credentials_path.exists():
        private_json(credentials_path, {'registration_key': secrets.token_urlsafe(40),
                                      'jwt_secret': secrets.token_urlsafe(48)})
    credentials = json.loads(credentials_path.read_text())
    (PREVIEW / 'data').mkdir(mode=0o700, exist_ok=True)
    os.environ.update({'DATA_DIR': str(PREVIEW / 'data'), 'LOG_FILE': str(PREVIEW / 'backend.log'),
        'AGENT_REGISTRATION_KEY': credentials['registration_key'], 'JWT_SECRET_KEY': credentials['jwt_secret'],
        'CONFIGFLOW_NODE_PROBE_BIN': str(PREVIEW / 'bin/node-probe'),
        'CONFIGFLOW_NODE_TEST_URL': f'http://127.0.0.1:{TARGET}/generate_204'})
    for key in ('ADMIN_USERNAME', 'ADMIN_PASSWORD'):
        os.environ.pop(key, None)
    sys.path.insert(0, str(PREVIEW / 'source'))
    from backend.common import config
    from backend.common.config_repository import ProfileRepository
    repository = ProfileRepository(PREVIEW / 'data', default_config_factory=config.get_default_config,
                                   initial_config_factory=config.get_default_config)
    config.set_repository(repository)
    from backend.app import app
    from backend.utils.local_http import register_application
    register_application(app)
    from waitress import serve
    serve(app, host='127.0.0.1', port=15901, threads=8, channel_timeout=300)


def backend_start():
    if VARIANT != 'upstreams':
        raise RuntimeError('The shared preview backend is managed separately')
    with socket.socket() as stream:
        stream.bind(('127.0.0.1', 15901))
    start('backend', [sys.executable, str(ROOT / 'runner.py'), 'backend-serve'], PREVIEW)
    for _ in range(80):
        try:
            api('GET', '/api/nodes/latency/settings')
            print('Independent backend ready at ' + API, flush=True)
            return
        except requests.RequestException:
            time.sleep(.25)
    raise RuntimeError('Independent backend failed to become ready; inspect private backend.log')


def poll(path, ready, seconds=90):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = api('GET', path)
        if value.get('status') in ('failed', 'rolled_back', 'rollback_failed'):
            raise RuntimeError('Operation failed: ' + json.dumps(value, ensure_ascii=False)[:2500])
        if value.get('status') == ready:
            return value
        time.sleep(.4)
    raise RuntimeError('Operation did not converge: ' + path)


def test():
    identity = json.loads((ROOT / 'identity.json').read_text())
    agent_id = identity['agent_id']
    # Repeat only our recorded resources, never enumerate/delete arbitrary
    # preview listeners or another Agent's user-created endpoint.
    owned_ids = set(identity.get('owned_endpoint_ids', []))
    if (ROOT / 'report.json').exists():
        previous = json.loads((ROOT / 'report.json').read_text())
        for step in previous.get('steps', []):
            binding = step.get('binding', {})
            if binding.get('agent_id') == agent_id:
                owned_ids.add(binding['endpoint_id'])
    # This preview may now contain changes made by the person reviewing it.
    # Before *any* unbind/delete/publication, refuse a non-fixture draft rather
    # than treating the shared preview as disposable after it has been edited.
    existing = yaml.safe_load(api('GET', f'/api/profiles/{PROFILE}/oxidns/settings')['native_config'])
    original = base_document()
    owned_tags = {'cf_node_test_' + ident + '_' + suffix for ident in owned_ids
                  for suffix in ('forward', 'sequence', 'udp', 'tcp')}
    remaining = {p['tag']: p for p in existing.get('plugins', []) if p.get('tag') not in owned_tags}
    expected = {p['tag']: p for p in original['plugins']}
    if 'lab_real_forward' not in remaining:
        expected.pop('lab_real_forward')
    assert remaining == expected and {k: v for k, v in existing.items() if k != 'plugins'} == {
        k: v for k, v in original.items() if k != 'plugins'}, 'Preview configuration was edited; refusing to alter user drafts or bindings'
    selected = api('GET', '/api/nodes/latency/settings')['dns']
    if selected.get('mode') == 'managed' and selected.get('managed', {}).get('endpoint_id') in owned_ids:
        api('POST', '/api/nodes/latency/settings', {'dns': {'mode': 'custom', 'servers': [f'{HOST}:{MAIN}']}})
    for endpoint_id in owned_ids:
        path = f'/api/nodes/latency/dns/managed/{agent_id}/{endpoint_id}'
        state = api('GET', path)
        if state['status'] != 'removed':
            assert state['endpoint'] == f'{HOST}:{MANAGED}' and state['upstream'] == f'udp://127.0.0.1:{REAL}'
            api('DELETE', path); poll(path, 'removed')
    with socket.socket() as stream:
        stream.settimeout(1)
        assert stream.connect_ex((HOST, MANAGED)) != 0, 'managed port must be absent before first create'
    # Older runner versions fetched without preview and created a manual copy.
    # Only this lab subscription's exact duplicate is eligible for cleanup.
    for node in api('GET', '/api/nodes'):
        if node.get('subscription_id') == SUBSCRIPTION and node.get('name') == 'LAB subscription HTTP':
            api('DELETE', '/api/nodes/' + node['id'])
    report = {'time': datetime.now(timezone.utc).isoformat(), 'variant': VARIANT, 'api_url': API,
        'isolation': 'Test server only; real OxiDNS + Go Agent command process harness. Docker metadata only disables native migration. No Docker daemon or OxiDNS/Agent system units; the separate preview uses task-specific backend/web transient units. No existing services, production DNS or subscription credentials are used.',
        'oxidns_sha256': hashlib.sha256((ROOT / 'bin/oxidns').read_bytes()).hexdigest(), 'steps': []}
    if VARIANT == 'upstreams':
        report['isolation'] = 'Independent empty loopback backend and separate real OxiDNS/Go Agent process harness on the test server. No Docker daemon/system units, existing preview data, production DNS or subscription credentials are used.'
    def record(name, **evidence):
        report['steps'].append({'name': name, 'passed': True, **evidence})
        private_json(ROOT / 'report.json', report)
        print(name, flush=True)
    def publish():
        value = api('POST', f'/api/agents/{agent_id}/push-config', {})
        deploy_id = value.get('deployment_id') or value.get('deployment', {}).get('deployment_id')
        if value.get('status') != 'succeeded':
            value = poll(f'/api/agents/{agent_id}/deployments/{deploy_id}', 'succeeded')
        return value
    def save(value):
        return api('POST', '/api/nodes/latency/settings', {'dns': value})
    def measure(success):
        def counts():
            events = [json.loads(line) for line in (ROOT / 'network-events.jsonl').read_text().splitlines()]
            return (sum(e['kind'] == 'proxy-connect' for e in events),
                    sum(e['kind'] == 'origin-204' and e.get('method') == 'HEAD' and e.get('status') == 204 for e in events))
        before_proxy, before_origin = counts()
        key = json.dumps(['subscription', SUBSCRIPTION, 'LAB subscription HTTP'], separators=(',', ':'))
        result = api('POST', '/api/nodes/latency', {'keys': [key]})
        items = list(result.get('results', {}).values())
        assert len(items) == 1, f'expected one isolated subscription node: {result}'
        if success:
            assert items[0].get('latency') is not None, items
            after_proxy, after_origin = counts()
            assert after_proxy > before_proxy and after_origin >= before_origin + 2, 'missing real proxy/HEAD 204 evidence'
        else:
            assert items[0].get('error_code') == 'dns_fake_ip', items
        return {**{key: items[0].get(key) for key in ('latency', 'error_code')},
                **({'origin_head_204_count': after_origin - before_origin, 'proxy_connections': after_proxy - before_proxy} if success else {})}
    def create():
        agent = next(a for a in api('GET', '/api/nodes/latency/dns/managed')['agents'] if a['id'] == agent_id)
        candidates = [u for u in agent['upstreams'] if u['eligible'] and u['address'] == f'udp://127.0.0.1:{REAL}']
        assert len(candidates) == 1 and candidates[0]['validation'] == 'unverified'
        value = api('POST', '/api/nodes/latency/dns/managed', {'agent_id': agent_id,
            'listen_address': HOST, 'port': MANAGED, 'upstream_id': candidates[0]['id']})
        endpoint_id = value['endpoint_id']
        owned_ids.add(endpoint_id)
        identity['owned_endpoint_ids'] = sorted(owned_ids)
        private_json(ROOT / 'identity.json', identity)
        path = f'/api/nodes/latency/dns/managed/{agent_id}/{endpoint_id}'
        value = poll(path, 'ready')
        return {'agent_id': agent_id, 'endpoint_id': endpoint_id}, path, value
    settings_path = f'/api/profiles/{PROFILE}/oxidns/settings'
    settings = api('GET', settings_path)
    initial = yaml.safe_load(settings['native_config'])
    if not any(p.get('tag') == 'lab_real_forward' for p in initial['plugins']):
        initial['plugins'].append(next(p for p in base_document()['plugins'] if p['tag'] == 'lab_real_forward'))
        settings['native_config'] = yaml.safe_dump(initial, sort_keys=False)
        api('POST', settings_path, settings)
    publish()
    rule_hash = hashlib.sha256((ROOT / 'core/rules/kept.txt').read_bytes()).hexdigest()
    record('baseline-published', main_answer=dns_query(MAIN), rule_sha256=rule_hash)
    save({'mode': 'custom', 'servers': [f'{HOST}:{MAIN}']})
    record('fake-ip-reproduced', **measure(False))
    before = api('GET', '/api/nodes/latency/settings')['dns']
    catalog = next(a for a in api('GET', '/api/nodes/latency/dns/managed')['agents'] if a['id'] == agent_id)['upstreams']
    draft = api('GET', settings_path)
    document = yaml.safe_load(draft['native_config'])
    document['plugins'].append({'tag': 'lab_pending_draft', 'type': 'forward',
        'args': {'upstreams': [{'addr': f'udp://127.0.0.1:{FAKE}'}]}})
    draft['native_config'] = yaml.safe_dump(document, sort_keys=False)
    api('POST', settings_path, draft)
    assert next(a for a in api('GET', '/api/nodes/latency/dns/managed')['agents'] if a['id'] == agent_id)['upstreams'] == catalog
    binding, path, ready = create()
    assert api('GET', '/api/nodes/latency/settings')['dns'] == before
    assert dns_query(MANAGED) == HOST and dns_query(MANAGED, tcp=True) == HOST
    record('created-ready-without-changing-selection', endpoint=ready['endpoint'], udp_answer=HOST, tcp_answer=HOST)
    live = yaml.safe_load((ROOT / 'core/config.yaml').read_text())
    assert not any(p.get('tag') == 'lab_pending_draft' for p in live['plugins'])
    expected_args = next(p['args'] for p in live['plugins'] if p.get('tag') == 'lab_real_forward')
    managed_args = next(p['args'] for p in live['plugins'] if p.get('tag') == 'cf_node_test_' + binding['endpoint_id'] + '_forward')
    assert managed_args == expected_args
    draft = api('GET', settings_path)
    document = yaml.safe_load(draft['native_config'])
    assert any(p.get('tag') == 'lab_pending_draft' for p in document['plugins'])
    record('existing-published-upstream-copied-with-pending-draft-preserved', candidate_count=len(catalog),
        source='lab_real_forward', source_parameters_preserved=True, unpublished_forward_excluded=True)
    # Explicitly discard only this runner's pending-draft sentinel before the
    # normal full publication; the provisioner itself preserved it untouched.
    document['plugins'] = [p for p in document['plugins'] if p.get('tag') != 'lab_pending_draft']
    draft['native_config'] = yaml.safe_dump(document, sort_keys=False)
    api('POST', settings_path, draft)
    save({'mode': 'managed', 'managed': binding})
    record('explicit-selection-and-real-subscription-speed-test', **measure(True))
    publish()
    assert dns_query(MANAGED) == HOST and dns_query(MAIN) == '198.18.0.99'
    assert hashlib.sha256((ROOT / 'core/rules/kept.txt').read_bytes()).hexdigest() == rule_hash
    owned = [p for p in yaml.safe_load((ROOT / 'core/config.yaml').read_text())['plugins'] if p.get('tag', '').startswith('cf_node_test_')]
    assert len(owned) == 4
    record('normal-republication-preserved-endpoint-and-rule-bytes', plugin_count=len(owned), rule_sha256=rule_hash, **measure(True))
    rejected = api('DELETE', path, expected=(400, 409))
    assert rejected['success'] is False and dns_query(MANAGED) == HOST
    record('in-use-delete-rejected')
    save({'mode': 'custom', 'servers': [f'{HOST}:{MAIN}']})
    api('DELETE', path)
    poll(path, 'removed')
    with socket.socket() as stream:
        stream.settimeout(1)
        assert stream.connect_ex((HOST, MANAGED)) != 0
    try:
        dns_query(MANAGED)
    except OSError:
        pass
    else:
        raise AssertionError('deleted UDP listener still answers')
    removed_prefix = 'cf_node_test_' + binding['endpoint_id']
    assert not any(plugin.get('tag', '').startswith(removed_prefix)
                   for plugin in yaml.safe_load((ROOT / 'core/config.yaml').read_text())['plugins'])
    assert dns_query(MAIN) == '198.18.0.99'
    assert hashlib.sha256((ROOT / 'core/rules/kept.txt').read_bytes()).hexdigest() == rule_hash
    record('deleted-port-closed-main-dns-and-rules-preserved', tcp_closed=True, udp_closed=True, owned_plugins_absent=True)
    binding, path, ready = create()
    save({'mode': 'managed', 'managed': binding})
    record('recreated-ready-for-preview', binding=binding, endpoint=ready['endpoint'], **measure(True))
    report['completed'] = True
    private_json(ROOT / 'report.json', report)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'test', 'fixtures', 'core-start', 'core-stop', 'core-status', 'stop',
                                         'backend-start', 'backend-serve'))
    args = parser.parse_args()
    guard()
    if args.action == 'core-status':
        raise SystemExit(0 if alive('core') else 3)
    if args.action == 'core-stop':
        stop('core')
    elif args.action == 'stop':
        for name in ('agent', 'core', 'fixtures'):
            stop(name)
        if VARIANT == 'upstreams':
            stop('backend')
    else:
        {'prepare': prepare, 'test': test, 'fixtures': fixtures, 'core-start': core_start,
         'backend-start': backend_start, 'backend-serve': backend_serve}[args.action]()
