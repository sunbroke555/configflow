"""Compact installation: fewer services, persistent identity, no host preparation."""
import concurrent.futures
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest
import yaml

from backend.agents.docker_ha_guide import generate_guide
from backend.test_agent_upgrade_auth import authenticated_app  # noqa: F401
from backend.test_docker_ha_compose import params, capability, origin, TOKEN, IDENTITY

SOURCE = Path(__file__).parents[1] / 'docker/docker-compose-compact.py'
MARKER = '.configflow-compose-compact.json'
INSTALLATION_ID = '.configflow-ha-identity'


def generated(tmp_path, **changes):
    body = params(tmp_path, install_method='compose_compact', **changes)
    result = generate_guide(body, capability(body['server_url']))
    return body, result, yaml.safe_load(result['compose'])


@pytest.mark.parametrize('network', ['host', 'macvlan'])
def test_compact_keeps_ha_isolation_without_external_files_or_init_service(tmp_path, network):
    body, result, document = generated(tmp_path, network_mode=network, ha_parent='eth0', ha_subnet='192.168.10.0/24',
                                       ha_host_id_path='', create_host_identity=True)
    info = result['compose_deployment']
    assert info == {'filename':'compose.yaml', 'project_name':document['name'], 'command':'docker compose up -d',
                    'identity_mode':'installation', 'service_count':2 if network=='host' else 1}
    assert set(document['services']) == ({'agent','ha-companion'} if network=='host' else {'agent'})
    assert 'volumes' not in document and 'compose_bundle' not in result
    assert result['identity_command'] == ''
    main = document['services']['agent']
    assert main['environment'] == {'SERVICE_TYPE':'oxidns-ha-'+network, 'SERVER_URL':body['server_url'],
        'AGENT_NAME':body['agent_name']+'-oxidns', 'AGENT_IP':body['agent_ip'], 'AGENT_PORT':str(body['oxidns_port']),
        'INSTALL_KEY':'a'*32+'.'+TOKEN}
    assert main['volumes'][:3] == [body['data_dir']+'/agent:/opt/configflow-agent',
                                  body['data_dir']+'/oxidns:/etc/oxidns', body['data_dir']+'/ha:/var/lib/configflow-ha']
    for service in document['services'].values():
        assert not {'entrypoint','command','env_file','labels','privileged','pid','depends_on'} & service.keys()
        assert 'docker.sock' not in str(service)
        assert service['image'] == body['image']
    for mount in main['volumes'][3:]:
        assert mount['read_only'] is True and mount['bind'] == {'create_host_path':False}
    assert main['volumes'][3]['source'] == '/proc/sys/kernel/random/boot_id'
    if network == 'host':
        helper = document['services']['ha-companion']
        assert 'cap_add' not in main and set(helper['cap_add']) == {'NET_ADMIN','NET_RAW'}
        assert helper['environment'] == {'SERVICE_TYPE':'ha-companion-compact', 'INSTALL_SCOPE':'a'*32}
        assert helper['volumes'][:2] == [value+':ro' for value in main['volumes'][:2]]
        assert helper['volumes'][2] == main['volumes'][2]
        assert main['volumes'][4]['source'] == '/proc/1/ns/net'
        assert '&agent_image' in result['compose'] and '*agent_image' in result['compose']
        assert TOKEN not in json.dumps(helper)
    else:
        assert set(main['cap_add']) == {'NET_ADMIN','NET_RAW'}
        assert main['networks']['dns-ha']['ipv4_address'] == body['agent_ip']
        assert document['networks']['dns-ha']['driver'] == 'macvlan'
    assert 'CONFIGFLOW_COMPOSE_INSTALL' not in result['compose'] and 'install-init' not in result['compose']
    direct = generate_guide({**body, 'install_method':'compose_direct'}, capability())
    assert len(result['compose'].splitlines()) < len(direct['compose'].splitlines()) * .65


