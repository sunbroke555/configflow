#!/usr/bin/env python3
"""Subscription hosts on the isolated PVE lab (VM151 router, CT152 Mihomo, CT153 client, CT154 node).

Run on the test server only, from a checkout synced by rt. Never touches other
guests; VM151 is used as it is (routing only). Everything the run changes is
backed up first and restored by ``restore``:

  backup        tar of CT152 /etc/mihomo + /opt/configflow-agent (deployment journal,
                cache.db), file manifest, services, addresses, net sysctls; CT153/CT154 network
  fixture-up    CT154: extra lab addresses + DNS/SOCKS/HTTP fixture (systemd-run, runtime only)
  source V REF  unpack a git archive (before/after/main) into the private work directory
  seed          fresh private ConfigFlow data: two lab subscriptions, group, rule, CT152 Agent
  serve V       private ConfigFlow (127.0.0.1:15951) + SSH tunnels, background
  stop          stop the private ConfigFlow and tunnels
  sub A [P]     serve fixture subscriptions: airport A (v1/v2/ip-only/none), plain P (plain-v1/plain-v2)
  refresh LABEL [SUB]  update a subscription now (同 订阅来源 → 更新)
  custom V      Mihomo base YAML variant: fake-ip, redir-host, no-nameserver, user-policy
  compare V1 V2 LABEL  generated Mihomo config of the same data by two source trees, byte for byte
  ct152-prepare put back the rule file the CT152 deployment journal still references (from its own backup)
  publish LABEL push to CT152 through the Agent and wait for the result
  measure LABEL [NODES]  client CT153 → Mihomo → each node; record which entry address was dialed
  dnsq LABEL    ask CT152's Mihomo DNS listener for the node domains
  device LABEL  CT152 Mihomo: deployed dns section, rule and proxy providers
  wait-node LABEL KEY  wait (no publication) until CT152 refreshed the node provider with that node
  raw LABEL     the raw subscription loaded by a separate Mihomo on CT152 (ports 17890/19090)
  control LABEL the deployed config minus proxy-server-nameserver-policy, in a separate Mihomo on CT152
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
PLAIN_ID = 'lab-plain'
GROUP = '代理'
NODES = {'A': 'A 共享入口', 'B': 'B 指定入口', 'C': 'C 普通机场', 'D': 'D 新增节点', 'E': 'E 同域新增'}
LEFT_A, RIGHT_A = 'edge-a.shared.lab.invalid', 'entry-a.dedicated.lab.invalid'
LEFT_B = 'edge-b.shared.lab.invalid'
PLAIN_C, PLAIN_D = 'node-c.plain.lab.invalid', 'node-d.plain.lab.invalid'
SERVERS = {'A': LEFT_A, 'B': LEFT_B, 'C': PLAIN_C, 'D': PLAIN_D, 'E': LEFT_A}
TARGET = 'target.lab.invalid'
EXTRA = ['198.51.100.11', '198.51.100.12', '198.51.100.13', '198.51.100.20']
FIXTURE_DIR = '/tmp/cf-subhosts'
FIXTURE_UNIT = 'cf-subhosts-fixture'
RELAY_UNIT = 'cf-subhosts-relay'      # on PVE: 10.242.150.254:15952 -> the SSH remote forward on 127.0.0.1

FIXTURE = r'''
import http.server, json, select, socket, socketserver, struct, threading, time
from urllib.parse import urlsplit
LOG = '/tmp/cf-subhosts/requests.jsonl'
ANSWERS = {'edge-a.shared.lab.invalid': '198.51.100.11', 'entry-a.dedicated.lab.invalid': '198.51.100.12',
           'edge-b.shared.lab.invalid': '198.51.100.11', 'target.lab.invalid': '198.51.100.20',
           'node-c.plain.lab.invalid': '198.51.100.11', 'node-d.plain.lab.invalid': '198.51.100.13'}
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
  proxy-server-nameserver: [198.51.100.20]
'''
VARIANTS = {
    'redir-host': CUSTOM,
    # Fake-IP: the mapping target must stay out of the fake pool once the mapped node resolves through the listener.
    'fake-ip': CUSTOM.replace('  enhanced-mode: redir-host\n', '  enhanced-mode: fake-ip\n  fake-ip-range: 198.18.0.1/16\n'),
}
# Mihomo refuses a policy without proxy-server-nameserver: ConfigFlow then adds nothing.
VARIANTS['no-nameserver'] = VARIANTS['fake-ip'].replace('  proxy-server-nameserver: [198.51.100.20]\n', '')
VARIANTS['user-policy'] = VARIANTS['fake-ip'] + f"  proxy-server-nameserver-policy: {{'{LEFT_A}': 198.51.100.20}}\n"
PROVIDER_INTERVAL = 60

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
    'plain-v1': f'''proxies:
  - {{name: {NODES['C']}, type: socks5, server: {PLAIN_C}, port: 1080}}
''',
}
SUBSCRIPTIONS['v2'] = SUBSCRIPTIONS['v1'].replace(f'{LEFT_B}: 198.51.100.13', f'{LEFT_B}: 198.51.100.12')
# No domain-to-domain mapping left: the output must equal the code before this change.
SUBSCRIPTIONS['ip-only'] = SUBSCRIPTIONS['v1'].replace(f'{LEFT_A}: {RIGHT_A}', f'{LEFT_A}: 198.51.100.12')
SUBSCRIPTIONS['none'] = SUBSCRIPTIONS['v1'][:SUBSCRIPTIONS['v1'].index('hosts:')] + f'rules:\n  - MATCH,{GROUP}\n'
# Another node on the mapped domain: the policy is per domain, so it works without a publication.
SUBSCRIPTIONS['v1-e'] = SUBSCRIPTIONS['v1'].replace(
    f"  - {{name: {NODES['B']}, type: socks5, server: {LEFT_B}, port: 1080}}\n",
    f"  - {{name: {NODES['B']}, type: socks5, server: {LEFT_B}, port: 1080}}\n"
    f"  - {{name: {NODES['E']}, type: socks5, server: {LEFT_A}, port: 1080}}\n")
SUBSCRIPTIONS['v2-e'] = SUBSCRIPTIONS['v1-e'].replace(f'{LEFT_B}: 198.51.100.13', f'{LEFT_B}: 198.51.100.12')
SUBSCRIPTIONS['plain-v2'] = SUBSCRIPTIONS['plain-v1'] + f"  - {{name: {NODES['D']}, type: socks5, server: {PLAIN_D}, port: 1080}}\n"


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
                                  'jwt_secret': secrets.token_hex(32), 'registration_key': secrets.token_urlsafe(24),
                                  'config_token': secrets.token_hex(16)})
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
    s['subscriptions'] = [{'id': 'lab-airport', 'name': '实验机场', 'url': sub_url + 'airport.yaml', 'enabled': True,
                           'interval': 86400},
                          {'id': 'lab-plain', 'name': '普通机场', 'url': sub_url + 'plain.yaml', 'enabled': True,
                           'interval': 86400}]
def profile(p):
    p['proxy_groups'] = [{'id': 'lab-group', 'name': '代理', 'type': 'select',
                          'subscriptions': ['lab-airport', 'lab-plain'], 'enabled': True}]
    # Mihomo fetches providers through its own rules: keep the panel (tunnel address) off the lab proxies.
    p['rule_configs'] = [{'id': 'lab-panel', 'itemType': 'rule', 'rule_type': 'IP-CIDR', 'value': '10.242.150.254/32',
                          'policy': 'DIRECT', 'no_resolve': True, 'enabled': True},
                         {'id': 'lab-match', 'itemType': 'rule', 'rule_type': 'MATCH', 'policy': '代理', 'enabled': True}]
    p.setdefault('mihomo', {}).update(custom_config=sys.argv[4], provider_update_interval=int(sys.argv[7]))
if sys.argv[5] == 'rules':
    repo.update_profile_transaction(repo.default_profile_id(), lambda p: p.update(
        rule_configs=[{'id': 'lab-panel', 'itemType': 'rule', 'rule_type': 'IP-CIDR', 'value': '10.242.150.254/32',
                       'policy': 'DIRECT', 'no_resolve': True, 'enabled': True}] + [
            item for item in p.get('rule_configs', []) if item.get('id') != 'lab-panel']))
    print(json.dumps({'rules': [item['id'] for item in repo.get_profile(repo.default_profile_id())['rule_configs']]}))
    raise SystemExit
if sys.argv[5] != 'agent':
    repo.update_system_transaction(lambda c: c['system_config'].update(server_domain=server_domain,
                                                                        config_token=sys.argv[6]))
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
    if (WORK / 'data').exists() and replace not in ('agent', 'rules'):
        raise SystemExit('Private data exists; refusing to overwrite')
    token = lab_agent()['token']
    agent = {'id': agent_id(), 'name': '实验 Mihomo（CT152）', 'host': '127.0.0.1', 'port': AGENT_LOCAL, 'token': token,
             'service_type': 'mihomo', 'profile_id': 'default', 'deployment_method': 'shell', 'status': 'online',
             'version': '1.8.50-go', 'config_version': '0', 'enabled': True,
             'last_heartbeat': time.strftime('%Y-%m-%dT%H:%M:%S'), 'created_at': time.strftime('%Y-%m-%dT%H:%M:%S')}
    result = subprocess.run(python('before') + ['-c', SEED, str(WORK), f'http://127.0.0.1:{SUB}/',
                            f'http://10.242.150.254:{TUNNEL_API}', VARIANTS['fake-ip'], replace or 'all',
                            credentials()['config_token'], str(PROVIDER_INTERVAL)],
                            cwd=WORK / 'src-before', env=environment(),
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
    # sshd binds remote forwards to loopback (no GatewayPorts): relay the lab-bridge address to it, runtime only.
    time.sleep(2)
    lab.pve('systemctl', 'stop', RELAY_UNIT, check=False)
    lab.pve('systemd-run', f'--unit={RELAY_UNIT}', '--collect', 'socat',
            f'TCP-LISTEN:{TUNNEL_API},bind=10.242.150.254,reuseaddr,fork', f'TCP:127.0.0.1:{TUNNEL_API}')
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
    lab.pve('systemctl', 'stop', RELAY_UNIT, check=False)
    time.sleep(1)


def sub(version, plain='plain-v1'):
    _kill('sub.pid')
    directory = WORK / 'sub'
    directory.mkdir(exist_ok=True)
    (directory / 'airport.yaml').write_text(SUBSCRIPTIONS[version])
    (directory / 'plain.yaml').write_text(SUBSCRIPTIONS[plain])
    process = subprocess.Popen([sys.executable, '-m', 'http.server', str(SUB), '--bind', '127.0.0.1'], cwd=directory,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    (WORK / 'sub.pid').write_text(str(process.pid))
    time.sleep(1)
    bodies = {name: urllib.request.urlopen(f'http://127.0.0.1:{SUB}/{name}.yaml', timeout=5).read()
              for name in ('airport', 'plain')}
    print(json.dumps({'airport': version, 'plain': plain,
                      **{name: hashlib.sha256(body).hexdigest()[:16] for name, body in bodies.items()}}))


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


def refresh(label, sub_id=SUB_ID):
    status, body = api('POST', f'/subscriptions/{sub_id}/fetch', {'preview': True})
    assert status == 200, body
    save(f'evidence/{label}-refresh-{sub_id}.json', {'status': status, 'count': body.get('count'),
                                                     'from_cache': body.get('from_cache')})
    print(json.dumps({'subscription': sub_id, 'refreshed': body.get('count'), 'from_cache': body.get('from_cache')},
                     ensure_ascii=False))


def custom(variant):
    status, body = api('POST', '/custom-config/mihomo', {'config': VARIANTS[variant],
                                                          'provider_update_interval': PROVIDER_INTERVAL})
    assert status == 200 and body.get('success'), body
    print(json.dumps({'custom': variant}))


COMPARE = r'''
import hashlib, json, sys
from pathlib import Path
from backend.common import config as store
from backend.common.config_repository import ProfileRepository
store.set_repository(ProfileRepository(Path(sys.argv[1]), default_config_factory=store.get_default_config,
                                       initial_config_factory=store.get_default_config))
from backend.converters.mihomo import generate_mihomo_config
config = store.get_config()
text = generate_mihomo_config(config, base_url=sys.argv[2], preflight_providers=False)
Path(sys.argv[3]).write_text(text)
print(json.dumps({'sha256': hashlib.sha256(text.encode()).hexdigest(), 'bytes': len(text.encode())}))
'''


def compare(first, second, label):
    """The same data generated by two source trees (``source`` them first), compared byte for byte."""
    out = {}
    for variant in (first, second):
        path = WORK / f'evidence/{label}-{variant}.yaml'
        result = subprocess.run(python(variant) + ['-c', COMPARE, str(WORK / 'data'), f'http://10.242.150.254:{TUNNEL_API}',
                                str(path)], cwd=WORK / f'src-{variant}', env=environment(), capture_output=True,
                                text=True, check=True)
        out[variant] = json.loads(result.stdout.strip().splitlines()[-1])
    texts = [(WORK / f'evidence/{label}-{variant}.yaml').read_text() for variant in (first, second)]
    out['identical'] = texts[0] == texts[1]
    if not out['identical']:
        import difflib
        out['diff'] = list(difflib.unified_diff(texts[0].splitlines(), texts[1].splitlines(), first, second, lineterm=''))[:80]
    import yaml
    document = yaml.safe_load(texts[1])
    out['hosts'] = document.get('hosts')
    out['dns'] = document.get('dns')
    out['rule_providers'] = list(document.get('rule-providers') or {})
    save(f'evidence/{label}-compare.json', out)
    print(json.dumps(out, ensure_ascii=False, indent=1))


def ct152_prepare():
    """The lab's deployment journal still lists a rule file an earlier run deleted; restore it from the journal's backup."""
    script = r'''import hashlib, json, os, shutil
base = '/etc/mihomo/.configflow-deployments'
manifest = json.load(open(os.path.join(base, 'active-manifest.json')))
result = []
for item in manifest['files']:
    target = os.path.join('/etc/mihomo', item['path'])
    if item['role'] == 'config' or os.path.exists(target):
        continue
    for name in sorted(os.listdir(base)):
        source = os.path.join(base, name, 'backup', item['path'])
        if os.path.isfile(source) and hashlib.sha256(open(source, 'rb').read()).hexdigest() == item['sha256']:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(source, target)
            result.append({'path': item['path'], 'from': name, 'sha256': item['sha256'][:16]})
            break
    else:
        result.append({'path': item['path'], 'missing': True})
print(json.dumps(result, ensure_ascii=False))
'''
    restored = guest_json(152, script)
    save('evidence/ct152-prepare.json', restored)
    print(json.dumps(restored, ensure_ascii=False))
    assert not any(item.get('missing') for item in restored), restored


def preview(label):
    status, body = api('POST', '/generate/mihomo/preview', {'base_url': ''})
    assert status == 200, body
    import yaml
    document = yaml.safe_load(body['content'])
    report = body.get('subscription_hosts') or {}
    save(f'evidence/{label}-preview.json', {'hosts': document.get('hosts'), 'report': report, 'dns': document.get('dns'),
                                            'rule_providers': document.get('rule-providers'),
                                            'providers': list((document.get('proxy-providers') or {}))})
    (WORK / f'evidence/{label}-preview.yaml').write_text(body['content'])
    print(json.dumps({'hosts': document.get('hosts'), 'dns': document.get('dns'),
                      'report': {key: report.get(key) for key in ('merged', 'ignored', 'domain_mapped', 'dns')}},
                     ensure_ascii=False, indent=1))


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
    document = yaml.safe_load(deployed)
    print(json.dumps({'deployment': deployment, 'status': body.get('status'), 'message': body.get('message'),
                      'deployed_hosts': document.get('hosts'), 'deployed_dns': document.get('dns'),
                      'rule_providers': list(document.get('rule-providers') or {})}, ensure_ascii=False))
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


def measure(label, keys='AB', *, mixed=7890, controller=9090):
    results = {}
    for key, node in ((key, NODES[key]) for key in keys):
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
        # Only this request: provider health checks reach the same SOCKS servers for other targets.
        socks = [event for event in fixture_events(since)
                 if event['kind'] == 'socks' and event.get('target', '').startswith(f'{TARGET}:')]
        connections = holder.get('connections') or []
        results[key] = {
            'node': node, 'server': SERVERS[key], 'curl_ok': curl.returncode == 0,
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


def dnsq(label):
    """What the deployed Mihomo's DNS listener answers for node domains (and, for contrast, the target site)."""
    names = [LEFT_A, RIGHT_A, LEFT_B, PLAIN_C, PLAIN_D, TARGET]
    answers = json.loads(guest(152, 'python3', '-c', QUERY, '1053', *names).stdout)
    save(f'evidence/{label}-dns.json', answers)
    print(json.dumps(answers))
    return answers


