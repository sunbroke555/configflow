#!/usr/bin/env python3
"""IPv6 acceptance on fresh, isolated PVE guests, driven from the test server.

Only VM151 and CT152/153/154 created by this run may be mutated. The source
CT130 is read-only cloned while stopped. The three new bridges have no physical
ports; all proxy/DNS/HTTP traffic uses documentation addresses and lab fixtures.
Credentials stay in /tmp/configflow-ipv6-readiness/private.json, never reports.

prepare; install --agent-binary PATH; test; smoke; cleanup
To retest a newly built Agent from an empty feature-owned configuration:
  refresh --agent-binary PATH --reset-managed
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path('/tmp/configflow-ipv6-readiness')
PVE_ROOT = '/root/cf-ipv6-readiness'
OWNER = 'configflow-ipv6-readiness-20261030'
BRIDGES = ('cfv6am', 'cfv6al', 'cfv6aw')
GUESTS = {151: 'cf-ipv6-ready-ros', 152: 'cf-ipv6-ready-mihomo',
          153: 'cf-ipv6-ready-client', 154: 'cf-ipv6-ready-node'}
MANAGEMENT = {151: '10.242.150.1', 152: '10.242.150.2',
              153: '10.242.150.3', 154: '10.242.150.4'}
AGENT_PORT = 27647


def run(args, *, data=None, check=True, timeout=120):
    result = subprocess.run([str(x) for x in args], input=data, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f'Command failed: {shlex.join([str(x) for x in args[:5]])}: '
                           + result.stderr.decode(errors='replace')[-1200:])
    return result


def pve(*args, data=None, check=True, timeout=120):
    return run(['ssh', '-o', 'BatchMode=yes', 'pve', shlex.join([str(x) for x in args])],
               data=data, check=check, timeout=timeout)


def guest(cid, *args, **kwargs):
    if cid not in (152, 153, 154):
        raise RuntimeError('Not a task-owned Linux guest')
    return pve('pct', 'exec', cid, '--', *args, **kwargs)


def write_remote(path, data, mode=0o600, cid=None):
    if cid is None and not path.startswith(PVE_ROOT + '/'):
        raise RuntimeError('Not an owned PVE path')
    script = ('import os,sys; from pathlib import Path; p=Path(sys.argv[1]); '
              'p.parent.mkdir(parents=True,exist_ok=True); p.write_bytes(sys.stdin.buffer.read()); '
              'p.chmod(int(sys.argv[2]))')
    call = pve if cid is None else lambda *a, **kw: guest(cid, *a, **kw)
    call('python3', '-c', script, path, mode,
         data=data.encode() if isinstance(data, str) else data)


def save(name, value, private=False):
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = ROOT / name
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    path.chmod(0o600 if private else 0o644)


def guard(*, guests=True):
    if sys.platform != 'linux' or socket.gethostname() == 'pve':
        raise RuntimeError('Run on the Linux test server, never the Mac or PVE host')
    if not guests:
        return
    manifest = json.loads((ROOT / 'manifest.json').read_text())
    if manifest.get('owner') != OWNER:
        raise RuntimeError('Ownership manifest mismatch')
    for gid, name in GUESTS.items():
        text = pve('qm' if gid == 151 else 'pct', 'config', gid).stdout.decode()
        if f'{"name" if gid == 151 else "hostname"}: {name}\n' not in text or OWNER not in text:
            raise RuntimeError(f'Guest {gid} identity/ownership changed')
        nets = [line for line in text.splitlines() if line.startswith('net')]
        if any('bridge=cfv6a' not in line for line in nets):
            raise RuntimeError(f'Guest {gid} is attached outside its isolated bridges')
    return manifest


SERIAL_SETUP = r'''
import socket,sys,time,re
vmid,address,name,password,keyfile=sys.argv[1:]
key=open(keyfile).read().strip()
s=socket.socket(socket.AF_UNIX);s.connect(f'/var/run/qemu-server/{vmid}.serial0');s.settimeout(.5)
def rd(t=1):
 b=b'';end=time.monotonic()+t
 while time.monotonic()<end:
  try:b+=s.recv(65536)
  except socket.timeout:pass
 return b.decode('latin1')
def send(v):s.send(v.encode()+b'\r')
send('');end=time.monotonic()+180;tail=''
while time.monotonic()<end:
 out=rd();tail=(tail+out)[-800:]
 if re.search(r'\] >\s*$',tail):break
 if 'new password>' in out or 'repeat new password' in out:s.send(b'\x03');tail='';continue
 if 'software license? [Y/n]' in out:send('n');tail='';continue
 if re.search(r'--More--|q to abort|<-- ',out):s.send(b'q');tail='';continue
 if out.rstrip().endswith('Password:'):send('');tail='';continue
 if out.rstrip().endswith('Login:'):send('admin');tail='';continue
 if not out:send('')
else:raise RuntimeError('RouterOS serial login timed out')
for cmd in [f'/system identity set name={name}',f'/ip address add address={address}/24 interface=ether1',
 f'/user ssh-keys add user=admin key="{key}"',f'/user set admin password="{password}"',
 '/ip service set www disabled=no address=10.242.150.0/24',
 '/ip service set ssh disabled=no address=10.242.150.0/24']:
 send(cmd);out=rd(2)
 if 'failure:' in out or 'syntax error' in out:raise RuntimeError('RouterOS initialization failed')
print('RouterOS initialized')
'''

ROS_CONFIG = r'''
/ip address add address=192.0.2.1/24 interface=ether2 comment="ipv6-readiness-lab"
/ip address add address=198.51.100.1/24 interface=ether3 comment="ipv6-readiness-lab"
/ip route add dst-address=198.18.0.0/16 gateway=192.0.2.3 comment="ipv6-readiness-lab"
/ip dns set servers=198.51.100.10 allow-remote-requests=no
/ipv6 address add address=2001:db8:150:1::1/64 interface=ether2 advertise=yes comment="ipv6-readiness-lab"
/ipv6 address add address=fdda:150::1/64 interface=ether2 advertise=yes comment="ipv6-readiness-lab"
/ipv6 address add address=2001:db8:150:2::1/64 interface=ether3 advertise=no comment="ipv6-readiness-lab"
/ipv6 nd set [find default=yes] interface=ether2 ra-interval=3s-6s ra-lifetime=30s advertise-dns=no
/ipv6 nd prefix default set valid-lifetime=2m preferred-lifetime=1m
/ipv6 firewall filter add chain=input action=accept connection-state=established,related comment="ipv6-readiness-lab"
/ipv6 firewall filter add chain=input action=accept protocol=icmpv6 comment="ipv6-readiness-lab"
/ipv6 firewall filter add chain=input action=accept in-interface=ether2 comment="ipv6-readiness-lab"
/ipv6 firewall filter add chain=input action=drop comment="ipv6-readiness-lab"
/ipv6 firewall filter add chain=forward action=accept protocol=icmpv6 comment="ipv6-readiness-lab"
'''


def ros(command):
    return pve('ssh', '-i', PVE_ROOT + '/id_lab', '-o', 'BatchMode=yes',
               '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null',
               '-o', 'LogLevel=ERROR', 'admin@' + MANAGEMENT[151], command)


def prepare():
    guard(guests=False)
    if (ROOT / 'manifest.json').exists():
        raise RuntimeError('Existing manifest: inspect/clean before preparing again')
    inventory = json.loads(pve('pvesh', 'get', '/cluster/resources', '--type', 'vm', '--output-format', 'json').stdout)
    if set(GUESTS) & {int(row['vmid']) for row in inventory}:
        raise RuntimeError('A requested guest ID already exists; refusing reuse')
    source = pve('pct', 'config', 130).stdout.decode()
    if 'hostname: cf-oxi-a-retest\n' not in source or 'status: stopped' not in pve('pct', 'status', 130).stdout.decode():
        raise RuntimeError('Expected stopped, disposable clone source CT130 is unavailable')
    pve('test', '-f', '/root/cf-gwip-lab/pd/chr-7.25.img')
    pve('test', '-x', '/root/cf-gwip-lab/v6/mihomo')
    for bridge in BRIDGES:
        if pve('ip', 'link', 'show', bridge, check=False).returncode == 0:
            raise RuntimeError(f'Bridge already exists: {bridge}')
    manifest = {'owner': OWNER, 'created': time.time(), 'status': 'creating',
                'guests': GUESTS, 'bridges': BRIDGES, 'management': MANAGEMENT,
                'source_config_sha256': hashlib.sha256(source.encode()).hexdigest(),
                'proxy': {'address': '2001:db8:150:2::10', 'port': 1080},
                'agent': {'address': MANAGEMENT[152], 'port': AGENT_PORT},
                'router': {'address': MANAGEMENT[151], 'username': 'admin'},
                'upstream_interface': 'eth1', 'production_guests_modified': False}
    save('manifest.json', manifest)
    creds = {'agent_token': secrets.token_urlsafe(32), 'router_password': secrets.token_urlsafe(24)}
    save('private.json', creds, private=True)
    write_remote(PVE_ROOT + '/owner.json', json.dumps(manifest))
    pve('ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', PVE_ROOT + '/id_lab')
    for bridge in BRIDGES:
        pve('ip', 'link', 'add', bridge, 'type', 'bridge')
        pve('ip', 'link', 'set', bridge, 'up')
    pve('ip', 'address', 'add', '10.242.150.254/24', 'dev', BRIDGES[0])
    pve('qm', 'create', 151, '--name', GUESTS[151], '--description', OWNER,
        '--memory', 384, '--cores', 1, '--cpu', 'host', '--onboot', 0,
        '--scsihw', 'virtio-scsi-single', '--serial0', 'socket',
        '--net0', 'virtio=02:cf:15:00:01:51,bridge=cfv6am',
        '--net1', 'virtio=02:cf:15:00:11:51,bridge=cfv6al',
        '--net2', 'virtio=02:cf:15:00:21:51,bridge=cfv6aw')
    pve('qm', 'importdisk', 151, '/root/cf-gwip-lab/pd/chr-7.25.img', 'local-lvm')
    pve('qm', 'set', 151, '--scsi0', 'local-lvm:vm-151-disk-0', '--boot', 'order=scsi0')
    for cid in (152, 153, 154):
        pve('pct', 'clone', 130, cid, '--full', 1, '--hostname', GUESTS[cid], '--storage', 'local-lvm', timeout=180)
        pve('pct', 'set', cid, '--description', OWNER, '--onboot', 0, '--memory', 512 if cid == 152 else 256,
            '--net0', f'name=eth0,bridge=cfv6am,ip={MANAGEMENT[cid]}/24,ip6=manual',
            '--nameserver', '198.51.100.10')
        if cid == 154:
            spec = 'name=eth1,bridge=cfv6aw,ip=198.51.100.10/24,gw=198.51.100.1,ip6=2001:db8:150:2::10/64,gw6=2001:db8:150:2::1'
        else:
            octet = 3 if cid == 152 else 100
            spec = f'name=eth1,bridge=cfv6al,hwaddr=02:cf:15:00:00:{cid % 100:02d},ip=192.0.2.{octet}/24,gw=192.0.2.1,ip6=auto'
        pve('pct', 'set', cid, '--net1', spec)
        if cid == 152:
            pve('pct', 'set', cid, '--dev0', '/dev/net/tun')
    pve('qm', 'start', 151)
    for cid in (152, 153, 154):
        pve('pct', 'start', cid)
        guest(cid, 'sh', '-c', 'systemctl disable --now oxidns configflow-agent configflow-agent-oxidns configflow-agent-mihomo 2>/dev/null || true')
    write_remote(PVE_ROOT + '/serial.py', SERIAL_SETUP)
    pve('python3', PVE_ROOT + '/serial.py', 151, MANAGEMENT[151], GUESTS[151],
        creds['router_password'], PVE_ROOT + '/id_lab.pub', timeout=240)
    for line in ROS_CONFIG.strip().splitlines():
        ros(line)
    manifest['status'] = 'prepared'
    save('manifest.json', manifest)
    write_remote(PVE_ROOT + '/owner.json', json.dumps(manifest))
    print(json.dumps(manifest, indent=2))


FIXTURE = r'''
import http.server,json,select,socket,socketserver,struct,threading,time
LOG='/opt/cf-ipv6-fixture/requests.jsonl'
lock=threading.Lock()
def event(v):
 with lock,open(LOG,'a') as f:f.write(json.dumps(v)+'\n')
class HTTP(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  v={'path':self.path,'client':self.client_address[0],'origin':'IPv6-node-fixture'}
  b=json.dumps(v).encode();event(dict(v,kind='http'))
  self.send_response(200);self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
 def log_message(self,*a):pass
def exact(s,n):
 b=b''
 while len(b)<n:
  p=s.recv(n-len(b))
  if not p:raise EOFError()
  b+=p
 return b
class Socks(socketserver.BaseRequestHandler):
 def handle(self):
  c=self.request;c.settimeout(8);up=None
  try:
   ver,n=exact(c,2);exact(c,n);c.sendall(b'\x05\x00')
   ver,cmd,_,kind=exact(c,4)
   if ver!=5 or cmd!=1:raise ValueError('CONNECT only')
   if kind==1:host=socket.inet_ntop(socket.AF_INET,exact(c,4))
   elif kind==4:host=socket.inet_ntop(socket.AF_INET6,exact(c,16))
   elif kind==3:host=exact(c,exact(c,1)[0]).decode()
   else:raise ValueError('bad address')
   port=struct.unpack('!H',exact(c,2))[0]
   if host not in ('fixture.lab.invalid','198.51.100.10') or port!=18080:raise ValueError('outside fixture')
   up=socket.create_connection(('198.51.100.10',18080),3)
   c.sendall(b'\x05\x00\x00\x01'+socket.inet_aton('198.51.100.10')+b'\x00\x00')
   event({'kind':'proxy','source':self.client_address[0],'server':c.getsockname()[0],'host':host,'port':port})
   while True:
    ready,_,_=select.select([c,up],[],[],8)
    if not ready:return
    for src in ready:
     data=src.recv(65536)
     if not data:return
     (up if src is c else c).sendall(data)
  except Exception as e:event({'kind':'proxy_error','error':str(e)})
  finally:
   if up:up.close()
class IPv6Server(socketserver.ThreadingTCPServer):
 address_family=socket.AF_INET6;allow_reuse_address=True;daemon_threads=True
def dns(data):
 p=12
 while p<len(data) and data[p]:p+=data[p]+1
 if p+5>len(data):return b''
 typ,cls=struct.unpack('!HH',data[p+1:p+5]);question=data[12:p+5]
 value=socket.inet_aton('198.51.100.10') if typ==1 else socket.inet_pton(socket.AF_INET6,'2001:db8:150:2::10') if typ==28 else b''
 ans=b'\xc0\x0c'+struct.pack('!HHIH',typ,1,20,len(value))+value if value else b''
 return data[:2]+struct.pack('!HHHHH',0x8180,1,bool(ans),0,0)+question+ans
class DNS(socketserver.BaseRequestHandler):
 def handle(self):
  data,sock=self.request;sock.sendto(dns(data),self.client_address)
servers=[IPv6Server(('2001:db8:150:2::10',1080),Socks),http.server.ThreadingHTTPServer(('198.51.100.10',18080),HTTP),socketserver.ThreadingUDPServer(('198.51.100.10',53),DNS)]
for s in servers:threading.Thread(target=s.serve_forever,daemon=True).start()
while True:time.sleep(60)
'''

MIHOMO = '''mixed-port: 7890
allow-lan: true
ipv6: true
log-level: info
external-controller: 127.0.0.1:9090
tun:
  enable: true
  stack: mixed
  auto-route: true
  auto-redirect: true
  auto-detect-interface: true
  dns-hijack: [any:53, tcp://any:53]
dns:
  enable: true
  listen: 0.0.0.0:1053
  ipv6: true
  enhanced-mode: fake-ip
  fake-ip-range: 198.18.0.1/16
  fake-ip-range6: fc00::1/64
  nameserver: [198.51.100.10]
proxies:
  - name: IPv6-lab-node
    type: socks5
    server: 2001:db8:150:2::10
    port: 1080
rules:
  - MATCH,IPv6-lab-node
'''


def install(binary):
    manifest = guard()
    creds = json.loads((ROOT / 'private.json').read_text())
    if not binary.is_file():
        raise RuntimeError('Build the actual Go Agent on the test server first')
    for cid in (152, 153, 154):
        # The stopped source is a retained lab image. Remove only its inherited
        # fixture setup from the NEW clones; the source guest remains untouched.
        guest(cid, 'sh', '-c', 'systemctl disable --now cf-retest-network cf-retest-egress 2>/dev/null || true; ip link del eth0.91 2>/dev/null || true; nft flush ruleset')
    write_remote('/usr/local/bin/configflow-agent', binary.read_bytes(), mode=0o755, cid=152)
    mihomo = pve('cat', '/root/cf-gwip-lab/v6/mihomo').stdout
    write_remote('/usr/local/bin/mihomo', mihomo, mode=0o755, cid=152)
    write_remote('/etc/mihomo/config.yaml', MIHOMO, cid=152)
    # Remove cloned lab-specific Agent configuration, never source CT130.
    guest(152, 'sh', '-c', 'rm -rf /opt/configflow-agent; mkdir -p /opt/configflow-agent')
    config = {'server_url': 'http://127.0.0.1:1', 'agent_name': GUESTS[152],
              'agent_host': '0.0.0.0', 'agent_port': AGENT_PORT, 'agent_ip': MANAGEMENT[152],
              'service_type': 'mihomo', 'deployment_method': 'shell', 'service_name': 'mihomo',
              'service_manager': 'systemd', 'service_binary': '/usr/local/bin/mihomo',
              'service_unit': 'mihomo.service', 'agent_service_unit': 'configflow-agent',
              'config_path': '/etc/mihomo/config.yaml', 'restart_command': 'systemctl restart mihomo',
              'heartbeat_interval': 600, 'agent_id': 'ipv6-readiness-lab', 'token': creds['agent_token'],
              'dns_watchdog_disabled': True}
    write_remote('/opt/configflow-agent/config-mihomo.json', json.dumps(config), cid=152)
    units = {
        'mihomo': '[Unit]\nAfter=network-online.target\n[Service]\nExecStart=/usr/local/bin/mihomo -d /etc/mihomo\nRestart=on-failure\n[Install]\nWantedBy=multi-user.target\n',
        'configflow-agent': '[Unit]\nAfter=network-online.target\n[Service]\nWorkingDirectory=/opt/configflow-agent\nExecStart=/usr/local/bin/configflow-agent -config /opt/configflow-agent/config-mihomo.json\nRestart=on-failure\n[Install]\nWantedBy=multi-user.target\n'}
    for name, content in units.items():
        write_remote('/etc/systemd/system/' + name + '.service', content, mode=0o644, cid=152)
    write_remote('/opt/cf-ipv6-fixture/fixture.py', FIXTURE, cid=154)
    write_remote('/etc/systemd/system/cf-ipv6-fixture.service',
                 '[Unit]\nAfter=network-online.target\n[Service]\nExecStart=/usr/bin/python3 /opt/cf-ipv6-fixture/fixture.py\nRestart=on-failure\n[Install]\nWantedBy=multi-user.target\n', mode=0o644, cid=154)
    for cid, names in ((154, ['cf-ipv6-fixture']), (152, ['configflow-agent', 'mihomo'])):
        guest(cid, 'systemctl', 'daemon-reload')
        guest(cid, 'systemctl', 'enable', '--now', *names)
    guest(152, 'sysctl', '-w', 'net.ipv4.ip_forward=1')
    wait_agent()
    agent_api('POST', '/api/linux-gateway/forwarding', {})
    manifest['agent_binary_sha256'] = hashlib.sha256(binary.read_bytes()).hexdigest()
    save('manifest.json', manifest)
    print('Lab Agent and IPv6 proxy fixture installed')


def refresh(binary, reset_managed=False):
    manifest = guard()
    if not binary or not binary.is_file():
        raise RuntimeError('A built test Agent is required')
    guest(152, 'systemctl', 'stop', 'configflow-agent')
    try:
        if reset_managed:
            script = '''import json,pathlib
state=pathlib.Path('/opt/configflow-agent/ipv6-readiness.json')
config=pathlib.Path('/etc/sysctl.d/99-configflow-ipv6.conf')
if state.exists() or config.exists():
 assert state.is_file() and config.is_file() and not state.is_symlink() and not config.is_symlink()
 d=json.loads(state.read_text());assert d['interface']=='eth1' and d['mac']=='02:cf:15:00:00:52' and d['version']==1
 assert config.read_text().startswith('# Managed by ConfigFlow IPv6 readiness v1\\n')
 state.unlink();config.unlink()
'''
            guest(152, 'python3', '-c', script)
            guest(152, 'sysctl', '-w', 'net.ipv6.conf.all.forwarding=0', 'net.ipv6.conf.eth1.accept_ra=2')
        write_remote('/usr/local/bin/configflow-agent', binary.read_bytes(), mode=0o755, cid=152)
    finally:
        guest(152, 'systemctl', 'start', 'configflow-agent')
    wait_agent()
    manifest['agent_binary_sha256'] = hashlib.sha256(binary.read_bytes()).hexdigest()
    save('manifest.json', manifest)
    print('Updated the task-owned Agent' + (' and reset only its owned IPv6 preparation' if reset_managed else ''))


def agent_api(method, path, body=None, *, allow_error=False):
    creds = json.loads((ROOT / 'private.json').read_text())
    # Request runs from PVE via Python. Body/token are stdin, never shell argv.
    payload = {'url': f'http://{MANAGEMENT[152]}:{AGENT_PORT}' + path, 'method': method,
               'body': body, 'token': creds['agent_token']}
    script = '''import json,sys,urllib.request,urllib.error
d=json.load(sys.stdin);h={'Authorization':'Bearer '+d['token'],'Content-Type':'application/json'}
r=urllib.request.Request(d['url'],method=d['method'],headers=h,data=None if d['body'] is None else json.dumps(d['body']).encode())
try:
 with urllib.request.urlopen(r,timeout=25) as s:code=s.status;body=json.load(s)
except urllib.error.HTTPError as e:code=e.code;body=json.load(e)
print(json.dumps({'code':code,'body':body}))
'''
    result = json.loads(pve('python3', '-c', script, data=json.dumps(payload).encode(), timeout=35).stdout)
    if result['code'] >= 400 and not allow_error:
        raise RuntimeError(f'Agent {method} {path}: {result}')
    return result


def wait_agent():
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            value = agent_api('GET', '/api/ipv6-readiness')
            if value['code'] == 200:
                return value
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError('New Agent readiness API did not become available')


def metadata():
    script = '''import hashlib,json,pathlib
paths=list(pathlib.Path('/etc/sysctl.d').glob('*.conf'))
paths += [pathlib.Path('/etc/systemd/system/configflow-agent.service'),pathlib.Path('/opt/configflow-agent/ipv6-readiness.json')]
out={}
for p in paths:
 if p.is_file():
  s=p.stat();out[str(p)]={'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'mtime_ns':s.st_mtime_ns,'mode':s.st_mode&511}
print(json.dumps(out,sort_keys=True))
'''
    return json.loads(guest(152, 'python3', '-c', script).stdout)


def wait_ready(timeout=30):
    deadline = time.monotonic() + timeout
    value = None
    while time.monotonic() < deadline:
        value = agent_api('GET', '/api/ipv6-readiness')['body']
        if value.get('configuration_ready'):
            return value
        time.sleep(1)
    save('not-ready.json', value)
    raise RuntimeError('IPv6 configuration did not become ready; see not-ready.json')


def fake_address(family=4, *, allow_empty=False):
    script = r'''import json,socket,struct,sys
typ=1 if sys.argv[1]=='4' else 28
name='fixture.lab.invalid'
question=b''.join(bytes([len(p)])+p.encode() for p in name.split('.'))+b'\x00'+struct.pack('!HH',typ,1)
request=b'\x51\x06'+struct.pack('!HHHHH',0x100,1,0,0,0)+question
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.settimeout(5);s.sendto(request,('192.0.2.3',1053));data=s.recv(4096)
def skip(i):
 while data[i]:
  if data[i]&0xc0==0xc0:return i+2
  i+=data[i]+1
 return i+1
p=skip(12)+4;out=[]
for _ in range(struct.unpack('!H',data[6:8])[0]):
 p=skip(p);t,c,ttl,n=struct.unpack('!HHIH',data[p:p+10]);p+=10
 if t==typ:out.append(socket.inet_ntop(socket.AF_INET if typ==1 else socket.AF_INET6,data[p:p+n]))
 p+=n
print(json.dumps(out))
'''
    addresses = json.loads(guest(153, 'python3', '-c', script, family).stdout)
    if not addresses and allow_empty:
        return None
    if not addresses or not addresses[0].startswith('198.18.' if family == 4 else 'fc00:'):
        raise RuntimeError(f'Mihomo did not return an IPv{family} Fake-IP: {addresses}')
    return addresses[0]


def transfer(label, family=4):
    count_script = "from pathlib import Path;p=Path('/opt/cf-ipv6-fixture/requests.jsonl');print(len(p.read_text().splitlines()) if p.exists() else 0)"
    before = int(guest(154, 'python3', '-c', count_script).stdout)
    address = fake_address(family)
    resolved = '[' + address + ']' if family == 6 else address
    result = guest(153, 'curl', '--noproxy', '*', '-sS', '--connect-timeout', '8', '--max-time', '12',
                   '--resolve', 'fixture.lab.invalid:18080:' + resolved,
                   'http://fixture.lab.invalid:18080/' + label)
    value = json.loads(result.stdout)
    if value.get('path') != '/' + label or value.get('origin') != 'IPv6-node-fixture':
        raise RuntimeError('The request did not reach the isolated proxy fixture')
    log = guest(154, 'cat', '/opt/cf-ipv6-fixture/requests.jsonl').stdout.decode()
    rows = [json.loads(line) for line in log.splitlines()][before:]
    if not any(row.get('kind') == 'proxy' and ':' in row.get('source', '')
               and row.get('server') == '2001:db8:150:2::10' for row in rows):
        raise RuntimeError('No evidence of Mihomo connecting to the node over IPv6')
    if not any(row.get('kind') == 'http' and row.get('path') == '/' + label for row in rows):
        raise RuntimeError('The fixture did not record this individual HTTP request')
    return {'client_family': family, 'fake_ip': address, 'response': value,
            'node_transport': 'IPv6', 'node': '2001:db8:150:2::10:1080'}


def test():
    guard()
    report = {'scope': 'Fresh isolated PVE CHR7.25 + Debian LXC; no production or Internet endpoints',
              'state': 'running',
              'agent_binary_sha256': json.loads((ROOT / 'manifest.json').read_text()).get('agent_binary_sha256'),
              'rounds': [], 'started': time.time()}
    save('agent-report.json', report)
    original = metadata()
    status = agent_api('GET', '/api/ipv6-readiness')['body']
    assert status['interface'] == 'eth1', status
    assert status['interface_index'] > 0, status
    assert metadata() == original, 'GET modified configuration'
    report['read_only_inspection'] = True
    stale = agent_api('POST', '/api/ipv6-readiness/prepare',
                      {'interface': 'eth1', 'interface_index': status['interface_index'] + 1000}, allow_error=True)
    assert stale['code'] == 409 and metadata() == original, stale
    report['stale_interface_rejected_without_writes'] = True
    conflict = '/etc/sysctl.d/zzzz-cf-ipv6-lab-conflict.conf'
    write_remote(conflict, 'net.ipv6.conf.all.forwarding=0\n', mode=0o644, cid=152)
    conflicted_before = metadata()
    try:
        blocked = agent_api('GET', '/api/ipv6-readiness')['body']
        assert blocked['can_prepare'] is False, blocked
        rejected = agent_api('POST', '/api/ipv6-readiness/prepare',
                            {'interface': 'eth1', 'interface_index': status['interface_index']}, allow_error=True)
        assert rejected['code'] == 409 and metadata() == conflicted_before, rejected
    finally:
        guest(152, 'rm', conflict)
    report['later_user_config_conflict_preserved'] = True
    # Reproduce forwarding+accept_ra=1 dropping the learned default route.
    guest(152, 'sysctl', '-w', 'net.ipv6.conf.all.forwarding=1', 'net.ipv6.conf.eth1.accept_ra=1')
    guest(152, 'ip', '-6', 'route', 'flush', 'default', 'dev', 'eth1')
    before = agent_api('GET', '/api/ipv6-readiness')['body']
    assert not before['default_routes'] and before['interface'] == 'eth1', before
    applied = agent_api('POST', '/api/ipv6-readiness/prepare',
                        {'interface': before['interface'], 'interface_index': before['interface_index']})['body']
    assert applied.get('changed') is True, applied
    ready = wait_ready()
    assert ready['runtime']['all.forwarding'] == 1 and ready['runtime']['eth1.accept_ra'] == 2, ready
    assert any(route['protocol'] == 'ra' for route in ready['default_routes']), ready
    probe = agent_api('POST', '/api/ipv6-readiness/probe', {'host': '2001:db8:150:2::10', 'port': 1080})['body']
    assert probe.get('connectivity', {}).get('status') == 'ok', probe
    closed = agent_api('POST', '/api/ipv6-readiness/probe', {'host': '2001:db8:150:2::10', 'port': 1081})['body']
    assert closed.get('connectivity', {}).get('status') == 'error', closed
    report['closed_node_port_not_masked_by_TUN'] = closed
    fake_probe = agent_api('POST', '/api/ipv6-readiness/probe', {'host': 'fc00::5', 'port': 1080}, allow_error=True)
    assert fake_probe['code'] >= 400 or fake_probe['body'].get('connectivity', {}).get('status') != 'ok', fake_probe
    report['fake_ip_probe_not_reported_as_node_success'] = fake_probe
    domain_probe = agent_api('POST', '/api/ipv6-readiness/probe', {'host': 'node.lab.invalid', 'port': 1080}, allow_error=True)
    connectivity = domain_probe['body'].get('connectivity', {})
    assert connectivity.get('status') != 'ok' or connectivity.get('address') == '2001:db8:150:2::10', domain_probe
    report['domain_probe_avoids_fake_ip_success'] = domain_probe
    report['rounds'].append({'name': 'first_apply_repairs_RA', 'status': ready, 'probe': probe,
                             'fake_ipv6_answer': fake_address(6),
                             'transfer': transfer('round1-ipv4-via-ipv6')})
    preserved = metadata()
    second = agent_api('POST', '/api/ipv6-readiness/prepare',
                       {'interface': ready['interface'], 'interface_index': ready['interface_index']})['body']
    assert second.get('changed', False) is False, second
    assert metadata() == preserved, 'Repeated preparation rewrote files'
    report['rounds'].append({'name': 'repeat_apply_idempotent', 'files_unchanged': True,
                             'fake_ipv6_answer': fake_address(6),
                             'transfer': transfer('round2-ipv4-via-ipv6')})
    guest(152, 'systemctl', 'restart', 'configflow-agent')
    wait_agent()
    assert metadata() == preserved, 'Agent startup unexpectedly rewrote correct configuration'
    report['agent_restart_preserved'] = True
    old_uptime = guest(152, 'cat', '/proc/uptime').stdout.decode().split()[0]
    start_script = "from pathlib import Path;print(Path('/proc/1/stat').read_text().rsplit(') ',1)[1].split()[19])"
    old_start = guest(152, 'python3', '-c', start_script).stdout.strip()
    # Deterministically reproduce a slow upstream: the core starts before RA,
    # so YAML alone cannot prove its actual TUN supports IPv6. The managed boot
    # recovery must notice arriving IPv6 and reload only the running core.
    ros('/ipv6 nd disable [find where interface=ether2]')
    try:
        guest(152, 'ip', '-6', 'route', 'flush', 'default', 'dev', 'eth1')
        guest(152, 'ip', '-6', 'addr', 'flush', 'dev', 'eth1', 'scope', 'global')
        pve('pct', 'reboot', 152, timeout=90)
        # pct reboot can acknowledge before the old Agent has disconnected.
        # Wait for the NEW PID1 rather than treating a successful old API reply
        # as evidence that the reboot already finished.
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            started = guest(152, 'python3', '-c', start_script, check=False, timeout=10)
            if started.returncode == 0 and started.stdout.strip() != old_start:
                break
            time.sleep(.5)
        else:
            raise RuntimeError('The container did not enter a new boot')
        early = wait_agent()['body']
        require_late = not early.get('default_routes')
        if not require_late:
            raise RuntimeError('RA delay fixture failed: a default IPv6 route already exists')
        report['before_delayed_ra'] = early
    finally:
        ros('/ipv6 nd enable [find where interface=ether2]')
    rebooted = wait_ready(timeout=45)
    after_uptime = guest(152, 'cat', '/proc/uptime').stdout.decode().split()[0]
    assert float(after_uptime) < float(old_uptime), 'Container reboot was not observed'
    assert metadata() == preserved, 'Reboot rewrote persisted configuration'
    assert rebooted['runtime']['all.forwarding'] == 1 and rebooted['runtime']['eth1.accept_ra'] == 2, rebooted
    report['rounds'].append({'name': 'container_reboot_recovers_RA_and_forwarding',
                             'runtime': rebooted['runtime'], 'routes': rebooted['default_routes'],
                             'upstream_ra_delayed_until_core_started': True,
                             'uptime_before': old_uptime, 'uptime_after': after_uptime,
                             'tun_ipv6': rebooted.get('tun_ipv6'), 'fake_ipv6_answer': fake_address(6),
                             'transfer': transfer('round3-ipv4-via-ipv6')})
    # Wait beyond the 30-second RA default-router lifetime. Continued access
    # then demonstrates renewal instead of reusing a surviving static route.
    time.sleep(35)
    renewed = wait_ready()
    assert any(route['protocol'] == 'ra' for route in renewed['default_routes']), renewed
    report['ra_renewal_beyond_lifetime'] = transfer('ra-renewed-ipv4-via-ipv6')
    report['completed'] = time.time()
    report['state'] = 'passed'
    save('agent-report.json', report)
    print(json.dumps({'rounds': [item['name'] for item in report['rounds']],
                      'read_only': True, 'idempotence': True, 'real_reboot': True,
                      'ra_renewal': True, 'report': str(ROOT / 'agent-report.json')}, indent=2))


def smoke():
    """Verify a final binary refresh without repeating disruptive fault setup."""
    manifest = guard()
    previous = json.loads((ROOT / 'agent-report.json').read_text())
    if previous.get('state') != 'passed':
        raise RuntimeError('Complete the three real Agent rounds before the final smoke')
    before = metadata()
    ready = wait_ready()
    repeated = agent_api('POST', '/api/ipv6-readiness/prepare', {
        'interface': ready['interface'], 'interface_index': ready['interface_index']})['body']
    assert repeated.get('changed', False) is False, repeated
    assert metadata() == before, 'Final binary rewrote a prepared host'
    probes = {}
    for port, expected in ((1080, 'ok'), (1081, 'error')):
        probes[str(port)] = agent_api('POST', '/api/ipv6-readiness/probe', {
            'host': '2001:db8:150:2::10', 'port': port})['body']
        assert probes[str(port)]['connectivity']['status'] == expected, probes[str(port)]
    report = {'state': 'passed', 'completed': time.time(),
              'agent_binary_sha256': manifest['agent_binary_sha256'],
              'three_round_binary_sha256': previous['agent_binary_sha256'],
              'files_unchanged': True, 'tun_ipv6': ready.get('tun_ipv6'),
              'probes': probes,
              'ipv4_transfer': transfer('final-smoke-ipv4-via-ipv6'),
              'ipv6_transfer': transfer('final-smoke-fake6-via-ipv6', family=6)}
    save('final-smoke-report.json', report)
    print(json.dumps({'state': report['state'], 'files_unchanged': True,
                      'ipv4_and_ipv6_transfers': True,
                      'report': str(ROOT / 'final-smoke-report.json')}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'install', 'refresh', 'status', 'test', 'smoke', 'cleanup'])
    parser.add_argument('--agent-binary', type=Path)
    parser.add_argument('--reset-managed', action='store_true')
    args = parser.parse_args()
    if args.action == 'prepare':
        prepare()
    elif args.action == 'install':
        if not args.agent_binary:
            parser.error('--agent-binary is required')
        install(args.agent_binary)
    elif args.action == 'refresh':
        refresh(args.agent_binary, args.reset_managed)
    elif args.action == 'status':
        guard()
        print(json.dumps(agent_api('GET', '/api/ipv6-readiness'), ensure_ascii=False, indent=2))
    elif args.action == 'smoke':
        smoke()
    elif args.action == 'test':
        try:
            test()
        except Exception as error:
            path = ROOT / 'agent-report.json'
            if path.exists():
                value = json.loads(path.read_text())
                value.update(state='failed', error=str(error), completed=time.time())
                save('agent-report.json', value)
            raise
    else:
        manifest = guard()
        source = pve('pct', 'config', 130).stdout
        if hashlib.sha256(source).hexdigest() != manifest['source_config_sha256']:
            raise RuntimeError('Clone source configuration changed; inspect before cleanup')
        # Read-only production snapshots prove teardown did not change either
        # existing router or proxy. These guests never enter mutation helpers.
        def production_snapshot():
            return {str(gid): {
                'config_sha256': hashlib.sha256(pve(tool, 'config', gid).stdout).hexdigest(),
                'status': pve(tool, 'status', gid).stdout.decode().strip(),
            } for gid, tool in ((101, 'qm'), (105, 'pct'))}
        production_before = production_snapshot()
        for gid in (152, 153, 154, 151):
            tool = 'qm' if gid == 151 else 'pct'
            pve(tool, 'stop', gid, check=False)
            pve(tool, 'destroy', gid, '--purge', 1)
        for bridge in reversed(BRIDGES):
            pve('ip', 'link', 'delete', bridge)
        inventory = json.loads(pve('pvesh', 'get', '/cluster/resources', '--type', 'vm', '--output-format', 'json').stdout)
        if set(GUESTS) & {int(row['vmid']) for row in inventory}:
            raise RuntimeError('A task-owned guest remains after teardown')
        for bridge in BRIDGES:
            if pve('ip', 'link', 'show', bridge, check=False).returncode == 0:
                raise RuntimeError('A task-owned bridge remains after teardown')
        if production_snapshot() != production_before:
            raise RuntimeError('Production state changed during teardown; inspect the read-only snapshot')
        if hashlib.sha256(pve('pct', 'config', 130).stdout).hexdigest() != manifest['source_config_sha256']:
            raise RuntimeError('Clone source configuration changed during teardown')
        manifest.update(status='completed-cleaned', cleaned=time.time(), source_config_preserved=True,
                        all_owned_guests_removed=True, all_owned_bridges_removed=True,
                        production_cleanup_snapshot_preserved=production_before)
        save('manifest.json', manifest)
        write_remote(PVE_ROOT + '/owner.json', json.dumps(manifest))
        pve('rm', '-f', PVE_ROOT + '/id_lab', PVE_ROOT + '/id_lab.pub')
        (ROOT / 'private.json').unlink()
        (ROOT / 'tunnel.log').unlink(missing_ok=True)
        shutil.rmtree(ROOT / 'controller-data', ignore_errors=True)
        print('Only task-owned guests and bridges removed')


if __name__ == '__main__':
    main()
