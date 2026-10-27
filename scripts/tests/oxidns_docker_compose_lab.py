#!/usr/bin/env python3
"""Real Compose packages in an isolated .32 Docker/network namespace.

Requires the current Dockerfile image built in ROOT/builder first. Never uses the
default Docker socket, the real LAN, PVE, or the existing preview backend.
"""
import argparse
import base64
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import zipfile

ROOT = Path('/home/sunbroke/configflow-docker-compose-lab')
REPO = ROOT / 'source'
SOCKET = 'unix://' + str(ROOT / 'builder/docker.sock')
IMAGE = 'cf-oxidns-compose-lab:20261027'
NS = 'cf-compose-lab'
BRIDGE = 'cfcp-br'
VETH = 'cfcp-out'
NETWORK = ipaddress.ip_network('10.203.77.0/24')
BACKEND_IP = '10.203.77.1'
HOST_IP = '10.203.77.2'
MAC_IP = '10.203.77.81'
URL = 'http://' + BACKEND_IP + ':19420'
UNIT = 'cf-compose-lab-backend'
RUNTIME = 'cf-compose-lab-dockerd'
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def run(args, *, check=True, input=None, timeout=60):
    result = subprocess.run([str(v) for v in args], input=input, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f'{args[0]} failed, exit {result.returncode}; command output withheld (may contain installation credentials)')
    return result


def guard():
    assert sys.platform == 'linux'
    assert '192.168.0.32' in run(['ip', '-j', '-4', 'address']).stdout.decode()


def private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n'); path.chmod(0o600)


def state():
    return json.loads((ROOT / 'baseline.json').read_text())


def docker(*args, **kw):
    return run(['sudo', '-n', 'docker', '-H', SOCKET, *args], **kw).stdout


def local(*args, **kw):
    return run(['sudo', '-n', 'ip', 'netns', 'exec', NS, 'env', 'DOCKER_HOST=' + SOCKET, *args], **kw)


def wait_docker():
    for _ in range(40):
        result = run(['sudo', '-n', 'docker', '-H', SOCKET, 'info', '--format', '{{.ServerVersion}}'], check=False, timeout=5)
        if result.returncode == 0: return
        time.sleep(.5)
    raise AssertionError('Isolated Docker daemon not ready')


def prepare_builder():
    guard(); assert not ROOT.exists(), 'Refuse to reuse an existing lab directory'
    before = {name: run(['systemctl', 'is-active', name], check=False).stdout.decode().strip()
              for name in ('docker', 'docker.socket', 'containerd')}
    assert all(value == 'inactive' for value in before.values())
    before['dns'] = {p: sorted(run(['ss', flags, 'sport = :53']).stdout.decode().splitlines())
                     for p, flags in [('udp', '-Hlun'), ('tcp', '-Hltn')]}
    before['unit'] = 'cf-docker-compose-builder-' + uuid.uuid4().hex[:8]
    ROOT.mkdir(mode=0o700); REPO.mkdir(); (ROOT / 'builder').mkdir()
    private(ROOT / 'baseline.json', before)
    private(ROOT / 'builder/daemon.json', {
        'data-root': str(ROOT / 'builder/data'), 'exec-root': str(ROOT / 'builder/exec'),
        'pidfile': str(ROOT / 'builder/docker.pid'), 'hosts': [SOCKET], 'bridge': 'none',
        'iptables': False, 'ip6tables': False, 'ip-forward': False, 'ip-masq': False})
    run(['sudo', '-n', 'systemd-run', '--unit=' + before['unit'], '--property=Delegate=yes',
         '--property=TasksMax=infinity', '/usr/bin/dockerd', '--config-file=' + str(ROOT / 'builder/daemon.json')])
    wait_docker()


def build_image():
    guard()
    docker('build', '--network=host', '--platform=linux/amd64', '-f', str(REPO / 'docker/Dockerfile.agent'),
           '-t', IMAGE, str(REPO), timeout=1800)


