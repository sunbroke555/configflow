#!/usr/bin/env python3
"""Real Docker HA acceptance in two separate, snapshot-protected LXC hosts.

Run only from 192.168.0.32. No production guest is in the explicit allowlist.
Every Docker runtime, image and test address lives inside CT130/131, while the
backend and independent ARP/DNS observers run in CT132.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

import oxidns_ha_sync_lab as lab

HOST_ROOT = Path.home() / 'configflow-docker-ha-lab'
lab.ROOT = HOST_ROOT / 'private'
lab.GROOT = '/opt/cf-docker-ha'
lab.SNAPSHOT = 'cf-docker-ha-20261027'
lab.UNIT_PREFIX = 'cf-docker-ha'
lab.SCRIPT = 'scripts/tests/oxidns_docker_ha_lab.py'
lab.LOCAL_URL = 'http://127.0.0.1:19284'
SOCKET = 'unix://' + lab.GROOT + '/docker.sock'
IMAGE = 'cf-docker-ha-kernel:20261027'
NETWORK = 'cf-docker-ha-macvlan'
PARENT = 'eth0.91'
VIP = '192.168.79.53'


def docker(cid, *args, data=None, check=True):
    if cid not in (130, 131):
        raise ValueError('Only the two independent Docker hosts are allowed')
    return lab.guest(cid, lab.GROOT + '/docker/docker', '-H', SOCKET, *args, data=data, check=check)


def install_hosts():
    lab.guard()
    archive = (HOST_ROOT / 'docker-29.9.2.tgz').read_bytes()
    for cid in (130, 131):
        lab.guest(cid, 'mkdir', '-p', lab.GROOT)
        lab.guest(cid, 'chown', 'root:root', '/opt')
        lab.guest(cid, 'tar', '-xzf', '-', '-C', lab.GROOT, data=archive)
        config = {'data-root': lab.GROOT + '/docker-data', 'exec-root': '/run/cf-docker-ha-docker',
                  'pidfile': '/run/cf-docker-ha-docker/docker.pid', 'hosts': [SOCKET],
                  'bridge': 'none', 'iptables': False, 'ip6tables': False,
                  'ip-forward': False, 'ip-masq': False, 'storage-driver': 'overlay2'}
        lab.put(cid, lab.GROOT + '/docker-daemon.json', json.dumps(config))
        lab.guest(cid, 'systemd-run', '--unit=cf-docker-ha-dockerd', '--property=Delegate=yes',
                  '--property=TasksMax=infinity', '--property=KillMode=process',
                  '--setenv=PATH=' + lab.GROOT + '/docker:/usr/sbin:/usr/bin:/sbin:/bin',
                  lab.GROOT + '/docker/dockerd', '--config-file=' + lab.GROOT + '/docker-daemon.json')
        for _ in range(30):
            version = docker(cid, 'version', '--format', '{{.Server.Version}}', check=False).decode().strip()
            if version:
                print(json.dumps({'host': cid, 'version': version}), flush=True)
                break
            time.sleep(1)
        else: raise RuntimeError('Docker daemon did not become ready on CT' + str(cid))


def load_kernel_image():
    lab.guard()
    archive = (HOST_ROOT / 'kernel-image.tar').read_bytes()
    for cid in (130, 131):
        docker(cid, 'load', data=archive)
        docker(cid, 'network', 'create', '-d', 'macvlan', '--subnet=192.168.79.0/24',
               '-o', 'parent=' + PARENT, NETWORK)
        print(json.dumps({'host': cid, 'kernel_image_loaded': True}), flush=True)


def arp_observe():
    script = '''import json,socket,struct,time
from pathlib import Path
interface='eth0.91'; target=socket.inet_aton('192.168.79.53'); source=socket.inet_aton('192.168.79.30')
mac=bytes.fromhex(Path('/sys/class/net/'+interface+'/address').read_text().strip().replace(':',''))
frame=b'\\xff'*6+mac+b'\\x08\\x06'+struct.pack('!HHBBH',1,0x800,6,4,1)+mac+source+b'\\0'*6+target
seen=set()
with socket.socket(socket.AF_PACKET,socket.SOCK_RAW,socket.htons(0x806)) as probe:
 probe.bind((interface,0)); probe.settimeout(.3)
 for _ in range(3):
  probe.send(frame); until=time.monotonic()+.3
  while time.monotonic()<until:
   try: data=probe.recv(4096)
   except socket.timeout: break
   if len(data)>=42 and data[20:22]==b'\\0\\x02' and data[28:32]==target:
    seen.add(':'.join(f'{part:02x}' for part in data[22:28]))
print(json.dumps(sorted(seen)))
'''
    return json.loads(lab.guest(132, 'python3', '-c', script))


def host_has_vip():
    links = json.loads(lab.guest(130, 'ip', '-j', 'address', 'show', 'dev', PARENT))
    return any(address.get('local') == VIP for link in links for address in link.get('addr_info', []))


def kernel_probe():
    lab.guard()
    report = {'started_at': time.time(), 'hosts': [130, 131], 'observer': 132,
              'kernel_image': docker(130, 'image', 'inspect', IMAGE, '--format', '{{.Id}}').decode().strip(),
              'cases': []}
    assert not host_has_vip() and not arp_observe(), 'Test VIP already has an owner'
    for mode, signal in (('macvlan', 'stop'), ('macvlan', 'kill'), ('host', 'stop'), ('host', 'kill')):
        interface = 'eth0' if mode == 'macvlan' else PARENT
        local = '192.168.79.81' if mode == 'macvlan' else '192.168.79.21'
        peer = '192.168.79.82' if mode == 'macvlan' else '192.168.79.22'
        config = f'''global_defs {{
  router_id CF_DOCKER_KERNEL_PROBE
}}
vrrp_instance CF_KERNEL {{
  state MASTER
  interface {interface}
  virtual_router_id 251
  priority 150
  advert_int 1
  unicast_src_ip {local}
  unicast_peer {{
    {peer}
  }}
  virtual_ipaddress {{
    {VIP}/32 dev {interface}
  }}
}}
'''
        lab.put(130, lab.GROOT + '/kernel/keepalived.conf', config)
        name = 'cf-docker-ha-kernel-' + mode + '-' + signal
        args = ['run', '-d', '--name', name, '--network', NETWORK if mode == 'macvlan' else 'host',
                '--cap-add=NET_ADMIN', '--cap-add=NET_RAW', '-v', lab.GROOT + '/kernel/keepalived.conf:/etc/keepalived/keepalived.conf:ro']
        if mode == 'macvlan': args += ['--ip', local]
        args += [IMAGE, 'keepalived', '-n', '-l', '-D', '-f', '/etc/keepalived/keepalived.conf']
        try:
            docker(130, *args)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                addresses = json.loads(docker(130, 'exec', name, 'ip', '-j', 'addr', 'show', 'dev', interface))
                if any(address.get('local') == VIP for link in addresses for address in link.get('addr_info', [])): break
                time.sleep(.3)
            else: raise AssertionError('keepalived did not assign the test VIP')
            before = arp_observe()
            assert before, 'External observer cannot resolve the active test VIP'
            links = json.loads(docker(130, 'exec', name, 'ip', '-j', '-d', 'link', 'show'))
            old_netns = docker(130, 'inspect', name, '--format', '{{.NetworkSettings.SandboxKey}}').decode().strip()
            if signal == 'stop': docker(130, 'stop', '--time=5', name)
            else: docker(130, 'kill', '--signal=KILL', name)
            time.sleep(.5)
            after = arp_observe(); retained = host_has_vip()
            running = docker(130, 'inspect', name, '--format', '{{.State.Running}}').decode().strip()
            record = {'mode': mode, 'termination': signal, 'arp_before': before, 'arp_after': after,
                      'host_retained_vip': retained, 'container_running': running,
                      'network_namespace': old_netns, 'links': links}
            expected_retained = mode == 'host' and signal == 'kill'
            assert running == 'false' and retained == expected_retained, record
            assert bool(after) == expected_retained, record
            report['cases'].append(record)
            print(json.dumps({'case': mode + '-' + signal, 'host_retained_vip': retained, 'arp_after': after}), flush=True)
        finally:
            docker(130, 'rm', '-f', name, check=False)
            if host_has_vip(): lab.guest(130, 'ip', 'address', 'del', VIP + '/32', 'dev', PARENT)
    assert not host_has_vip() and not arp_observe()
    report.update(passed=True, finished_at=time.time(), test_vip_removed=True)
    (HOST_ROOT / 'kernel-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')


APP_IMAGE = 'cf-oxidns-docker-ha:20261027'
CORE = 'cf-docker-ha-core'
COMPANION = 'cf-docker-ha-companion'


def document(address, workers=2):
    value = lab.document(address, workers, main_tag='sequence_main', native_api=True)
    value['plugins'][1] = {'tag': 'forward_fallback', 'type': 'forward', 'args': {
        'upstreams': [{'addr': 'udp://192.168.79.30:' + str(5500 + int(address.rsplit('.', 1)[1]))}]}}
    value['plugins'][2]['args'] = [{'exec': '$forward_fallback'}]
    return value


def dns_fixture():
    import selectors
    import socket
    import struct
    selector = selectors.DefaultSelector()
    for suffix in (10, 11, 20, 30, 40, 50, 60, 70, 80, 90):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(('0.0.0.0', 5500 + suffix)); selector.register(sock, selectors.EVENT_READ, suffix)
    while True:
        for item, _ in selector.select():
            data, peer = item.fileobj.recvfrom(4096)
            if len(data) < 17: continue
            offset = 12
            while offset < len(data) and data[offset]: offset += data[offset] + 1
            end = offset + 5
            if end > len(data): continue
            question = data[12:end]
            answer = b'\xc0\x0c' + struct.pack('!HHIH', 1, 1, 30, 4) + socket.inet_aton('198.51.100.' + str(item.data))
            response = data[:2] + struct.pack('!HHHHH', 0x8180, 1, 1, 0, 0) + question + answer
            item.fileobj.sendto(response, peer)


def prepare_backend():
    import secrets
    lab.guard()
    if (lab.ROOT / 'private.json').exists(): raise RuntimeError('Backend fixture already exists')
    credentials = {'username': 'docker-ha-lab', 'password': secrets.token_urlsafe(30),
                   'jwt_secret': secrets.token_urlsafe(48), 'registration_key': secrets.token_urlsafe(30)}
    lab.private(lab.ROOT / 'private.json', credentials)
    lab.stage_source(); lab.put(132, lab.GROOT + '/credentials.json', json.dumps(credentials))
    lab.guest(132, 'systemd-run', '--unit=' + lab.UNIT_PREFIX + '-backend',
              '--property=WorkingDirectory=' + lab.GROOT, '/opt/cf-retest/venv/bin/python',
              lab.GROOT + '/source/' + lab.SCRIPT, 'server')
    lab.start_tunnel()
    for _ in range(30):
        try:
            _, data = lab.api('POST', '/api/auth/login', {'username': credentials['username'], 'password': credentials['password']})
            credentials['admin_token'] = data['token']; break
        except (OSError, RuntimeError): time.sleep(1)
    else: raise RuntimeError('Backend fixture failed to become ready')
    lab.private(lab.ROOT / 'private.json', credentials)
    lab.api('POST', '/api/server-domain', {'new_domain': lab.SERVER_URL})
    lab.guest(132, 'systemd-run', '--unit=' + lab.UNIT_PREFIX + '-dns-fixture',
              '/opt/cf-retest/venv/bin/python', lab.GROOT + '/source/' + lab.SCRIPT, 'dns-fixture')
    print(json.dumps({'backend_ready': True, 'dns_fixture_ready': True}))


def load_app_image():
    lab.guard()
    archive = (HOST_ROOT / 'app-image.tar').read_bytes()
    for cid in (130, 131):
        docker(cid, 'load', data=archive)
        print(json.dumps({'host': cid, 'image': docker(cid, 'image', 'inspect', APP_IMAGE, '--format', '{{.Id}}').decode().strip()}), flush=True)


def start_containers(cid, mode, *, only_core=False):
    host = lab.GUESTS[cid][1] if mode == 'host' else '192.168.79.' + str(cid - 49)
    root = lab.GROOT + '/' + mode
    common = ['-e', 'CONFIGFLOW_DNS_HA=1', '-e', 'CONFIGFLOW_DNS_HA_NETWORK=' + mode, '-e', 'TZ=UTC',
              '-v', root + '/ha:/var/lib/configflow-ha',
              '-v', '/etc/machine-id:/run/configflow-host-machine-id:ro']
    if mode == 'host' and not only_core:
        docker(cid, 'run', '-d', '--name', COMPANION, '--network', 'host',
               '--cap-add=NET_ADMIN', '--cap-add=NET_RAW', '-e', 'SERVICE_TYPE=ha-companion',
               '-v', root + '/agent:/opt/configflow-agent:ro', '-v', root + '/oxidns:/etc/oxidns:ro',
               *common, APP_IMAGE)
    args = ['run', '-d', '--name', CORE, '--network', 'host' if mode == 'host' else NETWORK,
            '-e', 'SERVICE_TYPE=oxidns', '-e', 'SERVER_URL=' + lab.SERVER_URL,
            '-e', 'AGENT_IP=' + host, '-e', 'AGENT_NAME=Docker HA ' + str(cid), '-e', 'AGENT_PORT=18081',
            '-e', 'HEARTBEAT_INTERVAL=2', '-e', 'DEPLOYMENT_HEALTH_TIMEOUT=8',
            '-e', 'OXIDNS_HEALTH_DNS_ADDRESS=127.0.0.1:53', '-e', 'OXIDNS_HEALTH_DNS_NAME=sync.lab.invalid',
            '-v', root + '/agent:/opt/configflow-agent', '-v', root + '/oxidns:/etc/oxidns', *common]
    if mode == 'host': args += ['-v', '/proc/1/ns/net:/run/configflow-host-netns:ro']
    else: args += ['--ip', host, '--cap-add=NET_ADMIN', '--cap-add=NET_RAW']
    docker(cid, *args, APP_IMAGE)


def agent_api(cid, method, path, body=None, *, mode=None):
    credentials = json.loads((lab.ROOT / 'private.json').read_text())
    mode = mode or credentials['mode']
    member = credentials['pairs'][mode][str(cid)]
    host = lab.GUESTS[cid][1] if mode == 'host' else '192.168.79.' + str(cid - 49)
    script = '''import json,sys,urllib.error,urllib.request
v=json.load(sys.stdin)
r=urllib.request.Request(v['url'],method=v['method'],headers={'Authorization':'Bearer '+v['token'],'Content-Type':'application/json'},data=None if v['body'] is None else json.dumps(v['body']).encode())
try:
 with urllib.request.urlopen(r,timeout=30) as response: code,data=response.status,json.load(response)
except urllib.error.HTTPError as error: code,data=error.code,json.load(error)
print(json.dumps({'code':code,'data':data}))
'''
    value = {'url': 'http://' + host + ':18081' + path, 'method': method, 'body': body, 'token': member['token']}
    return json.loads(lab.guest(132, 'python3', '-c', script, data=json.dumps(value).encode()))


def prepare_mode(mode):
    lab.guard()
    credentials = json.loads((lab.ROOT / 'private.json').read_text())
    members = {}
    for cid, address in ((130, '198.51.100.10'), (131, '198.51.100.11')):
        root = lab.GROOT + '/' + mode
        lab.guest(cid, 'mkdir', '-p', root + '/agent', root + '/oxidns/rules', root + '/ha')
        lab.guest(cid, 'chmod', '700', root + '/agent', root + '/ha')
        lab.put(cid, root + '/oxidns/config.yaml', json.dumps(document(address)))
        lab.put(cid, root + '/oxidns/rules/lab.txt', 'full:initial-' + str(cid) + '.invalid\n')
        host = lab.GUESTS[cid][1] if mode == 'host' else '192.168.79.' + str(cid - 49)
        _, member = lab.api('POST', '/api/agents/register', {'name': 'Docker HA ' + mode + ' ' + str(cid),
            'host': host, 'port': 18081, 'registration_key': credentials['registration_key'],
            'service_type': 'oxidns', 'deployment_method': 'docker', 'profile_id': 'default'})
        assert member['token'] != '[REDACTED]'
        members[str(cid)] = member
        lab.put(cid, root + '/agent/config-oxidns.json', json.dumps({'agent_id': member['id'], 'token': member['token']}))
    credentials.setdefault('pairs', {})[mode] = members; credentials['mode'] = mode
    lab.private(lab.ROOT / 'private.json', credentials)
    for cid in (130, 131): start_containers(cid, mode)
    for cid in (130, 131):
        for _ in range(30):
            try:
                result = agent_api(cid, 'GET', '/api/capabilities')
                if result['code'] == 200:
                    print(json.dumps({'host': cid, 'mode': mode, 'capabilities': result['data']}), flush=True)
                    break
            except Exception: pass
            time.sleep(1)
        else: raise RuntimeError('Docker Agent did not become reachable on ' + str(cid))


def mode_state():
    credentials = json.loads((lab.ROOT / 'private.json').read_text())
    mode = credentials['mode']
    return mode, {cid: credentials['pairs'][mode][str(cid)]['id'] for cid in (130, 131)}


def dns_observe(mode):
    hosts = [lab.GUESTS[cid][1] if mode == 'host' else '192.168.79.' + str(cid - 49) for cid in (130, 131)] + [VIP]
    script = 'import sys,json; sys.path.insert(0,' + repr(lab.GROOT + '/source/scripts/tests') + '); from oxidns_ha_sync_lab import dns_query; print(json.dumps({host:dns_query(host) for host in ' + repr(hosts) + '}))'
    return json.loads(lab.guest(132, 'python3', '-c', script))


def core_snapshot(cid):
    script = '''import hashlib,json,subprocess
from pathlib import Path
config=json.loads(Path('/opt/configflow-agent/config-oxidns.json').read_text())
print(json.dumps({'agent_id':config['agent_id'],'hashes':{p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in ['/etc/oxidns/config.yaml','/etc/oxidns/rules/lab.txt']},'core_pid':subprocess.check_output(['supervisorctl','-c','/etc/supervisor/supervisord.conf','pid','oxidns'],stderr=subprocess.DEVNULL,text=True).strip()}))
'''
    value = json.loads(docker(cid, 'exec', CORE, 'python3', '-c', script))
    value['container_id'] = docker(cid, 'inspect', CORE, '--format', '{{.Id}}').decode().strip()
    return value


def owners(mode):
    result = []
    for cid in (130, 131):
        if mode == 'host': raw = lab.guest(cid, 'ip', '-j', 'addr', 'show', 'dev', PARENT)
        elif docker(cid, 'inspect', CORE, '--format', '{{.State.Running}}').decode().strip() == 'true':
            raw = docker(cid, 'exec', CORE, 'ip', '-j', 'addr', 'show', 'dev', 'eth0')
        else: continue
        if any(address.get('local') == VIP for link in json.loads(raw) for address in link.get('addr_info', [])):
            result.append(cid)
    return result


def wait_owners(mode, expected, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if owners(mode) == expected: return
        # A status read also exercises the Agent's actual recovery reconciliation.
        for cid in (130, 131):
            try: agent_api(cid, 'GET', '/api/oxidns-ha')
            except Exception: pass
        time.sleep(.3)
    raise AssertionError('VIP holders did not converge to ' + str(expected))


def set_saved(address, workers, rule):
    _, settings = lab.api('GET', '/api/oxidns/settings')
    settings.update(config_mode='native', native_config=json.dumps(document(address, workers)),
                    additional_files=[{'path': 'rules/lab.txt', 'encoding': 'text', 'content': rule}])
    lab.api('POST', '/api/oxidns/settings', settings)


def start_probe():
    mode, _ = mode_state()
    lab.put(132, lab.GROOT + '/source/' + lab.SCRIPT, Path(__file__).read_bytes())
    lab.guest(132, 'systemd-run', '--unit=' + lab.UNIT_PREFIX + '-probe-' + mode,
              '/opt/cf-retest/venv/bin/python', lab.GROOT + '/source/' + lab.SCRIPT,
              'probe' if mode == 'host' else 'probe-macvlan')
    time.sleep(.5)


def core_events(started):
    from datetime import datetime
    result = {}
    for cid in (130, 131):
        entries = []
        for line in docker(cid, 'logs', '--timestamps', '--since', str(started), CORE).decode().splitlines():
            if not any(value in line for value in ("spawned: 'oxidns'", 'stopped: oxidns', 'exited: oxidns')): continue
            timestamp, _, message = line.partition(' ')
            entries.append({'time': datetime.fromisoformat(timestamp.replace('Z', '+00:00')).timestamp(), 'message': message})
        result[str(cid)] = entries
    return result


def record(name, started, **details):
    mode, _ = mode_state()
    samples = [json.loads(line) for line in lab.guest(132, 'cat', lab.GROOT + '/dns-probe.jsonl').splitlines()]
    selected = [sample for sample in samples if started <= sample['time'] <= time.time()]
    item = {'name': name, 'started_at': started, 'finished_at': time.time(), 'dns_probe': {
        'samples': len(selected), 'vip_failed_samples': sum(not sample['nodes'][2]['ok'] for sample in selected),
        'both_nodes_unavailable_samples': sum(not sample['nodes'][0]['ok'] and not sample['nodes'][1]['ok'] for sample in selected)}, **details}
    path = HOST_ROOT / (mode + '-report.json'); report = json.loads(path.read_text())
    report['scenarios'].append(item); path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'scenario': name, 'passed': True, 'dns_probe': item['dns_probe']}, ensure_ascii=False), flush=True)
    return item, selected


def test_publish():
    mode, ids = mode_state(); source = owners(mode)[0]; target = 131 if source == 130 else 130
    address = '198.51.100.30' if mode == 'host' else '198.51.100.80'
    started = time.time(); set_saved(address, 4, 'full:docker-published-rule.invalid\n')
    lab.publish(ids[131]); complete = lab.wait_ha(ids[131])
    snapshots = {str(cid): core_snapshot(cid) for cid in (130, 131)}
    assert snapshots['130']['hashes'] == snapshots['131']['hashes']
    answers = dns_observe(mode); assert all(answer.get('ip') == address for answer in answers.values())
    events = core_events(started)
    starts = [entry['time'] for entry in events[str(target)] if "spawned: 'oxidns'" in entry['message']]
    stops = [entry['time'] for entry in events[str(source)] if 'stopped: oxidns' in entry['message'] or 'exited: oxidns' in entry['message']]
    raw = [json.loads(line) for line in lab.guest(132, 'cat', lab.GROOT + '/dns-probe.jsonl').splitlines()]
    ready = [sample['time'] for sample in raw if started <= sample['time'] and sample['nodes'][2].get('ip') == address]
    assert starts and stops and ready and starts[0] < ready[0] < stops[0], {'events': events, 'ready': ready[:1]}
    record('publication-migrates-vip-before-old-core-restart', started, publication=complete['publication'],
           files=snapshots, answers=answers, service_events=events,
           handoff_proof={'standby_started_at': starts[0], 'updated_vip_dns_at': ready[0], 'old_core_stopped_at': stops[0]})


def test_core_kill():
    mode, _ = mode_state(); source = owners(mode)[0]; target = 131 if source == 130 else 130
    before = core_snapshot(source); started = time.time()
    docker(source, 'kill', '--signal=KILL', CORE)
    companion_running = mode != 'host' or docker(source, 'inspect', COMPANION, '--format', '{{.State.Running}}').decode().strip() == 'true'
    assert companion_running
    try:
        wait_owners(mode, [target]); answer = dns_observe(mode)[VIP]; assert answer.get('ok')
        no_residual_source_vip = source not in owners(mode)
    finally: docker(source, 'start', CORE)
    wait_owners(mode, [130])
    for _ in range(30):
        try:
            after = core_snapshot(source); assert after['agent_id'] == before['agent_id']; agent_api(source, 'GET', '/api/capabilities'); break
        except Exception: time.sleep(.5)
    else: raise AssertionError('Core restart did not recover Agent identity and service')
    record('core-container-kill-and-recovery', started, before=before, after=after,
           companion_running=companion_running, peer_dns=answer, source_vip_removed=no_residual_source_vip)


def test_recreate():
    mode, _ = mode_state(); source = owners(mode)[0]; target = 131 if source == 130 else 130
    before = core_snapshot(source); started = time.time()
    docker(source, 'stop', '--time=30', CORE); wait_owners(mode, [target])
    docker(source, 'rm', CORE); start_containers(source, mode, only_core=True)
    wait_owners(mode, [130]); after = core_snapshot(source)
    assert after['container_id'] != before['container_id'] and after['agent_id'] == before['agent_id'] and after['hashes'] == before['hashes']
    assert agent_api(source, 'GET', '/api/capabilities')['code'] == 200
    record('normal-stop-and-container-recreation-preserve-identity-and-files', started, before=before, after=after)


def test_guardian_restart():
    mode, _ = mode_state(); assert mode == 'host'
    started = time.time(); before = core_snapshot(130)
    docker(130, 'restart', '--time=30', COMPANION)
    wait_owners(mode, [130]); runtime = agent_api(130, 'GET', '/api/oxidns-ha')['data']
    deadline = time.monotonic() + 40
    while not runtime.get('runtime', {}).get('keepalived_running') and time.monotonic() < deadline:
        time.sleep(.5); runtime = agent_api(130, 'GET', '/api/oxidns-ha')['data']
    assert runtime.get('runtime', {}).get('keepalived_running'), runtime
    after = core_snapshot(130); assert before == after
    record('normal-guardian-restart-recovers-with-core-untouched', started, before=before, after=after, runtime=runtime)


def finish_probe():
    import hashlib
    mode, _ = mode_state()
    lab.guest(132, 'touch', lab.GROOT + '/stop-probe'); time.sleep(.5)
    raw = lab.guest(132, 'cat', lab.GROOT + '/dns-probe.jsonl')
    (HOST_ROOT / (mode + '-dns-probe.jsonl')).write_bytes(raw)
    samples = [json.loads(line) for line in raw.splitlines()]
    path = HOST_ROOT / (mode + '-report.json'); report = json.loads(path.read_text())
    report['dns_probe'] = {'sample_count': len(samples), 'sha256': hashlib.sha256(raw).hexdigest(),
        'vip_failed_samples': sum(not sample['nodes'][2]['ok'] for sample in samples),
        'both_nodes_unavailable_samples': sum(not sample['nodes'][0]['ok'] and not sample['nodes'][1]['ok'] for sample in samples)}
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report['dns_probe']), flush=True)


def enable_mode():
    mode, ids = mode_state(); path = HOST_ROOT / (mode + '-report.json')
    if path.exists(): raise RuntimeError('A report already exists for this mode')
    address = '198.51.100.20' if mode == 'host' else '198.51.100.70'
    initial = {str(cid): core_snapshot(cid) for cid in (130, 131)}
    answers = dns_observe(mode)
    node_hosts = [lab.GUESTS[cid][1] if mode == 'host' else '192.168.79.' + str(cid - 49) for cid in (130, 131)]
    assert [answers[host].get('ip') for host in node_hosts] == ['198.51.100.10', '198.51.100.11']
    started = time.time(); set_saved(address, 3, 'full:docker-ha-shared.invalid\n')
    lab.api('PUT', f'/api/agents/{ids[130]}/oxidns-ha', {'peer_id': ids[131], 'vip': VIP})
    complete = lab.wait_ha(ids[130]); final = {str(cid): core_snapshot(cid) for cid in (130, 131)}
    assert final['130']['hashes'] == final['131']['hashes']
    answers_after = dns_observe(mode); assert all(answer.get('ip') == address for answer in answers_after.values())
    report = {'mode': mode, 'started_at': started, 'initial_hashes': initial, 'initial_dns': answers,
              'agent_version': docker(130, 'exec', CORE, '/usr/local/bin/configflow-agent', '--version').decode().strip(),
              'scenarios': [{'name': 'enable-synchronizes-different-real-fallback-upstreams',
                             'publication': complete['publication'], 'files': final, 'dns': answers_after,
                             'finished_at': time.time()}]}
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'mode': mode, 'enabled': True, 'answers': answers_after}), flush=True)


def test_rules():
    mode, ids = mode_state(); started = time.time(); before = {str(cid): core_snapshot(cid) for cid in (130, 131)}
    code, result = lab.api('POST', f'/api/agents/{ids[131]}/oxidns/rules/update', {})
    assert result.get('success') is True
    complete = lab.wait_ha(ids[131]); after = {str(cid): core_snapshot(cid) for cid in (130, 131)}
    for cid in ('130', '131'):
        assert before[cid]['core_pid'] == after[cid]['core_pid'] and before[cid]['container_id'] == after[cid]['container_id']
    assert after['130']['hashes'] == after['131']['hashes']
    record('manual-rule-update-completes-for-pair-without-core-restarts', started,
           http_status=code, publication=complete['publication'], before=before, after=after)


def test_guardian_boundary():
    mode, _ = mode_state(); assert mode == 'host'
    wait_owners(mode, [130]); started = time.time(); before = core_snapshot(130)
    docker(130, 'kill', '--signal=KILL', COMPANION)
    try:
        wait_owners(mode, [130, 131], timeout=30)
        duplicate_arp = arp_observe(); assert len(duplicate_arp) == 2, duplicate_arp
    finally: docker(130, 'start', COMPANION)
    wait_owners(mode, [130], timeout=45)
    recovered_arp = arp_observe(); assert len(recovered_arp) == 1, recovered_arp
    after = core_snapshot(130); assert before == after
    record('guardian-force-kill-retains-vip-until-guardian-recovery', started,
           duplicate_vip_holders=[130, 131], duplicate_arp=duplicate_arp, recovered_arp=recovered_arp,
           recovered_vip_holders=owners(mode), core_unchanged=True,
           limitation='Killing the host-network HA companion can retain the host VIP; automatic residue removal is not promised.')


def disable_mode():
    mode, ids = mode_state()
    lab.api('DELETE', f'/api/agents/{ids[130]}/oxidns-ha')
    assert not owners(mode)
    print(json.dumps({'mode': mode, 'ha_disabled': True}), flush=True)


def remove_containers():
    mode, _ = mode_state()
    for cid in (130, 131):
        docker(cid, 'stop', '--time=30', CORE, check=False)
        docker(cid, 'rm', CORE, check=False)
        if mode == 'host':
            docker(cid, 'stop', '--time=30', COMPANION, check=False)
            docker(cid, 'rm', COMPANION, check=False)


def start_companion(cid, *, admin=True):
    root = lab.GROOT + '/host'
    args = ['run', '-d', '--name', COMPANION, '--network', 'host', '--cap-add=NET_RAW']
    if admin: args += ['--cap-add=NET_ADMIN']
    docker(cid, *args, '-e', 'SERVICE_TYPE=ha-companion', '-e', 'CONFIGFLOW_DNS_HA=1',
           '-e', 'CONFIGFLOW_DNS_HA_NETWORK=host', '-e', 'TZ=UTC',
           '-v', root + '/ha:/var/lib/configflow-ha',
           '-v', root + '/agent:/opt/configflow-agent:ro', '-v', root + '/oxidns:/etc/oxidns:ro',
           '-v', '/etc/machine-id:/run/configflow-host-machine-id:ro', APP_IMAGE)


def test_rejections():
    mode, ids = mode_state(); assert mode == 'host' and not owners(mode)
    before = {str(cid): core_snapshot(cid) for cid in (130, 131)}
    results = []
    docker(130, 'stop', '--time=30', COMPANION); docker(130, 'rm', COMPANION)
    try:
        start_companion(130, admin=False); time.sleep(2)
        caps = agent_api(130, 'GET', '/api/capabilities')['data']['oxidns_ha_environment']
        assert caps['ready'] is False, caps
        code, data = lab.api('PUT', f'/api/agents/{ids[130]}/oxidns-ha', {'peer_id': ids[131], 'vip': VIP}, allow_error=True)
        assert code == 409, (code, data)
        results.append({'name': 'missing-companion-net-admin-is-rejected', 'http_status': code, 'environment': caps, 'message': data.get('message')})
    finally:
        docker(130, 'stop', '--time=30', COMPANION, check=False); docker(130, 'rm', COMPANION, check=False)
        start_companion(130)
    time.sleep(2)
    assert agent_api(130, 'GET', '/api/capabilities')['data']['oxidns_ha_environment']['ready']
    credentials = json.loads((lab.ROOT / 'private.json').read_text())
    _, member = lab.api('POST', '/api/agents/register', {'name': 'Docker HA same-host rejection', 'host': '192.168.79.81',
        'port': 18083, 'registration_key': credentials['registration_key'], 'service_type': 'oxidns',
        'deployment_method': 'docker', 'profile_id': 'default'})
    config = json.loads(lab.guest(130, 'cat', lab.GROOT + '/host/agent/config-oxidns.json'))
    config.update(agent_id=member['id'], token=member['token'], agent_ip='192.168.79.81', agent_port=18083)
    root = lab.GROOT + '/rejection'
    lab.put(130, root + '/agent/config-oxidns.json', json.dumps(config))
    lab.put(130, root + '/oxidns/config.yaml', json.dumps(document('198.51.100.30', 4)))
    lab.put(130, root + '/oxidns/rules/lab.txt', 'full:docker-published-rule.invalid\n')
    common = ['-e', 'CONFIGFLOW_DNS_HA=1', '-e', 'CONFIGFLOW_DNS_HA_NETWORK=host',
              '-v', root + '/agent:/opt/configflow-agent', '-v', root + '/oxidns:/etc/oxidns',
              '-v', lab.GROOT + '/host/ha:/var/lib/configflow-ha',
              '-v', '/etc/machine-id:/run/configflow-host-machine-id:ro',
              '-v', '/proc/1/ns/net:/run/configflow-host-netns:ro',
              '--entrypoint', '/usr/local/bin/configflow-agent']
    name = 'cf-docker-ha-rejection'
    def shadow_caps():
        script = '''import json,urllib.request
from pathlib import Path
v=json.loads(Path('/opt/configflow-agent/config-oxidns.json').read_text())
r=urllib.request.Request('http://127.0.0.1:18083/api/capabilities',headers={'Authorization':'Bearer '+v['token']})
with urllib.request.urlopen(r,timeout=3) as response: print(json.dumps(json.load(response)['oxidns_ha_environment']))
'''
        for _ in range(20):
            try: return json.loads(docker(130, 'exec', name, 'python3', '-c', script))
            except RuntimeError: time.sleep(.5)
        raise RuntimeError('Shadow Agent did not become reachable')
    lab.guest(130, 'ip', 'addr', 'add', '192.168.79.81/24', 'dev', PARENT)
    try:
        docker(130, 'run', '-d', '--name', name, '--network', 'host', *common, APP_IMAGE,
               '-config', '/opt/configflow-agent/config-oxidns.json')
        caps = shadow_caps(); original = agent_api(130, 'GET', '/api/capabilities')['data']['oxidns_ha_environment']
        assert caps['ready'] and caps['host_id'] == original['host_id'], caps
        code, data = lab.api('PUT', f'/api/agents/{ids[130]}/oxidns-ha', {'peer_id': member['id'], 'vip': VIP}, allow_error=True)
        assert code == 409, (code, data)
        results.append({'name': 'two-agents-on-same-docker-host-are-rejected', 'http_status': code,
                        'environment': caps, 'original_host_id': original['host_id'], 'message': data.get('message')})
    finally:
        docker(130, 'stop', '--time=5', name, check=False); docker(130, 'rm', name, check=False)
        lab.guest(130, 'ip', 'addr', 'del', '192.168.79.81/24', 'dev', PARENT)
    network = 'cf-docker-ha-rejection-bridge'
    docker(130, 'network', 'create', '-d', 'bridge', '--subnet', '172.31.77.0/24', network)
    try:
        docker(130, 'run', '-d', '--name', name, '--network', network, '--cap-add=NET_ADMIN', '--cap-add=NET_RAW',
               *common, APP_IMAGE, '-config', '/opt/configflow-agent/config-oxidns.json')
        caps = shadow_caps(); assert caps['ready'] is False, caps
        results.append({'name': 'ordinary-bridge-is-not-a-valid-host-ha-network', 'environment': caps,
                        'scope': 'real Agent capability rejection; no backend mutation submitted'})
    finally:
        docker(130, 'stop', '--time=5', name, check=False); docker(130, 'rm', name, check=False)
        docker(130, 'network', 'rm', network)
    after = {str(cid): core_snapshot(cid) for cid in (130, 131)}
    assert before == after and not owners(mode)
    path = HOST_ROOT / 'host-report.json'; report = json.loads(path.read_text())
    report['rejections'] = {'cases': results, 'core_unchanged': True, 'vip_absent': True}
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'rejections': results}, ensure_ascii=False), flush=True)


def export_evidence():
    import gzip
    import hashlib
    for mode in ('host', 'macvlan'):
        path = HOST_ROOT / (mode + '-report.json')
        report = json.loads(path.read_text())
        raw = (HOST_ROOT / (mode + '-dns-probe.jsonl')).read_bytes()
        samples = [json.loads(line) for line in raw.splitlines()]
        for case in report['scenarios']:
            if 'dns_probe' not in case: continue
            selected = [row for row in samples if case['started_at'] <= row['time'] <= case['finished_at']]
            failed = [row for row in selected if not row['nodes'][2]['ok']]
            case['dns_probe']['failed_request_start_times'] = [row['time'] for row in failed]
            case['dns_probe']['first_failed_request_at'] = failed[0]['time'] if failed else None
            case['dns_probe']['last_failed_request_at'] = failed[-1]['time'] if failed else None
        report['versions'] = {'docker': '29.9.2', 'agent': '1.8.47-go', 'oxidns': '1.6.1', 'keepalived': '2.3.4',
                              'image': 'sha256:a5df4033c5711832479ed40075720b40ba6fcd9faa6c23169024139147c2fa4b'}
        report['probe_method'] = {'observer': 'CT132', 'target_interval_ms': 100, 'dns_timeout_ms': 300,
                                  'requests_parallel_per_round': 3, 'name': 'sync.lab.invalid',
                                  'note': 'A failed sample is a failed DNS request, not a fixed 100 ms outage; timeouts stretch rounds.'}
        report['source_sha256'] = {str(p.relative_to(lab.REPO)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in lab.REPO.rglob('*') if p.is_file() and (
                p.name in ('oxidns_ha.py', 'oxidns_ha_handoff.py', 'oxidns_ha_publish.py', 'Dockerfile.agent',
                           'docker-ha-companion-entrypoint.sh', 'docker-agent-entrypoint-service.sh', 'supervisor-ha-companion.conf', 'oxidns_docker_ha_lab.py')
                or p.name.startswith('oxidns_ha') and p.suffix == '.go')}
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        (HOST_ROOT / (mode + '-dns-probe.jsonl.gz')).write_bytes(gzip.compress(raw, mtime=0))
    print(json.dumps({'evidence_export_ready': True}), flush=True)


def cleanup():
    lab.cleanup()
    states = {}
    for cid, memory in ((130, 384), (131, 384), (132, 1536)):
        config = lab.pve('pct', 'config', cid).decode()
        status = lab.pve('pct', 'status', cid).decode().strip()
        snapshots = lab.pve('pct', 'listsnapshot', cid).decode()
        assert status == 'status: stopped' and f'memory: {memory}\n' in config and lab.SNAPSHOT not in snapshots
        states[str(cid)] = {'status': 'stopped', 'memory_mib': memory, 'task_snapshot_removed': True,
                            'disk_and_configuration_rolled_back': True}
    lab.run(['sudo', 'systemctl', 'stop', 'cf-docker-ha-builder'])
    builder = lab.run(['systemctl', 'is-active', 'cf-docker-ha-builder'], check=False).decode().strip()
    assert builder in ('inactive', 'unknown', 'failed'), builder
    report = {'finished_at': time.time(), 'guests': states, 'builder_service_state': builder,
              'tunnel_pid_file_removed': not (lab.ROOT / 'tunnel.pid').exists(),
              'production_guests_modified': False,
              'test_server_packages_retained': ['docker', 'docker-buildx', 'containerd', 'runc', 'libpathrs'],
              'task_directory_removed': False}
    (HOST_ROOT / 'cleanup-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['install-hosts', 'load-kernel-image', 'kernel-probe', 'prepare-backend', 'load-app-image', 'prepare-host', 'prepare-macvlan', 'server', 'dns-fixture', 'update-source', 'start-probe', 'probe', 'probe-macvlan', 'test-publish', 'test-core-kill', 'test-recreate', 'test-guardian-restart', 'finish-probe', 'enable-mode', 'test-rules', 'test-guardian-boundary', 'disable-mode', 'remove-containers', 'test-rejections', 'export-evidence', 'cleanup'])
    command = parser.parse_args().command
    if command == 'install-hosts': install_hosts()
    elif command == 'load-kernel-image': load_kernel_image()
    elif command == 'kernel-probe': kernel_probe()
    elif command == 'prepare-backend': prepare_backend()
    elif command == 'load-app-image': load_app_image()
    elif command == 'prepare-host': prepare_mode('host')
    elif command == 'prepare-macvlan': prepare_mode('macvlan')
    elif command == 'server': lab.server()
    elif command == 'start-probe': start_probe()
    elif command == 'probe': lab.probe()
    elif command == 'probe-macvlan':
        lab.GUESTS.update({cid: (lab.GUESTS[cid][0], '192.168.79.' + str(cid - 49)) for cid in (130, 131)}); lab.probe()
    elif command == 'test-publish': test_publish()
    elif command == 'test-core-kill': test_core_kill()
    elif command == 'test-recreate': test_recreate()
    elif command == 'test-guardian-restart': test_guardian_restart()
    elif command == 'finish-probe': finish_probe()
    elif command == 'enable-mode': enable_mode()
    elif command == 'test-rules': test_rules()
    elif command == 'test-guardian-boundary': test_guardian_boundary()
    elif command == 'disable-mode': disable_mode()
    elif command == 'remove-containers': remove_containers()
    elif command == 'test-rejections': test_rejections()
    elif command == 'export-evidence': export_evidence()
    elif command == 'cleanup': cleanup()
    elif command == 'dns-fixture': dns_fixture()
    elif command == 'update-source':
        lab.guard(); lab.stage_source(); lab.guest(132, 'systemctl', 'restart', lab.UNIT_PREFIX + '-backend'); lab.start_tunnel()


if __name__ == '__main__':
    main()