def test_image_upgrade_keeps_project_and_installer_request_scope(authenticated_app, tmp_path):
    client, manager, _, admin, _ = authenticated_app
    body = params(tmp_path, install_method='compose_compact')
    before = manager.get_all_agents()
    first = client.post('/api/agents/docker-oxidns-ha-guide', json=body, headers=admin).get_json()
    changed = {**body, 'image':'local.test/agent:new-version', 'ha_host_id_path':'/unused', 'create_host_identity':True}
    second = client.post('/api/agents/docker-oxidns-ha-guide', json=changed, headers=admin).get_json()
    assert first['success'] and second['success'] and manager.get_all_agents() == before
    a, b = [yaml.safe_load(item['compose']) for item in (first,second)]
    assert a['name'] == b['name']
    assert a['services']['agent']['environment']['INSTALL_KEY'] == b['services']['agent']['environment']['INSTALL_KEY']
    session, token = a['services']['agent']['environment']['INSTALL_KEY'].split('.')
    url = '/api/agents/docker-oxidns-ha-enroll/'+session
    one = client.post(url, json={}, headers={'Authorization':'Bearer '+token}).get_json()
    two = client.post(url, json={}, headers={'Authorization':'Bearer '+token}).get_json()
    assert one['agent_id'] == two['agent_id'] and one['token'] == two['token']
    assert len(manager.get_all_agents()) == len(before) + 1
    for change in ({'agent_name':'another'}, {'agent_ip':'192.168.10.21'}, {'data_dir':str(tmp_path/'new-data')}):
        response = client.post('/api/agents/docker-oxidns-ha-guide', json={**body, **change}, headers=admin).get_json()
        key = yaml.safe_load(response['compose'])['services']['agent']['environment']['INSTALL_KEY']
        assert key.split('.')[0] != session


@pytest.mark.parametrize('changes', [{'mode':'migrate'}, {'data_dir':'/etc'}, {'data_dir':'/volume1/docker'},
                                      {'network_mode':'bridge'}, {'data_dir':'/srv/../etc'}])
def test_compact_invalid_requests_are_rejected_before_capability_issue(authenticated_app, tmp_path, monkeypatch, changes):
    client, _, _, admin, _ = authenticated_app
    monkeypatch.setattr('backend.agents.docker_ha_enrollment.issue', lambda *a: pytest.fail('invalid request must not issue capability'))
    response = client.post('/api/agents/docker-oxidns-ha-guide', json=params(tmp_path, install_method='compose_compact', **changes), headers=admin)
    assert response.status_code == 400


def test_compact_literal_values_and_old_install_methods_are_preserved(tmp_path):
    body, result, document = generated(tmp_path, data_dir=str(tmp_path/'目录 $data'), server_url='http://panel.example/$path')
    assert '$$data' in result['compose'] and '$$path' in result['compose']
    assert '目录' in result['compose']
    for method in ('script','compose','compose_direct'):
        old = generate_guide({**body, 'install_method':method}, capability())
        assert 'INSTALL_KEY' not in old['compose']


RUNNER = '''import os,runpy,sys
from pathlib import Path
m=runpy.run_path(sys.argv[1]);s=m['settings_from_environment']()
m[sys.argv[2]](s,*[Path(v) for v in sys.argv[3:]])
print('ENTRYPOINT_ALLOWED')
'''


def fixture(tmp_path, url='http://panel.example'):
    body, _, document = generated(tmp_path, server_url=url)
    root = Path(body['data_dir']); root.mkdir()
    dirs = tuple(root/name for name in ('agent','oxidns','ha'))
    for directory in dirs: directory.mkdir()
    return document['services']['agent']['environment'], dirs


def invoke(env, dirs, action='run', prefix=''):
    return subprocess.run([sys.executable,'-c',prefix+RUNNER,str(SOURCE),action,*map(str,dirs)],
        env={**os.environ, **env, 'HTTP_PROXY':'http://127.0.0.1:1','HTTPS_PROXY':'http://127.0.0.1:1'},
        capture_output=True,text=True,timeout=10)


