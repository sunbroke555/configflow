"""Opt-in core phase for native AIO: API constraints and isolated runtime ordering."""
import json
from pathlib import Path
import subprocess

from flask import Flask
import pytest

from backend.agents.core_install_script import generate_core_install_script
from backend.agents.go_install_script import AGENT_CORE_INSTALL_INTERFACE, generate_go_aio_install_script
from backend.routes.agents import get_install_script
from backend import test_agent_native_aio_install as aio


@pytest.fixture
def client():
    app = Flask(__name__)
    app.add_url_rule('/api/agents/install-script', view_func=get_install_script)
    return app.test_client()


def test_combined_aio_api_embeds_canonical_core_phase_after_choice_and_architecture(client, tmp_path):
    response = client.get('/api/agents/install-script', query_string={
        'type': 'aio', 'name': 'gateway', 'install_core': 'true',
        'mihomo_agent_port': '18080', 'oxidns_agent_port': '18082',
        'agent_ip': '192.0.2.3', 'server_url': 'https://config.test',
    })
    assert response.status_code == 200
    source = response.get_data(as_text=True)
    assert source.splitlines()[1] == AGENT_CORE_INSTALL_INTERFACE
    # The installer persists forwarding itself, so its core phase says so.
    assert generate_core_install_script(installer_forwarding=True).rstrip() in source
    assert '旁路网关修复：' not in source
    assert '(\nset -- --core both\n' in source
    assert source.index('case "$choice"') < source.index('case "$(uname -m)"') < source.index('set -- --core both')
    assert source.index('CORE_PHASE_COMPLETED=true') < source.index('PHYSICAL_AGENT_ROOT=')
    assert source.index('CORE_PHASE_COMPLETED=true') < source.index('mkdir -p "$AGENT_ROOT"')
    cfgs = aio.configs(source)
    for service, port in (('mihomo', 18080), ('oxidns', 18082)):
        cfg = cfgs[service]
        assert cfg['agent_name'] == f'gateway-{service}'
        assert cfg['agent_port'] == port
        assert cfg['agent_ip'] == '192.0.2.3'
        assert cfg['server_url'] == 'https://config.test'
        assert cfg['native_aio'] is True
    script = tmp_path / 'install.sh'
    script.write_text(source)
    result = subprocess.run(['sh', '-n', str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('options', [
    {'agent_type': 'shell'}, {'agent_type': 'unknown'},
    {'install_core': 'yes'}, {'install_core': 'TRUE'},
    {'mihomo_config_path': '/srv/mihomo/config.yaml'},
    {'oxidns_config_path': '/srv/oxidns/config.yaml'},
    {'mihomo_service_unit': 'custom'}, {'oxidns_service_unit': 'custom'},
    {'mihomo_service_binary': '/usr/bin/custom'}, {'oxidns_service_binary': '/usr/bin/custom'},
    {'mihomo_agent_port': '53'}, {'mihomo_agent_port': '1053'},
    {'oxidns_agent_port': '53'}, {'oxidns_agent_port': '1053'},
    {'mihomo_agent_port': '8082'}, {'oxidns_agent_port': 'invalid'},
])
def test_invalid_combined_settings_rejected_before_loading_core(client, monkeypatch, options):
    def unexpected(**_):
        pytest.fail('Invalid settings must be rejected before loading the core installer')
    monkeypatch.setattr('backend.agents.core_install_script.generate_core_install_script', unexpected)
    response = client.get('/api/agents/install-script', query_string={
        'type': 'aio', 'name': 'gateway', 'install_core': 'true', **options})
    assert response.status_code == 400, response.get_data(as_text=True)
    assert response.json['success'] is False


def test_default_unit_and_binary_aliases_are_accepted():
    source = generate_go_aio_install_script(
        'https://config.test', 'gateway', install_core=True,
        mihomo_service_unit='mihomo.service', oxidns_service_unit='oxidns.service',
        mihomo_service_binary='/usr/local/bin/mihomo', oxidns_service_binary='/usr/local/bin/oxidns')
    assert source.splitlines()[1] == AGENT_CORE_INSTALL_INTERFACE
    with pytest.raises(ValueError, match='true 或 false'):
        generate_go_aio_install_script('https://config.test', 'gateway', install_core='false')


FAKE_CORE = '''#!/bin/sh
set -eu
printf 'core %s\\n' "$*" >> "$EVENTS"
[ "$1" = --core ]
[ "$2" = both ]
if [ "${FAIL_CORE:-}" = yes ]; then false; fi
printf 'complete\\n' > "$CORE_COMPLETED"
# The outer Agent phase must not inherit core variables or traps.
AGENT_ROOT=/must-not-leak
INIT_SYSTEM=must-not-leak
STAGE_DIR=/must-not-delete
trap 'printf "core cleanup\\n" >> "$EVENTS"' 0
exit 0
'''


def harness(tmp_path, monkeypatch, manager='systemd', existing=True, real_core=False):
    if not real_core:
        monkeypatch.setattr('backend.agents.core_install_script.generate_core_install_script', lambda **_: FAKE_CORE)
    monkeypatch.setattr(aio, 'generated', lambda: generate_go_aio_install_script(
        'https://config.test', 'cohost', binary_download_url='https://config.test/download', install_core=True))
    installer, env, units = aio.harness(tmp_path, manager, existing)
    source = installer.read_text()
    for old, new in (
        ('/run/', str(tmp_path / 'run') + '/'),
        ('/sbin/openrc-run', str(tmp_path / 'sbin/openrc-run')),
        ('/usr/lib/', str(tmp_path / 'usr/lib') + '/'),
        ('/lib/systemd/', str(tmp_path / 'lib/systemd') + '/'),
    ):
        source = source.replace(old, new)
    source = source.replace('in /run /etc /usr/local ', f'in {tmp_path}/run {tmp_path}/etc {tmp_path}/usr-local ')
    installer.write_text(source)
    (tmp_path / 'run').mkdir()
    (tmp_path / 'usr-local').mkdir()
    (tmp_path / 'local-bin').mkdir()
    (tmp_path / 'sbin').mkdir()
    if manager == 'systemd':
        (tmp_path / 'run/systemd/system').mkdir(parents=True)
    else:
        init = tmp_path / 'sbin/openrc-run'
        init.write_text('#!/bin/sh\n')
        init.chmod(0o755)
    # Make both manager command sets available: the running init decides.
    commands = tmp_path / 'commands'
    manager_mock = '''#!/bin/sh
set -eu
printf '%s %s\\n' "$(basename "$0")" "$*" >> "$EVENTS"
case "$*" in *--property=LoadState*) printf '%s\\n' "${CORE_LOAD_STATE:-loaded}" ;; esac
'''
    for name in ('systemctl', 'rc-service', 'rc-update'):
        command = commands / name
        command.write_text(manager_mock)
        command.chmod(0o755)
    (commands / 'uname').write_text('#!/bin/sh\ncase "$1" in -s) echo Linux ;; *) echo "${TEST_ARCH:-x86_64}" ;; esac\n')
    (commands / 'wget').write_text('''#!/bin/sh
set -eu
echo agent-download >> "$EVENTS"
[ "${FAIL_AGENT_DOWNLOAD:-}" != yes ] || exit 49
cp "$FAKE_AGENT" "$3"
''')
    (tmp_path / 'events').touch()
    env['CORE_COMPLETED'] = str(tmp_path / 'core-completed')
    return installer, env, units


@pytest.mark.parametrize('manager', ['systemd', 'openrc'])
@pytest.mark.parametrize('existing', [True, False])
def test_core_phase_precedes_agent_with_isolated_state_and_identity(tmp_path, monkeypatch, manager, existing):
    installer, env, units = harness(tmp_path, monkeypatch, manager, existing)
    result = aio.run(installer, env)
    assert result.returncode == 0, result.stdout + result.stderr
    log = (tmp_path / 'events').read_text()
    assert log.index('core --core both') < log.index('core cleanup') < log.index('agent-download')
    unexpected_manager = 'rc-service' if manager == 'systemd' else 'systemctl'
    assert unexpected_manager not in log
    for service in ('mihomo', 'oxidns'):
        cfg = json.loads((tmp_path / f'agent/{service}/config.json').read_text())
        assert cfg['service_manager'] == manager
        assert cfg['agent_id'] == (f'id-{service}' if existing else '')
        assert cfg['token'] == (f'token-{service}' if existing else '')
        assert (tmp_path / f'etc/{service}/config.yaml').read_text() == '# unchanged core config\n'
    assert not list((tmp_path / 'agent').glob('.aio-install.*'))


def test_new_combined_install_does_not_repeat_dns_setup_instructions(tmp_path, monkeypatch):
    installer, env, _ = harness(tmp_path, monkeypatch, existing=False)
    source = installer.read_text().replace(
        '# The outer Agent phase must not inherit core variables or traps.',
        'cf_fact core_mihomo_new yes\ncf_fact core_oxidns_new yes\n'
        '# The outer Agent phase must not inherit core variables or traps.')
    installer.write_text(source)
    result = aio.run(installer, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Mihomo Agent：端口 8080，服务已启动' in result.stdout
    assert 'OxiDNS Agent：端口 8082，服务已启动' in result.stdout
    assert '请核对 DNS 链路' not in result.stdout
    assert '将 DNS 调整到 1053' not in result.stdout
    assert '随后发布 OxiDNS 配置' not in result.stdout


@pytest.mark.parametrize('manager', ['systemd', 'openrc'])
@pytest.mark.parametrize('choice', ['2', '3'])
def test_uninstall_and_cancel_skip_core_phase(tmp_path, monkeypatch, manager, choice):
    installer, env, units = harness(tmp_path, monkeypatch, manager)
    originals = {path: path.read_bytes() for path in units.iterdir() if not path.name.startswith('configflow-agent')}
    result = aio.run(installer, env, choice)
    assert result.returncode == 0, result.stdout + result.stderr
    log = (tmp_path / 'events').read_text()
    assert 'core --core' not in log and 'agent-download' not in log
    for path, content in originals.items():
        assert path.read_bytes() == content


def test_core_failure_stops_before_agent_changes(tmp_path, monkeypatch):
    installer, env, _ = harness(tmp_path, monkeypatch)
    originals = {path: path.read_bytes() for path in (tmp_path / 'agent').rglob('*') if path.is_file()}
    env['FAIL_CORE'] = 'yes'
    result = aio.run(installer, env)
    assert result.returncode != 0
    log = (tmp_path / 'events').read_text()
    assert log == 'core --core both\n'
    assert not Path(env['CORE_COMPLETED']).exists()
    assert '核心予以保留' not in result.stderr
    for path, content in originals.items():
        assert path.read_bytes() == content


@pytest.mark.parametrize('failure', ['FAIL_AGENT_DOWNLOAD', 'FAIL_ENVIRONMENT'])
def test_agent_failure_keeps_completed_core_phase(tmp_path, monkeypatch, failure):
    installer, env, units = harness(tmp_path, monkeypatch)
    originals = {path: path.read_bytes() for path in (tmp_path / 'agent').rglob('*') if path.is_file()}
    env[failure] = 'yes'
    result = aio.run(installer, env)
    assert result.returncode != 0
    assert Path(env['CORE_COMPLETED']).read_text() == 'complete\n'
    assert '核心予以保留' in result.stderr
    assert 'stop' not in (tmp_path / 'events').read_text()
    assert not list((tmp_path / 'agent').glob('.aio-install.*'))
    for path, content in originals.items():
        assert path.read_bytes() == content


@pytest.mark.parametrize('manager', ['systemd', 'openrc'])
def test_late_agent_failure_restarts_previous_agents_without_changing_cores(tmp_path, monkeypatch, manager):
    installer, env, units = harness(tmp_path, monkeypatch, manager)
    originals = {path: path.read_bytes() for path in (tmp_path / 'agent').rglob('*') if path.is_file()}
    env['FAIL_JOURNAL_AFTER_STOP'] = 'oxidns'
    result = aio.run(installer, env)
    assert result.returncode == 47, result.stderr
    assert '核心予以保留' in result.stderr
    log = (tmp_path / 'events').read_text().splitlines()
    for service in ('mihomo', 'oxidns'):
        stop = f'systemctl stop configflow-agent-{service}' if manager == 'systemd' else f'rc-service configflow-agent-{service} stop'
        start = f'systemctl start configflow-agent-{service}' if manager == 'systemd' else f'rc-service configflow-agent-{service} start'
        assert log.index(stop) < log.index(start)
        assert (tmp_path / f'etc/{service}/config.yaml').read_text() == '# unchanged core config\n'
    for path, content in originals.items():
        assert path.read_bytes() == content


def test_unsupported_architecture_stops_before_core(tmp_path, monkeypatch):
    installer, env, _ = harness(tmp_path, monkeypatch, existing=False)
    env['TEST_ARCH'] = 'unknown'
    result = aio.run(installer, env)
    assert result.returncode != 0
    assert (tmp_path / 'events').read_text() == ''
    assert not (tmp_path / 'agent').exists()


@pytest.mark.parametrize('manager', ['systemd', 'openrc'])
def test_real_existing_core_skip_logic_preserves_both_cores(tmp_path, monkeypatch, manager):
    installer, env, units = harness(tmp_path, monkeypatch, manager, real_core=True)
    originals = {path: path.read_bytes() for path in [
        *(tmp_path / 'etc').rglob('config.yaml'),
        tmp_path / 'commands/mihomo', tmp_path / 'commands/oxidns',
        units / ('mihomo.service' if manager == 'systemd' else 'mihomo'),
        units / ('oxidns.service' if manager == 'systemd' else 'oxidns'),
    ]}
    result = aio.run(installer, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Mihomo：已有部署，已保留' in result.stdout
    assert 'OxiDNS：已有部署，已保留' in result.stdout
    details = aio.detail_output(result)
    assert '跳过已有 mihomo' in details and '跳过已有 oxidns' in details
    for path, content in originals.items():
        assert path.read_bytes() == content
    log = (tmp_path / 'events').read_text().splitlines()
    assert 'agent-download' in log
    assert not any(line in (f'systemctl stop {service}', f'rc-service {service} stop')
                   for service in ('mihomo', 'oxidns') for line in log)


def test_new_core_is_ready_before_agent_core_checks(tmp_path, monkeypatch):
    installer, env, units = harness(tmp_path, monkeypatch, existing=False)
    # Simulate the canonical phase installing the missing OxiDNS peer. The
    # existing core installer's own tests cover downloads and rollback.
    config = tmp_path / 'etc/oxidns/config.yaml'
    binary = tmp_path / 'commands/oxidns'
    unit = units / 'oxidns.service'
    for path in (config, binary, unit):
        path.unlink()
    original_mihomo = (tmp_path / 'etc/mihomo/config.yaml').read_bytes()
    env.update(NEW_CONFIG=str(config), NEW_BINARY=str(binary), NEW_UNIT=str(unit))
    source = installer.read_text().replace(
        'printf \'complete\\n\' > "$CORE_COMPLETED"',
        'printf \'# newly installed peer\\n\' > "$NEW_CONFIG"\n'
        'printf \'#!/bin/sh\\nexit 0\\n\' > "$NEW_BINARY"\nchmod 755 "$NEW_BINARY"\n'
        'printf \'# newly installed service\\n\' > "$NEW_UNIT"\n'
        'printf \'complete\\n\' > "$CORE_COMPLETED"', 1)
    installer.write_text(source)
    result = aio.run(installer, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert config.read_text() == '# newly installed peer\n'
    assert (tmp_path / 'etc/mihomo/config.yaml').read_bytes() == original_mihomo
    assert (tmp_path / 'agent/oxidns/config.json').is_file()


def test_missing_agent_parent_is_created_after_core_completion(tmp_path, monkeypatch):
    installer, env, _ = harness(tmp_path, monkeypatch, existing=False)
    parent = tmp_path / 'new-opt'
    source = installer.read_text().replace(str(tmp_path / 'agent'), str(parent / 'configflow-agent'))
    installer.write_text(source)
    result = aio.run(installer, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert Path(env['CORE_COMPLETED']).exists()
    assert (parent / 'configflow-agent/mihomo/config.json').is_file()
    assert (parent / 'configflow-agent/oxidns/config.json').is_file()
