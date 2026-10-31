#!/usr/bin/env python3
"""Subscription hosts on the isolated PVE lab (VM151 router, CT152 Mihomo, CT153 client, CT154 node).

Run on the test server only, from a checkout synced by rt. Never touches other
guests; VM151 is used as it is (routing only). Everything the run changes is
backed up first and restored by ``restore``:

  backup        tar of CT152 /etc/mihomo + /opt/configflow-agent (deployment journal,
                cache.db), file manifest, services, addresses, net sysctls; CT153/CT154 network
  fixture-up    CT154: extra lab addresses + DNS/SOCKS/HTTP fixture (systemd-run, runtime only)
  source V REF  unpack a git archive (before/after) into the private work directory
  seed          fresh private ConfigFlow data: lab subscription, group, rule, CT152 Agent
  serve V       private ConfigFlow (127.0.0.1:15951) + SSH tunnels, background
  stop          stop the private ConfigFlow and tunnels
  sub VERSION   serve fixture subscription v1/v2 at 127.0.0.1:15953
  refresh       update the subscription now (同 订阅来源 → 更新)
  publish LABEL push to CT152 through the Agent and wait for the result
  measure LABEL client CT153 → Mihomo → each node; record which entry address was dialed
  raw LABEL     the raw subscription loaded by a separate Mihomo on CT152 (ports 17890/19090)
  restore       put everything back and compare with the backup

The fixture subscription only uses .invalid names and documentation addresses.
"""
import base64
import hashlib
import io
import json
import os
import re
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ipv6_readiness_lab as lab  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
WORK = Path('/tmp/cf-subhosts-lab')
API = 15951
TUNNEL_API = 15952          # 10.242.150.254:15952 on PVE -> 127.0.0.1:15951 (provider refreshes)
SUB = 15953
AGENT_LOCAL = 15954         # 127.0.0.1:15954 -> CT152 Agent 10.242.150.2:27647
SUB_ID = 'lab-airport'
GROUP = '代理'
NODES = {'A': 'A 共享入口', 'B': 'B 指定入口'}
LEFT_A, RIGHT_A = 'edge-a.shared.lab.invalid', 'entry-a.dedicated.lab.invalid'
LEFT_B = 'edge-b.shared.lab.invalid'
TARGET = 'target.lab.invalid'
EXTRA = ['198.51.100.11', '198.51.100.12', '198.51.100.13', '198.51.100.20']
FIXTURE_DIR = '/tmp/cf-subhosts'
FIXTURE_UNIT = 'cf-subhosts-fixture'