DEVICE = r'''import json, urllib.request
def get(path):
    return json.load(urllib.request.urlopen('http://127.0.0.1:9090' + path, timeout=5))
rules = get('/providers/rules').get('providers') or {}
proxies = get('/providers/proxies').get('providers') or {}
print(json.dumps({
    'rule_providers': {name: {k: item.get(k) for k in ('ruleCount', 'updatedAt', 'vehicleType', 'behavior')} for name, item in rules.items()},
    'proxy_providers': {name: sorted(p['name'] for p in item.get('proxies') or []) for name, item in proxies.items()
                        if item.get('vehicleType') == 'HTTP'},
}, ensure_ascii=False))
'''


def config_digest():
    return hashlib.sha256(guest(152, 'cat', '/etc/mihomo/config.yaml').stdout).hexdigest()


def wait_node(label, key):
    """Wait, without publishing, until CT152's Mihomo refreshed the node provider and lists that node."""
    started, before = time.time(), config_digest()
    deadline = time.monotonic() + 300
    while True:
        state = guest_json(152, DEVICE)
        present = any(NODES[key] in names for names in state['proxy_providers'].values())
        if present or time.monotonic() > deadline:
            break
        time.sleep(5)
    result = {'node_in_provider': present, 'seconds': round(time.time() - started),
              'config_unchanged': config_digest() == before, **state}
    save(f'evidence/{label}-wait.json', result)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    assert present and result['config_unchanged'], result


