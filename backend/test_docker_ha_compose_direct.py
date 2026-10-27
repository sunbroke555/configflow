"""Single-file Compose initialization stays inside declared data and identity mounts."""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from backend.agents.docker_ha_guide import generate_guide
from backend.agents.docker_ha_compose_direct import IDENTITY_VOLUME, RUNTIME
from backend.test_agent_upgrade_auth import authenticated_app  # noqa: F401
from backend.test_docker_ha_compose import capability, origin, params, IDENTITY, TOKEN

RUNTIME_SOURCE = Path(__file__).parents[1] / 'docker/docker-compose-install.py'
ENV_KEY = 'CONFIGFLOW_COMPOSE_INSTALL'
ROOT_MARKER = '.configflow-compose-direct.json'


def generated(tmp_path, **changes):
    body = params(tmp_path, install_method='compose_direct', **changes)
    return body, generate_guide(body, capability())


def env_settings(service):
    return json.loads(service['environment'][ENV_KEY])


@pytest.mark.parametrize('network', ['host', 'macvlan'])
@pytest.mark.parametrize('volume', [False, True])
def test_direct_is_one_yaml_with_minimal_init_mounts_and_final_network_enrollment(tmp_path, network, volume):
    body, result = generated(tmp_path, network_mode=network, ha_parent='eth0', ha_subnet='192.168.10.0/24',
                             create_host_identity=volume, **({'ha_host_id_path': ''} if volume else {}))
    assert 'compose_bundle' not in result
    assert result['identity_command'] == '' and result['install_command'] == 'docker compose up -d'
    # Project-wide logs include install-init and HA companion when Agent never started.
    assert next(block['command'] for block in result['command_blocks'] if block['id'] == 'logs') == 'docker compose logs --tail 80\n'
    info = result['compose_deployment']
    assert info['filename'] == 'compose.yaml' and info['identity_mode'] == ('docker-volume' if volume else 'file')
    compose = yaml.safe_load(result['compose'])
    assert compose['name'] == info['project_name']
    init, main = compose['services']['install-init'], compose['services']['agent']
    assert init['network_mode'] == 'none' and init['read_only'] is True
    assert init['cap_drop'] == ['ALL'] and set(init['cap_add']) == {'DAC_OVERRIDE','FOWNER'}
    assert init['entrypoint'] == ['python3', RUNTIME, 'prepare']
    assert main['entrypoint'] == ['python3', RUNTIME, 'run']
    assert 'token' not in env_settings(init) and env_settings(main)['token'] == TOKEN
    assert main['depends_on']['install-init']['condition'] == 'service_completed_successfully'
    assert len(init['volumes']) == 2
    assert init['volumes'][0] == {'type':'bind','source':body['data_dir'],'target':'/configflow-data',
                                  'read_only':False,'bind':{'create_host_path':True}}
    assert not any(key in compose for key in ('configs','secrets'))
    for name, service in compose['services'].items():
        assert 'env_file' not in service and 'privileged' not in service and service.get('pid') != 'host'
        assert not any('docker.sock' in str(m) for m in service['volumes'])
        for mount in service['volumes']:
            assert 'subpath' not in mount.get('volume', {})
            assert not mount.get('source','').endswith(('bootstrap.py','enrollment.json','prepare.sh'))
        if name != 'agent':
            assert TOKEN not in json.dumps(service)
    persistent = [m for m in main['volumes'] if m['target'] in ('/opt/configflow-agent','/etc/oxidns','/var/lib/configflow-ha')]
    assert len(persistent) == 3 and all(m['bind']['create_host_path'] for m in persistent)
    boot = next(m for m in main['volumes'] if m['target'] == '/run/configflow-host-boot-id')
    assert boot['source'] == '/proc/sys/kernel/random/boot_id' and boot['read_only'] is True
    identity = init['volumes'][1]
    if volume:
        assert compose['volumes']['host-identity'] == {'name': IDENTITY_VOLUME, 'driver':'local'}
        assert identity['type'] == 'volume' and identity['read_only'] is False and identity['volume']['nocopy'] is True
        assert main['environment']['CONFIGFLOW_DNS_HA_IDENTITY'] == 'volume'
        assert next(m for m in main['volumes'] if m['target']=='/run/configflow-host-identity')['read_only'] is True
    else:
        assert identity['source'] == body['ha_host_id_path'] and identity['read_only'] is True
        assert identity['bind']['create_host_path'] is False
    if network == 'host':
        helper = compose['services']['ha-companion']
        assert helper['depends_on']['install-init']['condition'] == 'service_completed_successfully'
        assert main['network_mode'] == helper['network_mode'] == 'host'
        assert 'cap_add' not in main and set(helper['cap_add']) == {'NET_ADMIN','NET_RAW'}
        assert main['depends_on']['ha-companion']['condition'] == 'service_started'
    else:
        assert set(compose['services']) == {'agent', 'install-init'}
        assert main['networks']['dns-ha']['ipv4_address'] == body['agent_ip']
        assert 'networks' not in init  # No temporary container consumes the Macvlan IP.


