"""Structured host preflight is complete, bounded, read-only and never executes input."""
import base64
import json
import os
from pathlib import Path
import subprocess
import shlex
import sys

import pytest

from backend.agents import docker_ha_preflight as check
from backend.agents.docker_ha_guide import generate_guide
from backend.test_agent_upgrade_auth import authenticated_app  # noqa: F401


def evidence(**changes):
    values = {
        'kernel': (0, 'Linux'), 'docker_endpoint': (0, 'local-unix'),
        'docker_version': (0, '{"client":"29.9.2","server":"29.9.2"}'),
        'daemon_os': (0, 'linux'), 'daemon_platform': (0, '{"os":"Arch Linux","kernel":"6.18.3"}'),
        'daemon_security': (0, '["name=seccomp,profile=builtin","name=cgroupns"]'),
        'compose': (0, '2.30.3'), 'ipv4': (0, '2: eth0    inet 192.168.10.20/24 brd 192.168.10.255 scope global eth0\n'),
        'udp53': (0, ''), 'tcp53': (0, ''), 'nesting': (0, 'none'), 'machine_fingerprint': (0, 'a' * 64),
    }
    values.update(changes)
    return values


def block(values=None):
    values = values if values is not None else evidence()
    lines = [check.BEGIN]
    for key, (code, value) in values.items():
        lines.append('CFHA1\t' + key + '\t' + str(code) + '\t' + base64.b64encode(value.encode()).decode())
    return '\n'.join(lines + [check.END]) + '\n'


def item(report, ident):
    return next(value for value in report['checks'] if value['id'] == ident)


def test_complete_native_local_output_only_passes_basic_checks():
    report = check.parse_report(block())
    assert report['status'] == 'pass'
    assert report['host_fingerprint'] == 'a' * 64
    assert '基础检查' in report['summary'] and 'VIP' in report['summary']
    assert 'Docker bridge' in item(report, 'ipv4')['detail']


def test_terminal_prompts_and_old_container_suffix_do_not_replace_protocol_evidence():
    report = check.parse_report('root@nas:~# pasted command\n' + block() + '\nold mounts: /persistent/core\n')
    assert report['status'] == 'pass'


@pytest.mark.parametrize('output', [
    'Linux\nDocker version 29.9.2\n', '', block().replace(check.END, ''),
    block() + block(), block().replace(check.BEGIN, 'CONFIGFLOW_DOCKER_HA_PREFLIGHT_V2_BEGIN'),
    block({key: val for key, val in evidence().items() if key != 'udp53'}),
    block().replace('CFHA1\tudp53\t0\t', 'CFHA1\tipv4\t0\t'),
    block().replace('CFHA1\tudp53\t0\t', 'CFHA1\tunexpected\t0\t'),
    block().replace('CFHA1\tudp53\t0\t', 'CFHA1\tudp53\t999\t'),
    block().replace('CFHA1\tudp53\t0\t', 'CFHA1\tudp53\t0\t!invalid!'),
    'CFHA1\ttcp53\t0\t\n' + block(),
    block(evidence(ipv4=(0, 'x' * (check.MAX_FIELD_BYTES + 1)))),
])
def test_old_duplicate_truncated_or_malformed_output_is_never_a_pass(output):
    assert check.parse_report(output)['status'] == 'unknown'


def test_binary_and_deep_json_cannot_trigger_unhandled_parser_error():
    invalid = block().replace('CFHA1\tudp53\t0\t', 'CFHA1\tudp53\t0\t/w==')
    assert check.parse_report(invalid)['status'] == 'unknown'
    deep = '[' * 2000 + '0' + ']' * 2000
    report = check.parse_report(block(evidence(daemon_security=(0, deep))))
    assert report['status'] == 'unknown'
    assert item(report, 'docker_rootful')['status'] == 'unknown'


@pytest.mark.parametrize('output', [None, [], '\ud800', 'x' * (check.MAX_OUTPUT_BYTES + 1)])
def test_parser_rejects_wrong_type_encoding_or_oversized_input(output):
    with pytest.raises(ValueError):
        check.parse_report(output)