def device(label):
    import yaml
    deployed = yaml.safe_load(guest(152, 'cat', '/etc/mihomo/config.yaml').stdout)
    state = guest_json(152, DEVICE)
    result = {'dns': deployed.get('dns'), 'hosts': deployed.get('hosts'),
              'rule_providers_config': deployed.get('rule-providers'), 'rules': deployed.get('rules'), **state}
    save(f'evidence/{label}-device.json', result)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return result


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


QUERY = r'''import json, random, socket, struct, sys
port, names = int(sys.argv[1]), sys.argv[2:]
out = {}
for name in names:
    ident = random.randrange(65536)
    question = b''.join(bytes([len(p)]) + p.encode() for p in name.split('.')) + b'\0' + struct.pack('!HH', 1, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(5)
    s.sendto(struct.pack('!HHHHHH', ident, 0x0100, 1, 0, 0, 0) + question, ('127.0.0.1', port))
    data = s.recv(4096)
    count, p = struct.unpack('!H', data[6:8])[0], 12 + len(question)
    answers = []
    for _ in range(count):
        if data[p] & 0xC0 == 0xC0:
            p += 2
        else:
            while data[p]:
                p += data[p] + 1
            p += 1
        typ, _, _, size = struct.unpack('!HHIH', data[p:p + 10]); p += 10
        if typ == 1:
            answers.append(socket.inet_ntoa(data[p:p + size]))
        p += size
    out[name] = answers
print(json.dumps(out))
'''