def test_first_registration_retry_then_expired_and_removed_key_recreation(tmp_path):
    with origin() as (url, state):
        env, dirs = fixture(tmp_path,url)
        state['status']=401
        failed=invoke(env,dirs)
        assert failed.returncode != 0 and not (dirs[0]/'config-oxidns.json').exists()
        installation=(dirs[0]/INSTALLATION_ID).read_bytes()
        assert re.fullmatch(rb'[0-9a-f]{32}\n',installation)
        records=[json.loads((directory/MARKER).read_text()) for directory in dirs]
        assert all(record['installation_id']==installation.strip().decode() for record in records)
        assert records[0]['ready'] is True
        state['status']=200
        success=invoke(env,dirs)
        assert success.returncode==0, success.stderr
        saved=(dirs[0]/'config-oxidns.json').read_bytes()
        assert json.loads(saved)=={key:IDENTITY[key] for key in ('agent_id','token')}
        assert (dirs[0]/'config-oxidns.json').stat().st_mode & 0o777 == 0o600
        assert (dirs[0]/INSTALLATION_ID).stat().st_mode & 0o777 == 0o600
        (dirs[1]/'rules.txt').write_text('preserved'); (dirs[2]/'journal.json').write_text('preserved')
        before=len(state['calls']);state['status']=401
        assert invoke(env,dirs).returncode==0
        env['INSTALL_KEY']=''
        assert invoke(env,dirs).returncode==0
        assert len(state['calls'])==before
    assert invoke(env,dirs).returncode==0
    assert (dirs[0]/'config-oxidns.json').read_bytes()==saved and (dirs[0]/INSTALLATION_ID).read_bytes()==installation
    assert (dirs[1]/'rules.txt').read_text()==(dirs[2]/'journal.json').read_text()=='preserved'


def test_concurrent_first_starts_share_identity_and_enroll_once(tmp_path):
    with origin() as (url,state):
        env,dirs=fixture(tmp_path,url)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:invoke(env,dirs),range(2)))
        assert all(result.returncode==0 for result in results), [result.stderr for result in results]
        assert len(state['calls'])==1
        identity=(dirs[0]/INSTALLATION_ID).read_text().strip()
        assert all(json.loads((directory/MARKER).read_text())['installation_id']==identity for directory in dirs)


@pytest.mark.parametrize('fault',['agent-data','core-data','ha-data','legacy-data','directory-link','marker-link','marker-fifo','identity-link'])
def test_new_installation_refuses_unowned_or_nonordinary_data(tmp_path,fault):
    env,dirs=fixture(tmp_path)
    original=None
    if fault in ('agent-data','core-data','ha-data','legacy-data'):
        index={'agent-data':0,'core-data':1,'ha-data':2,'legacy-data':0}[fault]
        original=dirs[index]/('.configflow-compose-direct.json' if fault=='legacy-data' else 'original')
        original.write_text('preserve')
    elif fault=='directory-link':
        dirs[1].rmdir();dirs[1].symlink_to(dirs[2],target_is_directory=True)
    elif fault in ('marker-link','marker-fifo'):
        path=dirs[0]/MARKER
        if fault=='marker-link':path.symlink_to(tmp_path/'absent')
        else:os.mkfifo(path)
    else:(dirs[0]/INSTALLATION_ID).symlink_to(tmp_path/'absent')
    result=invoke(env,dirs,'prepare')
    assert result.returncode!=0 and 'ENTRYPOINT_ALLOWED' not in result.stdout
    if original:assert original.read_text()=='preserve'
    assert not (dirs[0]/'config-oxidns.json').exists()


@pytest.mark.parametrize('fault',['identity-missing','identity-changed','identity-symlink','core-marker-missing','ha-owner','agent-token','agent-symlink','session','host','name','port','network'])
def test_owned_restart_refuses_corruption_or_changed_installation_without_network(tmp_path,fault):
    with origin() as (url,state):
        env,dirs=fixture(tmp_path,url)
        assert invoke(env,dirs).returncode==0
        if fault.startswith('identity-'):
            path=dirs[0]/INSTALLATION_ID
            if fault=='identity-missing':path.unlink()
            elif fault=='identity-changed':path.write_text('b'*32)
            else:
                target=tmp_path/'original';path.rename(target);path.symlink_to(target)
        elif fault=='core-marker-missing':(dirs[1]/MARKER).unlink()
        elif fault=='ha-owner':
            path=dirs[2]/MARKER;record=json.loads(path.read_text());record['owner']='f'*64;path.write_text(json.dumps(record))
        elif fault in ('agent-token','agent-symlink'):
            path=dirs[0]/'config-oxidns.json'
            if fault=='agent-token':
                record=json.loads(path.read_text());record['token']='changed-token-123456789';path.write_text(json.dumps(record))
            else:
                target=tmp_path/'original';path.rename(target);path.symlink_to(target)
        else:
            key,value={'session':('INSTALL_KEY','b'*32+'.'+TOKEN),'host':('AGENT_IP','192.168.10.21'),
                       'name':('AGENT_NAME','changed-oxidns'),'port':('AGENT_PORT','8083'),
                       'network':('SERVICE_TYPE','oxidns-ha-macvlan')}[fault]
            env[key]=value
        before=len(state['calls']);result=invoke(env,dirs)
        assert result.returncode!=0 and len(state['calls'])==before
        assert TOKEN not in result.stdout+result.stderr


