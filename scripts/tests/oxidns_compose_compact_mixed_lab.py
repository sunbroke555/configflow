#!/usr/bin/env python3
"""Compact Compose + native OxiDNS HA in CT130/131/132, driven only on .32.

All guests must have the unique pre-task snapshot. No production/LAN/preview
instance is in the allowlist. Credentials and full Compose stay private.
"""
from __future__ import annotations
import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import secrets
import time
import urllib.error
import uuid
import yaml
import oxidns_docker_ha_lab as legacy

lab = legacy.lab
ROOT = Path('/home/sunbroke/configflow-compose-compact-lab')
lab.ROOT = ROOT / 'private'
lab.GROOT = '/opt/cf-compose-compact'
lab.SNAPSHOT = 'cf-compose-compact-20261027'
lab.UNIT_PREFIX = 'cf-compose-compact'
lab.SCRIPT = 'scripts/tests/oxidns_compose_compact_mixed_lab.py'
lab.LOCAL_URL = 'http://127.0.0.1:19288'
lab.AGENT_BINARY = ROOT / 'configflow-agent'
legacy.HOST_ROOT = ROOT
legacy.SOCKET = 'unix://' + lab.GROOT + '/docker.sock'
legacy.APP_IMAGE = IMAGE = 'cf-compose-compact:20261027'
DOCKER = lab.GROOT + '/docker/docker'
VIP = lab.VIP
PARENT = 'eth0.91'


def docker(*args, data=None, check=True):
    return legacy.docker(131, *args, data=data, check=check)


def compose(mode, *args):
    return lab.guest(131, DOCKER, '-H', legacy.SOCKET, 'compose', '-f', lab.GROOT + '/packages/' + mode + '.yaml', *args)


def record(name, **values):
    path = ROOT / 'report.json'
    report = json.loads(path.read_text()) if path.exists() else {'scenarios': []}
    report['scenarios'].append({'name': name, 'time': time.time(), **values})
    lab.private(path, report)
    print(json.dumps({'scenario': name, 'passed': True, **{k: v for k, v in values.items() if k == 'dns_probe'}}, ensure_ascii=False), flush=True)


def install_docker():
    lab.guard()
    lab.guest(131, 'mkdir', '-p', lab.GROOT)
    lab.guest(131, 'chown', 'root:root', '/opt')
    archive = (ROOT / 'docker-29.9.2.tgz').read_bytes()
    assert hashlib.sha256(archive).hexdigest() == '9ac225999fd5e863f7694c728ef23d480616b1e5639328fa309befd4243d69eb'
    lab.guest(131, 'tar', '-xzf', '-', '-C', lab.GROOT, data=archive)
    config = {'data-root': lab.GROOT + '/docker-data', 'exec-root': '/run/cf-compose-compact-docker',
              'pidfile': '/run/cf-compose-compact-docker/docker.pid', 'hosts': [legacy.SOCKET],
              'bridge': 'none', 'iptables': False, 'ip6tables': False, 'ip-forward': False,
              'ip-masq': False, 'storage-driver': 'overlay2'}
    lab.put(131, lab.GROOT + '/daemon.json', json.dumps(config))
    lab.guest(131, 'systemd-run', '--unit=cf-compose-compact-dockerd', '--property=Delegate=yes', '--property=TasksMax=infinity',
              '--property=KillMode=process', '--setenv=PATH=' + lab.GROOT + '/docker:/usr/sbin:/usr/bin:/sbin:/bin',
              lab.GROOT + '/docker/dockerd', '--config-file=' + lab.GROOT + '/daemon.json')
    for _ in range(35):
        if docker('info', '--format', '{{.ServerVersion}}', check=False).strip(): break
        time.sleep(1)
    else: raise AssertionError('CT131 Docker not ready')
    lab.put(131, lab.GROOT + '/docker-compose', (ROOT / 'docker-compose-2.20.0').read_bytes(), mode=0o755)
    lab.guest(131, 'mkdir', '-p', '/root/.docker/cli-plugins')
    lab.guest(131, 'ln', '-s', lab.GROOT + '/docker-compose', '/root/.docker/cli-plugins/docker-compose')
    compose_version = docker('compose', 'version', '--short').decode().strip()
    assert compose_version == '2.20.0'
    lab.private(ROOT / 'compose-2.20.json', {'version': compose_version, 'sha256': hashlib.sha256((ROOT / 'docker-compose-2.20.0').read_bytes()).hexdigest(), 'source': 'official Docker Compose v2.20.0 release'})
    docker('load', data=(ROOT / 'app-image.tar').read_bytes())
    print(json.dumps({'docker_installed_in_guest': 131, 'image': IMAGE}), flush=True)