def test_direct_compose_escapes_literal_dollars_and_does_not_change_legacy_modes(tmp_path):
    _, result = generated(tmp_path, data_dir=str(tmp_path/'$data'), server_url='http://panel.example/$route')
    assert '$$data' in result['compose'] and '$$route' in result['compose']
    legacy = generate_guide(params(tmp_path, install_method='script'), capability())
    assert 'compose_deployment' not in legacy and 'compose_bundle' not in legacy
    zipped = generate_guide(params(tmp_path, install_method='compose'), capability())
    assert 'compose_bundle' in zipped and 'compose_deployment' not in zipped


@pytest.mark.parametrize('changes', [
    {'mode':'migrate'}, {'data_dir':'/etc'}, {'data_dir':'/etc/configflow'}, {'data_dir':'/volume1'},
    {'data_dir':'/volume1/docker'}, {'data_dir':'/opt'}, {'ha_host_id_path':''},
])
def test_invalid_direct_request_is_rejected_before_capability_issue(authenticated_app, tmp_path, monkeypatch, changes):
    client, _, _, admin, _ = authenticated_app
    monkeypatch.setattr('backend.agents.docker_ha_enrollment.issue', lambda *a: pytest.fail('invalid request cannot issue capability'))
    body = params(tmp_path, install_method='compose_direct', **changes)
    response = client.post('/api/agents/docker-oxidns-ha-guide', json=body, headers=admin)
    assert response.status_code == 400


def test_direct_authenticated_generation_supports_locked_registration_without_creating_agent(authenticated_app, tmp_path, monkeypatch):
    client, manager, _, admin, _ = authenticated_app
    monkeypatch.setenv('AGENT_REGISTRATION_KEY', 'never-put-global-key-in-yaml')
    body = params(tmp_path, install_method='compose_direct', create_host_identity=True, ha_host_id_path='')
    before = manager.get_all_agents()
    response = client.post('/api/agents/docker-oxidns-ha-guide', json=body, headers=admin)
    assert response.status_code == 200, response.get_json()
    settings = env_settings(yaml.safe_load(response.get_json()['compose'])['services']['agent'])
    assert 'never-put-global-key-in-yaml' not in response.text
    assert admin['Authorization'].split()[1] not in response.text
    assert manager.get_all_agents() == before
    reply = client.post('/api'+settings['url'].split('/api',1)[1], json={},
                        headers={'Authorization':'Bearer '+settings['token']})
    assert reply.status_code == 200
    identity = reply.get_json()
    heartbeat = client.post('/api/agents/'+identity['agent_id']+'/heartbeat', json={'version':'test'},
                            headers={'Authorization':'Bearer '+identity['token']})
    assert heartbeat.status_code == 200


RUNNER = '''import json,os,runpy,sys
from pathlib import Path
runtime=runpy.run_path(sys.argv[1])
settings=runtime['settings_from_environment']()
if sys.argv[2]=='prepare':
    runtime['prepare'](settings,Path(sys.argv[3]),Path(sys.argv[4]),Path(sys.argv[5]))
else:
    runtime['run'](settings,Path(sys.argv[3])/'agent',Path(sys.argv[4]),Path(sys.argv[5]))
    print('ENTRYPOINT_ALLOWED')
'''


def invoke(action, settings, root, identity_dir, identity_file):
    return subprocess.run([sys.executable, '-c', RUNNER, str(RUNTIME_SOURCE), action,
                           str(root), str(identity_dir), str(identity_file)],
                          text=True, capture_output=True, timeout=10,
                          env={**os.environ, ENV_KEY:json.dumps(settings),
                               'HTTP_PROXY':'http://127.0.0.1:1', 'HTTPS_PROXY':'http://127.0.0.1:1'})


