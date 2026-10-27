"""Generated guide commands preserve data and refuse ambiguous migrations."""
import json
import os
import subprocess
import shlex
import sys
from pathlib import Path

import pytest
import yaml

from backend.agents.docker_ha_guide import generate_guide, identity_command
from backend.test_agent_upgrade_auth import authenticated_app  # noqa: F401


def params(tmp_path, mode='new', kind='bind'):
    root = tmp_path / 'persistent'
    root.mkdir(exist_ok=True)
    identity = root / 'host-id'
    if not identity.exists():
        identity.write_text('unique-host-identity-1234567890\n')
    data = dict(mode=mode, server_url='http://config.test:8000', agent_name='dns-one', agent_ip='192.168.10.20',
                data_dir=str(root / 'new-data'), network_mode='host', ha_host_id_path=str(identity), oxidns_port=8082,
                container_name='original-oxidns', existing_agent_name='original-dns', image='local.test/agent:guide',
                ha_data_dir=str(root / 'new-ha'), backup_dir=str(root / 'backup'))
    for key in ('agent', 'core'):
        source = root / ('old-' + key)
        source.mkdir(exist_ok=True)
        data[key + '_mount'] = {'type': kind, 'source': str(source) if kind == 'bind' else 'old_' + key}
    return data


def run(script, tmp_path, env=None):
    path = tmp_path / 'command.sh'
    path.write_text(script)
    return subprocess.run(['bash', str(path)], capture_output=True, text=True, env={**os.environ, **(env or {})})


def test_identity_create_once_and_never_overwrite_regular_or_symlink(tmp_path):
    identity = tmp_path / 'persistent' / 'host-id'
    command = identity_command(str(identity))
    first = run(command, tmp_path)
    assert first.returncode == 0, first.stderr
    original = identity.read_text()
    assert len(original.strip()) == 32
    assert identity.stat().st_mode & 0o777 == 0o600
    assert run(command, tmp_path).returncode != 0
    assert identity.read_text() == original
    dangling = tmp_path / 'link'
    dangling.symlink_to(tmp_path / 'absent')
    assert run(identity_command(str(dangling)), tmp_path).returncode != 0
    assert dangling.is_symlink() and not (tmp_path / 'absent').exists()
    link = tmp_path / 'valid-link'
    link.symlink_to(identity)
    assert run(identity_command(str(link)), tmp_path).returncode != 0
    assert identity.read_text() == original


def test_identity_path_is_literal_and_does_not_execute_shell(tmp_path):
    marker = tmp_path / 'must-not-exist'
    identity = tmp_path / ('host-$(touch ' + marker.name + ')')
    result = run(identity_command(str(identity)), tmp_path)
    assert result.returncode == 0, result.stderr
    assert identity.exists() and not marker.exists()


@pytest.mark.parametrize('path', ['relative', '/', '/a/../b', '/a//b', '/a,id', '/a:id', '/a\nid'])
def test_identity_rejects_nonpersistent_ambiguous_paths(path):
    with pytest.raises(ValueError):
        identity_command(path)


def test_inspection_is_available_before_full_form_and_never_dumps_credentials():
    generic = generate_guide({'action': 'inspect'})['inspection_command']
    assert 'docker version' in generic and 'docker compose version' in generic
    named = generate_guide({'action': 'inspect', 'container_name': 'existing'})['inspection_command']
    assert '.Mounts' in named and 'agent_name' in named
    assert '.Config.Env' not in named and '"token"' not in named and '"agent_id"' not in named
    with pytest.raises(ValueError):
        generate_guide({'action': 'inspect', 'container_name': 'x;echo bad'})


@pytest.mark.parametrize('vip', ['127.0.0.1', '192.168.1.2;echo', '::1', None, '0.0.0.0'])
def test_verification_rejects_invalid_vip(vip):
    with pytest.raises(ValueError):
        generate_guide({'action': 'verify', 'vip': vip})


def test_verification_uses_literal_ipv4():
    assert generate_guide({'action': 'verify', 'vip': '192.168.10.8'})['verification_command'] == 'nslookup example.com 192.168.10.8\n'