@pytest.mark.parametrize('change,ident', [
    ({'kernel': (0, 'Darwin')}, 'linux'),
    ({'docker_endpoint': (0, 'remote')}, 'docker_endpoint'),
    ({'docker_version': (1, 'Cannot connect to the Docker daemon')}, 'docker_daemon'),
    ({'docker_version': (127, 'docker: command not found')}, 'docker_daemon'),
    ({'daemon_os': (0, 'windows')}, 'docker_os'),
    ({'daemon_platform': (0, '{"os":"Docker Desktop","kernel":"linuxkit"}')}, 'docker_platform'),
    ({'daemon_security': (0, '["name=rootless"]')}, 'docker_rootful'),
])
def test_known_unsupported_environments_fail_without_echoing_raw_errors(change, ident):
    report = check.parse_report(block(evidence(**change)))
    assert report['status'] == 'fail'
    assert item(report, ident)['status'] == 'fail'


@pytest.mark.parametrize('field,ident', [('ipv4','ipv4'), ('udp53','udp53'), ('tcp53','tcp53'),
                                         ('docker_endpoint','docker_endpoint'), ('daemon_security','docker_rootful')])
def test_failed_required_probe_is_unknown_not_empty_success(field, ident):
    report = check.parse_report(block(evidence(**{field: (127, 'missing tool')})))
    assert report['status'] == 'unknown'
    assert item(report, ident)['status'] == 'unknown'


@pytest.mark.parametrize('field,line', [
    ('udp53', 'UNCONN 0 0 127.0.0.53%lo:53 0.0.0.0:*'),
    ('udp53', 'UNCONN 0 0 [::]:53 [::]:*'),
    ('tcp53', 'LISTEN 0 4096 127.0.0.54:53 0.0.0.0:*'),
])
def test_host_loopback_or_wildcard_port_collision_fails_but_macvlan_does_not(field, line):
    data = block(evidence(**{field: (0, line)}))
    report = check.parse_report(data, 'host')
    assert report['status'] == 'fail' and item(report, field)['status'] == 'fail'
    assert '迁移' in item(report, field)['remedy']
    macvlan = check.parse_report(data, 'macvlan')
    assert macvlan['status'] == 'pass' and item(macvlan, field)['status'] == 'pass'


@pytest.mark.parametrize('line', ['ss: not supported', 'UNCONN 0 0 127.0.0.53:53', 'LISTEN 0 1 *:53 *:*'])
def test_unrecognized_udp_output_is_unknown(line):
    assert item(check.parse_report(block(evidence(udp53=(0,line)))), 'udp53')['status'] == 'unknown'


@pytest.mark.parametrize('version', [(127,''), (0,'2.29.9'), (0,'nonsense')])
def test_compose_is_optional_for_run_installation(version):
    report = check.parse_report(block(evidence(compose=version)))
    assert report['status'] == 'warning'
    assert 'Docker Run' in item(report, 'compose')['detail']


def test_missing_default_identity_and_lxc_are_warnings_not_native_success_or_failure():
    report = check.parse_report(block(evidence(machine_fingerprint=(1,''), nesting=(0,'container'))))
    assert report['status'] == 'warning' and 'host_fingerprint' not in report
    assert '/etc/machine-id' in item(report, 'host_identity')['detail']
    assert item(report, 'nesting')['status'] == 'warning'


def test_pasted_shell_and_html_are_never_executed(tmp_path):
    marker = tmp_path / 'should-not-exist'
    attack = '$(touch ' + str(marker) + ')<script>alert(1)</script>'
    report = check.parse_report(block(evidence(ipv4=(0,attack))))
    assert report['status'] == 'unknown'
    assert not marker.exists()
    assert '<script>' not in json.dumps(report)


def test_preflight_api_requires_auth_and_never_issues_enrollment_or_persists_input(authenticated_app, monkeypatch):
    client, manager, _, admin, _ = authenticated_app
    monkeypatch.setattr('backend.agents.docker_ha_enrollment.issue', lambda *a: pytest.fail('check cannot issue enrollment'))
    endpoint = '/api/agents/docker-oxidns-ha-guide'
    body = {'action':'check', 'output':block(), 'network_mode':'host'}
    assert client.post(endpoint, json=body).status_code == 401
    before = manager.repository.get_system()
    response = client.post(endpoint, json=body, headers=admin)
    assert response.status_code == 200 and response.headers['Cache-Control'] == 'no-store'
    assert response.get_json()['preflight_report']['status'] == 'pass'
    assert manager.repository.get_system() == before
    assert not (Path(manager.repository.data_dir)/'docker-ha-installations').exists()
    response = client.post(endpoint, json={**body, 'output':'old legacy text'}, headers=admin)
    assert response.status_code == 200 and response.get_json()['preflight_report']['status'] == 'unknown'


