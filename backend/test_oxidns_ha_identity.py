"""Live boot proof closes identity-source bypasses without owning the HA journal."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from backend.agents import oxidns_ha
from backend.test_agent_upgrade_auth import authenticated_app  # noqa: F401
from backend.test_oxidns_ha import PRIMARY_HOST, SECONDARY_HOST, enable, pair  # noqa: F401
from backend.test_oxidns_ha_docker import prepare

ROOT = Path(__file__).parents[1]


def proof(capabilities, host, source, boot):
    capabilities[host]['oxidns_ha_environment'].update(host_id_source=source, host_boot_id=boot)


@pytest.mark.parametrize('sources,mixed', [
    (('file', 'docker-volume'), True),
    (('docker-volume', 'file'), True),
    (('docker-volume', 'docker-volume'), False),
])
def test_distinct_live_boot_ids_allow_volume_and_mixed_native_pairs(pair, monkeypatch, sources, mixed):
    capabilities = prepare(pair, monkeypatch)
    manager, devices, primary, secondary = pair
    for host, source, digit in zip((PRIMARY_HOST, SECONDARY_HOST), sources, ('c', 'd')):
        proof(capabilities, host, source, digit * 64)
    if mixed:
        native = primary if sources[0] == 'file' else secondary
        manager._update_agents(lambda agents: next(item for item in agents if item['id'] == native['id'])
                               .update(deployment_method='shell'))
        capabilities[manager.get_agent_by_id(native['id'])['host']]['oxidns_ha_environment']['kind'] = 'native'
    enable(manager, primary, secondary)
    assert [call[1] for call in devices.calls if call[0] == 'apply'] == [SECONDARY_HOST, PRIMARY_HOST]


@pytest.mark.parametrize('missing_host', [PRIMARY_HOST, SECONDARY_HOST])
@pytest.mark.parametrize('invalid', [None, '', 'a', 17, 'x' * 64])
def test_volume_requires_both_live_boot_proofs_before_mutation(pair, monkeypatch, missing_host, invalid):
    capabilities = prepare(pair, monkeypatch)
    manager, devices, primary, secondary = pair
    proof(capabilities, PRIMARY_HOST, 'docker-volume', 'c' * 64)
    proof(capabilities, SECONDARY_HOST, 'file', 'd' * 64)
    capabilities[missing_host]['oxidns_ha_environment']['host_boot_id'] = invalid
    with pytest.raises(oxidns_ha.HAError, match='实时启动身份'):
        enable(manager, primary, secondary)
    assert all(call[0] == 'capabilities' for call in devices.calls)


@pytest.mark.parametrize('sources', [('file', 'file'), ('file', 'docker-volume'),
                                     ('docker-volume', 'file'), ('docker-volume', 'docker-volume')])
def test_same_boot_id_rejects_different_ips_and_different_persistent_ids(pair, monkeypatch, sources):
    capabilities = prepare(pair, monkeypatch)
    manager, devices, primary, secondary = pair
    for host, source in zip((PRIMARY_HOST, SECONDARY_HOST), sources):
        proof(capabilities, host, source, 'c' * 64)
    if sources[0] == 'file':
        manager._update_agents(lambda agents: next(item for item in agents if item['id'] == primary['id'])
                               .update(deployment_method='shell'))
        capabilities[PRIMARY_HOST]['oxidns_ha_environment']['kind'] = 'native'
    with pytest.raises(oxidns_ha.HAError, match='同一台宿主机'):
        enable(manager, primary, secondary)
    assert all(call[0] == 'capabilities' for call in devices.calls)


def test_same_boot_proof_also_rejects_two_native_aliases():
    members = [{'id': 'a', 'name': 'shell-a'}, {'id': 'b', 'name': 'shell-b'}]
    capabilities = [{'oxidns_ha_environment': {'kind': 'native', 'host_boot_id': 'c' * 64}}
                    for _ in members]
    with pytest.raises(oxidns_ha.HAError, match='同一台宿主机'):
        oxidns_ha.validate_pair_environments(members, capabilities)


@pytest.mark.parametrize('source', [None, '', 'native', 'volume', 42])
def test_explicit_unknown_identity_source_fails_closed(pair, monkeypatch, source):
    capabilities = prepare(pair, monkeypatch)
    manager, devices, primary, secondary = pair
    capabilities[PRIMARY_HOST]['oxidns_ha_environment']['host_id_source'] = source
    with pytest.raises(oxidns_ha.HAError, match='身份来源无效'):
        enable(manager, primary, secondary)
    assert all(call[0] == 'capabilities' for call in devices.calls)


def test_live_environment_exposes_only_valid_identity_evidence(authenticated_app, monkeypatch):
    client, manager, _, admin, _ = authenticated_app
    agent = manager.register_agent({'name': 'volume-oxidns', 'host': '192.168.10.20', 'port': 8082,
                                    'service_type': 'oxidns', 'deployment_method': 'docker'})
    environment = {'kind': 'container', 'ready': True, 'host_id': 'a' * 64,
                   'host_id_source': 'docker-volume', 'host_boot_id': 'b' * 64,
                   'raw_identity': 'must-not-return', 'blockers': []}
    monkeypatch.setattr(manager, '_deployment_request', lambda *args, **kwargs: {'oxidns_ha_environment': environment})
    endpoint = '/api/agents/' + agent['id'] + '/oxidns-ha-environment'
    result = client.get(endpoint, headers=admin).get_json()['oxidns_ha_environment']
    assert result['host_id_source'] == 'docker-volume' and result['host_boot_id'] == 'b' * 64
    assert 'raw_identity' not in result
    environment.update(host_id_source='unexpected', host_boot_id='not-a-hash')
    result = client.get(endpoint, headers=admin).get_json()['oxidns_ha_environment']
    assert result['host_id_source'] == 'unknown' and result['host_boot_id'] == ''
    del environment['host_id_source']
    assert client.get(endpoint, headers=admin).get_json()['oxidns_ha_environment']['host_id_source'] == 'file'


def companion_fixture(tmp_path):
    identity = tmp_path / 'identity volume'
    identity.mkdir()
    (identity / 'host-id').write_text('the-same-stable-host\n')
    mountinfo = tmp_path / 'mountinfo'
    escaped = str(identity).replace(' ', r'\040')
    mountinfo.write_text(f'1 0 8:1 / {escaped} ro - ext4 /dev/vda rw\n')
    ha, event = tmp_path / 'ha', tmp_path / 'supervisor-called'
    supervisor = tmp_path / 'supervisord'
    supervisor.write_text('#!/bin/sh\nprintf "%s\\n" "$*" > "$EVENT"\n')
    supervisor.chmod(0o755)
    source = (ROOT / 'docker/docker-ha-companion-entrypoint.sh').read_text()
    source = source.replace('/var/lib/configflow-ha', str(ha)).replace('/run/configflow-host-identity', str(identity))
    source = source.replace('/proc/self/mountinfo', str(mountinfo)).replace('/usr/bin/supervisord', str(supervisor))
    script = tmp_path / 'companion.sh'
    script.write_text(source)
    env = {**os.environ, 'CONFIGFLOW_DNS_HA': '1', 'CONFIGFLOW_DNS_HA_NETWORK': 'host',
           'CONFIGFLOW_DNS_HA_IDENTITY': 'volume', 'EVENT': str(event)}
    return script, env, identity, mountinfo, ha, event


def test_companion_accepts_readonly_volume_with_same_evidence_contract(tmp_path):
    script, env, _, _, ha, event = companion_fixture(tmp_path)
    result = subprocess.run(['sh', str(script)], env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    evidence = ha / 'run/companion.json'
    value = json.loads(evidence.read_text())
    assert value['host_id'] == hashlib.sha256(b'the-same-stable-host').hexdigest()
    assert value['protocol'] == 1 and value['mode'] == 'host'
    assert evidence.stat().st_mode & 0o777 == 0o600
    assert event.exists()


@pytest.mark.parametrize('bad', ['unmounted', 'writable', 'missing', 'symlink', 'directory', 'invalid', 'unknown-mode'])
def test_companion_rejects_unproven_volume_before_starting_supervisor(tmp_path, bad):
    script, env, identity, mountinfo, ha, event = companion_fixture(tmp_path)
    path = identity / 'host-id'
    if bad == 'unmounted':
        mountinfo.write_text('')
    elif bad == 'writable':
        mountinfo.write_text(mountinfo.read_text().replace(' ro - ', ' rw - '))
    elif bad == 'missing':
        path.unlink()
    elif bad == 'symlink':
        original = identity / 'real-id'
        path.rename(original)
        path.symlink_to(original)
    elif bad == 'directory':
        path.unlink()
        path.mkdir()
    elif bad == 'invalid':
        path.write_text('bad')
    else:
        env['CONFIGFLOW_DNS_HA_IDENTITY'] = 'unexpected'
    result = subprocess.run(['sh', str(script)], env=env, text=True, capture_output=True)
    assert result.returncode != 0
    assert not event.exists() and not (ha / 'run/companion.json').exists()
