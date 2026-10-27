"""Docker HA installation stays opt-in and does not alter ordinary containers."""
import subprocess
import json
import os
import hashlib
from pathlib import Path

import pytest
import yaml

from backend.agents.install_script import generate_docker_agent_compose, generate_docker_agent_run
from backend.test_agent_upgrade_auth import authenticated_app  # noqa: F401

ROOT = Path(__file__).parents[1]


def ha_params(mode='host'):
    return dict(server_url='https://config.example', agent_name='dns-a', agent_ip='192.168.50.20',
                enable_mihomo=False, enable_oxidns=True, enable_dns_ha=True, network_mode=mode,
                **({'ha_parent': 'eth0', 'ha_subnet': '192.168.50.0/24', 'ha_gateway': '192.168.50.1'} if mode == 'macvlan' else {}))


def test_host_compose_separates_core_and_ha_authority():
    compose = yaml.safe_load(generate_docker_agent_compose(**ha_params()))
    main, companion = compose['services']['agent'], compose['services']['ha-companion']
    assert main['network_mode'] == companion['network_mode'] == 'host'
    assert 'cap_add' not in main
    assert set(companion['cap_add']) == {'NET_ADMIN', 'NET_RAW'}
    assert main['depends_on'] == ['ha-companion']
    assert 'SERVICE_TYPE=ha-companion' in companion['environment']
    for service in (main, companion):
        assert 'ports' not in service and 'privileged' not in service
        assert 'CONFIGFLOW_DNS_HA=1' in service['environment']
        assert 'CONFIGFLOW_DNS_HA_NETWORK=host' in service['environment']
        assert './agent_data/ha:/var/lib/configflow-ha' in service['volumes']
        identity = next(item for item in service['volumes'] if isinstance(item, dict) and item['target'].endswith('machine-id'))
        assert identity['read_only'] is True and identity['bind']['create_host_path'] is False
        assert all('docker.sock' not in str(item) and '/etc:/etc' not in str(item) for item in service['volumes'])
    assert './agent_data/agent:/opt/configflow-agent:ro' in companion['volumes']
    assert './agent_data/oxidns:/etc/oxidns:ro' in companion['volumes']
    assert './agent_data/agent:/opt/configflow-agent' in main['volumes']
    assert any(isinstance(item, dict) and item['source'] == '/proc/1/ns/net' and item['read_only'] for item in main['volumes'])
    assert not any('host-netns' in str(item) for item in companion['volumes'])


def test_macvlan_compose_has_one_isolated_static_ip_and_no_companion():
    compose = yaml.safe_load(generate_docker_agent_compose(**ha_params('macvlan')))
    assert list(compose['services']) == ['agent']
    main = compose['services']['agent']
    assert 'network_mode' not in main and 'ports' not in main
    assert main['networks'] == {'dns-ha': {'ipv4_address': '192.168.50.20'}}
    assert set(main['cap_add']) == {'NET_ADMIN', 'NET_RAW'}
    assert 'CONFIGFLOW_DNS_HA_NETWORK=macvlan' in main['environment']
    network = compose['networks']['dns-ha']
    assert network['driver'] == 'macvlan' and network['driver_opts'] == {'parent': 'eth0'}
    assert network['ipam']['config'] == [{'subnet': '192.168.50.0/24', 'gateway': '192.168.50.1'}]
    assert not any('host-netns' in str(item) for item in main['volumes'])


@pytest.mark.parametrize('function', [generate_docker_agent_compose, generate_docker_agent_run])
@pytest.mark.parametrize('change', [
    {'network_mode': 'bridge'}, {'enable_mihomo': True}, {'enable_mosdns': True}, {'enable_oxidns': False},
    {'agent_ip': ''}, {'agent_ip': '127.0.0.1'}, {'agent_ip': '224.0.0.1'}, {'agent_ip': '0.0.0.0'},
    {'ha_host_id_path': 'relative'}, {'ha_host_id_path': '/tmp/id,readonly'},
])
def test_ha_generator_rejects_incompatible_or_unsafe_inputs(function, change):
    params = ha_params()
    params.update(change)
    with pytest.raises(ValueError):
        function(**params)