def test_api_body_limit_only_expands_for_check(authenticated_app):
    client, _, _, admin, _ = authenticated_app
    endpoint = '/api/agents/docker-oxidns-ha-guide'
    response = client.post(endpoint, json={'action':'check', 'output':'terminal notes\n' * 1400 + block(), 'network_mode':'host'}, headers=admin)
    assert response.status_code == 200 and response.get_json()['preflight_report']['status'] == 'pass'
    assert client.post(endpoint, json={'action':'inspect', 'padding':'x'*17000}, headers=admin).status_code == 413
    assert client.post(endpoint, data='x'*17000, content_type='application/json', headers=admin).status_code == 413
    assert client.post(endpoint, json={'action':'check', 'output':'x'*(129*1024)}, headers=admin).status_code == 413
    assert client.post('/api/agents/docker-oxidns-ha-enroll/'+'a'*32, data='x'*17000, content_type='application/json').status_code == 413


def test_invalid_mode_does_not_fall_back_to_host(authenticated_app):
    client, _, _, admin, _ = authenticated_app
    result = client.post('/api/agents/docker-oxidns-ha-guide', json={'action':'check','output':block(),'network_mode':'bridge'},headers=admin)
    assert result.status_code == 400


DOCKER = r'''#!/usr/bin/env python3
import json,os,sys
args=sys.argv[1:]
if os.environ.get('CF_TEST_DOCKER_EVENTS'):
    with open(os.environ['CF_TEST_DOCKER_EVENTS'],'a') as output: output.write(json.dumps(args)+'\n')
if args[:2]==['context','show']: print('default')
elif args[:2]==['context','inspect']: print('ssh://private-user:secret@remote' if args[2]=='remote-context' else 'unix:///var/run/docker.sock')
elif args[0]=='version':
    if os.environ.get('CF_TEST_DAEMON_FAIL'): print('Cannot connect to the Docker daemon',file=sys.stderr);sys.exit(1)
    print('{"client":"29.9.2","server":"29.9.2"}')
elif args[0]=='compose': print('2.30.3')
elif args[0]=='info':
    if os.environ.get('CF_TEST_DAEMON_FAIL'): sys.exit(1)
    fmt=args[-1]
    print('linux' if fmt=='{{.OSType}}' else '["name=seccomp"]' if '.SecurityOptions' in fmt else '{"os":"Arch Linux","kernel":"6.18.3"}')
else: sys.exit(2)
'''


def command(tmp_path, name, body):
    path = tmp_path/name
    path.write_text(body)
    path.chmod(0o755)


def collector_env(tmp_path):
    command(tmp_path,'docker',DOCKER)
    command(tmp_path,'ip','#!/bin/sh\nprintf "%s\\n" "2: eth0    inet 192.168.10.20/24 scope global eth0"\n')
    command(tmp_path,'ss','#!/bin/sh\nexit 0\n')
    command(tmp_path,'systemd-detect-virt','#!/bin/sh\nexit 1\n')
    return {**os.environ,'PATH':str(tmp_path)+':'+os.environ['PATH'], 'DOCKER_HOST':'', 'DOCKER_CONTEXT':''}


def test_collector_continues_after_daemon_failure_and_preserves_callers_options(tmp_path):
    env = collector_env(tmp_path); env['CF_TEST_DAEMON_FAIL']='1'
    command_text = 'set -eu\n' + check.collector_script() + '\ncase $- in *e*u*|*u*e*) printf "OPTIONS_RETAINED\\n" ;; *) exit 9 ;; esac\n'
    result = subprocess.run(['bash'], input=command_text, text=True, capture_output=True, env=env)
    assert result.returncode == 0, result.stderr
    assert check.END in result.stdout and 'OPTIONS_RETAINED' in result.stdout
    report = check.parse_report(result.stdout)
    assert report['status'] == 'fail'
    assert item(report,'docker_daemon')['status'] == 'fail'
    assert item(report,'ipv4')['status'] == 'pass'
    assert item(report,'udp53')['status'] == 'pass'