def prepare_runtime():
    guard(); before = state()
    assert NS not in run(['sudo', '-n', 'ip', 'netns', 'list']).stdout.decode()
    assert run(['ip', 'link', 'show', BRIDGE], check=False).returncode
    routes = json.loads(run(['ip', '-j', '-4', 'route', 'show', 'table', 'all']).stdout)
    for route in routes:
        dest = route.get('dst', 'default')
        if dest != 'default': assert not ipaddress.ip_network(dest).overlaps(NETWORK), dest
    image_id = docker('image', 'inspect', IMAGE, '--format', '{{.Id}}').decode().strip()
    private(ROOT / 'provenance.json', {'image': IMAGE, 'image_id': image_id,
        'dockerfile_sha256': hashlib.sha256((REPO / 'docker/Dockerfile.agent').read_bytes()).hexdigest()})
    run(['sudo', '-n', 'systemctl', 'stop', before['unit']])
    for command in (
        ['ip', 'link', 'add', BRIDGE, 'type', 'bridge'],
        ['ip', 'addr', 'add', BACKEND_IP + '/24', 'dev', BRIDGE],
        ['ip', 'link', 'set', BRIDGE, 'up'],
        ['ip', 'netns', 'add', NS],
        ['ip', 'link', 'add', VETH, 'type', 'veth', 'peer', 'name', 'cfcp-in'],
        ['ip', 'link', 'set', VETH, 'master', BRIDGE],
        ['ip', 'link', 'set', VETH, 'up'],
        ['ip', 'link', 'set', 'cfcp-in', 'netns', NS],
        ['ip', '-n', NS, 'link', 'set', 'cfcp-in', 'name', 'eth0'],
        ['ip', '-n', NS, 'addr', 'add', HOST_IP + '/24', 'dev', 'eth0'],
        ['ip', '-n', NS, 'link', 'set', 'eth0', 'up'],
        ['ip', '-n', NS, 'link', 'set', 'lo', 'up'],
    ): run(['sudo', '-n', *command])
    daemon_config = json.loads((ROOT / 'builder/daemon.json').read_text())
    daemon_config.update({'exec-opts': ['native.cgroupdriver=cgroupfs'], 'cgroup-parent': 'cf-compose-lab'})
    private(ROOT / 'builder/daemon.json', daemon_config)
    run(['sudo', '-n', 'systemd-run', '--unit=' + RUNTIME, '--property=Delegate=yes', '--property=TasksMax=infinity',
         '/usr/bin/ip', 'netns', 'exec', NS, '/usr/bin/unshare', '--mount', '--pid', '--fork', '--mount-proc',
         '/bin/bash', '-c', 'mount -t cgroup2 none /sys/fs/cgroup && exec /usr/bin/dockerd --config-file=' + str(ROOT / 'builder/daemon.json')])
    wait_docker()
    assert docker('image', 'inspect', IMAGE, '--format', '{{.Id}}').decode().strip() == image_id
    credentials = {'username': 'compose-lab', 'password': secrets.token_urlsafe(24),
                   'jwt_secret': secrets.token_urlsafe(48), 'registration_key': secrets.token_urlsafe(24)}
    private(ROOT / 'credentials.json', credentials)
    start_backend()
    print(json.dumps({'isolated_daemon_ready': True, 'backend': URL, 'image_id': image_id}), flush=True)


def start_backend():
    run(['sudo', '-n', 'systemd-run', '--unit=' + UNIT, '--uid=sunbroke',
         '--property=WorkingDirectory=' + str(ROOT), '/home/sunbroke/cf-test/.venv/bin/python',
         str(REPO / 'scripts/tests/oxidns_docker_compose_lab.py'), 'server'])
    login()


def server():
    credentials = json.loads((ROOT / 'credentials.json').read_text())
    os.environ.update(DATA_DIR=str(ROOT / 'data'), LOG_FILE=str(ROOT / 'backend.log'), LOG_LEVEL='WARNING',
        ADMIN_USERNAME=credentials['username'], ADMIN_PASSWORD=credentials['password'],
        JWT_SECRET_KEY=credentials['jwt_secret'], AGENT_REGISTRATION_KEY=credentials['registration_key'],
        SUB_STORE_BUILTIN_DIR=str(ROOT / 'no-sub-store'), NO_PROXY='*')
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        os.environ.pop(key, None)
    os.chdir(ROOT); sys.path.insert(0, str(REPO))
    from backend.common import config as store
    from backend.common.config_repository import ProfileRepository
    store.set_repository(ProfileRepository(ROOT / 'data', default_config_factory=store.get_default_config,
                                           initial_config_factory=store.get_default_config))
    from backend.app import app
    from flask import request
    @app.before_request
    def count_enrollment():
        if request.endpoint == 'agents.docker_oxidns_ha_enroll':
            with (ROOT / 'enrollment-events.jsonl').open('a') as stream:
                stream.write(json.dumps({'time': time.time(), 'event': 'enroll'}) + '\n')
    from werkzeug.serving import make_server
    make_server(BACKEND_IP, 19420, app, threaded=True).serve_forever()