def runtime_fixture(tmp_path, url='http://panel.example', volume=False, prefix='data'):
    body = params(tmp_path, install_method='compose_direct', data_dir=str(tmp_path/prefix),
                  create_host_identity=volume, **({'ha_host_id_path':''} if volume else {}))
    result = generate_guide(body, capability(url))
    settings = env_settings(yaml.safe_load(result['compose'])['services']['agent'])
    root = Path(body['data_dir']); root.mkdir(exist_ok=True)
    for name in ('agent','oxidns','ha'):
        (root/name).mkdir(exist_ok=True)  # Docker container-create stage.
    (root/'compose.yaml').write_text(result['compose'])
    identity_dir = tmp_path/'global-identity-volume'; identity_dir.mkdir(exist_ok=True)
    return settings, root, identity_dir, tmp_path/'host-id'


@pytest.mark.parametrize('volume', [False, True])
def test_actual_init_then_registration_retry_and_expired_offline_recreation(tmp_path, volume):
    with origin() as (url, state):
        settings, root, identity_dir, identity_file = runtime_fixture(tmp_path, url, volume)
        prepared = invoke('prepare', {k:v for k,v in settings.items() if k!='token'}, root, identity_dir, identity_file)
        assert prepared.returncode == 0, prepared.stderr
        assert state['calls'] == []
        state['status'] = 401
        failed = invoke('run', settings, root, identity_dir, identity_file)
        assert failed.returncode != 0 and 'ENTRYPOINT_ALLOWED' not in failed.stdout
        assert not (root/'agent/config-oxidns.json').exists()
        state['status'] = 200
        success = invoke('run', settings, root, identity_dir, identity_file)
        assert success.returncode == 0, success.stderr
        saved = (root/'agent/config-oxidns.json').read_bytes()
        assert json.loads(saved) == {k:IDENTITY[k] for k in ('agent_id','token')}
        assert (root/'agent/config-oxidns.json').stat().st_mode & 0o777 == 0o600
        (root/'oxidns/rules.txt').write_text('published-rules')
        assert invoke('prepare', settings, root, identity_dir, identity_file).returncode == 0
        assert (root/'oxidns/rules.txt').read_text() == 'published-rules'
        settings['token']='expired'
        state['status']=401
        before=len(state['calls'])
        assert invoke('run', settings, root, identity_dir, identity_file).returncode == 0
        assert len(state['calls']) == before
    assert invoke('run', settings, root, identity_dir, identity_file).returncode == 0
    assert (root/'agent/config-oxidns.json').read_bytes() == saved


def test_two_project_initializers_concurrently_share_one_docker_host_identity(tmp_path):
    a = runtime_fixture(tmp_path, volume=True, prefix='project-a')
    b = runtime_fixture(tmp_path, volume=True, prefix='project-b')
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda args: invoke('prepare', *args), (a,b)))
    assert all(result.returncode == 0 for result in results), [r.stderr for r in results]
    identity = (a[2]/'host-id').read_bytes()
    assert len(identity.strip()) == 32 and (a[2]/'host-id').stat().st_mode & 0o777 == 0o600
    records = [json.loads((entry[1]/ROOT_MARKER).read_text()) for entry in (a,b)]
    assert records[0]['owner'] != records[1]['owner'] and records[0]['host_id'] == records[1]['host_id']
    assert invoke('prepare', *a).returncode == 0 and (a[2]/'host-id').read_bytes() == identity


@pytest.mark.parametrize('fault', ['old-agent','old-core','old-ha','unknown-root','legacy-marker','link-agent','link-root-marker','link-identity','invalid-identity','missing-file'])
def test_init_never_adopts_old_data_or_unsafe_identity(tmp_path, fault):
    args = runtime_fixture(tmp_path, volume=fault != 'missing-file')
    settings, root, identity_dir, identity_file = args
    if fault.startswith('old-'):
        directory = {'old-agent':'agent','old-core':'oxidns','old-ha':'ha'}[fault]
        (root/directory/'original').write_text('must-survive')
    elif fault in ('unknown-root','legacy-marker'):
        (root/('unexpected' if fault=='unknown-root' else '.ha-compose')).mkdir()
    elif fault == 'link-agent':
        (root/'agent').rmdir(); (root/'agent').symlink_to(identity_dir, target_is_directory=True)
    elif fault == 'link-root-marker':
        (root/ROOT_MARKER).symlink_to(tmp_path/'absent')
    elif fault == 'link-identity':
        (identity_dir/'host-id').symlink_to(identity_file)
    elif fault == 'invalid-identity':
        (identity_dir/'host-id').write_text('bad')
    else:
        identity_file.unlink()
    result = invoke('prepare', *args)
    assert result.returncode != 0
    assert not (root/'agent'/ROOT_MARKER).exists()
    if fault.startswith('old-'):
        assert (root/directory/'original').read_text() == 'must-survive'
        assert not (identity_dir/'host-id').exists()
    if fault == 'invalid-identity':
        assert (identity_dir/'host-id').read_text() == 'bad'