@pytest.mark.parametrize('change', [
    {'ha_parent': 'eth0;bad'}, {'ha_subnet': '192.168.50.3/24'}, {'ha_subnet': '192.168.50.20/32'},
    {'ha_gateway': '192.168.60.1'}, {'agent_ip': '192.168.50.0'}, {'agent_ip': '192.168.50.255'},
    {'agent_ip': '192.168.60.20'}, {'agent_ip': '192.168.50.1'},
])
def test_macvlan_generator_rejects_mismatched_addressing(change):
    params = ha_params('macvlan')
    params.update(change)
    with pytest.raises(ValueError):
        generate_docker_agent_compose(**params)


@pytest.mark.parametrize('mode', ['host', 'macvlan'])
def test_generated_run_executes_literal_arguments_and_starts_only_expected_services(tmp_path, mode):
    identity = tmp_path / 'host identity'
    identity.write_text('unique-test-host\n')
    docker = tmp_path / 'docker'
    docker.write_text('#!/usr/bin/env python3\nimport json, os, sys\n'
                      'with open(os.environ["DOCKER_EVENTS"], "a") as output: output.write(json.dumps(sys.argv[1:]) + "\\n")\n')
    docker.chmod(0o755)
    events = tmp_path / 'events'
    marker = tmp_path / 'must-not-execute'
    url = f'https://config.test/"$(touch {marker})'
    params = ha_params(mode)
    params.update(server_url=url, data_dir=str(tmp_path / "data ' folder"), ha_host_id_path=str(identity))
    script = tmp_path / 'install.sh'
    script.write_text(generate_docker_agent_run(**params))
    result = subprocess.run(['bash', str(script)], env={**os.environ, 'PATH': str(tmp_path) + ':' + os.environ['PATH'],
                            'DOCKER_EVENTS': str(events)}, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr + result.stdout
    calls = [json.loads(line) for line in events.read_text().splitlines()]
    assert not marker.exists()
    main = calls[-1]
    assert 'SERVER_URL=' + url in main
    assert f'type=bind,src={identity},dst=/run/configflow-host-machine-id,readonly' in main
    assert len(calls) == 2
    if mode == 'host':
        assert 'SERVICE_TYPE=ha-companion' in calls[0]
        assert '--cap-add' in calls[0] and '--cap-add' not in main
        assert any(value.endswith(':/etc/oxidns:ro') for value in calls[0])
    else:
        assert calls[0][:3] == ['network', 'create', '--driver']
        assert 'SERVICE_TYPE=ha-companion' not in calls[0]
        assert main[main.index('--ip') + 1] == '192.168.50.20'


@pytest.mark.parametrize('mode', ['host', 'bridge', 'existing-private-network'])
@pytest.mark.parametrize('services', [
    {'enable_mihomo': True},
    {'enable_mihomo': False, 'enable_mosdns': True},
    {'enable_mihomo': False, 'enable_oxidns': True},
    {'enable_mihomo': True, 'enable_oxidns': True},
])
def test_ordinary_docker_keeps_original_network_and_privileges(mode, services):
    params = dict(server_url='https://config.example', network_mode=mode, **services)
    implicit = generate_docker_agent_compose(**params)
    assert implicit == generate_docker_agent_compose(**params, enable_dns_ha=False)
    compose = yaml.safe_load(implicit)
    assert list(compose['services']) == ['agent']
    service = compose['services']['agent']
    assert service['network_mode'] == mode
    assert ('ports' in service) == (mode == 'bridge')
    assert 'cap_add' not in service and 'privileged' not in service
    assert 'networks' not in compose
    assert not any('CONFIGFLOW_DNS_HA' in item for item in service['environment'])
    assert not any('configflow-ha' in str(item) or 'machine-id' in str(item) for item in service['volumes'])
    command = generate_docker_agent_run(**params)
    assert command == generate_docker_agent_run(**params, enable_dns_ha=False)
    assert f'--network {mode}' in command
    assert '--cap-add' not in command and 'CONFIGFLOW_DNS_HA' not in command


@pytest.mark.parametrize('services', [
    {'SERVICE_TYPE': 'mihomo'}, {'SERVICE_TYPE': 'mosdns'},
    {'ENABLE_MIHOMO': 'true', 'ENABLE_OXIDNS': 'true'},
    {'ENABLE_MOSDNS': 'true', 'ENABLE_OXIDNS': 'true'},
    {'ENABLE_OXIDNS': 'false'},
])
def test_entrypoint_rejects_ha_for_other_cores_before_creating_service_config(tmp_path, services):
    from backend.test_agent_transaction_install import entrypoint_harness
    entrypoint, env = entrypoint_harness(tmp_path)
    env.update({'SERVICE_TYPE': '', 'ENABLE_MIHOMO': 'false', 'ENABLE_MOSDNS': 'false',
                'ENABLE_OXIDNS': 'false', 'CONFIGFLOW_DNS_HA': '1', **services})
    result = subprocess.run(['sh', str(entrypoint), 'true'], env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert 'dedicated OxiDNS' in result.stderr
    assert not list((tmp_path / 'agent').glob('config-*.json'))


def test_keepalived_program_has_separate_shutdown_and_no_automatic_activation():
    import configparser
    parser = configparser.ConfigParser()
    parser.read(ROOT / 'backend/agents/go-agent/supervisor/configflow-keepalived.conf')
    program = parser['program:configflow-keepalived']
    assert program.getboolean('autostart') is False
    assert program.getboolean('autorestart') is True
    assert program['stopsignal'] == 'TERM'
    assert program.getboolean('stopasgroup') and program.getboolean('killasgroup')
    assert int(program['priority']) > 99


@pytest.mark.parametrize('endpoint', ['compose', 'run'])
def test_authenticated_docker_install_api_preserves_ha_choices(authenticated_app, endpoint):
    client, _, _, admin, _ = authenticated_app
    params = ha_params('host')
    params.update(enable_mihomo='false', enable_oxidns='true', enable_dns_ha='true',
                  ha_host_id_path='/volume1/configflow/host-id')
    response = client.get('/api/agents/docker-agent-' + endpoint, query_string=params, headers=admin)
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert '/volume1/configflow/host-id' in text and 'ha-companion' in text
    params['network_mode'] = 'bridge'
    response = client.get('/api/agents/docker-agent-' + endpoint, query_string=params, headers=admin)
    assert response.status_code == 400
    assert response.get_json()['success'] is False


def test_companion_entrypoint_writes_private_evidence_without_starting_core(tmp_path):
    identity = tmp_path / 'host-id'
    identity.write_text('the-same-stable-host\n')
    ha = tmp_path / 'ha'
    event = tmp_path / 'supervisor-called'
    supervisor = tmp_path / 'supervisord'
    supervisor.write_text('#!/bin/sh\nprintf "%s\\n" "$*" > "$EVENT"\n')
    supervisor.chmod(0o755)
    source = (ROOT / 'docker/docker-ha-companion-entrypoint.sh').read_text()
    source = source.replace('/var/lib/configflow-ha', str(ha)).replace('/run/configflow-host-machine-id', str(identity))
    source = source.replace('/usr/bin/supervisord', str(supervisor))
    script = tmp_path / 'companion.sh'
    script.write_text(source)
    result = subprocess.run(['sh', str(script)], env={**os.environ, 'CONFIGFLOW_DNS_HA': '1',
                            'CONFIGFLOW_DNS_HA_NETWORK': 'host', 'EVENT': str(event)}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    evidence = ha / 'run/companion.json'
    data = json.loads(evidence.read_text())
    assert data['protocol'] == 1 and data['mode'] == 'host'
    assert data['host_id'] == hashlib.sha256(b'the-same-stable-host').hexdigest()
    assert int(data['cap_eff'], 16) >= 0
    for name in ('network_namespace', 'pid_namespace'):
        assert data[name]['device'] > 0 and data[name]['inode'] > 0
    assert evidence.stat().st_mode & 0o777 == 0o600
    assert event.read_text().strip() == '-c /etc/supervisor/ha-companion.conf'
    assert not list(ha.glob('*.conf')) and not list(ha.glob('*ha.json'))


def test_companion_supervisor_socket_cannot_control_core_or_agent():
    import configparser
    parser = configparser.ConfigParser()
    parser.read(ROOT / 'docker/supervisor-ha-companion.conf')
    assert parser['unix_http_server']['chmod'] == '0600'
    assert parser['unix_http_server']['file'] == '/var/lib/configflow-ha/run/supervisor.sock'
    assert parser['include']['files'] == '/etc/supervisor/conf.d/configflow-keepalived.conf'
    assert 'inet_http_server' not in parser