def prepare_backend():
    legacy.prepare_backend()
    credentials = json.loads((lab.ROOT / 'private.json').read_text())
    lab.guest(130, 'chown', 'root:root', '/opt')
    lab.put(130, lab.GROOT + '/configflow-agent', (ROOT / 'configflow-agent').read_bytes(), mode=0o755)
    lab.put(130, lab.GROOT + '/oxidns', (ROOT / 'oxidns').read_bytes(), mode=0o755)
    lab.put(130, '/etc/oxidns/config.yaml', json.dumps(legacy.document('198.51.100.10')))
    lab.put(130, '/etc/oxidns/rules/lab.txt', 'full:initial-shell.invalid\n')
    lab.put(130, '/run/systemd/system/oxidns.service.d/compose-lab.conf',
            '[Service]\nWorkingDirectory=/etc/oxidns\nExecStart=\nExecStart=' + lab.GROOT + '/oxidns start -c /etc/oxidns/config.yaml -d /etc/oxidns\n')
    lab.guest(130, 'systemctl', 'daemon-reload')
    _, member = lab.api('POST', '/api/agents/register', {'name': 'Compact mixed shell', 'host': lab.GUESTS[130][1],
        'port': 18081, 'registration_key': credentials['registration_key'], 'service_type': 'oxidns',
        'deployment_method': 'shell', 'profile_id': 'default'})
    cfg = {'server_url': lab.SERVER_URL, 'agent_name': 'Compact mixed shell', 'agent_host': lab.GUESTS[130][1],
        'agent_ip': lab.GUESTS[130][1], 'agent_port': 18081, 'service_type': 'oxidns', 'deployment_method': 'shell',
        'service_name': 'oxidns', 'config_path': '/etc/oxidns/config.yaml', 'service_manager': 'systemd',
        'service_binary': lab.GROOT + '/oxidns', 'service_unit': 'oxidns.service',
        'restart_command': 'systemctl restart oxidns', 'stop_command': 'systemctl stop oxidns', 'start_command': 'systemctl start oxidns',
        'status_command': 'systemctl is-active --quiet oxidns', 'health_dns_address': '127.0.0.1:53',
        'health_dns_name': 'sync.lab.invalid', 'deployment_health_timeout': 15, 'dns_watchdog_disabled': True,
        'heartbeat_interval': 2, 'agent_id': member['id'], 'token': member['token'], 'enable_metrics': False}
    lab.put(130, lab.GROOT + '/agent.json', json.dumps(cfg))
    lab.guest(130, 'systemctl', 'start', 'oxidns')
    lab.guest(130, 'systemd-run', '--unit=' + lab.UNIT_PREFIX + '-agent', '--property=WorkingDirectory=' + lab.GROOT,
              lab.GROOT + '/configflow-agent', '-config', lab.GROOT + '/agent.json')
    credentials['members'] = {'shell': member}; lab.private(lab.ROOT / 'private.json', credentials)
    print(json.dumps({'backend_ready': True, 'native_service_ready': True}), flush=True)


def host_network_state():
    script = '''import hashlib,json,subprocess
from pathlib import Path
r={'resolv_conf_sha256':hashlib.sha256(Path('/etc/resolv.conf').read_bytes()).hexdigest()}
for key,args in [('routes',['ip','-j','route','show','table','all']),('nft',['nft','list','ruleset']),('iptables',['iptables-save']),('ip6tables',['ip6tables-save'])]:
 try:
  p=subprocess.run(args,capture_output=True);r[key]={'exit':p.returncode,'sha256':hashlib.sha256(p.stdout).hexdigest()}
 except FileNotFoundError:r[key]={'unavailable':True}
print(json.dumps(r))
'''
    return json.loads(lab.guest(131, 'python3', '-c', script))


def current():
    return json.loads((lab.ROOT / 'current.json').read_text())


def snapshot_core(mode):
    name = 'compact-' + mode + '-oxidns'
    config = json.loads(docker('exec', name, 'cat', '/opt/configflow-agent/config-oxidns.json'))
    raw = json.loads(docker('inspect', name))[0]
    script = 'import hashlib,json;from pathlib import Path;print(json.dumps({n:hashlib.sha256(Path(n).read_bytes()).hexdigest() for n in ["/etc/oxidns/config.yaml","/etc/oxidns/rules/lab.txt"] if Path(n).exists()}))'
    return {'config': config, 'container_id': raw['Id'], 'mounts': raw['Mounts'],
            'hashes': json.loads(docker('exec', name, 'python3', '-c', script))}


def agent_api(which, method, path, body=None):
    credentials = json.loads((lab.ROOT / 'private.json').read_text())
    member = credentials['members'][which]
    script = '''import json,sys,urllib.request,urllib.error
v=json.load(sys.stdin);r=urllib.request.Request(v['url'],method=v['method'],headers={'Content-Type':'application/json','Authorization':'Bearer '+v['token']},data=None if v['body'] is None else json.dumps(v['body']).encode())
try:
 with urllib.request.urlopen(r,timeout=30) as res: code,value=res.status,json.load(res)
except urllib.error.HTTPError as e: code,value=e.code,json.load(e)
print(json.dumps({'code':code,'data':value}))
'''
    host = lab.GUESTS[130][1] if which == 'shell' else ('192.168.79.22' if which.startswith('host') else '192.168.79.82')
    port = 18081 if which == 'shell' else 18082
    payload = {'url': f'http://{host}:{port}' + path, 'method': method, 'body': body, 'token': member['token']}
    return json.loads(lab.guest(132, 'python3', '-c', script, data=json.dumps(payload).encode()))


