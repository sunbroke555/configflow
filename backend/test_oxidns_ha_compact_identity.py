"""Compact installs prove host independence without a shared writable ID volume."""
import hashlib
import json
import os
import subprocess
import time

import pytest

from backend.agents import oxidns_ha
from backend.test_oxidns_ha import PRIMARY_HOST, SECONDARY_HOST, enable, pair  # noqa: F401
from backend.test_oxidns_ha_docker import prepare
from backend.test_oxidns_ha_identity import companion_fixture, proof


@pytest.mark.parametrize('sources,mixed', [
    (('file', 'installation'), True),
    (('installation', 'file'), True),
    (('installation', 'docker-volume'), False),
    (('installation', 'installation'), False),
])
def test_compact_pairs_require_distinct_live_hosts_and_keep_mixed_native_support(pair, monkeypatch, sources, mixed):
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
@pytest.mark.parametrize('bad', [None, '', 'invalid', 'x' * 64, 32])
def test_compact_missing_boot_evidence_rejects_before_any_mutation(pair, monkeypatch, missing_host, bad):
    capabilities = prepare(pair, monkeypatch)
    manager, devices, primary, secondary = pair
    proof(capabilities, PRIMARY_HOST, 'installation', 'c' * 64)
    proof(capabilities, SECONDARY_HOST, 'file', 'd' * 64)
    capabilities[missing_host]['oxidns_ha_environment']['host_boot_id'] = bad
    with pytest.raises(oxidns_ha.HAError, match='实时启动身份'):
        enable(manager, primary, secondary)
    assert all(call[0] == 'capabilities' for call in devices.calls)


@pytest.mark.parametrize('other_source', ['file', 'docker-volume', 'installation'])
def test_compact_different_persistent_ids_do_not_bypass_same_host_guard(pair, monkeypatch, other_source):
    capabilities = prepare(pair, monkeypatch)
    manager, devices, primary, secondary = pair
    proof(capabilities, PRIMARY_HOST, 'installation', 'c' * 64)
    proof(capabilities, SECONDARY_HOST, other_source, 'c' * 64)
    assert capabilities[PRIMARY_HOST]['oxidns_ha_environment']['host_id'] != capabilities[SECONDARY_HOST]['oxidns_ha_environment']['host_id']
    with pytest.raises(oxidns_ha.HAError, match='同一台宿主机'):
        enable(manager, primary, secondary)
    assert all(call[0] == 'capabilities' for call in devices.calls)


INSTALLATION = '0123456789abcdef0123456789abcdef'
INSTALL_SCOPE = 'f' * 32


def compact_companion_fixture(tmp_path):
    script, env, _, mountinfo, ha, event = companion_fixture(tmp_path)
    agent = tmp_path / 'agent data'
    agent.mkdir()
    core = tmp_path / 'core'
    core.mkdir()
    ha.mkdir()
    source = script.read_text().replace('/opt/configflow-agent', str(agent))
    source = source.replace('/etc/oxidns', str(core))
    source = source.replace('time.monotonic() + 30', 'time.monotonic() + 0.05')
    assert 'time.monotonic() + 0.05' in source
    script.write_text(source)
    escaped = str(agent).replace(' ', r'\040')
    mountinfo.write_text(f'1 0 8:1 / {escaped} ro - ext4 /dev/vda rw\n')
    env['CONFIGFLOW_DNS_HA_IDENTITY'] = 'installation'
    env['INSTALL_SCOPE'] = INSTALL_SCOPE
    write_compact_marker(agent, ready=True)
    write_compact_marker(core, ready=True)
    write_compact_marker(ha, ready=True)
    (agent / '.configflow-ha-identity').write_text(INSTALLATION + '\n')
    return script, env, agent, mountinfo, ha, event