@pytest.mark.parametrize('kind', ['bind', 'volume'])
def test_migration_compose_reuses_exact_mount_and_agent_identity(tmp_path, kind):
    data = params(tmp_path, 'migrate', kind)
    result = generate_guide(data)
    compose = yaml.safe_load(result['compose'])
    main, helper = compose['services']['agent'], compose['services']['ha-companion']
    assert main['container_name'] == data['container_name']
    assert 'AGENT_OXIDNS_NAME=original-dns' in main['environment']
    assert main['image'] == helper['image'] == data['image']
    for service, ro in ((main, False), (helper, True)):
        assert service['volumes'][0]['source'] == data['agent_mount']['source']
        assert service['volumes'][1]['source'] == data['core_mount']['source']
        assert service['volumes'][0]['type'] == kind
        assert service['volumes'][0]['read_only'] is ro
    if kind == 'volume':
        assert compose['volumes']['old_agent'] == {'external': True, 'name': 'old_agent'}
    assert './oxidns_data' not in result['install_command']
    assert 'docker rm' not in result['install_command'] + result['rollback_command']
    assert 'down -v' not in result['install_command'] + result['rollback_command']
    assert 'docker rename' in result['install_command'] and 'docker cp' in result['install_command']
    assert 'oxidns-ha.json' in result['install_command'] and '自定义启动命令' in result['install_command']
    assert main['env_file'] == [{'path': data['backup_dir'] + '/runtime.env', 'format': 'raw'}]


@pytest.mark.parametrize('change', [
    {'agent_mount': {'type': 'bind', 'source': 'relative'}},
    {'agent_mount': {'type': 'volume', 'source': '--option'}},
    {'ha_data_dir': '/tmp/x', 'backup_dir': '/tmp/x/backup'},
    {'container_name': 'bad;name'}, {'existing_agent_name': 'bad name'},
    {'image': 'repo/image:latest;touch bad'}, {'server_url': 'file:///tmp/x'},
    {'oxidns_port': True}, {'create_host_identity': 'true'},
])
def test_generation_rejects_unsafe_or_ambiguous_fields(tmp_path, change):
    data = params(tmp_path, 'migrate')
    with pytest.raises(ValueError):
        generate_guide({**data, **change})


def test_all_generated_commands_are_bash_syntax_and_compose_interpolation_safe(tmp_path):
    data = params(tmp_path, 'migrate')
    data['server_url'] += '/$NOT_A_VARIABLE'
    result = generate_guide(data)
    assert '$$NOT_A_VARIABLE' in result['compose']
    for block in result['command_blocks']:
        parsed = subprocess.run(['bash', '-n'], input=block['command'], text=True, capture_output=True)
        assert parsed.returncode == 0, parsed.stderr


FAKE_DOCKER = r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args=sys.argv[1:]; path=Path(os.environ['FAKE_STATE']); state=json.loads(path.read_text())
with open(os.environ['FAKE_EVENTS'],'a') as out: out.write(json.dumps(args)+'\n')
def save(): path.write_text(json.dumps(state))
def lookup(value):
    for ident, c in state['containers'].items():
        if value in (ident,c['name']): return ident,c
    sys.exit(1)
if args[0]=='info': print('linux' if '.OSType' in args[-1] else '[]')
elif args[0]=='image': print('[]' if '.Config.' in ' '.join(args) else '{}')
elif args[0]=='network':
    if args[1]=='create':
        state['network']={'name':args[-1], 'owner':args[args.index('--label')+1].split('=',1)[1],
                          'parent':args[args.index('--opt')+1].split('=',1)[1], 'subnet':args[args.index('--subnet')+1],
                          'gateway':args[args.index('--gateway')+1] if '--gateway' in args else ''};save();print('network-id')
    elif not state.get('network'): sys.exit(1)
    elif '--format' in args:
        fmt=args[args.index('--format')+1];net=state['network']
        print('macvlan' if '.Driver' in fmt else net['parent'] if '.Options' in fmt else net['owner'] if '.Labels' in fmt
              else '1' if 'len ' in fmt else net['subnet'] if '.Subnet' in fmt else net['gateway'])
    else: print('{}')
elif args[0] in ('inspect','container'):
    if args[0]=='container': args=args[1:]
    ident,c=lookup(args[-1]); fmt=args[args.index('--format')+1] if '--format' in args else ''
    if '.Id' in fmt: print(ident)
    elif '.Name' in fmt and '.Type' not in fmt and '.RestartPolicy' not in fmt: print('/'+c['name'])
    elif '.State.Running' in fmt: print(str(c.get('running',False)).lower())
    elif 'len .Mounts' in fmt: print(state.get('mount_count',2))
    elif '.Type' in fmt:
        m=state['agent_mount' if '/opt/configflow-agent' in fmt else 'core_mount']
        print(m['type']+'|'+m['source']+'|true')
    elif '.Config.Labels' in fmt: print(c.get('owner',''))
    elif '.Config.' in fmt: print('[]')
    elif '.Image' in fmt: print('original-image')
    elif '.RestartPolicy' in fmt: print('always:0')
    else: print('{}')