FIXTURE = r'''
import http.server, json, select, socket, socketserver, struct, threading, time
from urllib.parse import urlsplit
LOG = '/tmp/cf-subhosts/requests.jsonl'
ANSWERS = {'edge-a.shared.lab.invalid': '198.51.100.11', 'entry-a.dedicated.lab.invalid': '198.51.100.12',
           'edge-b.shared.lab.invalid': '198.51.100.11', 'target.lab.invalid': '198.51.100.20'}
lock = threading.Lock()
def event(**v):
    v['at'] = time.time()
    with lock, open(LOG, 'a') as f:
        f.write(json.dumps(v) + '\n')
class DNS(socketserver.BaseRequestHandler):
    def handle(self):
        data, sock = self.request
        p, labels = 12, []
        while p < len(data) and data[p]:
            labels.append(data[p + 1:p + 1 + data[p]].decode(errors='replace')); p += data[p] + 1
        if p + 5 > len(data):
            return
        name = '.'.join(labels).lower(); typ = struct.unpack('!H', data[p + 1:p + 3])[0]; q = data[12:p + 5]
        ip = ANSWERS.get(name) if typ == 1 else None
        event(kind='dns', name=name, type=typ, client=self.client_address[0])
        ans = b'\xc0\x0c' + struct.pack('!HHIH', 1, 1, 5, 4) + socket.inet_aton(ip) if ip else b''
        flags = 0x8180 if name in ANSWERS else 0x8183
        sock.sendto(data[:2] + struct.pack('!HHHHH', flags, 1, 1 if ans else 0, 0, 0) + q + ans, self.client_address)
def exact(c, n):
    b = b''
    while len(b) < n:
        part = c.recv(n - len(b))
        if not part:
            raise EOFError()
        b += part
    return b
class Socks(socketserver.BaseRequestHandler):
    def handle(self):
        c = self.request; c.settimeout(10); up = None
        try:
            _, n = exact(c, 2); exact(c, n); c.sendall(b'\x05\x00')
            _, cmd, _, kind = exact(c, 4)
            host = (socket.inet_ntoa(exact(c, 4)) if kind == 1 else exact(c, exact(c, 1)[0]).decode() if kind == 3
                    else socket.inet_ntop(socket.AF_INET6, exact(c, 16)))
            port = struct.unpack('!H', exact(c, 2))[0]
            event(kind='socks', server=c.getsockname()[0], client=self.client_address[0], target=f'{host}:{port}')
            if host not in ('target.lab.invalid', '198.51.100.20') or port != 18080 or cmd != 1:
                raise ValueError('outside fixture')
            up = socket.create_connection(('198.51.100.20', 18080), 3)
            c.sendall(b'\x05\x00\x00\x01' + socket.inet_aton('198.51.100.20') + b'\x46\xa0')
            while True:
                ready, _, _ = select.select([c, up], [], [], 10)
                if not ready:
                    return
                for src in ready:
                    data = src.recv(65536)
                    if not data:
                        return
                    (up if src is c else c).sendall(data)
        except Exception as e:
            event(kind='socks_error', error=str(e))
        finally:
            if up:
                up.close()
class HTTP(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def do_GET(self):
        hold = 'hold' in urlsplit(self.path).query
        body = json.dumps({'path': self.path, 'origin': 'cf-subhosts-fixture'}).encode()
        event(kind='http', path=self.path, client=self.client_address[0])
        time.sleep(3 if hold else 0)
        self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *a):
        pass
class TS(socketserver.ThreadingTCPServer):
    allow_reuse_address = True; daemon_threads = True
servers = [socketserver.ThreadingUDPServer(('198.51.100.20', 53), DNS), http.server.ThreadingHTTPServer(('198.51.100.20', 18080), HTTP)]
servers += [TS((f'198.51.100.{i}', 1080), Socks) for i in (11, 12, 13)]
for s in servers:
    threading.Thread(target=s.serve_forever, daemon=True).start()
while True:
    time.sleep(60)
'''

CUSTOM = '''mixed-port: 7890
allow-lan: true
bind-address: '*'
mode: rule
log-level: info
ipv6: false
external-controller: 127.0.0.1:9090
dns:
  enable: true
  listen: 127.0.0.1:1053
  enhanced-mode: redir-host
  default-nameserver: [198.51.100.20]
  nameserver: [198.51.100.20]
'''

SUBSCRIPTIONS = {
    # The raw provider config: a top-level hosts section next to proxies.
    'v1': f'''mixed-port: 7890
proxies:
  - {{name: {NODES['A']}, type: socks5, server: {LEFT_A}, port: 1080}}
  - {{name: {NODES['B']}, type: socks5, server: {LEFT_B}, port: 1080}}
proxy-groups:
  - {{name: {GROUP}, type: select, proxies: [{NODES['A']}, {NODES['B']}]}}
hosts:
  {LEFT_A}: {RIGHT_A}
  {LEFT_B}: 198.51.100.13
  {TARGET}: 198.51.100.66
  "+.lab.invalid": 198.51.100.66
rules:
  - MATCH,{GROUP}
''',
}
SUBSCRIPTIONS['v2'] = SUBSCRIPTIONS['v1'].replace(f'{LEFT_B}: 198.51.100.13', f'{LEFT_B}: 198.51.100.12')


def save(name, value):
    path = WORK / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    path.chmod(0o600)
    return path


def guest(cid, *args, **kwargs):
    return lab.guest(cid, *args, **kwargs)


def guest_json(cid, script, *args, data=None, timeout=120):
    return json.loads(guest(cid, 'python3', '-c', script, *args, data=data, timeout=timeout).stdout)


MANIFEST = r'''import hashlib, json, os, sys
out = {}
for base in sys.argv[1:]:
    for root, dirs, files in os.walk(base):
        for name in sorted(dirs + files):
            path = os.path.join(root, name)
            st = os.lstat(path)
            entry = {'mode': st.st_mode, 'uid': st.st_uid, 'gid': st.st_gid, 'mtime_ns': st.st_mtime_ns}
            if os.path.isfile(path) and not os.path.islink(path):
                entry['sha256'] = hashlib.sha256(open(path, 'rb').read()).hexdigest()
                entry['size'] = st.st_size
            out[path] = entry
print(json.dumps(out, sort_keys=True))
'''