def test_collector_docker_context_overrides_host_without_disclosing_endpoint(tmp_path):
    env = collector_env(tmp_path)
    env.update(DOCKER_CONTEXT='remote-context',DOCKER_HOST='unix:///var/run/docker.sock', CF_TEST_DOCKER_EVENTS=str(tmp_path/'docker-events'))
    result = subprocess.run(['bash'],input=check.collector_script(),text=True,capture_output=True,env=env)
    assert result.returncode == 0
    report = check.parse_report(result.stdout)
    assert item(report,'docker_endpoint')['status'] == 'fail'
    assert 'private-user' not in result.stdout and 'secret' not in result.stdout
    calls = [json.loads(line) for line in (tmp_path/'docker-events').read_text().splitlines()]
    assert not any(call[0] in ('version', 'info') for call in calls)
    assert item(report,'docker_daemon')['status'] == 'unknown'
    assert '已跳过' in item(report,'docker_daemon')['detail']
    env.update(DOCKER_CONTEXT='default', DOCKER_HOST='ssh://elsewhere')
    result = subprocess.run(['bash'],input=check.collector_script(),text=True,capture_output=True,env=env)
    assert item(check.parse_report(result.stdout),'docker_endpoint')['status'] == 'pass'


def test_collector_without_encoding_tools_never_emits_false_complete_block(tmp_path):
    result = subprocess.run(['/bin/bash'], input=check.collector_script(),text=True,capture_output=True,
                            env={**os.environ,'PATH':str(tmp_path)})
    assert result.returncode == 0
    assert check.END not in result.stdout and 'base64' in result.stdout
    assert check.parse_report(result.stdout)['status'] == 'unknown'


def test_fingerprint_collection_never_outputs_raw_machine_id():
    script = check.collector_script()
    assert 'sha256sum' in script and '/var/lib/dbus/machine-id' not in script
    assert "printf '%s' \"$cf_identity\" | sha256sum" in script
    assert 'docker inspect' not in script and 'docker stop' not in script and 'docker run' not in script
    assert generate_guide({'action':'inspect'})['inspection_command'] == script

def test_collector_identity_symlink_is_not_claimed_compatible_with_installation(tmp_path):
    env = collector_env(tmp_path)
    target = tmp_path/'real-id'
    target.write_text('0123456789abcdef0123456789abcdef\n')
    link = tmp_path/'identity-link'
    link.symlink_to(target)
    source = check.collector_script().replace('/etc/machine-id', shlex.quote(str(link)))
    result = subprocess.run(['bash'],input=source,text=True,capture_output=True,env=env)
    report = check.parse_report(result.stdout)
    assert item(report,'host_identity')['status'] == 'warning'
    assert 'host_fingerprint' not in report
    assert '软链接' in item(report,'host_identity')['detail']
    source = check.collector_script().replace('/etc/machine-id', shlex.quote(str(target)))
    result = subprocess.run(['bash'],input=source,text=True,capture_output=True,env=env)
    report = check.parse_report(result.stdout)
    assert item(report,'host_identity')['status'] == 'pass'
    assert report['host_fingerprint'] == __import__('hashlib').sha256(target.read_text().strip().encode()).hexdigest()
    assert target.read_text().strip() not in result.stdout


def test_ipv4_report_shows_actual_interfaces_and_prefixes_without_selecting_lan():
    addresses = ('2: enp3s0 inet 192.168.10.20/24 scope global enp3s0\n'
                 '3: docker0 inet 172.17.0.1/16 scope global docker0\n')
    detail = item(check.parse_report(block(evidence(ipv4=(0, addresses)))), 'ipv4')['detail']
    assert '2 个候选' in detail
    assert 'enp3s0 · 192.168.10.20/24' in detail
    assert 'docker0 · 172.17.0.1/16' in detail
    assert 'Docker bridge' in detail and '尚未验证局域网' in detail and '不会自动选择' in detail


def test_ipv4_report_bounds_visible_list_but_keeps_total_count():
    addresses = ''.join(f'{n+1}: eth{n} inet 192.168.10.{n+1}/24 scope global eth{n}\n' for n in range(10))
    detail = item(check.parse_report(block(evidence(ipv4=(0, addresses)))), 'ipv4')['detail']
    assert '10 个候选' in detail and '仅显示前 8 条' in detail
    assert 'eth7 · 192.168.10.8/24' in detail and 'eth8 ·' not in detail
    assert detail.count(' · ') == 8


def test_ipv4_report_cleans_control_and_markup_and_limits_interface_name():
    name = 'eth<script>' + '\x1b[31m' + 'x' * 100
    addresses = f'2: {name} inet 192.168.10.20/24 scope global interface\n'
    detail = item(check.parse_report(block(evidence(ipv4=(0, addresses)))), 'ipv4')['detail']
    assert '<' not in detail and '>' not in detail and '\x1b' not in detail
    assert 'x' * 49 not in detail
    assert '名称已截短或清理' in detail and ' · 192.168.10.20/24' in detail