elif args[0]=='exec':
    if '-i' in args: sys.stdin.read()
    if state.get('reject_existing'): sys.exit(1)
    print('HEARTBEAT_INTERVAL=42\nDEPLOYMENT_HEALTH_TIMEOUT=30\nAGENT_HOST=0.0.0.0')
elif args[0]=='update':
    _,c=lookup(args[-1]); c['restart']=next(x.split('=',1)[1] for x in args if x.startswith('--restart=')); save()
elif args[0] in ('start','stop'):
    ident,c=lookup(args[-1]); c['running']=args[0]=='start';save();print(ident)
elif args[0]=='rename':
    _,c=lookup(args[1]);c['name']=args[2];save()
elif args[0]=='cp': pass
elif args[0]=='run':
    if '--rm' in args: sys.exit(1 if state.get('ha_journal') else 0)
    name=args[args.index('--name')+1]; ident=('b' if name.endswith('-ha') else 'c')*64
    if state.get('fail_before_main') and not name.endswith('-ha'):
        if '--cidfile' in args: Path(args[args.index('--cidfile')+1]).write_text('')
        sys.exit(123)
    state['containers'][ident]={'name':name,'running':True, 'owner':args[args.index('--label')+1].split('=',1)[1] if '--label' in args else ''};save()
    if '--cidfile' in args: Path(args[args.index('--cidfile')+1]).write_text(ident+'\n')
    if state.get('fail_main') and not name.endswith('-ha'):
        state['containers'][ident]['running']=False;save();sys.exit(123)
    print(ident)