def test_missing_identity_failure_does_not_claim_data_and_corrected_file_can_retry(tmp_path):
    settings, root, identity_dir, identity_file = runtime_fixture(tmp_path)
    identity_file.unlink()
    assert invoke('prepare', settings, root, identity_dir, identity_file).returncode != 0
    assert not (root/ROOT_MARKER).exists()
    identity_file.write_text('corrected-stable-host-identity-value\n')
    assert invoke('prepare', settings, root, identity_dir, identity_file).returncode == 0


@pytest.mark.parametrize('fault', ['foreign-owner','foreign-marker','changed-token','symlink-config'])
def test_main_refuses_foreign_identity_without_new_enrollment(tmp_path, fault):
    with origin() as (url, state):
        args = runtime_fixture(tmp_path, url)
        settings, root, _, _ = args
        assert invoke('prepare', *args).returncode == 0
        assert invoke('run', *args).returncode == 0
        path = root/'agent/config-oxidns.json'
        if fault == 'foreign-owner':
            settings['owner']='b'*64
        elif fault == 'foreign-marker':
            marker=root/'agent/.docker-ha-installation.json'
            value=json.loads(marker.read_text()); value['owner']='b'*64; marker.write_text(json.dumps(value))
        elif fault == 'changed-token':
            value=json.loads(path.read_text()); value['token']='changed-token-1234567890'; path.write_text(json.dumps(value))
        else:
            target=tmp_path/'original-identity'; path.rename(target); path.symlink_to(target)
        before=len(state['calls'])
        result=invoke('run', *args)
        assert result.returncode != 0 and 'ENTRYPOINT_ALLOWED' not in result.stdout
        assert len(state['calls']) == before


@pytest.mark.parametrize('status', [302,307])
def test_direct_runtime_never_forwards_registration_bearer_on_redirect(tmp_path, status):
    with origin() as (target_url,target), origin() as (source_url,source):
        source.update(status=status,location=capability(target_url)['url'])
        args=runtime_fixture(tmp_path,source_url)
        assert invoke('prepare',*args).returncode == 0
        result=invoke('run',*args)
        assert result.returncode != 0 and target['calls'] == []
        assert TOKEN not in result.stdout+result.stderr
        assert not (args[1]/'agent/config-oxidns.json').exists()


@pytest.mark.parametrize('stage', ['root-write', 'agent-write'])
def test_same_owner_partial_atomic_initialization_can_resume_without_deleting_residue(tmp_path, stage):
    import hashlib
    args=runtime_fixture(tmp_path, volume=False)
    settings,root,_,identity_file=args
    temporary = (root if stage=='root-write' else root/'agent') / ('.compose-write-'+settings['owner']+'-abcd1234')
    temporary.write_bytes(b'{"partial":')
    if stage=='agent-write':
        owner={k:settings[k] for k in ('protocol','owner','url','identity_mode')}
        (root/ROOT_MARKER).write_text(json.dumps({**owner, 'ready':False,
            'host_id':hashlib.sha256(identity_file.read_bytes().strip()).hexdigest()}))
    result=invoke('prepare',*args)
    assert result.returncode == 0, result.stderr
    assert temporary.read_bytes() == b'{"partial":'
    assert json.loads((root/ROOT_MARKER).read_text())['ready'] is True


def test_foreign_owner_temporary_does_not_bypass_old_data_rejection(tmp_path):
    args=runtime_fixture(tmp_path)
    (args[1]/('.compose-write-'+'b'*64+'-abcd1234')).write_text('unrelated')
    assert invoke('prepare',*args).returncode != 0
    assert not (args[1]/ROOT_MARKER).exists()


@pytest.mark.parametrize('change', ['replace','delete'])
def test_main_never_recreates_or_adopts_replaced_host_identity_volume(tmp_path, change):
    with origin() as (url,state):
        args=runtime_fixture(tmp_path,url,volume=True)
        assert invoke('prepare',*args).returncode == 0
        assert invoke('run',*args).returncode == 0
        identity=args[2]/'host-id'
        if change=='delete': identity.unlink()
        else: identity.write_text('another-docker-host-identity-value')
        before=len(state['calls'])
        result=invoke('run',*args)
        assert result.returncode != 0 and 'ENTRYPOINT_ALLOWED' not in result.stdout
        assert len(state['calls']) == before
        if change=='delete': assert not identity.exists()
        else: assert identity.read_text() == 'another-docker-host-identity-value'