STATE = r'''import json, subprocess, urllib.request
def run(*a):
    return subprocess.run(a, capture_output=True, text=True).stdout
sysctl = dict(line.split(' = ', 1) for line in run('sysctl', '-a').splitlines() if line.startswith('net.') and ' = ' in line)
try:
    configs = json.load(urllib.request.urlopen('http://127.0.0.1:9090/configs', timeout=3))
except Exception as e:
    configs = {'error': str(e)}
print(json.dumps({'services': {u: run('systemctl', 'is-active', u).strip() for u in ('mihomo', 'configflow-agent')},
                  'addr': json.loads(run('ip', '-j', 'addr')), 'route': sorted(run('ip', 'route', 'show', 'table', 'all').splitlines()),
                  'route6': sorted(run('ip', '-6', 'route', 'show', 'table', 'all').splitlines()),
                  'sysctl': sysctl, 'configs': configs}, sort_keys=True))
'''

VOLATILE_SYSCTL = ('net.netfilter.nf_conntrack_count',)


def ct152_files():
    return guest_json(152, MANIFEST, '/etc/mihomo', '/opt/configflow-agent')


def network(cid):
    return {'addr': json.loads(guest(cid, 'ip', '-j', 'addr').stdout),
            'route': sorted(guest(cid, 'ip', 'route', 'show', 'table', 'all').stdout.decode().splitlines())}


def backup():
    lab.guard()
    WORK.mkdir(mode=0o700, exist_ok=True)
    if (WORK / 'backup/complete.json').exists():
        raise SystemExit('Backup already exists; refusing to overwrite')
    (WORK / 'backup').mkdir(mode=0o700, exist_ok=True)
    archive = guest(152, 'tar', '-C', '/', '--numeric-owner', '-cpf', '-', 'etc/mihomo', 'opt/configflow-agent',
                    timeout=180).stdout
    (WORK / 'backup/ct152.tar').write_bytes(archive)
    (WORK / 'backup/ct152.tar').chmod(0o600)
    files = ct152_files()
    save('backup/ct152-files.json', files)
    save('backup/ct152-state.json', guest_json(152, STATE))
    save('backup/ct153-network.json', network(153))
    save('backup/ct154-network.json', network(154))
    save('backup/ct154-units.json', guest(154, 'systemctl', 'list-units', '--all', '--no-legend', 'cf-*',
                                          check=False).stdout.decode().splitlines())
    names = sorted(path for path in files if path.startswith('/etc/mihomo'))
    save('backup/complete.json', {'at': time.time(), 'tar_sha256': hashlib.sha256(archive).hexdigest(),
                                  'files': len(files), 'etc_mihomo': names})
    print(json.dumps({'backup': str(WORK / 'backup'), 'files': len(files), 'tar_bytes': len(archive)}, ensure_ascii=False))


def fixture_up():
    lab.guard()
    assert (WORK / 'backup/complete.json').exists(), 'backup first'
    present = guest(154, 'ip', '-4', '-o', 'addr', 'show', 'dev', 'eth1').stdout.decode()
    for address in EXTRA:
        if f' {address}/' not in present:
            guest(154, 'ip', 'addr', 'add', f'{address}/24', 'dev', 'eth1')
    guest(154, 'mkdir', '-p', FIXTURE_DIR)
    lab.write_remote(f'{FIXTURE_DIR}/fixture.py', FIXTURE, cid=154)
    guest(154, 'systemctl', 'reset-failed', FIXTURE_UNIT, check=False)
    if guest(154, 'systemctl', 'is-active', FIXTURE_UNIT, check=False).stdout.strip() != b'active':
        guest(154, 'systemd-run', f'--unit={FIXTURE_UNIT}', '--collect', 'python3', f'{FIXTURE_DIR}/fixture.py')
    time.sleep(1)
    print(json.dumps({'ct154_addresses': guest(154, 'ip', '-4', '-br', 'addr', 'show', 'dev', 'eth1').stdout.decode().strip(),
                      'fixture': guest(154, 'systemctl', 'is-active', FIXTURE_UNIT, check=False).stdout.decode().strip()}))


def fixture_down():
    guest(154, 'systemctl', 'stop', FIXTURE_UNIT, check=False)
    guest(154, 'systemctl', 'reset-failed', FIXTURE_UNIT, check=False)
    log = guest(154, 'cat', f'{FIXTURE_DIR}/requests.jsonl', check=False).stdout
    if log:
        (WORK / 'evidence').mkdir(exist_ok=True)
        (WORK / 'evidence/fixture-requests.jsonl').write_bytes(log)
    guest(154, 'rm', '-rf', FIXTURE_DIR)
    present = guest(154, 'ip', '-4', '-o', 'addr', 'show', 'dev', 'eth1').stdout.decode()
    for address in EXTRA:
        if f' {address}/' in present:
            guest(154, 'ip', 'addr', 'del', f'{address}/24', 'dev', 'eth1')