def request(path, data=None, token=None, allow_error=False):
    headers = {'Content-Type': 'application/json'}
    if token: headers['Authorization'] = 'Bearer ' + token
    req = urllib.request.Request(URL + path, None if data is None else json.dumps(data).encode(), headers)
    try:
        with HTTP.open(req, timeout=10) as response: return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        result = json.load(error)
        if allow_error: return error.code, result
        raise RuntimeError(f'Backend request failed: HTTP {error.code}') from None


def login():
    credentials = json.loads((ROOT / 'credentials.json').read_text())
    for _ in range(40):
        try:
            _, value = request('/api/auth/login', {key: credentials[key] for key in ('username', 'password')})
            credentials['admin_token'] = value['token']; private(ROOT / 'credentials.json', credentials)
            return value['token']
        except (OSError, RuntimeError): time.sleep(.5)
    raise AssertionError('Isolated backend not ready')


def api(path, data=None):
    return request(path, data, json.loads((ROOT / 'credentials.json').read_text())['admin_token'])


def snapshot(name):
    config = json.loads(docker('exec', name, 'cat', '/opt/configflow-agent/config-oxidns.json'))
    inspect = json.loads(docker('inspect', name))[0]
    return {'config': config, 'id': inspect['Id'], 'mounts': inspect['Mounts']}


def agent_get(snap, path):
    config = snap['config']
    req = urllib.request.Request('http://' + config['agent_ip'] + ':' + str(config['agent_port']) + path,
                                headers={'Authorization': 'Bearer ' + config['token']})
    with HTTP.open(req, timeout=8) as response: return json.load(response)


def online(name, network):
    for _ in range(70):
        try:
            snap = snapshot(name); capability = agent_get(snap, '/api/capabilities')
            environment = capability['oxidns_ha_environment']
            agents = api('/api/agents')[1]
            agent = next(item for item in agents if item['id'] == snap['config']['agent_id'])
            if agent.get('status') == 'online' and environment['ready'] and environment['network'] == network:
                return snap, environment
        except (OSError, RuntimeError, KeyError, StopIteration, ValueError): pass
        time.sleep(.5)
    raise AssertionError('Agent not online with ready HA environment')


def config_fixture(name):
    config = {'log': {'level': 'warn'}, 'plugins': [
        {'tag': 'lab_answer', 'type': 'black_hole', 'args': {'mode': 'custom', 'ips': ['198.51.100.27'], 'short_circuit': True}},
        {'tag': 'sequence_main', 'type': 'sequence', 'args': [{'exec': '$lab_answer'}]},
        {'tag': 'udp', 'type': 'udp_server', 'args': {'entry': 'sequence_main', 'listen': '0.0.0.0:53'}},
        {'tag': 'tcp', 'type': 'tcp_server', 'args': {'entry': 'sequence_main', 'listen': '0.0.0.0:53'}}]}
    docker('exec', '-i', name, 'python3', '-c', 'import sys;from pathlib import Path;Path("/etc/oxidns/config.yaml").write_bytes(sys.stdin.buffer.read())', input=json.dumps(config).encode())
    docker('exec', name, 'supervisorctl', '-c', '/etc/supervisor/supervisord.conf', 'restart', 'oxidns')


def dns(address):
    query_id = secrets.randbelow(65536)
    question = b''.join(bytes([len(part)]) + part.encode() for part in 'compose-lab.invalid'.split('.')) + b'\0' + struct.pack('!HH', 1, 1)
    query = struct.pack('!HHHHHH', query_id, 0x100, 1, 0, 0, 0) + question
    for _ in range(20):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(.5); sock.sendto(query, (address, 53)); response, _ = sock.recvfrom(4096)
            assert response[:2] == query[:2] and response[3] & 15 == 0
            assert response[-4:] == socket.inet_aton('198.51.100.27')
            return True
        except (OSError, AssertionError): time.sleep(.25)
    raise AssertionError('Local fixture DNS did not answer')


def compose(package, *args):
    return local('docker', 'compose', '-f', str(package / 'compose.yaml'), *args, timeout=90)