@pytest.mark.parametrize('status',[302,307])
def test_compact_runtime_rejects_redirect_without_forwarding_capability(tmp_path,status):
    with origin() as (target_url,target), origin() as (url,source):
        source.update(status=status,location=capability(target_url)['url'])
        env,dirs=fixture(tmp_path,url);result=invoke(env,dirs)
        assert result.returncode!=0 and target['calls']==[]
        assert TOKEN not in result.stdout+result.stderr and not (dirs[0]/'config-oxidns.json').exists()


@pytest.mark.parametrize('stage',['agent-marker','core-marker','ha-marker','identity','ready'])
def test_interrupted_owned_initialization_resumes_before_services_start(tmp_path,stage):
    env,dirs=fixture(tmp_path)
    # Interrupt real atomic operations at each publication boundary, preserving
    # whatever the runtime wrote. A second process must complete the same claim.
    runner='''import os,runpy,sys
from pathlib import Path
m=runpy.run_path(sys.argv[1]);s=m['settings_from_environment']();dirs=[Path(v) for v in sys.argv[3:]]
original=m['prepare'].__globals__['write_json'];phase=sys.argv[2]
def stop_after(path,value,replace=False):
 original(path,value,replace)
 index=dirs.index(path.parent)
 if (phase==('agent-marker','core-marker','ha-marker')[index] and not replace) or (phase=='ready' and replace):os._exit(33)
m['prepare'].__globals__['write_json']=stop_after
if phase=='identity':
 old=m['prepare'].__globals__['write_identity']
 def identity(*a):old(*a);os._exit(33)
 m['prepare'].__globals__['write_identity']=identity
m['prepare'](s,*dirs)
'''
    first=subprocess.run([sys.executable,'-c',runner,str(SOURCE),stage,*map(str,dirs)],env={**os.environ,**env},capture_output=True,text=True,timeout=10)
    assert first.returncode==33,first.stderr
    saved=json.loads((dirs[0]/MARKER).read_text())['installation_id']
    second=invoke(env,dirs,'prepare')
    assert second.returncode==0,second.stderr
    assert (dirs[0]/INSTALLATION_ID).read_text().strip()==saved
    assert json.loads((dirs[0]/MARKER).read_text())['ready'] is True


def test_different_enrollment_scope_uses_a_separate_project_and_helper_scope(tmp_path):
    body = params(tmp_path, install_method='compose_compact')
    first = capability()
    other = {**first, 'url': first['url'].rsplit('/', 1)[0] + '/' + 'b'*32}
    documents = [yaml.safe_load(generate_guide(body, grant)['compose']) for grant in (first, other)]
    assert documents[0]['name'] != documents[1]['name']
    assert documents[0]['services']['ha-companion']['environment']['INSTALL_SCOPE'] == 'a'*32
    assert documents[1]['services']['ha-companion']['environment']['INSTALL_SCOPE'] == 'b'*32


def test_colon_in_compact_directory_uses_unambiguous_long_bind_mounts(tmp_path):
    body, _, document = generated(tmp_path, data_dir=str(tmp_path/'my:data'))
    for role, service in document['services'].items():
        for name, mount in zip(('agent','oxidns','ha'), service['volumes'][:3]):
            assert mount['type'] == 'bind' and mount['source'] == body['data_dir'] + '/' + name
            assert mount['bind'] == {'create_host_path':True}
            assert mount['read_only'] == (role == 'ha-companion' and name != 'ha')
