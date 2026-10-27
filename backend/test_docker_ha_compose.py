"""First Compose startup enrolls in the final container; restarts retain identity."""
import base64
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import zipfile

import pytest
import yaml

from backend.agents.docker_ha_guide import generate_guide
from backend.agents.docker_ha_compose import BOOTSTRAP, LABEL
from backend.test_agent_upgrade_auth import authenticated_app  # noqa: F401


TOKEN = 't' * 43
IDENTITY = {'success': True, 'agent_id': 'agent_' + 'a' * 32, 'token': 'z' * 32}


def params(tmp_path, **changes):
    identity = tmp_path / 'host-id'
    if not identity.exists():
        identity.write_text('unique-compose-host-identity-1234567890\n')
    value = {'install_method': 'compose', 'mode': 'new', 'server_url': 'http://panel.example',
             'agent_name': 'compose-dns', 'agent_ip': '192.168.10.20', 'oxidns_port': 8082,
             'data_dir': str(tmp_path / 'data'), 'network_mode': 'host', 'ha_host_id_path': str(identity),
             'image': 'local.test/agent:compose'}
    value.update(changes)
    return value


def capability(url='http://panel.example'):
    return {'url': url + '/api/agents/docker-oxidns-ha-enroll/' + 'a' * 32, 'token': TOKEN}


def unpack(result, destination):
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(result['compose_bundle']['content_base64']))) as archive:
        for info in archive.infolist():
            assert Path(info.filename).name == info.filename
            path = destination / info.filename
            path.write_bytes(archive.read(info))
            path.chmod((info.external_attr >> 16) & 0o777)
    return destination


@pytest.mark.parametrize('mode', ['host', 'macvlan'])
def test_compose_bundle_topology_permissions_and_literal_values(tmp_path, mode):
    body = params(tmp_path, network_mode=mode, ha_parent='eth0', ha_subnet='192.168.10.0/24',
                  ha_gateway='192.168.10.1', data_dir=str(tmp_path/'data-$literal'), server_url='http://panel.example/$route')
    result = generate_guide(body, capability())
    bundle = result['compose_bundle']
    archive = zipfile.ZipFile(io.BytesIO(base64.b64decode(bundle['content_base64'])))
    assert set(archive.namelist()) == {'compose.yaml', 'prepare.sh', 'bootstrap.py', 'enrollment.json', 'README.txt'}
    assert all((entry.external_attr >> 16) & 0o777 == (0o700 if entry.filename == 'prepare.sh' else 0o600)
               for entry in archive.infolist())
    assert archive.read('compose.yaml').decode() == result['compose']
    assert '$$literal' in result['compose'] and '$$route' in result['compose']
    for filename in ('compose.yaml', 'prepare.sh', 'bootstrap.py', 'README.txt'):
        assert TOKEN not in archive.read(filename).decode()
    compose = yaml.safe_load(result['compose'])
    assert compose['name'] == bundle['project_name']
    assert bundle['prepare_command'] == 'sudo bash prepare.sh'
    assert bundle['start_command'] == 'docker compose up -d'
    main = compose['services']['agent']
    assert main['entrypoint'] == ['python3', '/run/configflow-compose/bootstrap.py'] and main['command'] == []
    boot_mount = next(m for m in main['volumes'] if m['target'] == '/run/configflow-compose')
    assert boot_mount['read_only'] is True and boot_mount['bind']['create_host_path'] is False
    assert not any(m['target'].endswith('enrollment.json') for m in main['volumes'])
    assert all('token' not in e.lower() for e in main['environment'])
    assert main['image'] == body['image']
    assert 'docker run --rm' not in archive.read('prepare.sh').decode()
    if mode == 'host':
        assert set(compose['services']) == {'agent', 'ha-companion'}
        assert main['network_mode'] == 'host' and 'cap_add' not in main
        assert compose['services']['ha-companion']['cap_add'] == ['NET_ADMIN', 'NET_RAW']
    else:
        assert set(compose['services']) == {'agent'}
        assert main['networks']['dns-ha']['ipv4_address'] == body['agent_ip']
        assert compose['networks']['dns-ha']['name'] == bundle['project_name'] + '-dns-ha'
        assert main['cap_add'] == ['NET_ADMIN', 'NET_RAW']
    assert 'Compose 仅供查看' not in '\n'.join(result['warnings'])
    assert 'docker run' in result['install_command']  # Existing optional script stays available.