def install_mode(mode):
    lab.guard(); name = 'compact-' + mode
    body = {'action': 'generate', 'mode': 'new', 'install_method': 'compose_compact', 'server_url': lab.SERVER_URL,
        'agent_name': name, 'agent_ip': '192.168.79.22' if mode == 'host' else '192.168.79.82',
        'data_dir': lab.GROOT + '/data-' + mode, 'network_mode': mode, 'oxidns_port': 18082,
        'ha_parent': PARENT if mode == 'macvlan' else '',
        'ha_subnet': '192.168.79.0/24' if mode == 'macvlan' else '', 'image': IMAGE}
    before = {entry['id'] for entry in lab.api('GET', '/api/agents')[1]}
    _, result = lab.api('POST', '/api/agents/docker-oxidns-ha-guide', body)
    assert result.get('compose_deployment') and not result.get('compose_bundle')
    text = result['compose']; parsed = yaml.safe_load(text)
    services = parsed['services']; assert set(services) == ({'agent', 'ha-companion'} if mode == 'host' else {'agent'})
    assert not parsed.get('volumes') and 'install-init' not in services
    assert all('entrypoint' not in item and 'command' not in item and 'labels' not in item for item in services.values())
    env = services['agent']['environment']; key = env['INSTALL_KEY']
    assert len(key.split('.')[0]) == 32 and len(key.split('.')[1]) == 43
    assert 'CONFIGFLOW_COMPOSE_INSTALL' not in text
    if mode == 'host': assert 'INSTALL_KEY' not in services['ha-companion'].get('environment', {})
    credentials = json.loads((lab.ROOT / 'private.json').read_text())
    assert all(credentials[k] not in text for k in ('registration_key', 'admin_token', 'password'))
    _, old = lab.api('POST', '/api/agents/docker-oxidns-ha-guide', {**body, 'install_method': 'compose_direct', 'create_host_identity': True})
    assert len(text.splitlines()) < len(old['compose'].splitlines())
    assert {entry['id'] for entry in lab.api('GET', '/api/agents')[1]} == before
    lab.private(lab.ROOT / (mode + '-bundle.json'), {'request': body, 'response': result})
    lab.put(131, lab.GROOT + '/packages/' + mode + '.yaml', text)
    network_before = host_network_state()
    compose(mode, 'up', '-d')
    deadline = time.monotonic() + 100
    while time.monotonic() < deadline:
        try:
            snap = snapshot_core(mode); cfg = snap['config']
            credentials['members'][mode] = {'id': cfg['agent_id'], 'token': cfg['token']}
            lab.private(lab.ROOT / 'private.json', credentials)
            environment = agent_api(mode, 'GET', '/api/capabilities')['data']['oxidns_ha_environment']
            online = next(entry for entry in lab.api('GET', '/api/agents')[1] if entry['id'] == cfg['agent_id'])
            if environment['ready'] and online['status'] == 'online': break
        except (RuntimeError, KeyError, StopIteration, ValueError): pass
        time.sleep(1)
    else: raise AssertionError('Compact Compose Agent not online/ready')
    network_after = host_network_state(); assert network_after == network_before
    assert environment['host_id_source'] == 'installation' and len(environment['host_boot_id']) == 64
    assert not next(m for m in snap['mounts'] if m['Destination'] == '/run/configflow-host-boot-id')['RW']
    identity = docker('exec', name + '-oxidns', 'cat', '/opt/configflow-agent/.configflow-ha-identity').strip()
    assert environment['host_id'] == hashlib.sha256(identity).hexdigest()
    assert len(compose(mode, 'ps', '-a', '-q').splitlines()) == (2 if mode == 'host' else 1)
    actual = json.loads(docker('inspect', name + '-oxidns'))[0]
    assert not actual['HostConfig']['Privileged'] and actual['HostConfig']['PidMode'] != 'host'
    assert not any('docker.sock' in m['Destination'] for m in snap['mounts'])
    if mode == 'host':
        assert not actual['HostConfig']['CapAdd']
        helper = json.loads(docker('inspect', name + '-oxidns-ha'))[0]
        assert not next(m for m in helper['Mounts'] if m['Destination'] == '/opt/configflow-agent')['RW']
        assert not any(v.startswith('INSTALL_KEY=') for v in helper['Config']['Env'])
    native_version = lab.guest(130, lab.GROOT + '/configflow-agent', '-version').decode().strip()
    docker_version = docker('exec', name + '-oxidns', '/usr/local/bin/configflow-agent', '-version').decode().strip()
    assert native_version == docker_version == '1.8.49-go'
    ids = {k: credentials['members'][k]['id'] for k in ('shell', mode)}
    lab.private(lab.ROOT / 'current.json', {'mode': mode, 'ids': ids})
    raw = json.dumps(legacy.document('198.51.100.11')).encode()
    docker('exec', '-i', name + '-oxidns', 'python3', '-c', 'import sys;from pathlib import Path;Path("/etc/oxidns/config.yaml").write_bytes(sys.stdin.buffer.read());Path("/etc/oxidns/rules").mkdir(exist_ok=True);Path("/etc/oxidns/rules/lab.txt").write_text("full:initial-docker.invalid\\n")', data=raw)
    docker('exec', name + '-oxidns', 'supervisorctl', '-c', '/etc/supervisor/supervisord.conf', 'restart', 'oxidns')
    record('compact-' + mode + '-registered', no_prepare_script=True, only_external_file='compose.yaml',
        real_online=True, ha_environment_ready=True, identity_mode=environment['host_id_source'],
        service_count=len(services), no_global_identity_volume=True, read_only_boot_evidence=True,
        host_main_has_no_added_caps=mode == 'host', helper_has_no_install_token=True,
        host_network_state_unchanged=True, yaml_lines=len(text.splitlines()), previous_yaml_lines=len(old['compose'].splitlines()),
        yaml_bytes=len(text.encode()), previous_yaml_bytes=len(old['compose'].encode()),
        agent_version=docker_version, compose_version=docker('compose', 'version', '--short').decode().strip(),
        firewall_checks={k: v.get('exit', 'unavailable') for k, v in network_after.items() if isinstance(v, dict)})