# An earlier revision probed fake-ip-filter on CT154 here; restore still removes its directory.
PROBE_DIR = '/tmp/cf-subhosts-probe'


def control(label):
    """Negative control: the deployed config minus proxy-server-nameserver-policy, in a separate Mihomo on CT152.

    Without the policy the mapped node resolves through proxy-server-nameserver
    like every other node, so it reaches its own domain's address again.
    """
    import yaml
    document = yaml.safe_load(guest(152, 'cat', '/etc/mihomo/config.yaml').stdout)
    document.update({'mixed-port': 17890, 'external-controller': '127.0.0.1:19090'})
    document['dns']['listen'] = '127.0.0.1:11053'
    document['dns'].pop('proxy-server-nameserver-policy', None)
    directory = '/tmp/cf-subhosts-raw'
    guest(152, 'sh', '-c', f'rm -rf {directory} && mkdir -p {directory} && cp -a /etc/mihomo/providers {directory}/')
    lab.write_remote(f'{directory}/config.yaml', yaml.safe_dump(document, allow_unicode=True, sort_keys=False), cid=152)
    save(f'evidence/{label}-control-config.json', document)
    guest(152, 'systemd-run', '--unit=cf-subhosts-raw', '--collect', '/usr/local/bin/mihomo', '-d', directory)
    try:
        for _ in range(40):
            if guest(152, 'python3', '-c', 'import urllib.request;urllib.request.urlopen("http://127.0.0.1:19090/version",timeout=1)',
                     check=False).returncode == 0:
                break
            time.sleep(0.5)
        # Its own label: the deployed config's measurement under LABEL stays as it was.
        results = measure(f'{label}-control', 'AC', mixed=17890, controller=19090)
        save(f'evidence/{label}-control.json', {'measure': results})
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
    guest(154, 'systemctl', 'stop', 'cf-subhosts-probe', 'cf-subhosts-probe-http', check=False)
    guest(154, 'rm', '-rf', PROBE_DIR)
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
    result['pve_relay_stopped'] = lab.pve('systemctl', 'is-active', RELAY_UNIT, check=False).stdout.strip() != b'active'
    result['ct154_probe_dir_removed'] = guest(154, 'test', '-e', PROBE_DIR, check=False).returncode != 0
    result['ct152_raw_dir_removed'] = guest(152, 'test', '-e', '/tmp/cf-subhosts-raw', check=False).returncode != 0
    save('evidence/restoration.json', result)
    print(json.dumps(result, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    lab.guard(guests=False)
    action, args = sys.argv[1], sys.argv[2:]
    if action not in ('stop', 'sub', 'source', 'seed', 'compare', 'custom', 'refresh', 'preview'):
        lab.guard()
    {'backup': backup, 'fixture-up': fixture_up, 'fixture-down': fixture_down, 'source': source, 'seed': seed,
     'serve': serve, 'stop': stop, 'sub': sub, 'refresh': refresh, 'preview': preview, 'agent-status': agent_status,
     'custom': custom, 'compare': compare, 'ct152-prepare': ct152_prepare, 'dnsq': dnsq, 'device': device,
     'control': control, 'wait-node': wait_node,
     'publish': publish, 'measure': measure, 'raw': raw,
     'restore': restore, 'verify': verify}[action](*args)