def test_mode(network):
    guard(); name = 'compose-lab-' + network
    package = ROOT / ('volume-host' if network == 'host' else 'package-macvlan')
    body = {'action': 'generate', 'mode': 'new', 'install_method': 'compose', 'server_url': URL, 'agent_name': name,
        'agent_ip': HOST_IP if network == 'host' else MAC_IP, 'data_dir': str(ROOT / ('volume-' + network)),
        'network_mode': network, 'oxidns_port': 18082, 'ha_host_id_path': '/etc/machine-id', 'create_host_identity': False,
        'ha_parent': 'eth0' if network == 'macvlan' else '', 'ha_subnet': str(NETWORK) if network == 'macvlan' else '', 'image': IMAGE}
    before = {item['id'] for item in api('/api/agents')[1]}
    _, generated = api('/api/agents/docker-oxidns-ha-guide', body)
    private(ROOT / (network + '-bundle.json'), generated)
    assert {item['id'] for item in api('/api/agents')[1]} == before
    bundle = generated['compose_bundle']; archive = base64.b64decode(bundle['content_base64'])
    package.mkdir(mode=0o700)
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        assert all(not Path(item).is_absolute() and '..' not in Path(item).parts for item in z.namelist())
        credentials = json.loads((ROOT / 'credentials.json').read_text())
        package_text = '\n'.join(z.read(item).decode() for item in z.namelist())
        assert all(credentials[key] not in package_text for key in ('password', 'admin_token', 'registration_key'))
        z.extractall(package)
    prepared = local('bash', str(package / 'prepare.sh'))
    private(ROOT / (network + '-prepare-output.json'), {'exit': prepared.returncode, 'stdout': prepared.stdout.decode(), 'stderr': prepared.stderr.decode()})
    assert {item['id'] for item in api('/api/agents')[1]} == before
    compose(package, 'up', '-d')
    core = name + '-oxidns'; initial, env = online(core, network)
    assert {item['id'] for item in api('/api/agents')[1]} - before == {initial['config']['agent_id']}
    identity_mount = next(item for item in initial['mounts'] if item['Destination'] == '/run/configflow-host-machine-id')
    assert identity_mount['RW'] is False
    bootstrap_mount = next(item for item in initial['mounts'] if item['Destination'] == '/run/configflow-compose')
    assert bootstrap_mount['RW'] is False
    config_fixture(core); dns(body['agent_ip'])
    # Explicitly expire this private fixture's persisted enrollment session; never
    # modify the application's global clock or a production installation record.
    enrollment = json.loads((package / 'enrollment.json').read_text())
    def expire(settings):
        session_id = settings['url'].rsplit('/', 1)[-1]
        assert len(session_id) == 32 and all(c in '0123456789abcdef' for c in session_id)
        path = ROOT / 'data/docker-ha-installations' / (session_id + '.json')
        value = json.loads(path.read_text()); value['expires'] = 0
        private(path, value)
        code, _ = request(settings['url'].removeprefix(URL), {}, settings['token'], allow_error=True)
        assert code == 401
        return code
    expiry_code = expire(enrollment)
    event_file = ROOT / 'enrollment-events.jsonl'
    events_before = len(event_file.read_text().splitlines())
    compose(package, 'restart')
    restarted, _ = online(core, network); dns(body['agent_ip'])
    assert restarted['config']['agent_id'] == initial['config']['agent_id'] and restarted['config']['token'] == initial['config']['token']
    assert len(event_file.read_text().splitlines()) == events_before
    # Regenerate identical settings, run prepare again, and verify that an atomic
    # replacement is visible through the live container's read-only directory bind.
    _, renewed = api('/api/agents/docker-oxidns-ha-guide', body)
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(renewed['compose_bundle']['content_base64']))) as z:
        z.extractall(package)
    local('bash', str(package / 'prepare.sh'))
    settings = json.loads((package / 'enrollment.json').read_text())
    assert settings['url'] == enrollment['url'] and settings['token'] != enrollment['token']
    mounted = json.loads(docker('exec', core, 'cat', '/run/configflow-compose/enrollment.json'))
    assert mounted == settings
    expire(settings)
    run(['sudo', '-n', 'systemctl', 'restart', UNIT]); login()
    events_before = len(event_file.read_text().splitlines())
    compose(package, 'up', '-d', '--force-recreate')
    final, final_env = online(core, network); dns(body['agent_ip'])
    assert final['id'] != initial['id']
    assert final['config']['agent_id'] == initial['config']['agent_id'] and final['config']['token'] == initial['config']['token']
    assert len(event_file.read_text().splitlines()) == events_before
    report_path = ROOT / 'report.json'
    report = json.loads(report_path.read_text()) if report_path.exists() else {'scenarios': [], 'provenance': json.loads((ROOT / 'provenance.json').read_text())}
    report['scenarios'].append({'mode': network, 'generation_and_prepare_register_nothing': True,
        'registered_online': True, 'ha_environment_ready': env['ready'] and final_env['ready'],
        'read_only_host_identity': True, 'read_only_bootstrap_directory': True,
        'expired_installation_http': expiry_code, 'ordinary_restart_keeps_identity': True,
        'renewed_settings_visible_through_read_only_directory': True, 'force_recreated_container': True,
        'agent_identity_and_token_preserved': True, 'additional_enrollment_requests': 0,
        'dns_fixture_answer_before_and_after': '198.51.100.27', 'real_nas_test': False})
    private(report_path, report)
    compose(package, 'down')
    print(json.dumps(report['scenarios'][-1]), flush=True)


