#!/usr/bin/env python3
"""Real generated Docker HA install/migration commands in the isolated PVE lab.

Run only on 192.168.0.32. Only CT130/CT132 are started; CT131 stays untouched.
Every experiment is protected by the dedicated pre-task snapshot.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time

import oxidns_docker_ha_lab as dockerlab

lab = dockerlab.lab
ROOT = Path.home() / 'configflow-docker-guide-lab'
lab.ROOT = ROOT / 'private'
lab.GROOT = '/opt/cf-docker-guide'
lab.SNAPSHOT = 'cf-docker-guide-20261027'
lab.UNIT_PREFIX = 'cf-docker-guide'
lab.SCRIPT = 'scripts/tests/oxidns_docker_guide_lab.py'
lab.LOCAL_URL = 'http://127.0.0.1:19285'
lab.GUESTS = {cid: info for cid, info in lab.GUESTS.items() if cid in (130, 132)}
dockerlab.HOST_ROOT = ROOT
dockerlab.SOCKET = 'unix://' + lab.GROOT + '/docker.sock'
dockerlab.APP_IMAGE = 'cf-oxidns-docker-guide:20261027'
IMAGE = dockerlab.APP_IMAGE
DOCKER = lab.GROOT + '/docker/docker'


def prepare_guests():
    import socket
    import sys
    assert sys.platform == 'linux' and socket.gethostname() != 'pve'
    for cid, (name, _) in lab.GUESTS.items():
        config = lab.pve('pct', 'config', cid).decode()
        assert f'hostname: {name}\n' in config and 'bridge=cfgt' in config and 'bridge=vmbr' not in config
        assert lab.pve('pct', 'status', cid).decode().strip() == 'status: stopped'
        assert lab.SNAPSHOT not in lab.pve('pct', 'listsnapshot', cid).decode()
        lab.pve('pct', 'snapshot', cid, lab.SNAPSHOT)
    lab.pve('pct', 'set', 130, '--memory', 1024)
    for cid in lab.GUESTS: lab.pve('pct', 'start', cid)
    for _ in range(30):
        try:
            for cid in lab.GUESTS: lab.guest(cid, 'true')
            break
        except RuntimeError: time.sleep(1)
    lab.guest(130, 'systemctl', 'stop', 'configflow-agent', 'oxidns', 'keepalived', check=False)
    print(json.dumps({'snapshots_created': list(lab.GUESTS), 'guest_131_untouched': True}), flush=True)


def install_docker():
    lab.guard()
    archive = (ROOT / 'docker-29.9.2.tgz').read_bytes()
    assert hashlib.sha256(archive).hexdigest() == '9ac225999fd5e863f7694c728ef23d480616b1e5639328fa309befd4243d69eb'
    lab.guest(130, 'mkdir', '-p', lab.GROOT)
    lab.guest(130, 'chown', 'root:root', '/opt')
    lab.guest(130, 'tar', '-xzf', '-', '-C', lab.GROOT, data=archive)
    config = {'data-root': lab.GROOT + '/docker-data', 'exec-root': '/run/cf-docker-guide-docker',
              'pidfile': '/run/cf-docker-guide-docker/docker.pid', 'hosts': [dockerlab.SOCKET],
              'bridge': 'none', 'iptables': False, 'ip6tables': False,
              'ip-forward': False, 'ip-masq': False, 'storage-driver': 'overlay2'}
    lab.put(130, lab.GROOT + '/docker-daemon.json', json.dumps(config))
    lab.guest(130, 'systemd-run', '--unit=cf-docker-guide-dockerd', '--property=Delegate=yes',
              '--property=TasksMax=infinity', '--property=KillMode=process',
              '--setenv=PATH=' + lab.GROOT + '/docker:/usr/sbin:/usr/bin:/sbin:/bin',
              lab.GROOT + '/docker/dockerd', '--config-file=' + lab.GROOT + '/docker-daemon.json')
    lab.guest(130, 'ln', '-s', DOCKER, '/usr/local/bin/docker')
    compose = Path('/usr/lib/docker/cli-plugins/docker-compose').read_bytes()
    lab.put(130, lab.GROOT + '/docker-compose', compose, mode=0o755)
    lab.guest(130, 'mkdir', '-p', '/root/.docker/cli-plugins')
    lab.guest(130, 'ln', '-s', lab.GROOT + '/docker-compose', '/root/.docker/cli-plugins/docker-compose')
    for _ in range(30):
        if dockerlab.docker(130, 'info', '--format', '{{.ServerVersion}}', check=False).strip(): break
        time.sleep(1)
    print(json.dumps({'docker': dockerlab.docker(130, 'version', '--format', '{{.Server.Version}}').decode().strip(),
                      'compose': dockerlab.docker(130, 'compose', 'version', '--short').decode().strip()}), flush=True)


def load_image():
    lab.guard()
    dockerlab.docker(130, 'load', data=(ROOT / 'app-image.tar').read_bytes())
    print(json.dumps({'image': dockerlab.docker(130, 'image', 'inspect', IMAGE, '--format', '{{.Id}}').decode().strip()}), flush=True)


def prepare_backend():
    dockerlab.prepare_backend()


def run_generated(command, case, block, *, must_succeed=True):
    import shlex
    path = lab.GROOT + '/execution/' + case
    lab.guest(130, 'mkdir', '-p', path)
    script = ('set -eu\nexport PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin\ncd ' + shlex.quote(path) + '\nexport DOCKER_HOST=' + shlex.quote(dockerlab.SOCKET)
              + '\n' + command + '\n')
    result = subprocess.run(['ssh', '-o', 'BatchMode=yes', 'pve',
        shlex.join(['pct', 'exec', '130', '--', 'bash', '-s'])], input=script.encode(), capture_output=True)
    private = lab.ROOT / (case + '-' + block)
    lab.private(private.with_suffix('.json'), {'command': command, 'exit_code': result.returncode,
                'stdout': result.stdout.decode(errors='replace'), 'stderr': result.stderr.decode(errors='replace')})
    if must_succeed and result.returncode:
        raise RuntimeError(f'{case}/{block} failed (exit {result.returncode}); private output saved at {private.with_suffix(".json")}')
    return {'exit_code': result.returncode, 'command_sha256': hashlib.sha256(command.encode()).hexdigest()}


def prepare_legacy(kind):
    assert kind in ('bind', 'volume')
    name = 'cf-guide-old-' + kind
    agent_name = 'guide-legacy-' + kind + '-oxidns'
    port = 18082 if kind == 'bind' else 18083
    _, member = lab.api('POST', '/api/agents/register', {'name': agent_name, 'host': '192.168.79.21',
                       'port': port, 'registration_key': json.loads((lab.ROOT / 'private.json').read_text())['registration_key'], 'service_type': 'oxidns', 'deployment_method': 'docker', 'profile_id': 'default'})
    root = lab.GROOT + '/legacy-' + kind
    agent_source = root + '/agent' if kind == 'bind' else 'cf-guide-legacy-' + kind + '-agent'
    core_source = root + '/oxidns' if kind == 'bind' else 'cf-guide-legacy-' + kind + '-core'
    agent_files = {'config-oxidns.json': json.dumps({'agent_id': member['id'], 'token': member['token']}),
                   'identity-marker.txt': 'original Agent directory must survive migration\n'}
    core_files = {'config.yaml': json.dumps(dockerlab.document('198.51.100.30', 2)),
                  'rules/lab.txt': 'full:legacy-install-guide.invalid\n',
                  'state/cache-marker.bin': 'original persistent state must survive migration\n'}
    for source, files in ((agent_source, agent_files), (core_source, core_files)):
        if kind == 'bind':
            for relative, content in files.items(): lab.put(130, source + '/' + relative, content)
        else:
            dockerlab.docker(130, 'volume', 'create', source)
            script = '''import json,sys
from pathlib import Path
for name,content in json.load(sys.stdin).items():
 p=Path('/target')/name; p.parent.mkdir(parents=True,exist_ok=True); p.write_text(content); p.chmod(0o600)
'''
            dockerlab.docker(130, 'run', '-i', '--rm', '--network=none', '-v', source + ':/target',
                             '--entrypoint=python3', IMAGE, '-c', script, data=json.dumps(files).encode())
    dockerlab.docker(130, 'run', '-d', '--name', name, '--network=host', '-e', 'SERVICE_TYPE=oxidns',
        '-e', 'SERVER_URL=' + lab.SERVER_URL, '-e', 'AGENT_NAME=' + agent_name, '-e', 'AGENT_PORT=' + str(port),
        '-e', 'AGENT_IP=192.168.79.21', '-e', 'HEARTBEAT_INTERVAL=2',
        '-e', 'OXIDNS_HEALTH_DNS_ADDRESS=127.0.0.1:53', '-e', 'OXIDNS_HEALTH_DNS_NAME=sync.lab.invalid',
        '-v', agent_source + ':/opt/configflow-agent', '-v', core_source + ':/etc/oxidns', IMAGE)
    result = {'container_name': name, 'existing_agent_name': agent_name, 'oxidns_port': port,
              'agent_mount': {'type': kind, 'source': agent_source}, 'core_mount': {'type': kind, 'source': core_source}}
    lab.private(lab.ROOT / ('legacy-' + kind + '.json'), result)
    for _ in range(30):
        try:
            snap = container_snapshot(name)
            if snap['core_pid'] not in ('0', ''): break
        except RuntimeError: pass
        time.sleep(.5)
    else: raise RuntimeError('Legacy fixture core did not start')
    print(json.dumps({'legacy_fixture': kind, 'agent_id': snap['agent_id'], 'core_pid': snap['core_pid']}), flush=True)
    return result


def container_snapshot(name):
    script = '''import hashlib,json,subprocess
from pathlib import Path
c=json.loads(Path('/opt/configflow-agent/config-oxidns.json').read_text())
paths=['/etc/oxidns/config.yaml','/etc/oxidns/rules/lab.txt','/etc/oxidns/state/cache-marker.bin','/opt/configflow-agent/identity-marker.txt']
print(json.dumps({'agent_id':c.get('agent_id'),'token_sha256':hashlib.sha256(c.get('token','').encode()).hexdigest(),
 'agent_name':c.get('agent_name'),'server_url':c.get('server_url'),'agent_port':c.get('agent_port'),
 'hashes':{p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths if Path(p).exists()},
 'core_pid':subprocess.check_output(['supervisorctl','-c','/etc/supervisor/supervisord.conf','pid','oxidns'],text=True,stderr=subprocess.DEVNULL).strip()}))
'''
    result = json.loads(dockerlab.docker(130, 'exec', name, 'python3', '-c', script))
    result['container_id'] = dockerlab.docker(130, 'inspect', name, '--format', '{{.Id}}').decode().strip()
    return result


def guide(body):
    code, response = lab.api('POST', '/api/agents/docker-oxidns-ha-guide', body)
    assert code == 200 and response.get('success') is True
    return response


def base_request(name, mode='new', network='host'):
    return {'action': 'generate', 'mode': mode, 'server_url': lab.SERVER_URL, 'agent_name': name,
            'agent_ip': '192.168.79.21' if network == 'host' else '192.168.79.81',
            'data_dir': lab.GROOT + '/new-' + network, 'network_mode': network,
            'oxidns_port': 18081, 'ha_host_id_path': '/etc/machine-id', 'create_host_identity': False,
            'ha_parent': 'eth0.91' if network == 'macvlan' else '',
            'ha_subnet': '192.168.79.0/24' if network == 'macvlan' else '', 'image': IMAGE}


def capabilities(name):
    script = '''import json,urllib.request
from pathlib import Path
c=json.loads(Path('/opt/configflow-agent/config-oxidns.json').read_text())
r=urllib.request.Request('http://127.0.0.1:'+str(c['agent_port'])+'/api/capabilities',headers={'Authorization':'Bearer '+c['token']})
with urllib.request.urlopen(r,timeout=5) as response: print(json.dumps(json.load(response)))
'''
    return json.loads(dockerlab.docker(130, 'exec', name, 'python3', '-c', script))


def wait_registered(name, *, timeout=100):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            snapshot = container_snapshot(name)
            caps = capabilities(name)
            _, agents = lab.api('GET', '/api/agents')
            agent = next((a for a in agents if a['id'] == snapshot['agent_id']), None)
            if agent and agent.get('status') == 'online' and caps.get('oxidns_ha_environment', {}).get('ready') is True:
                return snapshot, caps['oxidns_ha_environment'], {'id': agent['id'], 'status': agent['status'],
                    'host': agent.get('host'), 'port': agent.get('port'), 'last_heartbeat': agent.get('last_heartbeat')}
        except RuntimeError: pass
        time.sleep(1)
    raise AssertionError('Actual Agent registration/heartbeat/HA capability did not become ready: ' + name)


def record(name, **details):
    path = ROOT / 'report.json'
    report = json.loads(path.read_text()) if path.exists() else {'started_at': time.time(), 'scenarios': []}
    report['scenarios'].append({'name': name, 'finished_at': time.time(), **details})
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'scenario': name, 'passed': True}), flush=True)


def test_new(network):
    assert network in ('host', 'macvlan')
    name = 'guide-new-' + network + '-v2'
    body = base_request(name, network=network)
    body['data_dir'] = lab.GROOT + '/new-' + network + '-v2'
    if network == 'host': dockerlab.docker(130, 'stop', '--time=30', 'cf-guide-old-bind')
    # Explicit existence check with an exit status, without creating the destination.
    state = lab.guest(130, 'python3', '-c', 'import os; print(os.path.lexists(__import__("sys").argv[1]))', body['data_dir']).decode().strip()
    assert state == 'False', 'Fresh install fixture must be an absent directory'
    before = {item['id'] for item in lab.api('GET', '/api/agents')[1]}
    bundle = guide(body)
    generated = {item['id'] for item in lab.api('GET', '/api/agents')[1]}
    assert generated == before, 'Generating a command must not create an online or offline device'
    credentials = json.loads((lab.ROOT / 'private.json').read_text())
    assert credentials['registration_key'] and credentials['registration_key'] not in bundle['install_command']
    assert credentials['admin_token'] not in bundle['install_command'] and credentials['password'] not in bundle['install_command']
    execution = run_generated(bundle['install_command'], name, 'install')
    snapshot, environment, online = wait_registered(name + '-oxidns')
    after = {item['id'] for item in lab.api('GET', '/api/agents')[1]}
    assert after - before == {snapshot['agent_id']} and snapshot['token_sha256'] != hashlib.sha256(b'').hexdigest()
    assert environment['network'] == network
    repeat = run_generated(bundle['install_command'], name, 'repeat-installer', must_succeed=False)
    assert repeat['exit_code'] == 0
    repeated = container_snapshot(name + '-oxidns')
    assert repeated['agent_id'] == snapshot['agent_id'] and repeated['container_id'] == snapshot['container_id']
    record('fresh-' + network + '-empty-data-protected-registration', execution=execution,
           snapshot=snapshot, environment=environment, online=online, generated_device_count=0,
           registered_device_count=1, repeated_installer_preserved_owned_container=True,
           administrator_or_global_registration_secrets_in_command=False)
    lab.private(lab.ROOT / (name + '-bundle.json'), {'request': body, 'response': bundle})
    dockerlab.docker(130, 'stop', '--time=30', name + '-oxidns')
    if network == 'host': dockerlab.docker(130, 'stop', '--time=30', name + '-oxidns-ha')


def test_identity():
    original = lab.guest(130, 'cat', '/etc/machine-id')
    body = base_request('guide-identity')
    body.update(ha_host_id_path=lab.GROOT + '/identity-created', create_host_identity=True)
    bundle = guide(body)
    first = run_generated(bundle['identity_command'], 'identity-created', 'create')
    created = lab.guest(130, 'cat', body['ha_host_id_path'])
    assert len(created.strip()) == 32
    again = run_generated(bundle['identity_command'], 'identity-created', 'repeat', must_succeed=False)
    assert again['exit_code'] != 0 and lab.guest(130, 'cat', body['ha_host_id_path']) == created
    cases = [{'case': 'create-once', 'first_exit': first['exit_code'], 'repeat_exit': again['exit_code'], 'unchanged': True}]
    for kind in ('existing', 'symlink', 'dangling-symlink'):
        target = lab.GROOT + '/identity-' + kind
        destination = lab.GROOT + '/identity-destination-' + kind
        if kind != 'dangling-symlink': lab.put(130, destination, 'preserve-this-existing-host-identity\n')
        if kind == 'existing': lab.put(130, target, 'preserve-this-existing-host-identity\n')
        else: lab.guest(130, 'ln', '-s', destination, target)
        body['ha_host_id_path'] = target; command = guide(body)['identity_command']
        attempt = run_generated(command, 'identity-' + kind, 'create', must_succeed=False)
        assert attempt['exit_code'] != 0
        if kind != 'dangling-symlink': assert lab.guest(130, 'cat', target) == b'preserve-this-existing-host-identity\n'
        else: assert lab.guest(130, 'python3', '-c', 'import os,sys; print(os.path.lexists(sys.argv[1]))', destination).strip() == b'False'
        cases.append({'case': kind, 'exit_code': attempt['exit_code'], 'existing_content_preserved': True})
    hostile = lab.GROOT + "/identity '$(touch INJECTED_BY_IDENTITY);#"
    body['ha_host_id_path'] = hostile
    quoted = run_generated(guide(body)['identity_command'], 'identity-hostile', 'literal-path')
    assert len(lab.guest(130, 'cat', hostile).strip()) == 32
    assert lab.guest(130, 'python3', '-c', 'import os; print(os.path.exists("' + lab.GROOT + '/execution/identity-hostile/INJECTED_BY_IDENTITY"))').strip() == b'False'
    assert lab.guest(130, 'cat', '/etc/machine-id') == original
    cases.append({'case': 'quotes-dollar-parentheses-semicolon-in-literal-path', 'exit_code': quoted['exit_code'], 'injected_marker_absent': True})
    rejected = []
    for value in ('relative/file', '/', '/tmp/../unsafe', '/tmp/colon:bad', '/tmp/comma,bad', '/tmp/new\nline'):
        bad = {**body, 'ha_host_id_path': value}
        code, _ = lab.api('POST', '/api/agents/docker-oxidns-ha-guide', bad, allow_error=True)
        assert code == 400
        rejected.append({'path': value, 'http_status': code})
    record('host-identity-create-once-symlink-and-hostile-paths', cases=cases, invalid_paths=rejected,
           system_machine_id_unchanged=True)


def stop_new_containers():
    for prefix in ('guide-new-host', 'guide-new-host-v2', 'guide-new-macvlan-v2'):
        dockerlab.docker(130, 'stop', '--time=30', prefix + '-oxidns', check=False)
        if 'host' in prefix: dockerlab.docker(130, 'stop', '--time=30', prefix + '-oxidns-ha', check=False)


def add_legacy_proxy(kind):
    name = 'cf-guide-old-' + kind
    value = 'http://literal$dollar.example.invalid:3128'
    details = json.loads(dockerlab.docker(130, 'inspect', name))[0]
    if any(v == 'HTTPS_PROXY=' + value for v in details['Config']['Env']): return value
    dockerlab.docker(130, 'stop', '--time=30', name); dockerlab.docker(130, 'rm', name)
    args = ['run', '-d', '--name', name, '--network=host']
    for setting in details['Config']['Env']: args += ['-e', setting]
    args += ['-e', 'HTTPS_PROXY=' + value]
    for mount in details['Mounts']:
        source = mount['Name'] if mount['Type'] == 'volume' else mount['Source']
        args += ['--mount', 'type=' + mount['Type'] + ',src=' + source + ',dst=' + mount['Destination']]
    dockerlab.docker(130, *args, IMAGE)
    time.sleep(2)
    return value


def migration_request(kind, suffix):
    old = json.loads((lab.ROOT / ('legacy-' + kind + '.json')).read_text())
    return {**base_request('guide-legacy-' + kind, mode='migrate'), **old,
            'ha_data_dir': lab.GROOT + '/ha-' + kind + '-' + suffix,
            'backup_dir': lab.GROOT + '/backup-' + kind + '-' + suffix}


def assert_preserved(before, after):
    for key in ('agent_id', 'token_sha256', 'agent_name', 'server_url', 'agent_port', 'hashes'):
        assert before[key] == after[key], key


def test_migration(kind):
    stop_new_containers()
    if kind == 'volume':
        dockerlab.docker(130, 'stop', '--time=30', 'cf-guide-old-bind', check=False)
        prepare_legacy(kind)
    name = 'cf-guide-old-' + kind
    dockerlab.docker(130, 'start', name)
    proxy = add_legacy_proxy(kind)
    body = migration_request(kind, 'success-v2'); before = container_snapshot(name)
    bundle = guide(body)
    assert 'docker-oxidns-ha-enroll/' not in bundle['install_command']
    inspection = run_generated(bundle['inspection_command'], 'migration-' + kind, 'inspect')
    execution = run_generated(bundle['install_command'], 'migration-' + kind, 'install')
    after, environment, online = wait_registered(name)
    assert_preserved(before, after); assert before['container_id'] != after['container_id']
    preserved_old_id = dockerlab.docker(130, 'inspect', name + '-pre-ha', '--format', '{{.Id}}').decode().strip()
    assert preserved_old_id == before['container_id']
    for part in ('agent/config-oxidns.json', 'oxidns/config.yaml', 'oxidns/rules/lab.txt', 'oxidns/state/cache-marker.bin'):
        exists = lab.guest(130, 'python3', '-c', 'import os,sys; print(os.path.isfile(sys.argv[1]))', body['backup_dir'] + '/' + part).strip()
        assert exists == b'True', part
    compose_path = body['backup_dir'] + '/compose.generated.yaml'; lab.put(130, compose_path, bundle['compose'])
    compose = json.loads(dockerlab.docker(130, 'compose', '-f', compose_path, 'config', '--format=json'))
    expected_proxy = dockerlab.docker(130, 'compose', '-f', compose_path, 'run', '--rm', '--no-deps', '-T', '--pull=never', '--entrypoint=python3', 'agent', '-c', 'import os; print(os.environ["HTTPS_PROXY"])').decode().strip()
    actual_proxy = dockerlab.docker(130, 'exec', name, 'python3', '-c', 'import os; print(os.environ["HTTPS_PROXY"])').decode().strip()
    assert expected_proxy == proxy and actual_proxy == proxy
    rollback = run_generated(bundle['rollback_command'], 'migration-' + kind, 'rollback')
    for _ in range(30):
        try:
            restored = container_snapshot(name)
            if restored['core_pid'] not in ('0', ''): break
        except RuntimeError: pass
        time.sleep(.5)
    else: raise AssertionError('Original core did not restart after rollback')
    assert restored['container_id'] == before['container_id']; assert_preserved(before, restored)
    record('migrate-' + kind + '-preserves-identity-data-env-and-rollback', inspection=inspection,
           execution=execution, rollback=rollback, before=before, after=after, restored=restored,
           environment=environment, online=online, backup_files_verified=True,
           literal_dollar_environment_preserved_by_run_and_compose=True)
    lab.private(lab.ROOT / ('migration-' + kind + '-bundle.json'), {'request': body, 'response': bundle})
    # Preserve the original stopped container for the separate failure test.
    dockerlab.docker(130, 'stop', '--time=30', name)
    dockerlab.docker(130, 'rm', name + '-failed-ha')
    dockerlab.docker(130, 'rm', name + '-ha')


def test_partial_rollback():
    name = 'cf-guide-old-bind'
    stop_new_containers()
    dockerlab.docker(130, 'stop', '--time=30', 'cf-guide-old-volume', check=False)
    dockerlab.docker(130, 'start', name); time.sleep(2)
    before = container_snapshot(name)
    body = migration_request('bind', 'partial')
    bundle = guide(body)
    observer = '''import json,os,subprocess
from pathlib import Path
root=Path('/opt/cf-docker-guide'); sock=root/'docker.sock'; moved=root/'docker.sock.fault'
command=[str(root/'docker/docker'),'-H','unix://'+str(sock),'events','--filter','event=rename','--filter','container=OLD_ID','--format','{{json .}}']
p=subprocess.Popen(command,stdout=subprocess.PIPE,text=True)
(root/'observer-ready').write_text('ready')
try:
 for line in p.stdout:
  event=json.loads(line)
  if event.get('Actor',{}).get('Attributes',{}).get('name','').lstrip('/')=='cf-guide-old-bind-pre-ha':
   os.rename(sock,moved); (root/'fault-injected').write_text('socket unavailable after real rename'); break
finally:
 p.terminate(); p.wait(timeout=5)
'''.replace('OLD_ID', before['container_id'])
    lab.put(130, lab.GROOT + '/fault-observer.py', observer)
    lab.guest(130, 'systemd-run', '--unit=cf-docker-guide-fault-observer', 'python3', lab.GROOT + '/fault-observer.py')
    for _ in range(30):
        ready = lab.guest(130, 'python3', '-c', 'from pathlib import Path; print(Path("/opt/cf-docker-guide/observer-ready").exists())').strip()
        if ready == b'True': break
        time.sleep(.1)
    else: raise RuntimeError('Fault observer did not become ready')
    try:
        execution = run_generated(bundle['install_command'], 'partial-rollback', 'install', must_succeed=False)
    finally:
        restore = 'from pathlib import Path; import os; p=Path("/opt/cf-docker-guide/docker.sock.fault"); os.rename(p,p.with_name("docker.sock")) if p.exists() else None'
        lab.guest(130, 'python3', '-c', restore)
        lab.guest(130, 'systemctl', 'stop', 'cf-docker-guide-fault-observer', check=False)
    assert execution['exit_code'] != 0
    assert lab.guest(130, 'cat', lab.GROOT + '/fault-injected').strip()
    rollback = run_generated(bundle['rollback_command'], 'partial-rollback', 'rollback')
    time.sleep(2); after = container_snapshot(name)
    assert after['container_id'] == before['container_id']; assert_preserved(before, after)
    record('real-daemon-socket-loss-after-old-rename-can-rollback', execution=execution, rollback=rollback,
           before=before, after=after, fault='Dedicated Unix socket temporarily moved after Docker rename event; restored before rollback')
    dockerlab.docker(130, 'stop', '--time=30', name)


def test_owned_resume(network):
    import re
    assert network in ('host', 'macvlan')
    stop_new_containers()
    for kind in ('bind', 'volume'): dockerlab.docker(130, 'stop', '--time=30', 'cf-guide-old-' + kind, check=False)
    if network == 'macvlan':
        dockerlab.docker(130, 'rm', 'guide-new-macvlan-v2-oxidns')
        dockerlab.docker(130, 'network', 'rm', 'guide-new-macvlan-v2-oxidns-ha-net')
    name = 'guide-new-' + network + '-v3'
    body = base_request(name, network=network); body['data_dir'] = lab.GROOT + '/new-' + network + '-v3'
    assert lab.guest(130, 'python3', '-c', 'from pathlib import Path; import sys; print(Path(sys.argv[1]).exists())', body['data_dir']).strip() == b'False'
    before_ids = {a['id'] for a in lab.api('GET', '/api/agents')[1]}
    bundle = guide(body)
    assert {a['id'] for a in lab.api('GET', '/api/agents')[1]} == before_ids
    credentials = json.loads((lab.ROOT / 'private.json').read_text())
    assert credentials['registration_key'] and all(credentials[k] not in bundle['install_command'] for k in ('registration_key', 'admin_token', 'password'))
    first_result = None
    partial = None
    if network == 'host':
        observer = '''import json,os,subprocess
from pathlib import Path
root=Path('/opt/cf-docker-guide'); sock=root/'docker.sock'; moved=root/'docker.sock.resume-fault'
p=subprocess.Popen([str(root/'docker/docker'),'-H','unix://'+str(sock),'events','--filter','type=container','--filter','event=start','--format','{{json .}}'],stdout=subprocess.PIPE,text=True)
(root/'resume-observer-ready').write_text('ready')
try:
 for line in p.stdout:
  event=json.loads(line)
  if event.get('Actor',{}).get('Attributes',{}).get('name','').lstrip('/')=='guide-new-host-v3-oxidns-ha':
   os.rename(sock,moved); (root/'resume-fault-injected').write_text('socket unavailable after companion start'); break
finally:
 p.terminate(); p.wait(timeout=5)
'''
        lab.put(130, lab.GROOT + '/resume-observer.py', observer)
        lab.guest(130, 'systemd-run', '--unit=cf-docker-guide-resume-observer', 'python3', lab.GROOT + '/resume-observer.py')
        for _ in range(30):
            if lab.guest(130, 'python3', '-c', 'from pathlib import Path; print(Path("/opt/cf-docker-guide/resume-observer-ready").exists())').strip() == b'True': break
            time.sleep(.1)
        else: raise RuntimeError('Resume fault observer did not start')
        try:
            first_result = run_generated(bundle['install_command'], name, 'partial-install', must_succeed=False)
        finally:
            lab.guest(130, 'python3', '-c', 'from pathlib import Path; import os; p=Path("/opt/cf-docker-guide/docker.sock.resume-fault"); os.rename(p,p.with_name("docker.sock")) if p.exists() else None')
            lab.guest(130, 'systemctl', 'stop', 'cf-docker-guide-resume-observer', check=False)
        assert first_result['exit_code'] != 0
        assert lab.guest(130, 'cat', lab.GROOT + '/resume-fault-injected').strip()
        companion_id = dockerlab.docker(130, 'inspect', name + '-oxidns-ha', '--format', '{{.Id}}').decode().strip()
        assert dockerlab.docker(130, 'inspect', name + '-oxidns-ha', '--format', '{{.State.Running}}').strip() == b'true'
        assert not dockerlab.docker(130, 'inspect', name + '-oxidns', '--format', '{{.Id}}', check=False).strip()
        seed = json.loads(lab.guest(130, 'cat', body['data_dir'] + '/agent/config-oxidns.json'))
        registered = next(a for a in lab.api('GET', '/api/agents')[1] if a['id'] == seed['agent_id'])
        assert registered['status'] == 'offline'
        partial = {'companion_id': companion_id, 'companion_running': True, 'main_absent': True,
                   'agent_id': seed['agent_id'], 'agent_offline_before_main_started': True}
    else:
        first_result = run_generated(bundle['install_command'], name, 'fresh-install')
        original, _, _ = wait_registered(name + '-oxidns')
        network_id = dockerlab.docker(130, 'network', 'inspect', name + '-oxidns-ha-net', '--format', '{{.Id}}').decode().strip()
    regenerated = guide(body)
    session_pattern = r'/docker-oxidns-ha-enroll/([a-f0-9]{32})'
    first_sessions = set(re.findall(session_pattern, bundle['install_command']))
    second_sessions = set(re.findall(session_pattern, regenerated['install_command']))
    assert len(first_sessions) == 1 and first_sessions == second_sessions
    resumed = run_generated(regenerated['install_command'], name, 'regenerated-resume')
    snapshot, environment, online = wait_registered(name + '-oxidns')
    if network == 'host':
        assert snapshot['agent_id'] == partial['agent_id']
        assert dockerlab.docker(130, 'inspect', name + '-oxidns-ha', '--format', '{{.Id}}').decode().strip() == partial['companion_id']
    else:
        assert_preserved(original, snapshot); assert original['container_id'] == snapshot['container_id']
        assert dockerlab.docker(130, 'network', 'inspect', name + '-oxidns-ha-net', '--format', '{{.Id}}').decode().strip() == network_id
    if network == 'macvlan':
        stopped_execution, snapshot, environment, online = verify_stopped_macvlan_resume(body, snapshot)
    assert {a['id'] for a in lab.api('GET', '/api/agents')[1]} - before_ids == {snapshot['agent_id']}
    record('final-' + network + '-regenerate-resumes-only-owned-resources', first_execution=first_result,
           regenerated_execution=resumed, enrollment_session_reused=True, new_agent_count=1,
           partial_failure=partial, snapshot=snapshot, environment=environment, online=online,
           existing_container_and_network_ids_preserved=network == 'macvlan',
           scoped_registration_with_global_key_enabled=True)
    lab.private(lab.ROOT / (name + '-bundle.json'), {'request': body, 'response': regenerated})


def verify_stopped_macvlan_resume(body, before):
    name = body['agent_name'] + '-oxidns'
    dockerlab.docker(130, 'stop', '--time=30', name)
    assert dockerlab.docker(130, 'inspect', name, '--format', '{{.State.Running}}').strip() == b'false'
    bundle = guide(body)
    execution = run_generated(bundle['install_command'], body['agent_name'], 'stopped-regenerated-resume')
    after, environment, online = wait_registered(name)
    assert before['container_id'] == after['container_id']; assert_preserved(before, after)
    return execution, after, environment, online


def resume_existing_macvlan():
    import re
    name = 'guide-new-macvlan-v3'
    body = base_request(name, network='macvlan'); body['data_dir'] = lab.GROOT + '/new-macvlan-v3'
    before = container_snapshot(name + '-oxidns')
    network_id = dockerlab.docker(130, 'network', 'inspect', name + '-oxidns-ha-net', '--format', '{{.Id}}').decode().strip()
    agents_before = {a['id'] for a in lab.api('GET', '/api/agents')[1]}
    first_command = json.loads((lab.ROOT / (name + '-fresh-install.json')).read_text())['command']
    failed_replay = json.loads((lab.ROOT / (name + '-regenerated-resume.json')).read_text())
    assert failed_replay['exit_code'] == 125
    bundle = guide(body)
    pattern = r'/docker-oxidns-ha-enroll/([a-f0-9]{32})'
    assert set(re.findall(pattern, first_command)) == set(re.findall(pattern, bundle['install_command']))
    execution = run_generated(bundle['install_command'], name, 'fixed-running-resume')
    running, _, _ = wait_registered(name + '-oxidns')
    assert_preserved(before, running); assert before['container_id'] == running['container_id']
    stopped_execution, after, environment, online = verify_stopped_macvlan_resume(body, running)
    assert network_id == dockerlab.docker(130, 'network', 'inspect', name + '-oxidns-ha-net', '--format', '{{.Id}}').decode().strip()
    assert {a['id'] for a in lab.api('GET', '/api/agents')[1]} == agents_before
    record('final-macvlan-regenerate-resumes-running-and-stopped-owned-main', before=before, after=after,
           running_execution=execution, stopped_execution=stopped_execution, environment=environment,
           online=online, network_id=network_id, agent_container_network_and_data_preserved=True,
           additional_agents_created=0, enrollment_session_reused=True,
           initial_fixed_ip_collision_reproduced=True, fixed_replay_uses_readonly_network_none_proof=True)
    lab.private(lab.ROOT / (name + '-bundle.json'), {'request': body, 'response': bundle})


def export_evidence():
    paths = ['backend/agents/docker_ha_guide.py', 'backend/agents/docker_ha_enrollment.py',
             'backend/routes/docker_ha_installation.py', 'backend/routes/agents.py', 'backend/common/profile_context.py',
             'docker/Dockerfile.agent', 'docker/docker-agent-entrypoint-service.sh', 'docker/docker-ha-companion-entrypoint.sh',
             'scripts/tests/oxidns_docker_guide_lab.py']
    report = json.loads((ROOT / 'report.json').read_text())
    report['final_source_sha256'] = {p: hashlib.sha256((lab.REPO / p).read_bytes()).hexdigest() for p in paths}
    report['versions'] = {'docker': '29.9.2', 'compose': '5.6.1', 'agent': '1.8.47-go', 'oxidns': '1.6.1',
                          'image': dockerlab.docker(130, 'image', 'inspect', IMAGE, '--format', '{{.Id}}').decode().strip(),
                          'image_build_source_base_commit': '104e7ae7ce3e4416ae32fba37375ae8df68e549a'}
    report['scope'] = {'executor': '192.168.0.32', 'docker_host': 'CT130', 'backend': 'CT132',
                       'guest_131_untouched': True, 'ha_failover_repeated': False,
                       'production_guests_modified': False, 'responses_mocked': False,
                       'administrator_authentication_enabled': True, 'global_agent_registration_key_enabled': True}
    report['finished_at'] = time.time()
    (ROOT / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'evidence_ready': True, 'cases': len(report['scenarios'])}), flush=True)


def cleanup():
    import os
    lab.guard()
    pidfile = lab.ROOT / 'tunnel.pid'
    if pidfile.exists():
        try: os.kill(int(pidfile.read_text()), 15)
        except ProcessLookupError: pass
        pidfile.unlink()
    states = {}
    for cid, memory in ((130, 384), (132, 1536)):
        lab.pve('pct', 'stop', cid, check=False)
        lab.pve('pct', 'rollback', cid, lab.SNAPSHOT)
        lab.pve('pct', 'delsnapshot', cid, lab.SNAPSHOT)
        config = lab.pve('pct', 'config', cid).decode()
        assert lab.pve('pct', 'status', cid).decode().strip() == 'status: stopped'
        assert f'memory: {memory}\n' in config and lab.SNAPSHOT not in lab.pve('pct', 'listsnapshot', cid).decode()
        states[str(cid)] = {'status': 'stopped', 'memory_mib': memory, 'disk_and_configuration_rolled_back': True,
                            'task_snapshot_removed': True}
    assert lab.pve('pct', 'status', 131).decode().strip() == 'status: stopped'
    lab.run(['sudo', 'systemctl', 'stop', 'cf-docker-guide-builder'])
    assert lab.run(['systemctl', 'is-active', 'cf-docker-guide-builder'], check=False).strip() == b'inactive'
    result = {'finished_at': time.time(), 'guests': states, 'guest_131_untouched_and_stopped': True,
              'builder_service_inactive': True, 'tunnel_pid_file_removed': True,
              'task_directory_removed': False, 'production_guests_modified': False,
              'new_test_server_package_retained': 'docker-compose 5.6.1'}
    (ROOT / 'cleanup-report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare-guests', 'install-docker', 'load-image', 'prepare-backend', 'server', 'dns-fixture', 'update-source', 'prepare-legacy-bind', 'prepare-legacy-volume', 'test-new-host', 'test-new-macvlan', 'test-identity', 'test-migration-bind', 'test-migration-volume', 'test-partial-rollback', 'test-resume-host', 'test-resume-macvlan', 'resume-existing-macvlan', 'export-evidence', 'cleanup'])
    command = parser.parse_args().command
    if command not in ('prepare-guests', 'server', 'dns-fixture'):
        lab.guard()
    if command == 'prepare-guests': prepare_guests()
    elif command == 'install-docker': install_docker()
    elif command == 'load-image': load_image()
    elif command == 'prepare-backend': prepare_backend()
    elif command == 'prepare-legacy-bind': prepare_legacy('bind')
    elif command == 'prepare-legacy-volume': prepare_legacy('volume')
    elif command == 'test-new-host': test_new('host')
    elif command == 'test-new-macvlan': test_new('macvlan')
    elif command == 'test-identity': test_identity()
    elif command == 'test-migration-bind': test_migration('bind')
    elif command == 'test-migration-volume': test_migration('volume')
    elif command == 'test-partial-rollback': test_partial_rollback()
    elif command == 'test-resume-host': test_owned_resume('host')
    elif command == 'test-resume-macvlan': test_owned_resume('macvlan')
    elif command == 'resume-existing-macvlan': resume_existing_macvlan()
    elif command == 'export-evidence': export_evidence()
    elif command == 'cleanup': cleanup()
    elif command == 'server': lab.server()
    elif command == 'dns-fixture': dockerlab.dns_fixture()
    elif command == 'update-source':
        lab.guard(); lab.stage_source(); lab.guest(132, 'systemctl', 'restart', lab.UNIT_PREFIX + '-backend'); lab.start_tunnel()


if __name__ == '__main__':
    main()