def source(variant, ref):
    target = WORK / f'src-{variant}'
    archive = WORK / f'src-{variant}.tar'
    assert archive.exists(), f'copy {archive} first (git archive {ref})'
    if target.exists():
        subprocess.run(['rm', '-rf', str(target)], check=True)
    target.mkdir(parents=True)
    with tarfile.open(archive) as tar:
        tar.extractall(target, filter='data')
    print(json.dumps({'variant': variant, 'ref': ref, 'path': str(target)}))


def credentials():
    path = WORK / 'credentials.json'
    if not path.exists():
        import secrets
        save('credentials.json', {'username': 'admin', 'password': secrets.token_urlsafe(18),
                                  'jwt_secret': secrets.token_hex(32), 'registration_key': secrets.token_urlsafe(24)})
    return json.loads(path.read_text())


def environment():
    c = credentials()
    env = {key: value for key, value in os.environ.items()
           if key.lower() not in ('http_proxy', 'https_proxy', 'all_proxy')}
    env.update(DATA_DIR=str(WORK / 'data'), LOG_LEVEL='INFO', ADMIN_USERNAME=c['username'],
               ADMIN_PASSWORD=c['password'], JWT_SECRET_KEY=c['jwt_secret'], AGENT_REGISTRATION_KEY=c['registration_key'],
               SUB_STORE_BUILTIN_DIR=str(WORK / 'no-sub-store'), SUB_STORE_URL='http://127.0.0.1:9',
               CONFIGFLOW_BACKEND_HOST='127.0.0.1', CONFIGFLOW_BACKEND_PORT=str(API))
    return env


SEED = r'''
import json, sys
from pathlib import Path
work, sub_url, server_domain, agent = Path(sys.argv[1]), sys.argv[2], sys.argv[3], json.loads(sys.stdin.read())
from backend.common import config as store
from backend.common.config_repository import ProfileRepository
store.set_repository(ProfileRepository(work / 'data', default_config_factory=store.get_default_config,
                                       initial_config_factory=store.get_default_config))
repo = store.get_repository()
def shared(s):
    s['subscriptions'] = [{'id': 'lab-airport', 'name': '实验机场', 'url': sub_url, 'enabled': True, 'interval': 86400}]
def profile(p):
    p['proxy_groups'] = [{'id': 'lab-group', 'name': '代理', 'type': 'select', 'subscriptions': ['lab-airport'],
                          'enabled': True}]
    p['rule_configs'] = [{'id': 'lab-match', 'itemType': 'rule', 'rule_type': 'MATCH', 'policy': '代理', 'enabled': True}]
    p.setdefault('mihomo', {})['custom_config'] = sys.argv[4]
if sys.argv[5] != 'agent':
    repo.update_system_transaction(lambda c: c['system_config'].update(server_domain=server_domain))
    repo.update_shared_transaction(shared)
    repo.update_profile_transaction(repo.default_profile_id(), profile)
from backend.common.agent_manager import get_agent_manager
manager = get_agent_manager()
manager._update_agents(lambda rows: rows.__setitem__(slice(None), [agent]))
print(json.dumps({'agents': [a['id'] for a in manager.get_all_agents()], 'profile': repo.default_profile_id()}))
'''


def python(variant):
    return [str(Path.home() / 'cf-test/.venv/bin/python')]


def lab_agent():
    """Identity and token of the Agent actually installed on CT152 (never printed)."""
    path = WORK / 'agent.json'
    if not path.exists():
        config = json.loads(guest(152, 'cat', '/opt/configflow-agent/config-mihomo.json').stdout)
        save('agent.json', {'id': config['agent_id'], 'token': config['token']})
    return json.loads(path.read_text())


def agent_id():
    return lab_agent()['id']