else: sys.exit('unexpected fake Docker command: '+repr(args))
"""


def fake_host(tmp_path, data, **changes):
    docker = tmp_path / 'docker'
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    state = tmp_path / 'state.json'
    state.write_text(json.dumps({'containers': {'a'*64: {'name': data['container_name'], 'running': True}},
                                'agent_mount': data['agent_mount'], 'core_mount': data['core_mount'], **changes}))
    events = tmp_path / 'events'
    return state, events, {'PATH': str(tmp_path) + ':' + os.environ['PATH'], 'FAKE_STATE': str(state), 'FAKE_EVENTS': str(events)}


def test_migration_and_rollback_use_captured_ids_and_restore_restart_policy(tmp_path):
    data = params(tmp_path, 'migrate')
    state, events, env = fake_host(tmp_path, data)
    guide = generate_guide(data)
    result = run(guide['install_command'], tmp_path, env)
    assert result.returncode == 0, result.stderr
    calls = [json.loads(v) for v in events.read_text().splitlines()]
    stop = next(i for i,v in enumerate(calls) if v[0]=='stop')
    backup = next(i for i,v in enumerate(calls) if v[0]=='cp')
    rename = next(i for i,v in enumerate(calls) if v[0]=='rename')
    start = next(i for i,v in enumerate(calls) if v[0]=='run')
    assert stop < backup < rename < start
    assert (Path(data['backup_dir'])/'old-id').read_text().strip() == 'a'*64
    assert (Path(data['backup_dir'])/'runtime.env').read_text().startswith('HEARTBEAT_INTERVAL=42')
    result = run(guide['rollback_command'], tmp_path, env)
    assert result.returncode == 0, result.stderr
    old = json.loads(state.read_text())['containers']['a'*64]
    assert old == {'name': data['container_name'], 'running': True, 'restart': 'always'}


def test_failed_new_container_can_rollback_without_exec_into_it(tmp_path):
    data = params(tmp_path, 'migrate')
    state, events, env = fake_host(tmp_path, data, fail_main=True)
    guide = generate_guide(data)
    assert run(guide['install_command'], tmp_path, env).returncode != 0
    result = run(guide['rollback_command'], tmp_path, env)
    assert result.returncode == 0, result.stderr
    assert json.loads(state.read_text())['containers']['a'*64]['running'] is True


@pytest.mark.parametrize('changes', [{'mount_count': 3}, {'reject_existing': True}])
def test_unsupported_migration_aborts_before_stop_or_data_change(tmp_path, changes):
    data = params(tmp_path, 'migrate')
    _, events, env = fake_host(tmp_path, data, **changes)
    guide = generate_guide(data)
    assert run(guide['install_command'], tmp_path, env).returncode != 0
    calls = [json.loads(v) for v in events.read_text().splitlines()]
    assert not any(v[0] in ('stop','update','rename','cp','run') for v in calls)
    assert not Path(data['backup_dir']).exists()


def test_rollback_refuses_name_replaced_by_unrelated_container(tmp_path):
    data = params(tmp_path, 'migrate')
    state, events, env = fake_host(tmp_path, data)
    guide = generate_guide(data)
    assert run(guide['install_command'], tmp_path, env).returncode == 0
    current = json.loads(state.read_text())
    current['containers']['c'*64]['name'] = 'moved-new'
    current['containers']['d'*64] = {'name': data['container_name'], 'running': True}
    state.write_text(json.dumps(current)); events.write_text('')
    assert run(guide['rollback_command'], tmp_path, env).returncode != 0
    assert not any(json.loads(v)[0] in ('stop','update','rename') for v in events.read_text().splitlines())


def test_live_environment_requires_admin_and_never_exposes_extra_payload(authenticated_app, monkeypatch):
    client, manager, _, admin, _ = authenticated_app
    agent = manager.register_agent({'name': 'guide-oxidns', 'host': '192.168.10.20', 'port': 8082,
                                    'service_type': 'oxidns', 'deployment_method': 'docker'})
    endpoint = '/api/agents/' + agent['id'] + '/oxidns-ha-environment'
    assert client.get(endpoint).status_code == 401
    calls = []
    def live(*args, **kwargs):
        calls.append((args, kwargs))
        return {'oxidns_ha_protocols': [1], 'oxidns_ha_container_protocols': [1], 'oxidns_ha_handoff_protocols': [1],
                'secret': 'must-not-return', 'oxidns_ha_environment': {'kind': 'container', 'ready': True,
                'host_id': 'a'*64, 'network': 'host', 'interface': 'eth0', 'blockers': [], 'private': 'hidden'}}
    monkeypatch.setattr(manager, '_deployment_request', live)
    for _ in range(2):
        response = client.get(endpoint, headers=admin)
        assert response.status_code == 200
        value = response.get_json()
        assert value['oxidns_ha_environment']['ready'] is True
        assert value['oxidns_ha_environment']['kind'] == 'container'
        assert 'secret' not in value and 'private' not in value['oxidns_ha_environment']
    assert len(calls) == 2 and all(c[0][1:] == ('get', 'capabilities') for c in calls)


def test_migration_preserves_old_environment_without_echoing_secrets(tmp_path):
    data = params(tmp_path, 'migrate')
    script = generate_guide(data)['install_command']
    tokens = shlex.split(script)
    source = next(v for v in tokens if v.startswith('import json, os, pathlib, re, sys'))
    original = tmp_path / 'old-agent'
    original.mkdir()
    (original / 'config-oxidns.json').write_text(json.dumps({
        'agent_name': data['existing_agent_name'], 'server_url': data['server_url'], 'agent_port': data['oxidns_port'],
        'service_type': 'oxidns', 'config_path': '/etc/oxidns/config.yaml', 'heartbeat_interval': 42,
    }))
    source = source.replace('/opt/configflow-agent', str(original))
    result = subprocess.run([sys.executable, '-c', source, data['existing_agent_name'], data['server_url'], str(data['oxidns_port'])],
                            input=json.dumps(['HTTPS_PROXY=http://private-user:private$password@proxy:8080', 'TZ=UTC']),
                            text=True, capture_output=True, env={**os.environ, 'TZ': 'UTC'})
    assert result.returncode == 0, result.stderr
    assert 'HTTPS_PROXY=http://private-user:private$password@proxy:8080' in result.stdout
    compose = yaml.safe_load(generate_guide(data)['compose'])
    assert compose['services']['agent']['env_file'][0]['format'] == 'raw'
    assert 'HEARTBEAT_INTERVAL=42' in result.stdout and 'TZ=UTC' in result.stdout
    assert not result.stderr
    assert 'cf_runtime_env=$(' in script and '"$cf_runtime_env" > "$cf_backup/runtime.env"' in script
    assert 'umask 077' in script


def new_host(tmp_path, monkeypatch, mode='host', **changes):
    data = params(tmp_path)
    if mode == 'macvlan':
        data.update(network_mode=mode, ha_parent='eth0', ha_subnet='192.168.10.0/24', ha_gateway='192.168.10.1')
    state, events, env = fake_host(tmp_path, data, **changes)
    value = json.loads(state.read_text()); value['containers'] = {}; state.write_text(json.dumps(value))
    monkeypatch.setattr('backend.agents.docker_ha_enrollment.bootstrap_command',
                        lambda *_: 'printf bootstrap >> "$cf_state/bootstrap-count"\n')
    guide = generate_guide(data, enrollment={'url': 'http://config.test/enroll/same-installation', 'token': 'private'})
    return data, state, events, env, guide


@pytest.mark.parametrize('failure', ['fail_main', 'fail_before_main'])
def test_new_retry_reuses_only_owned_companion_and_resumes_failed_main(tmp_path, monkeypatch, failure):
    data, state, events, env, guide = new_host(tmp_path, monkeypatch, **{failure: True})
    first = run(guide['install_command'], tmp_path, env)
    assert first.returncode != 0, first.stdout
    value = json.loads(state.read_text()); value[failure] = False; state.write_text(json.dumps(value))
    second = run(guide['install_command'], tmp_path, env)
    assert second.returncode == 0, second.stderr
    calls = [json.loads(line) for line in events.read_text().splitlines()]
    starts = [call for call in calls if call[0] == 'run' and '--name' in call]
    assert sum(call[call.index('--name')+1].endswith('-ha') for call in starts) == 1
    assert len([c for c in json.loads(state.read_text())['containers'].values() if c['running']]) == 2
    before = events.read_text()
    assert run(guide['install_command'], tmp_path, env).returncode == 0
    tail = events.read_text()[len(before):]
    assert not any((lambda call: call[0] in ('start','stop','rename') or (call[0]=='run' and '--name' in call))(json.loads(line)) for line in tail.splitlines())
    assert all('--network' in json.loads(line) and json.loads(line)[json.loads(line).index('--network')+1]=='none' for line in tail.splitlines() if json.loads(line)[0]=='run')
    assert Path(data['data_dir'], '.ha-install', 'setup').exists()


def test_new_retry_rejects_unrelated_same_name_even_with_copied_owner_label(tmp_path, monkeypatch):
    _, state, events, env, guide = new_host(tmp_path, monkeypatch, fail_before_main=True)
    assert run(guide['install_command'], tmp_path, env).returncode != 0
    value = json.loads(state.read_text())
    owned = value['containers'].pop('b'*64)
    value['containers']['d'*64] = owned
    state.write_text(json.dumps(value)); events.write_text('')
    assert run(guide['install_command'], tmp_path, env).returncode != 0
    assert not any(json.loads(line)[0] in ('run','start','stop','rename') for line in events.read_text().splitlines())


def test_new_retry_reuses_owned_macvlan_but_rejects_changed_network(tmp_path, monkeypatch):
    _, state, events, env, guide = new_host(tmp_path, monkeypatch, 'macvlan', fail_before_main=True)
    assert run(guide['install_command'], tmp_path, env).returncode != 0
    value = json.loads(state.read_text()); value['fail_before_main'] = False; state.write_text(json.dumps(value))
    second = run(guide['install_command'], tmp_path, env)
    assert second.returncode == 0, second.stderr
    calls = [json.loads(line) for line in events.read_text().splitlines()]
    assert sum(call[:2] == ['network','create'] for call in calls) == 1
    before = events.read_text()
    assert run(guide['install_command'], tmp_path, env).returncode == 0
    tail = [json.loads(line) for line in events.read_text()[len(before):].splitlines()]
    assert any(call[0]=='run' and '--network' in call and call[call.index('--network')+1]=='none' for call in tail)
    assert not any(call[:2]==['network','create'] or (call[0]=='run' and '--name' in call) for call in tail)
    value = json.loads(state.read_text()); value['network']['parent'] = 'other0'; state.write_text(json.dumps(value))
    events.write_text('')
    assert run(guide['install_command'], tmp_path, env).returncode != 0
    assert not any(json.loads(line)[0] in ('run','start','stop','rename') for line in events.read_text().splitlines())


def test_new_retry_rejects_changed_settings_and_does_not_rewrite_marker(tmp_path, monkeypatch):
    data, state, events, env, guide = new_host(tmp_path, monkeypatch, fail_before_main=True)
    assert run(guide['install_command'], tmp_path, env).returncode != 0
    marker = Path(data['data_dir'], '.ha-install', 'setup')
    before = marker.read_bytes()
    changed = generate_guide({**data, 'oxidns_port': 9092}, enrollment={'url':'http://config.test/enroll/same-installation'})
    events.write_text('')
    assert run(changed['install_command'], tmp_path, env).returncode != 0
    assert marker.read_bytes() == before
    assert not any(json.loads(line)[0] in ('run','start','stop','rename') for line in events.read_text().splitlines())