def export_evidence():
    guard(); report = json.loads((ROOT / 'report.json').read_text())
    credentials = json.loads((ROOT / 'credentials.json').read_text())
    for mode in ('host', 'macvlan'):
        bundle = json.loads((ROOT / (mode + '-bundle.json')).read_text())['compose_bundle']
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(bundle['content_base64']))) as z:
            text = '\n'.join(z.read(name).decode() for name in z.namelist())
            assert all(credentials[key] not in text for key in ('password', 'admin_token', 'registration_key'))
    report['administrator_or_global_registration_secrets_in_packages'] = False
    report['scope'] = 'Actual Linux Docker Compose containers in dedicated daemon and PID/mount/network namespaces; no physical NAS or HA failover test'
    report['expiry_fixture'] = 'Only temporary backend installation sessions were assigned expires=0; actual enroll returned 401. No wall-clock 24-hour wait.'
    report['dns_fixture'] = 'After fresh registration, isolated black_hole config answers compose-lab.invalid with 198.51.100.27 without WAN DNS.'
    names = ['backend/agents/docker_ha_compose.py', 'backend/agents/docker_ha_guide.py',
             'backend/agents/docker_ha_enrollment.py', 'docker/Dockerfile.agent', 'docker/docker-agent-entrypoint-service.sh']
    report['source_sha256'] = {name: hashlib.sha256((REPO / name).read_bytes()).hexdigest() for name in names}
    private(ROOT / 'public-report.json', report)


def cleanup():
    guard(); before = state()
    for name in ('host', 'macvlan'):
        package = ROOT / ('volume-host' if name == 'host' else 'package-macvlan')
        if (package / 'compose.yaml').exists():
            try: compose(package, 'down')
            except Exception: pass
    for unit in (UNIT, RUNTIME, before['unit']): run(['sudo', '-n', 'systemctl', 'stop', unit], check=False)
    assert not run(['sudo', '-n', 'ip', 'netns', 'pids', NS]).stdout.strip()
    cgroup = Path('/sys/fs/cgroup/cf-compose-lab')
    if cgroup.exists(): run(['sudo', '-n', 'rmdir', str(cgroup)])
    assert not cgroup.exists()
    run(['sudo', '-n', 'ip', 'netns', 'delete', NS])
    run(['sudo', '-n', 'ip', 'link', 'delete', BRIDGE])
    after = {name: run(['systemctl', 'is-active', name], check=False).stdout.decode().strip()
             for name in ('docker', 'docker.socket', 'containerd')}
    assert all(after[name] == before[name] for name in after)
    listeners = {p: sorted(run(['ss', flags, 'sport = :53']).stdout.decode().splitlines()) for p, flags in [('udp', '-Hlun'), ('tcp', '-Hltn')]}
    assert listeners == before['dns']
    assert NS not in run(['sudo', '-n', 'ip', 'netns', 'list']).stdout.decode()
    assert run(['ip', 'link', 'show', VETH], check=False).returncode
    previews = {}
    for port in (15800, 15810):
        with HTTP.open(f'http://192.168.0.32:{port}/', timeout=10) as response: previews[str(port)] = response.status
    report = {'default_services_unchanged': True, 'host_dns_unchanged': True, 'namespace_and_links_removed': True,
              'dedicated_cgroup_removed': True, 'preview_http': previews,
              'temporary_units_stopped': True, 'pve_modified': False, 'preview_modified': False}
    assert ROOT.is_dir() and not ROOT.is_symlink()
    run(['sudo', '-n', 'rm', '-rf', '--', str(ROOT)])
    assert not ROOT.exists(); report['private_data_credentials_image_cache_removed'] = True
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare-builder', 'build-image', 'prepare-runtime', 'server', 'test-host', 'test-macvlan', 'export-evidence', 'cleanup'])
    command = parser.parse_args().command
    {'prepare-builder': prepare_builder, 'build-image': build_image, 'prepare-runtime': prepare_runtime, 'server': server, 'test-host': lambda: test_mode('host'),
     'test-macvlan': lambda: test_mode('macvlan'), 'export-evidence': export_evidence, 'cleanup': cleanup}[command]()