def test_existing_script_api_stays_default_and_migration_compose_is_rejected_before_issue(authenticated_app, tmp_path, monkeypatch):
    body = params(tmp_path)
    assert 'compose_bundle' not in generate_guide({**body, 'install_method': 'script'})
    body.pop('install_method')
    assert 'compose_bundle' not in generate_guide(body)
    client, _, _, admin, _ = authenticated_app
    monkeypatch.setattr('backend.agents.docker_ha_enrollment.issue', lambda *a: pytest.fail('invalid request must not issue capability'))
    for changes in ({'install_method': 'other'}, {'install_method': 'compose', 'mode': 'migrate'}):
        response = client.post('/api/agents/docker-oxidns-ha-guide', json={**body, **changes}, headers=admin)
        assert response.status_code == 400


def test_authenticated_package_generation_with_registration_locked_does_not_register(authenticated_app, tmp_path, monkeypatch):
    client, manager, _, admin, _ = authenticated_app
    monkeypatch.setenv('AGENT_REGISTRATION_KEY', 'global-key-not-in-bundle')
    body = params(tmp_path)
    before = manager.get_all_agents()
    response = client.post('/api/agents/docker-oxidns-ha-guide', json=body, headers=admin)
    assert response.status_code == 200, response.get_json()
    result = response.get_json()
    files = unpack(result, tmp_path/'bundle')
    settings = json.loads((files/'enrollment.json').read_text())
    assert len(settings['token']) == 43
    assert 'global-key-not-in-bundle' not in response.text
    assert manager.get_all_agents() == before
    path = settings['url'].split('/api', 1)[1]
    reply = client.post('/api'+path, json={}, headers={'Authorization': 'Bearer '+settings['token']})
    assert reply.status_code == 200
    assert manager.get_agent_by_id(reply.get_json()['agent_id'])['token'] == reply.get_json()['token']