def seed(replace=''):
    assert (WORK / 'src-before').exists()
    if (WORK / 'data').exists() and replace != 'agent':
        raise SystemExit('Private data exists; refusing to overwrite')
    token = lab_agent()['token']
    agent = {'id': agent_id(), 'name': '实验 Mihomo（CT152）', 'host': '127.0.0.1', 'port': AGENT_LOCAL, 'token': token,
             'service_type': 'mihomo', 'profile_id': 'default', 'deployment_method': 'shell', 'status': 'online',
             'version': '1.8.50-go', 'config_version': '0', 'enabled': True,
             'last_heartbeat': time.strftime('%Y-%m-%dT%H:%M:%S'), 'created_at': time.strftime('%Y-%m-%dT%H:%M:%S')}
    result = subprocess.run(python('before') + ['-c', SEED, str(WORK), f'http://127.0.0.1:{SUB}/airport.yaml',
                            f'http://10.242.150.254:{TUNNEL_API}', CUSTOM, replace or 'all'], cwd=WORK / 'src-before', env=environment(),
                            input=json.dumps(agent), capture_output=True, text=True, check=True)
    print(result.stdout.strip())


SERVE = r'''
import os, sys, threading, time
from datetime import datetime
sys.path.insert(0, os.getcwd())
from backend.common import config as store
from backend.common.config_repository import ProfileRepository
from pathlib import Path
store.set_repository(ProfileRepository(Path(os.environ['DATA_DIR']), default_config_factory=store.get_default_config,
                                       initial_config_factory=store.get_default_config))
from backend.common.agent_manager import get_agent_manager
manager = get_agent_manager()
def beat():
    # The lab Agent does not heartbeat to this private copy; keep its record online.
    while True:
        def change(agents):
            for item in agents:
                if item['id'] == sys.argv[1]:
                    item.update(status='online', last_heartbeat=datetime.now().isoformat())
        manager._update_agents(change)
        time.sleep(15)
threading.Thread(target=beat, daemon=True).start()
from backend.app import app
from backend.http_server import run_http_server
run_http_server(app)
'''


def serve(variant):
    stop()
    src = WORK / f'src-{variant}'
    log = open(WORK / f'server-{variant}.log', 'ab')
    process = subprocess.Popen(python(variant) + ['-c', SERVE, agent_id()], cwd=src, env=environment(),
                               stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    (WORK / 'server.pid').write_text(str(process.pid))
    tunnel = subprocess.Popen(['ssh', '-NT', '-o', 'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes',
                               '-o', 'ServerAliveInterval=15', '-L', f'127.0.0.1:{AGENT_LOCAL}:10.242.150.2:27647',
                               '-R', f'10.242.150.254:{TUNNEL_API}:127.0.0.1:{API}', 'pve'],
                              stdout=subprocess.DEVNULL, stderr=open(WORK / 'tunnel.log', 'ab'), start_new_session=True)
    (WORK / 'tunnel.pid').write_text(str(tunnel.pid))
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f'http://127.0.0.1:{API}/api/auth/status', timeout=2)
            break
        except Exception:
            if process.poll() is not None:
                raise SystemExit('server exited; see ' + str(WORK / f'server-{variant}.log'))
            time.sleep(0.5)
    else:
        raise SystemExit('server did not start')
    print(json.dumps({'variant': variant, 'api': f'http://127.0.0.1:{API}', 'pid': process.pid, 'tunnel': tunnel.pid}))


def _kill(name):
    path = WORK / name
    if not path.exists():
        return
    try:
        os.killpg(int(path.read_text()), signal.SIGTERM)
    except (ProcessLookupError, ValueError):
        pass
    path.unlink()


def stop():
    _kill('server.pid')
    _kill('tunnel.pid')
    time.sleep(1)