def wait_ready(mode):
    for _ in range(70):
        try:
            env = agent_api(mode, 'GET', '/api/capabilities')['data']['oxidns_ha_environment']
            if env['ready']: return env
        except (RuntimeError, KeyError, ValueError): pass
        time.sleep(.5)
    raise AssertionError('Agent HA environment did not recover')


def expire_and_recreate(mode):
    bundle = json.loads((lab.ROOT / (mode + '-bundle.json')).read_text())['response']
    env = yaml.safe_load(bundle['compose'])['services']['agent']['environment']
    session, token = env['INSTALL_KEY'].split('.')
    settings = {'url': env['SERVER_URL'].rstrip('/') + '/api/agents/docker-oxidns-ha-enroll/' + session, 'token': token}
    script = """import json,sys,urllib.request,urllib.error
from pathlib import Path
v=json.load(sys.stdin);session=v['url'].rsplit('/',1)[-1]
assert len(session)==32 and all(c in '0123456789abcdef' for c in session)
p=Path('/opt/cf-compose-compact/data/docker-ha-installations')/(session+'.json')
r=json.loads(p.read_text());r['expires']=0;p.write_text(json.dumps(r))
q=urllib.request.Request(v['url'],data=b'{}',headers={'Content-Type':'application/json','Authorization':'Bearer '+v['token']})
try:
 with urllib.request.urlopen(q,timeout=5) as response:code=response.status
except urllib.error.HTTPError as e:code=e.code
print(json.dumps({'http':code}))
"""
    assert json.loads(lab.guest(132, 'python3', '-c', script, data=json.dumps(settings).encode()))['http'] == 401
    before = snapshot_core(mode); before_count = len(lab.api('GET', '/api/agents')[1])
    instance = docker('exec', 'compact-' + mode + '-oxidns', 'cat', '/opt/configflow-agent/.configflow-ha-identity')
    started = observer_time()
    lab.guest(132, 'systemctl', 'stop', lab.UNIT_PREFIX + '-backend')
    try:
        compose(mode, 'up', '-d', '--force-recreate')
        wait_ready(mode)
        after = snapshot_core(mode)
        assert before['config']['agent_id'] == after['config']['agent_id'] and before['config']['token'] == after['config']['token']
        assert before['container_id'] != after['container_id'] and before['hashes'] == after['hashes']
        assert docker('exec', 'compact-' + mode + '-oxidns', 'cat', '/opt/configflow-agent/.configflow-ha-identity') == instance
    finally:
        lab.guest(132, 'systemd-run', '--unit=' + lab.UNIT_PREFIX + '-backend', '--property=WorkingDirectory=' + lab.GROOT,
                  '/opt/cf-retest/venv/bin/python', lab.GROOT + '/source/' + lab.SCRIPT, 'server')
    for _ in range(40):
        try:
            assert len(lab.api('GET', '/api/agents')[1]) == before_count
            break
        except (OSError, RuntimeError): time.sleep(.25)
    else: raise AssertionError('Lab backend did not recover')
    logs = lab.guest(132, 'journalctl', '-u', lab.UNIT_PREFIX + '-backend', '--since', '@' + str(started), '-o', 'json', '--no-pager')
    calls = sum('POST /api/agents/docker-oxidns-ha-enroll/' in str(json.loads(line).get('MESSAGE', '')) for line in logs.splitlines())
    assert calls == 0
    record(mode + '-expired-token-offline-recreate', expired_token_http=401, backend_was_stopped=True,
           agent_identity_preserved=True, installation_identity_preserved=True,
           config_and_rules_preserved=True, additional_enrollment_requests=calls,
           compose_version=docker('compose', 'version', '--short').decode().strip())


def identity_corruption(mode):
    name = 'compact-' + mode + '-oxidns'
    directory = lab.GROOT + '/data-' + mode + '/agent'
    path = directory + '/.configflow-ha-identity'
    marker = directory + '/.configflow-compose-compact.json'
    original = lab.guest(131, 'cat', path)
    setup = lab.guest(131, 'cat', marker)
    before = snapshot_core(mode); before_count = len(lab.api('GET', '/api/agents')[1])
    cases = []
    for fault in ('changed', 'missing', 'symlink'):
        docker('stop', name)
        script = """from pathlib import Path
import sys
p=Path(sys.argv[1]);p.unlink()
if sys.argv[2]=='changed':p.write_text('f'*32+'\\n');p.chmod(0o600)
elif sys.argv[2]=='symlink':p.symlink_to('/etc/machine-id')
"""
        lab.guest(131, 'python3', '-c', script, path, fault)
        try:
            docker('start', name)
            for _ in range(30):
                state = json.loads(docker('inspect', name))[0]['State']
                if state['ExitCode'] != 0 and (not state['Running'] or state['Restarting']): break
                time.sleep(.2)
            else: raise AssertionError('Invalid identity did not refuse startup: ' + fault)
            assert lab.guest(131, 'cat', marker) == setup
            assert len(lab.api('GET', '/api/agents')[1]) == before_count
            if fault == 'missing': lab.guest(131, 'test', '!', '-e', path)
            cases.append({'fault': fault, 'startup_refused': True, 'marker_preserved': True, 'no_reregistration': True})
        finally:
            docker('stop', name, check=False)
            lab.guest(131, 'python3', '-c', 'from pathlib import Path;import sys;p=Path(sys.argv[1]);p.unlink(missing_ok=True);p.write_bytes(sys.stdin.buffer.read());p.chmod(0o600)', path, data=original)
            docker('start', name); wait_ready(mode)
    after = snapshot_core(mode)
    assert before['config']['agent_id'] == after['config']['agent_id'] and before['config']['token'] == after['config']['token']
    assert before['hashes'] == after['hashes']
    record(mode + '-identity-corruption-guard', cases=cases, original_identity_and_config_restored=True)