def write_compact_marker(agent, **changes):
    binding = {'session': INSTALL_SCOPE}
    owner = hashlib.sha256(json.dumps(binding, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    value = {'protocol': 2, 'ready': True, 'installation_id': INSTALLATION,
             'binding': binding, 'owner': owner, **changes}
    temp = agent / '.marker-pending'
    temp.write_text(json.dumps(value))
    os.replace(temp, agent / '.configflow-compose-compact.json')


def test_companion_reads_ready_installation_without_identity_volume(tmp_path):
    script, env, agent, _, ha, event = compact_companion_fixture(tmp_path)
    before = {p.name: p.read_bytes() for p in agent.iterdir()}
    result = subprocess.run(['sh', str(script)], env=env, text=True, capture_output=True, timeout=3)
    assert result.returncode == 0, result.stderr
    evidence = json.loads((ha / 'run/companion.json').read_text())
    assert evidence['host_id'] == hashlib.sha256(INSTALLATION.encode()).hexdigest()
    assert evidence['protocol'] == 1 and evidence['mode'] == 'host'
    assert {p.name: p.read_bytes() for p in agent.iterdir()} == before
    assert event.exists()


@pytest.mark.parametrize('bad', ['unmounted', 'writable', 'tmpfs', 'missing-marker', 'not-ready', 'marker-link',
                                 'broken-marker', 'oversize-marker', 'old-protocol', 'wrong-id', 'id-link',
                                 'missing-id', 'id-directory', 'invalid-id', 'no-scope', 'invalid-scope',
                                 'foreign-scope', 'missing-binding', 'invalid-binding', 'invalid-owner', 'changed-binding'])
def test_companion_refuses_unready_or_unproven_installation_without_touching_ha(tmp_path, bad):
    script, env, agent, mountinfo, ha, event = compact_companion_fixture(tmp_path)
    marker = agent / '.configflow-compose-compact.json'
    identity = agent / '.configflow-ha-identity'
    if bad == 'unmounted':
        mountinfo.write_text('')
    elif bad == 'writable':
        mountinfo.write_text(mountinfo.read_text().replace(' ro - ', ' rw - '))
    elif bad == 'tmpfs':
        mountinfo.write_text(mountinfo.read_text().replace(' - ext4 ', ' - tmpfs '))
    elif bad == 'missing-marker':
        marker.unlink()
    elif bad == 'not-ready':
        write_compact_marker(agent, ready=False)
    elif bad == 'marker-link':
        marker.rename(agent / 'real-marker')
        marker.symlink_to(agent / 'real-marker')
    elif bad == 'broken-marker':
        marker.write_text('{')
    elif bad == 'oversize-marker':
        marker.write_text(' ' * 65537)
    elif bad == 'old-protocol':
        write_compact_marker(agent, protocol=1)
    elif bad == 'wrong-id':
        write_compact_marker(agent, installation_id='f' * 32)
    elif bad == 'id-link':
        identity.rename(agent / 'real-identity')
        identity.symlink_to(agent / 'real-identity')
    elif bad == 'missing-id':
        identity.unlink()
    elif bad == 'id-directory':
        identity.unlink()
        identity.mkdir()
    elif bad == 'no-scope':
        env.pop('INSTALL_SCOPE')
    elif bad == 'invalid-scope':
        env['INSTALL_SCOPE'] = 'unexpected'
    elif bad == 'foreign-scope':
        env['INSTALL_SCOPE'] = 'a' * 32
    elif bad == 'missing-binding':
        value = json.loads(marker.read_text())
        del value['binding']
        marker.write_text(json.dumps(value))
    elif bad == 'invalid-binding':
        write_compact_marker(agent, binding='not-a-binding')
    elif bad == 'invalid-owner':
        write_compact_marker(agent, owner='invalid')
    elif bad == 'changed-binding':
        write_compact_marker(agent, binding={'session': INSTALL_SCOPE, 'network': 'host'})
    else:
        identity.write_text('invalid')
    result = subprocess.run(['sh', str(script)], env=env, text=True, capture_output=True, timeout=3)
    assert result.returncode != 0
    assert not event.exists() and not (ha / 'run').exists()


@pytest.mark.parametrize('directory_name', ['core', 'ha'])
@pytest.mark.parametrize('bad', ['foreign-owner', 'wrong-id', 'missing', 'invalid-json', 'old-protocol', 'symlink'])
def test_companion_checks_every_mapped_directory_before_writing(tmp_path, directory_name, bad):
    script, env, _, _, ha, event = compact_companion_fixture(tmp_path)
    directory = tmp_path / directory_name
    marker = directory / '.configflow-compose-compact.json'
    if bad == 'foreign-owner':
        write_compact_marker(directory, owner='a' * 64)
    elif bad == 'wrong-id':
        write_compact_marker(directory, installation_id='b' * 32)
    elif bad == 'missing':
        marker.unlink()
    elif bad == 'invalid-json':
        marker.write_text('{')
    elif bad == 'old-protocol':
        write_compact_marker(directory, protocol=1)
    else:
        marker.rename(directory / 'actual-marker')
        marker.symlink_to(directory / 'actual-marker')
    before = {p.relative_to(ha): p.read_bytes() for p in ha.rglob('*') if p.is_file()}
    result = subprocess.run(['sh', str(script)], env=env, text=True, capture_output=True, timeout=3)
    assert result.returncode != 0
    assert not event.exists() and not (ha / 'run').exists()
    assert {p.relative_to(ha): p.read_bytes() for p in ha.rglob('*') if p.is_file()} == before


def test_companion_waits_for_atomic_ready_before_writing_ha_directory(tmp_path):
    script, env, agent, _, ha, event = compact_companion_fixture(tmp_path)
    # Restore a real bounded initialization window for this actual process test.
    script.write_text(script.read_text().replace('time.monotonic() + 0.05', 'time.monotonic() + 2'))
    marker = agent / '.configflow-compose-compact.json'
    marker.unlink()
    process = subprocess.Popen(['sh', str(script)], env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        time.sleep(.15)
        assert process.poll() is None and not (ha / 'run').exists()
        write_compact_marker(agent, ready=False)
        time.sleep(.15)
        assert process.poll() is None and not (ha / 'run').exists()
        write_compact_marker(agent, ready=True)
        _, stderr = process.communicate(timeout=3)
        assert process.returncode == 0, stderr
        assert event.exists() and (ha / 'run/companion.json').exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