def sub(version):
    _kill('sub.pid')
    directory = WORK / 'sub'
    directory.mkdir(exist_ok=True)
    (directory / 'airport.yaml').write_text(SUBSCRIPTIONS[version])
    process = subprocess.Popen([sys.executable, '-m', 'http.server', str(SUB), '--bind', '127.0.0.1'], cwd=directory,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    (WORK / 'sub.pid').write_text(str(process.pid))
    time.sleep(1)
    body = urllib.request.urlopen(f'http://127.0.0.1:{SUB}/airport.yaml', timeout=5).read().decode()
    print(json.dumps({'version': version, 'sha256': hashlib.sha256(body.encode()).hexdigest()}))


_token = None


def api(method, path, body=None, *, timeout=300):
    global _token
    if _token is None:
        c = credentials()
        request = urllib.request.Request(f'http://127.0.0.1:{API}/api/auth/login', method='POST',
                                         data=json.dumps({'username': c['username'], 'password': c['password']}).encode(),
                                         headers={'Content-Type': 'application/json'})
        _token = json.load(urllib.request.urlopen(request, timeout=30))['token']
    request = urllib.request.Request(f'http://127.0.0.1:{API}/api{path}', method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {_token}'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b'null')
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b'null')


def refresh(label):
    status, body = api('POST', f'/subscriptions/{SUB_ID}/fetch', {'preview': True})
    assert status == 200, body
    save(f'evidence/{label}-refresh.json', {'status': status, 'count': body.get('count'), 'from_cache': body.get('from_cache')})
    print(json.dumps({'refreshed': body.get('count'), 'from_cache': body.get('from_cache')}, ensure_ascii=False))


def preview(label):
    status, body = api('POST', '/generate/mihomo/preview', {'base_url': ''})
    assert status == 200, body
    import yaml
    document = yaml.safe_load(body['content'])
    save(f'evidence/{label}-preview.json', {'hosts': document.get('hosts'), 'report': body.get('subscription_hosts'),
                                            'providers': list((document.get('proxy-providers') or {}))})
    (WORK / f'evidence/{label}-preview.yaml').write_text(body['content'])
    print(json.dumps({'hosts': document.get('hosts'), 'report': body.get('subscription_hosts') and {
        key: body['subscription_hosts'][key] for key in ('merged', 'ignored', 'domain_mapped')}}, ensure_ascii=False))


def agent_status(label):
    deadline = time.monotonic() + 120
    while True:
        status, agents = api('GET', '/agents')
        sync = next(item for item in agents if item['id'] == agent_id()).get('content_sync')
        if sync and sync.get('status') != 'checking' or time.monotonic() > deadline:
            break
        time.sleep(2)
    save(f'evidence/{label}-agent-status.json', sync)
    print(json.dumps({'content_sync': sync}))


def publish(label):
    import uuid
    deployment = 'subhosts-' + uuid.uuid4().hex[:12]
    status, body = api('POST', f'/agents/{agent_id()}/push-config', {'deployment_id': deployment})
    assert status in (200, 202), body
    deadline = time.monotonic() + 240
    while body.get('status') not in ('succeeded', 'failed', 'rolled_back') and time.monotonic() < deadline:
        time.sleep(2)
        status, body = api('GET', f'/agents/{agent_id()}/deployments/{deployment}')
    save(f'evidence/{label}-publish.json', {key: body.get(key) for key in ('status', 'message', 'error', 'events')})
    deployed = guest(152, 'cat', '/etc/mihomo/config.yaml').stdout.decode()
    (WORK / f'evidence/{label}-deployed-config.yaml').write_text(deployed)
    import yaml
    hosts = yaml.safe_load(deployed).get('hosts')
    print(json.dumps({'deployment': deployment, 'status': body.get('status'), 'message': body.get('message'),
                      'deployed_hosts': hosts}, ensure_ascii=False))
    assert body.get('status') == 'succeeded', body


OBSERVE = r'''import json, sys, time, urllib.request
port, seconds = sys.argv[1], float(sys.argv[2]); seen = {}; end = time.monotonic() + seconds
while time.monotonic() < end:
    try:
        for c in json.load(urllib.request.urlopen(f'http://127.0.0.1:{port}/connections', timeout=1)).get('connections') or []:
            if c.get('metadata', {}).get('host') == 'target.lab.invalid':
                seen[c['id']] = c
    except Exception:
        pass
    time.sleep(0.1)
print(json.dumps(list(seen.values())))
'''

SELECT = r'''import json, sys, urllib.parse, urllib.request
port, group, name = sys.argv[1:4]
request = urllib.request.Request(f'http://127.0.0.1:{port}/proxies/' + urllib.parse.quote(group), method='PUT',
                                 data=json.dumps({'name': name}).encode(), headers={'Content-Type': 'application/json'})
urllib.request.urlopen(request, timeout=5)
print(json.load(urllib.request.urlopen(f'http://127.0.0.1:{port}/proxies/' + urllib.parse.quote(group), timeout=5))['now'])
'''


def fixture_events(since):
    lines = guest(154, 'cat', f'{FIXTURE_DIR}/requests.jsonl', check=False).stdout.decode().splitlines()
    return [event for event in map(json.loads, lines) if event['at'] >= since]


def measure(label, *, mixed=7890, controller=9090):
    results = {}
    for key, node in NODES.items():
        now = guest(152, 'python3', '-c', SELECT, str(controller), GROUP, node).stdout.decode().strip()
        assert now == node, now
        guest(152, 'python3', '-c', 'import urllib.request;urllib.request.urlopen(urllib.request.Request('
              f'"http://127.0.0.1:{controller}/connections", method="DELETE"), timeout=5)', check=False)
        holder = {}
        capture = {}

        def observe():
            holder['connections'] = json.loads(guest(152, 'python3', '-c', OBSERVE, str(controller), '6', timeout=30).stdout)

        def dump():
            capture['tcpdump'] = guest(152, 'timeout', '7', 'tcpdump', '-nni', 'eth1', '-c', '1',
                                       'tcp dst port 1080 and tcp[tcpflags] & tcp-syn != 0', check=False,
                                       timeout=30).stdout.decode().strip()

        threads = [threading.Thread(target=observe), threading.Thread(target=dump)]
        for thread in threads:
            thread.start()
        time.sleep(1.5)
        since = time.time() - 1
        url = f'http://{TARGET}:18080/{label}-{key}?hold=1'
        curl = guest(153, 'curl', '-sS', '--max-time', '15', '-x', f'socks5h://192.0.2.3:{mixed}', url, check=False, timeout=40)
        for thread in threads:
            thread.join()
        socks = [event for event in fixture_events(since) if event['kind'] == 'socks']
        connections = holder.get('connections') or []
        results[key] = {
            'node': node, 'server': LEFT_A if key == 'A' else LEFT_B, 'curl_ok': curl.returncode == 0,
            'response': curl.stdout.decode()[:200],
            'fixture_dialed': sorted({event['server'] for event in socks}),
            'mihomo_connections': [{'chains': c.get('chains'), 'remoteDestination': c['metadata'].get('remoteDestination'),
                                    'host': c['metadata'].get('host'), 'rule': c.get('rule')} for c in connections],
            'tcpdump_syn': capture.get('tcpdump', ''),
        }
    save(f'evidence/{label}-measure.json', results)
    print(json.dumps({key: {k: value[k] for k in ('curl_ok', 'fixture_dialed', 'tcpdump_syn')} | {
        'remoteDestination': [c['remoteDestination'] for c in value['mihomo_connections']]}
        for key, value in results.items()}, ensure_ascii=False))
    return results


RAW_CONFIG_EXTRA = '''
mixed-port: 17890
allow-lan: true
bind-address: '*'
external-controller: 127.0.0.1:19090
ipv6: false
dns:
  enable: true
  listen: 127.0.0.1:11053
  enhanced-mode: redir-host
  default-nameserver: [198.51.100.20]
  nameserver: [198.51.100.20]
'''


def raw(label, variant='full'):
    """The subscription file itself in Mihomo: what a client importing it directly gets.

    ``clean`` keeps only the two node entries, to show how Mihomo treats a
    domain-to-domain entry for a proxy server without the hijacking ones.
    """
    import yaml
    document = yaml.safe_load(SUBSCRIPTIONS['v1'])
    if variant == 'clean':
        document['hosts'] = {key: value for key, value in document['hosts'].items() if key in (LEFT_A, LEFT_B)}
    document.update(yaml.safe_load(RAW_CONFIG_EXTRA))
    directory = '/tmp/cf-subhosts-raw'
    guest(152, 'mkdir', '-p', directory)
    lab.write_remote(f'{directory}/config.yaml', yaml.safe_dump(document, allow_unicode=True, sort_keys=False), cid=152)
    guest(152, 'systemd-run', '--unit=cf-subhosts-raw', '--collect', '/usr/local/bin/mihomo', '-d', directory)
    try:
        for _ in range(40):
            if guest(152, 'python3', '-c', 'import urllib.request;urllib.request.urlopen("http://127.0.0.1:19090/version",timeout=1)',
                     check=False).returncode == 0:
                break
            time.sleep(0.5)
        save(f'evidence/{label}-raw-config.json', document)
        measure(label, mixed=17890, controller=19090)
    finally:
        guest(152, 'systemctl', 'stop', 'cf-subhosts-raw', check=False)
        guest(152, 'rm', '-rf', directory)


UTIME = r'''import json, os, sys
entries = json.load(sys.stdin)
# Deepest first, so a directory's time is set after its children are touched.
for path in sorted(entries, key=lambda item: item.count('/'), reverse=True):
    if os.path.lexists(path):
        os.utime(path, ns=(entries[path], entries[path]), follow_symlinks=False)
'''


def restore():
    lab.guard()
    stop()
    _kill('sub.pid')
    guest(152, 'systemctl', 'stop', 'configflow-agent', 'mihomo')
    guest(152, 'rm', '-rf', '/etc/mihomo', '/opt/configflow-agent')
    guest(152, 'tar', '-C', '/', '--numeric-owner', '-xpf', '-', data=(WORK / 'backup/ct152.tar').read_bytes(), timeout=180)
    guest(152, 'systemctl', 'start', 'mihomo', 'configflow-agent')
    time.sleep(5)
    fixture_down()
    guest(152, 'rm', '-rf', '/tmp/cf-subhosts-raw')
    verify()


def _routes(lines):
    """RA lifetimes count down and the recreated TUN gets new random link-local addresses."""
    return sorted(re.sub(r' expires \d+sec', '', line) for line in lines
                  if not re.match(r'local fe80::\S+ dev Meta ', line) and not re.match(r'fe80::/64 dev Meta ', line))


def verify():
    lab.guard()
    before_files = json.loads((WORK / 'backup/ct152-files.json').read_text())
    before_state = json.loads((WORK / 'backup/ct152-state.json').read_text())
    # tar keeps whole seconds only; put back the exact times recorded in the manifest.
    guest(152, 'python3', '-c', UTIME, data=json.dumps({path: entry['mtime_ns'] for path, entry in before_files.items()}).encode())
    after_files = ct152_files()
    diff = {path: [before_files.get(path), after_files.get(path)] for path in set(before_files) | set(after_files)
            if before_files.get(path) != after_files.get(path)}
    after_state = guest_json(152, STATE)
    for key in sorted(before_state['sysctl'], key=lambda item: not item.endswith('.stable_secret')):
        # A recreated TUN gets fresh IPv6 address-generation settings; the secret first, then the mode.
        if key.endswith(('.stable_secret', '.addr_gen_mode')) and after_state['sysctl'].get(key) != before_state['sysctl'][key]:
            guest(152, 'sysctl', '-w', f'{key}={before_state["sysctl"][key]}', check=False)
    after_state = guest_json(152, STATE)
    sysctl_diff = {key: [value, after_state['sysctl'].get(key)] for key, value in before_state['sysctl'].items()
                   if key not in VOLATILE_SYSCTL and after_state['sysctl'].get(key) != value}

    def addresses(entries):
        return sorted((item['ifname'], info.get('local'), info.get('prefixlen')) for item in entries
                      for info in item.get('addr_info', []) if info.get('scope') != 'link')

    result = {
        'ct152_file_diff': diff,
        'ct152_files_compared': len(before_files),
        'ct152_services': after_state['services'],
        'ct152_services_equal': after_state['services'] == before_state['services'],
        'ct152_mihomo_runtime_configs_equal': after_state['configs'] == before_state['configs'],
        'ct152_addresses_equal': addresses(after_state['addr']) == addresses(before_state['addr']),
        'ct152_routes_equal': _routes(after_state['route']) == _routes(before_state['route']),
        'ct152_routes6_equal': _routes(after_state['route6']) == _routes(before_state['route6']),
        'ct152_net_sysctl_diff': {key: ('[secret]' if 'secret' in key else value) for key, value in sysctl_diff.items()},
        'runtime_not_restorable': 'TUN Meta link-local addresses are regenerated when Mihomo restarts',
    }
    for cid in (153, 154):
        old = json.loads((WORK / f'backup/ct{cid}-network.json').read_text())
        now = network(cid)
        result[f'ct{cid}_addresses_equal'] = addresses(now['addr']) == addresses(old['addr'])
        result[f'ct{cid}_routes_equal'] = _routes(now['route']) == _routes(old['route'])
    units = guest(154, 'systemctl', 'list-units', '--all', '--no-legend', 'cf-*', check=False).stdout.decode().splitlines()
    result['ct154_units_equal'] = sorted(' '.join(line.split()[:4]) for line in units) == sorted(
        ' '.join(line.split()[:4]) for line in json.loads((WORK / 'backup/ct154-units.json').read_text()))
    result['ct154_fixture_dir_removed'] = guest(154, 'test', '-e', FIXTURE_DIR, check=False).returncode != 0
    result['ct152_raw_dir_removed'] = guest(152, 'test', '-e', '/tmp/cf-subhosts-raw', check=False).returncode != 0
    save('evidence/restoration.json', result)
    print(json.dumps(result, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    lab.guard(guests=False)
    action, args = sys.argv[1], sys.argv[2:]
    if action not in ('stop', 'sub', 'source', 'seed'):
        lab.guard()
    {'backup': backup, 'fixture-up': fixture_up, 'fixture-down': fixture_down, 'source': source, 'seed': seed,
     'serve': serve, 'stop': stop, 'sub': sub, 'refresh': refresh, 'preview': preview, 'agent-status': agent_status,
     'publish': publish, 'measure': measure, 'raw': raw, 'restore': restore, 'verify': verify}[action](*args)