def observer_time():
    return float(lab.guest(132, 'python3', '-c', 'import time;print(time.time())'))


def node_addresses(mode):
    return [lab.GUESTS[130][1], lab.GUESTS[131][1] if mode == 'host' else '192.168.79.82', VIP]


def dns_probe(mode):
    stop = Path(lab.GROOT + '/stop-probe-' + mode); stop.unlink(missing_ok=True)
    def query(address):
        query_started = time.time()
        value = lab.dns_query(address)
        value.update(started_at=query_started, completed_at=time.time())
        value['ok'] = bool(value.get('ok') and value.get('rcode') == 0 and (value.get('ip') or '').startswith('198.51.100.'))
        return value
    with Path(lab.GROOT + '/probe-' + mode + '.jsonl').open('w') as output, concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        while not stop.exists():
            started = time.time(); values = list(pool.map(query, node_addresses(mode)))
            output.write(json.dumps({'time': started, 'nodes': values}) + '\n'); output.flush()
            time.sleep(max(0, .1 - (time.time() - started)))


def read_samples(mode):
    raw = lab.guest(132, 'cat', lab.GROOT + '/probe-' + mode + '.jsonl')
    return raw, [json.loads(line) for line in raw.splitlines() if line.endswith(b'}')]


def phase(mode, name, started, **details):
    finished = observer_time()
    selected = [row for row in read_samples(mode)[1] if started <= row['time'] <= finished]
    assert selected, 'No samples in phase'
    if details.get('recovered_at'):
        assert any(row['nodes'][2]['ok'] and row['nodes'][2]['completed_at'] >= details['recovered_at'] for row in selected), 'No strict successful recovery sample'
    failed = [row for row in selected if not row['nodes'][2]['ok']]
    record(mode + '-' + name, started_at=started, finished_at=finished, dns_probe={
        'samples': len(selected), 'vip_failed_samples': len(failed),
        'both_nodes_unavailable_samples': sum(not row['nodes'][0]['ok'] and not row['nodes'][1]['ok'] for row in selected),
        'failed_request_start_times': [row['time'] for row in failed],
        'max_vip_latency_ms': max((row['nodes'][2]['ms'] for row in selected), default=0)}, **details)
    return selected


def owners(mode):
    found = []
    for which in ('shell', mode):
        if which == 'shell': raw = lab.guest(130, 'ip', '-j', 'addr', 'show', 'dev', PARENT)
        elif mode == 'host': raw = lab.guest(131, 'ip', '-j', 'addr', 'show', 'dev', PARENT)
        else:
            if docker('inspect', 'compact-macvlan-oxidns', '--format', '{{.State.Running}}').decode().strip() != 'true': continue
            raw = docker('exec', 'compact-macvlan-oxidns', 'ip', '-j', 'addr', 'show', 'dev', 'eth0')
        if any(address.get('local') == VIP for item in json.loads(raw) for address in item.get('addr_info', [])): found.append(which)
    return found