@contextmanager
def origin():
    state = {'status': 200, 'identity': IDENTITY.copy(), 'calls': []}
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            state['calls'].append((self.path, self.headers.get('Authorization'), self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(state['status'])
            if state.get('location'):
                self.send_header('Location', state['location'])
            self.end_headers()
            self.wfile.write(json.dumps(state['identity']).encode())
        def do_GET(self):
            state['calls'].append(('GET ' + self.path, self.headers.get('Authorization'), b''))
            self.send_response(200); self.end_headers()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', state
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def bootstrap_files(tmp_path, cap):
    files = unpack(generate_guide(params(tmp_path), cap), tmp_path/'bundle')
    data = tmp_path/'runtime-agent'; data.mkdir()
    setup = tmp_path/'setup'
    setup.write_text(json.loads((files/'enrollment.json').read_text())['owner'])
    return files, data, setup


def execute_bootstrap(files, data, setup):
    runner = "import runpy,sys; m=runpy.run_path(sys.argv[1]); m['bootstrap'](*sys.argv[2:]); print('ENTRYPOINT_ALLOWED')"
    return subprocess.run([sys.executable, '-c', runner, str(files/'bootstrap.py'), str(files/'enrollment.json'), str(data), str(setup)],
                          text=True, capture_output=True, timeout=10,
                          env={**os.environ, 'HTTP_PROXY': 'http://127.0.0.1:1', 'HTTPS_PROXY': 'http://127.0.0.1:1'})


def test_actual_bootstrap_http_first_enrollment_then_expired_offline_restart(tmp_path):
    with origin() as (url, state):
        files, data, setup = bootstrap_files(tmp_path, capability(url))
        first = execute_bootstrap(files, data, setup)
        assert first.returncode == 0, first.stderr
        assert 'ENTRYPOINT_ALLOWED' in first.stdout and TOKEN not in first.stdout + first.stderr
        assert state['calls'] == [('/api/agents/docker-oxidns-ha-enroll/' + 'a'*32, 'Bearer '+TOKEN, b'{}')]
        config = json.loads((data/'config-oxidns.json').read_text())
        assert config == {k: IDENTITY[k] for k in ('agent_id', 'token')}
        assert (data/'config-oxidns.json').stat().st_mode & 0o777 == 0o600
        # Original entrypoint and later publishes may add/update non-identity config.
        config.update(service_type='oxidns', server_url='http://panel.example', config_version='42')
        (data/'config-oxidns.json').write_text(json.dumps(config))
        state['status'] = 401
        settings = json.loads((files/'enrollment.json').read_text()); settings['token'] = 'expired'
        (files/'enrollment.json').write_text(json.dumps(settings))
        second = execute_bootstrap(files, data, setup)
        assert second.returncode == 0 and len(state['calls']) == 1
    # The registration server is gone entirely; saved identity still permits startup.
    assert execute_bootstrap(files, data, setup).returncode == 0
    assert json.loads((data/'config-oxidns.json').read_text()) == config
    assert "os.execv('/entrypoint.sh', ['/entrypoint.sh', '/usr/bin/supervisord', '-c', '/etc/supervisor/supervisord.conf'])" in BOOTSTRAP


def test_actual_bootstrap_expired_first_attempt_stays_stopped_and_can_retry(tmp_path):
    with origin() as (url, state):
        files, data, setup = bootstrap_files(tmp_path, capability(url))
        state['status'] = 401
        failed = execute_bootstrap(files, data, setup)
        assert failed.returncode != 0 and 'ENTRYPOINT_ALLOWED' not in failed.stdout
        assert not (data/'config-oxidns.json').exists()
        state['status'] = 200
        success = execute_bootstrap(files, data, setup)
        assert success.returncode == 0 and len(state['calls']) == 2


@pytest.mark.parametrize('foreign', ['identity', 'marker', 'symlink-identity', 'symlink-marker', 'corrupt-identity', 'changed-identity', 'wrong-setup'])
def test_bootstrap_refuses_foreign_or_corrupt_state_without_http_or_overwrite(tmp_path, foreign):
    with origin() as (url, state):
        files, data, setup = bootstrap_files(tmp_path, capability(url))
        path, marker = data/'config-oxidns.json', data/'.docker-ha-installation.json'
        if foreign == 'changed-identity':
            assert execute_bootstrap(files, data, setup).returncode == 0
            value = json.loads(path.read_text()); value['token'] = 'another-token-value-1234567890'
            path.write_text(json.dumps(value))
        elif foreign == 'wrong-setup':
            setup.write_text('b'*64)
        elif foreign == 'marker':
            marker.write_text(json.dumps({'url': capability(url)['url'], 'owner': 'b'*64}))
        elif foreign.startswith('symlink'):
            target = tmp_path/'outside'; target.write_text('do-not-touch')
            (path if foreign.endswith('identity') else marker).symlink_to(target)
        else:
            path.write_text('broken' if foreign == 'corrupt-identity' else json.dumps(IDENTITY))
        before = {p.name: p.read_bytes() for p in data.iterdir() if p.is_file()}
        calls = len(state['calls'])
        failed = execute_bootstrap(files, data, setup)
        assert failed.returncode != 0 and 'ENTRYPOINT_ALLOWED' not in failed.stdout
        assert len(state['calls']) == calls
        assert {p.name: p.read_bytes() for p in data.iterdir() if p.is_file() and p.name != '.compose-enrollment.lock'} == {
            k: v for k,v in before.items() if k != '.compose-enrollment.lock'}


FAKE_DOCKER = r'''#!/usr/bin/env python3
import json,os,re,sys
from pathlib import Path
args=sys.argv[1:]; state=json.loads(Path(os.environ['CF_COMPOSE_STATE']).read_text())
with open(os.environ['CF_COMPOSE_EVENTS'],'a') as stream: stream.write(json.dumps(args)+'\n')
if args[:2]==['context','show']: print('default')
elif args[:2]==['context','inspect']: print(state.get('endpoint','unix:///var/run/docker.sock'))
elif args[0]=='info':
    fmt=args[-1]; print('linux' if '.OSType' in fmt else '[]' if '.SecurityOptions' in fmt else 'Arch Linux 6.18')
elif args[:2]==['compose','version']: print(state.get('version','2.20.1'))
elif args[:2]==['container','inspect']:
    item=state.get('containers',{}).get(args[-1]);
    if item is None: sys.exit(1)
    fmt=args[args.index('--format')+1] if '--format' in args else ''
    if '.Labels' in fmt: print(item['labels'].get(re.search(r'Labels "([^"]+)"',fmt)[1],''))
    elif '.Mounts' in fmt:
        target=re.search(r'\.Destination "([^"]+)"',fmt)[1]
        mount=next((m for m in item['volumes'] if m['target']==target),None)
        print('bind|'+mount['source']+'|'+str(not mount.get('read_only',False)).lower() if mount else '')
    else: print('{}')
elif args[:2]==['network','inspect']:
    item=state.get('networks',{}).get(args[-1]);
    if item is None: sys.exit(1)
    fmt=args[args.index('--format')+1] if '--format' in args else ''
    print(item['driver'] if '.Driver' in fmt else item['driver_opts']['parent'] if '.Options' in fmt else
          item['labels'].get(re.search(r'Labels "([^"]+)"',fmt)[1],'') if '.Labels' in fmt else
          '1' if 'len .IPAM' in fmt else item['ipam']['config'][0]['subnet'] if '.Subnet' in fmt else
          item['ipam']['config'][0].get('gateway','') if '.Gateway' in fmt else '{}')
else: sys.exit('unexpected mutating Docker operation '+repr(args))
'''


def preparation(tmp_path, body, cap=None, in_data=False):
    guide = generate_guide(body, cap or capability())
    files = unpack(guide, Path(body['data_dir']) if in_data else tmp_path/'bundle')
    binary = tmp_path/'bin'; binary.mkdir(exist_ok=True)
    docker = binary/'docker'; docker.write_text(FAKE_DOCKER); docker.chmod(0o755)
    state = tmp_path/'docker-state.json'; state.write_text('{}')
    events = tmp_path/'docker-events'
    env = {**os.environ, 'PATH': str(binary)+':'+os.environ['PATH'], 'CF_COMPOSE_STATE': str(state),
           'CF_COMPOSE_EVENTS': str(events), 'DOCKER_CONTEXT': '', 'DOCKER_HOST': ''}
    return files, state, events, env


def prepare(files, env):
    return subprocess.run(['bash', str(files/'prepare.sh')], text=True, capture_output=True, env=env, timeout=10)


@pytest.mark.parametrize('in_data', [False, True])
def test_prepare_initializes_persistent_mounts_and_identity_once_without_starting_docker(tmp_path, in_data):
    body = params(tmp_path, create_host_identity=True)
    Path(body['ha_host_id_path']).unlink()
    files, state, events, env = preparation(tmp_path, body, in_data=in_data)
    first = prepare(files, env)
    assert first.returncode == 0, first.stderr
    data = Path(body['data_dir'])
    identity = Path(body['ha_host_id_path']).read_bytes()
    assert len(identity.strip()) == 32
    assert all((data/name).is_dir() for name in ('agent', 'oxidns', 'ha', '.ha-compose'))
    assert (data/'.ha-compose/enrollment.json').stat().st_mode & 0o777 == 0o600
    assert prepare(files, env).returncode == 0
    assert Path(body['ha_host_id_path']).read_bytes() == identity
    assert not any(json.loads(line)[0] in ('run','start','stop','rm','pull') for line in events.read_text().splitlines())


@pytest.mark.parametrize('mode', ['host','macvlan'])
def test_prepare_reuses_only_owned_compose_project_and_mounts_and_renews_files(tmp_path, mode):
    body = params(tmp_path, network_mode=mode, ha_parent='eth0', ha_subnet='192.168.10.0/24')
    files, state, _, env = preparation(tmp_path, body)
    assert prepare(files, env).returncode == 0
    compose = yaml.safe_load((files/'compose.yaml').read_text())
    stored = {'containers': {}, 'networks': {}}
    for name, service in compose['services'].items():
        service['labels'].update({'com.docker.compose.project': compose['name'], 'com.docker.compose.service': name})
        stored['containers'][service['container_name']] = service
    for network in compose.get('networks', {}).values():
        network['labels']['com.docker.compose.project'] = compose['name']
        stored['networks'][network['name']] = network
    state.write_text(json.dumps(stored))
    assert prepare(files, env).returncode == 0
    updated = {**capability(), 'token': 'u'*43}
    unpack(generate_guide(body, updated), files)
    assert prepare(files, env).returncode == 0
    assert json.loads((Path(body['data_dir'])/'.ha-compose/enrollment.json').read_text())['token'] == 'u'*43
    first = next(iter(stored['containers'].values()))
    first['volumes'][0]['source'] = '/foreign/agent'
    state.write_text(json.dumps(stored))
    assert prepare(files, env).returncode != 0
    first['volumes'][0]['source'] = body['data_dir']+'/agent'
    first['labels']['com.docker.compose.project'] = 'foreign-project'
    state.write_text(json.dumps(stored))
    assert prepare(files, env).returncode != 0


@pytest.mark.parametrize('fault', ['old-data', 'extra-package-file', 'foreign-container', 'foreign-network', 'data-link', 'identity-link', 'old-compose', 'remote-context'])
def test_prepare_refuses_existing_data_unowned_resources_and_invalid_environment(tmp_path, fault):
    body = params(tmp_path, network_mode='macvlan', ha_parent='eth0', ha_subnet='192.168.10.0/24')
    files, state, _, env = preparation(tmp_path, body, in_data=fault=='extra-package-file')
    if fault in ('old-data','extra-package-file'):
        data = Path(body['data_dir']); data.mkdir(exist_ok=True); (data/'do-not-overwrite').write_text('original')
    elif fault == 'data-link':
        target=tmp_path/'target'; target.mkdir(); Path(body['data_dir']).symlink_to(target)
    elif fault == 'identity-link':
        path=Path(body['ha_host_id_path']); target=path.with_name('real-id'); path.rename(target); path.symlink_to(target)
    elif fault == 'foreign-container':
        state.write_text(json.dumps({'containers': {body['agent_name']+'-oxidns': {}}}))
    elif fault == 'foreign-network':
        compose=yaml.safe_load((files/'compose.yaml').read_text())
        state.write_text(json.dumps({'networks': {compose['networks']['dns-ha']['name']: {}}}))
    elif fault == 'old-compose':
        state.write_text(json.dumps({'version': '2.19.9'}))
    else:
        state.write_text(json.dumps({'endpoint': 'ssh://remote.example'}))
    result = prepare(files, env)
    assert result.returncode != 0
    assert not (Path(body['data_dir'])/'.ha-compose/setup').exists()


@pytest.mark.parametrize('filename', ['compose.yaml','prepare.sh','bootstrap.py','enrollment.json','README.txt','agent/config.json'])
def test_host_identity_cannot_alias_package_or_mutable_managed_file(tmp_path, filename):
    body = params(tmp_path)
    body['ha_host_id_path'] = body['data_dir'] + '/' + filename
    with pytest.raises(ValueError, match='身份文件'):
        generate_guide(body, capability())


def test_missing_identity_does_not_reserve_data_before_user_corrects_path(tmp_path):
    body = params(tmp_path)
    Path(body['ha_host_id_path']).unlink()
    files, _, _, env = preparation(tmp_path, body, in_data=True)
    result = prepare(files, env)
    assert result.returncode != 0
    data = Path(body['data_dir'])
    assert not (data/'.ha-compose').exists() and not (data/'agent').exists()
    corrected = tmp_path/'correct-host-id'; corrected.write_text('correct-host-identity-1234567890\n')
    body['ha_host_id_path'] = str(corrected)
    unpack(generate_guide(body, capability()), files)
    assert prepare(files, env).returncode == 0


def test_root_level_selected_identity_can_be_created_and_reused_inside_data_directory(tmp_path):
    body = params(tmp_path, create_host_identity=True)
    body['ha_host_id_path'] = body['data_dir']+'/host-id'
    files, _, _, env = preparation(tmp_path, body, in_data=True)
    assert prepare(files, env).returncode == 0
    original = Path(body['ha_host_id_path']).read_bytes()
    assert prepare(files, env).returncode == 0
    assert Path(body['ha_host_id_path']).read_bytes() == original


@pytest.mark.parametrize('status', [302, 307])
def test_bootstrap_never_redirects_scoped_bearer_capability(tmp_path, status):
    with origin() as (target_url, target), origin() as (source_url, source):
        source.update(status=status, location=capability(target_url)['url'])
        files, data, setup = bootstrap_files(tmp_path, capability(source_url))
        result = execute_bootstrap(files, data, setup)
        assert result.returncode != 0 and 'ENTRYPOINT_ALLOWED' not in result.stdout
        assert len(source['calls']) == 1 and target['calls'] == []
        assert TOKEN not in result.stdout + result.stderr
        assert not (data/'config-oxidns.json').exists()