def wait_owner(mode, expected, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if owners(mode) == [expected]: return
        for which in ('shell', mode):
            try: agent_api(which, 'GET', '/api/oxidns-ha')
            except Exception: pass
        time.sleep(.3)
    raise AssertionError('VIP owner did not converge to ' + expected)


def dns_observe(mode):
    code = 'import json,sys;sys.path.insert(0,' + repr(lab.GROOT + '/source/scripts/tests') + ');from oxidns_ha_sync_lab import dns_query;print(json.dumps({a:dns_query(a) for a in ' + repr(node_addresses(mode)) + '}))'
    return json.loads(lab.guest(132, 'python3', '-c', code))


def snapshots(mode):
    return {'shell': lab.snapshot(130), mode: {'hashes': snapshot_core(mode)['hashes']}}


def set_saved(address, rule, workers=3):
    _, value = lab.api('GET', '/api/oxidns/settings')
    value.update(config_mode='native', native_config=json.dumps(legacy.document(address, workers)),
                 additional_files=[{'path': 'rules/lab.txt', 'encoding': 'text', 'content': rule}])
    lab.api('POST', '/api/oxidns/settings', value)


def service_events(mode, started):
    from datetime import datetime
    native = []
    raw = lab.guest(130, 'journalctl', '-u', 'oxidns', '--since', '@' + str(started), '-o', 'json', '--no-pager')
    for line in raw.splitlines():
        row = json.loads(line); message = str(row.get('MESSAGE', ''))
        if row.get('_COMM') == 'systemd' and message.startswith(('Stopping ', 'Stopped ', 'Starting ', 'Started ')):
            native.append({'time': int(row['__REALTIME_TIMESTAMP']) / 1e6, 'message': message})
    container = []
    for line in docker('logs', '--timestamps', '--since', str(started), 'compact-' + mode + '-oxidns').decode().splitlines():
        if not any(value in line for value in ("spawned: 'oxidns'", 'stopped: oxidns', 'exited: oxidns')): continue
        stamp, _, message = line.partition(' ')
        container.append({'time': datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp(), 'message': message})
    return {'shell': native, mode: container}


def mixed_enable(mode):
    ids = current()['ids']
    initial = dns_observe(mode)
    assert [initial[a].get('ip') for a in node_addresses(mode)[:2]] == ['198.51.100.10', '198.51.100.11']
    address = '198.51.100.20' if mode == 'host' else '198.51.100.70'
    set_saved(address, 'full:mixed-shared-' + mode + '.invalid\n')
    lab.api('PUT', '/api/agents/' + ids['shell'] + '/oxidns-ha', {'peer_id': ids[mode], 'vip': VIP})
    completed = lab.wait_ha(ids['shell']); wait_owner(mode, 'shell')
    after = snapshots(mode); assert after['shell']['hashes'] == after[mode]['hashes']
    answers = dns_observe(mode); assert all(v.get('ip') == address for v in answers.values())
    record(mode + '-mixed-enable-syncs-fallback-and-rules', publication_status=completed['publication']['status'],
           initial_answers=[initial[a]['ip'] for a in node_addresses(mode)[:2]], final_answer=address,
           file_hashes_equal=True, primary='native-shell', backup='docker-' + mode)
    lab.put(132, lab.GROOT + '/source/' + lab.SCRIPT, Path(__file__).read_bytes())
    lab.guest(132, 'systemd-run', '--unit=' + lab.UNIT_PREFIX + '-probe-' + mode,
              '/opt/cf-retest/venv/bin/python', lab.GROOT + '/source/' + lab.SCRIPT, 'probe-' + mode)
    time.sleep(1)


def mixed_publish(mode):
    ids = current()['ids']; source = owners(mode)[0]
    assert source == 'shell'
    address = '198.51.100.40' if mode == 'host' else '198.51.100.80'
    started = observer_time(); set_saved(address, 'full:mixed-published-' + mode + '.invalid\n', workers=4)
    lab.publish(ids[mode]); complete = lab.wait_ha(ids[mode]); wait_owner(mode, 'shell')
    answers = dns_observe(mode); assert all(v.get('ip') == address for v in answers.values())
    hashes = snapshots(mode); assert hashes['shell']['hashes'] == hashes[mode]['hashes']
    events = service_events(mode, started)
    standby_starts = [v['time'] for v in events[mode] if "spawned: 'oxidns'" in v['message']]
    primary_stops = [v['time'] for v in events['shell'] if v['message'].startswith('Stopping ')]
    healthy = [v['nodes'][2]['completed_at'] for v in read_samples(mode)[1] if v['time'] >= started and v['nodes'][2].get('ok') and v['nodes'][2].get('ip') == address]
    assert standby_starts and primary_stops and healthy
    assert standby_starts[0] < healthy[0] < primary_stops[0]
    phase(mode, 'planned-publish', started, publication_status=complete['publication']['status'],
          standby_started_at=standby_starts[0], new_vip_dns_at=healthy[0], native_primary_stopped_at=primary_stops[0],
          ordered_handoff_proved=True, service_events=events, file_hashes_equal=True)


def mixed_faults(mode):
    wait_owner(mode, 'shell'); started = observer_time()
    expected = '198.51.100.40' if mode == 'host' else '198.51.100.80'
    lab.guest(130, 'systemctl', 'stop', 'oxidns')
    try:
        wait_owner(mode, mode)
        answer = dns_observe(mode)[VIP]; assert answer['ok'] and answer['rcode'] == 0 and answer['ip'] == expected
        time.sleep(1)
    finally:
        lab.guest(130, 'systemctl', 'start', 'oxidns')
    wait_owner(mode, 'shell'); recovered = observer_time(); time.sleep(1)
    phase(mode, 'native-core-stop-and-recovery', started, takeover_observed=True, recovery_observed=True, recovered_at=recovered)
    # Planned yield first makes the Docker core the active VIP holder, so killing
    # it tests actual failover rather than stopping an idle backup.
    operation = 'lab-' + uuid.uuid4().hex
    yielding = observer_time()
    assert agent_api('shell', 'POST', '/api/oxidns-ha/handoff', {'operation_id': operation})['code'] == 200
    wait_owner(mode, mode); time.sleep(.5)
    phase(mode, 'planned-yield-before-fault', yielding, takeover_observed=True)
    started = observer_time(); name = 'compact-' + mode + '-oxidns'
    before = snapshot_core(mode)
    docker('kill', '--signal=KILL', name)
    try:
        wait_owner(mode, 'shell')
        answer = dns_observe(mode)[VIP]; assert answer['ok'] and answer['rcode'] == 0 and answer['ip'] == expected
        time.sleep(1)
    finally:
        docker('start', name); wait_ready(mode)
        agent_api('shell', 'DELETE', '/api/oxidns-ha/handoff', {'operation_id': operation})
    wait_owner(mode, 'shell'); recovered = observer_time(); time.sleep(1)
    after = snapshot_core(mode)
    assert before['config']['agent_id'] == after['config']['agent_id'] and before['config']['token'] == after['config']['token']
    phase(mode, 'active-docker-core-kill-and-recovery', started, takeover_observed=True, recovered_identity_preserved=True, recovered_at=recovered,
          companion_survived=(mode != 'host' or docker('inspect', 'compact-host-oxidns-ha', '--format', '{{.State.Running}}').decode().strip() == 'true'))


def finish_mode(mode):
    lab.put(132, lab.GROOT + '/stop-probe-' + mode, '')
    lab.guest(132, 'systemctl', 'stop', lab.UNIT_PREFIX + '-probe-' + mode, check=False)
    raw, samples = read_samples(mode); (ROOT / ('probe-' + mode + '.jsonl')).write_bytes(raw)
    lab.api('DELETE', '/api/agents/' + current()['ids']['shell'] + '/oxidns-ha')
    assert not owners(mode)
    # Reset only the native fixture for the next independently installed mode.
    lab.put(130, '/etc/oxidns/config.yaml', json.dumps(legacy.document('198.51.100.10')))
    lab.put(130, '/etc/oxidns/rules/lab.txt', 'full:initial-shell.invalid\n')
    lab.guest(130, 'systemctl', 'restart', 'oxidns')
    record(mode + '-probe-exported', total_samples=len(samples), raw_probe_contains_credentials=False)


def same_host_checks():
    host_env = agent_api('host', 'GET', '/api/capabilities')['data']['oxidns_ha_environment']
    mac_env = agent_api('macvlan', 'GET', '/api/capabilities')['data']['oxidns_ha_environment']
    assert host_env['host_id_source'] == mac_env['host_id_source'] == 'installation'
    assert host_env['host_id'] != mac_env['host_id'] and host_env['host_boot_id'] == mac_env['host_boot_id']
    credentials = json.loads((lab.ROOT / 'private.json').read_text())
    code, result = lab.api('PUT', '/api/agents/' + credentials['members']['host']['id'] + '/oxidns-ha',
                          {'peer_id': credentials['members']['macvlan']['id'], 'vip': VIP}, allow_error=True)
    assert code in (400, 409) and '同一台宿主机' in result.get('message', '')
    record('distinct-installations-same-host-guard', different_persistent_installation_ids=True,
           same_real_boot_identity=True, pairing_http=code, same_host_pairing_rejected=True)


def export_evidence():
    import gzip
    report = json.loads((ROOT / 'report.json').read_text())
    report['matrix_agent_version'] = '1.8.49-go'
    report['image'] = json.loads((ROOT / 'image.json').read_text())
    report['compose_minimum_version'] = json.loads((ROOT / 'compose-2.20.json').read_text())
    report['all_installs_compose_version'] = '2.20.0'
    report['runtime_scope'] = 'CT130 native Shell Agent + CT131 independent Docker daemon + CT132 observer, on the same isolated PVE laboratory host; not physical NAS or physical power-loss tests'
    report['network_scope'] = 'Only cfgt VLAN91; no production guest or production LAN modification'
    report['probe_method'] = {'observer': 'CT132', 'cadence_target_ms': 100, 'timeout_ms': 300,
        'success': 'rcode0 with198.51.100.* A answer', 'ordering_timestamp': 'Per-query completed_at and system lifecycle events share PVE clock'}
    names = ['backend/agents/docker_ha_compact.py', 'backend/agents/docker_ha_guide.py',
             'backend/agents/oxidns_ha.py', 'docker/docker-compose-compact.py', 'docker/Dockerfile.agent',
             'docker/docker-ha-companion-entrypoint.sh', 'backend/agents/go-agent/routes/oxidns_ha_container.go', 'backend/agents/go-agent/upgrade/migration.go', 'backend/agents/version.py']
    report['source_sha256'] = {name: hashlib.sha256((lab.REPO / name).read_bytes()).hexdigest() for name in names}
    report['native_agent_binary_sha256'] = hashlib.sha256((ROOT / 'configflow-agent').read_bytes()).hexdigest()
    report['probe_totals'] = {}
    for mode in ('host', 'macvlan'):
        raw = (ROOT / ('probe-' + mode + '.jsonl')).read_bytes(); rows = [json.loads(line) for line in raw.splitlines()]
        report['probe_totals'][mode] = {'samples': len(rows), 'vip_failed_samples': sum(not row['nodes'][2]['ok'] for row in rows)}
        for case in report['scenarios']:
            if case['name'].startswith(mode + '-') and 'dns_probe' in case:
                selected = [row for row in rows if case['started_at'] <= row['time'] <= case['finished_at']]
                failed = [row['nodes'][2] for row in selected if not row['nodes'][2]['ok']]
                assert len(selected) == case['dns_probe']['samples'] and len(failed) == case['dns_probe']['vip_failed_samples']
                if any(label in case['name'] for label in ('core-stop', 'core-kill', 'yield-before')):
                    expected = '198.51.100.40' if mode == 'host' else '198.51.100.80'
                    assert all(row['nodes'][2]['ip'] == expected for row in selected if row['nodes'][2]['ok'])
                    case['dns_probe']['successful_answers_match_published_config'] = True
                if mode == 'macvlan' and 'companion_survived' in case:
                    case['companion_survived'] = None  # Macvlan has no independent companion.
                case['dns_probe']['failed_probe_window_s'] = round(failed[-1]['completed_at'] - failed[0]['started_at'], 3) if failed else 0
        with gzip.open(ROOT / ('probe-' + mode + '.jsonl.gz'), 'wb') as output: output.write(raw)
    diagnostic = ROOT / 'probe-host-first-diagnostic.jsonl'
    if diagnostic.exists():
        with gzip.open(ROOT / (diagnostic.name + '.gz'), 'wb') as output: output.write(diagnostic.read_bytes())
    lab.private(ROOT / 'public-report.json', report)
    print(json.dumps({'public_report_ready': True, 'probe_totals': report['probe_totals']}), flush=True)


def cleanup():
    lab.guard()
    baseline = json.loads((ROOT / 'baseline.json').read_text())
    import contextlib, io
    dns_before = lab.run(['ss', '-H', '-lntu', 'sport = :53'])
    assert sorted(dns_before.decode().splitlines()) == sorted(baseline['port53'].splitlines())
    with contextlib.redirect_stdout(io.StringIO()):
        lab.cleanup()
    guests = {}
    for cid in (130, 131, 132):
        config = lab.pve('pct', 'config', cid).decode()
        memory = int(next(line.split(':', 1)[1] for line in config.splitlines() if line.startswith('memory:')))
        expected = int(next(line.split(':', 1)[1] for line in baseline['guests'][str(cid)]['config'].splitlines() if line.startswith('memory:')))
        assert memory == expected
        assert lab.pve('pct', 'status', cid).decode().strip() == 'status: stopped'
        assert lab.SNAPSHOT not in lab.pve('pct', 'listsnapshot', cid).decode()
        guests[str(cid)] = {'status': 'stopped', 'memory_mib': memory, 'snapshot_removed': True, 'disk_and_config_restored': True}
    lab.run(['sudo', '-n', 'systemctl', 'stop', 'cf-compose-compact-builder'])
    defaults = {name: lab.run(['systemctl', 'is-active', name], check=False).decode().strip() for name in ('docker', 'docker.socket', 'containerd')}
    assert all(value == 'inactive' for value in defaults.values())
    assert not lab.run(['ss', '-H', '-ltn', 'sport = :19288']).strip()
    previews = {}
    for port in (15800, 15810):
        with urllib.request.urlopen(f'http://192.168.0.32:{port}/', timeout=10) as response:
            previews[str(port)] = response.status
            assert response.status == 200
    assert lab.run(['ss', '-H', '-lntu', 'sport = :53']) == dns_before
    with urllib.request.urlopen('http://192.168.0.32:15810/api/auth/status', timeout=10) as response:
        assert json.load(response)['authEnabled'] is False
    result = {'preview_http_status': previews, 'preview15810_authentication_remains_disabled': True, 'test_server_port53_matches_pretest_baseline': True, 'guests': guests, 'default_docker_services': defaults, 'builder_stopped': True,
              'tunnel_closed': True, 'production_guests_modified': False}
    assert ROOT == Path('/home/sunbroke/configflow-compose-compact-lab') and ROOT.is_dir() and not ROOT.is_symlink()
    lab.run(['sudo', '-n', 'rm', '-rf', '--', str(ROOT)])
    assert not ROOT.exists(); result['private_directory_credentials_image_cache_removed'] = True
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['install-docker', 'prepare-backend', 'server', 'dns-fixture', 'install-host', 'install-macvlan', 'expiry-host', 'expiry-macvlan', 'enable-host', 'enable-macvlan', 'publish-host', 'publish-macvlan', 'faults-host', 'faults-macvlan', 'finish-host', 'finish-macvlan', 'probe-host', 'probe-macvlan', 'same-host-checks', 'identity-host', 'identity-macvlan', 'export-evidence', 'cleanup'])
    command = parser.parse_args().command
    {'install-docker': install_docker, 'prepare-backend': prepare_backend, 'server': lab.server, 'dns-fixture': legacy.dns_fixture,
     'install-host': lambda: install_mode('host'), 'install-macvlan': lambda: install_mode('macvlan'),
     'expiry-host': lambda: expire_and_recreate('host'), 'expiry-macvlan': lambda: expire_and_recreate('macvlan'),
     'enable-host': lambda: mixed_enable('host'), 'enable-macvlan': lambda: mixed_enable('macvlan'),
     'publish-host': lambda: mixed_publish('host'), 'publish-macvlan': lambda: mixed_publish('macvlan'),
     'faults-host': lambda: mixed_faults('host'), 'faults-macvlan': lambda: mixed_faults('macvlan'),
     'finish-host': lambda: finish_mode('host'), 'finish-macvlan': lambda: finish_mode('macvlan'),
     'probe-host': lambda: dns_probe('host'), 'probe-macvlan': lambda: dns_probe('macvlan'),
     'same-host-checks': same_host_checks, 'identity-host': lambda: identity_corruption('host'), 'identity-macvlan': lambda: identity_corruption('macvlan'), 'export-evidence': export_evidence, 'cleanup': cleanup}[command]()
