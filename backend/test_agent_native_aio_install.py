"""Native cohost installer checks using isolated files and mocked services."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from backend.agents.go_install_script import generate_go_agent_install_script, generate_go_aio_install_script


def generated(**kwargs):
    return generate_go_aio_install_script(
        "https://config.test", "cohost", binary_download_url="https://config.test/download", **kwargs)


def configs(script):
    return {service: json.loads(script.split(f"<<'CONFIGFLOW_{service.upper()}_JSON_EOF'\n", 1)[1]
                               .split(f"\nCONFIGFLOW_{service.upper()}_JSON_EOF", 1)[0])
            for service in ("mihomo", "oxidns")}


@pytest.mark.parametrize("kwargs", [
    {"mihomo_agent_port": 0}, {"oxidns_agent_port": 65536}, {"mihomo_agent_port": True},
    {"mihomo_agent_port": 8080.5}, {"oxidns_agent_port": 8080},
    {"mihomo_config_path": "relative/config.yaml"}, {"mihomo_config_path": "/"},
    {"mihomo_config_path": "/etc/../oxidns/config.yaml"},
    {"mihomo_config_path": "/etc/oxidns/config.yaml"},
    {"mihomo_config_path": "/etc/oxidns/other.yaml"},
    {"mihomo_service_unit": "oxidns.service"}, {"mihomo_service_unit": "configflow-agent"},
    {"mihomo_config_path": "/opt/configflow-agent/mihomo/core.yaml"},
    {"oxidns_config_path": "//opt/configflow-agent/oxidns/core.yaml"},
    {"mihomo_service_binary": "/opt/configflow-agent/mihomo/core"},
    {"oxidns_service_binary": "/usr/../opt/configflow-agent/oxidns/core"},
])
def test_native_aio_rejects_conflicting_or_invalid_options(kwargs):
    with pytest.raises(ValueError):
        generated(**kwargs)


def test_native_aio_config_and_quoted_values(tmp_path):
    literal = '/etc/mihomo/custom ${HOME} "quoted".yaml'
    script = generated(mihomo_config_path=literal, mihomo_service_unit="mihomo-custom.service")
    cfgs = configs(script)
    assert cfgs["mihomo"]["config_path"] == literal
    assert cfgs["mihomo"]["service_unit"] == "mihomo-custom"
    for service, port in (("mihomo", 8080), ("oxidns", 8082)):
        cfg = cfgs[service]
        assert cfg["agent_port"] == port
        assert cfg["agent_name"] == f"cohost-{service}"
        assert cfg["native_aio"] is True
        assert cfg["agent_service_unit"] == f"configflow-agent-{service}"
        assert cfg["service_manager"] == "__CONFIGFLOW_INIT__"
    path = tmp_path / "install.sh"
    path.write_text(script)
    result = subprocess.run(["sh", "-n", str(path)], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_native_aio_allows_core_paths_with_similar_directory_prefix():
    cfg = configs(generated(mihomo_config_path="/opt/configflow-agent-core/mihomo/config.yaml",
                            mihomo_service_binary="/opt/configflow-agent-core/mihomo/core"))["mihomo"]
    assert cfg["config_path"] == "/opt/configflow-agent-core/mihomo/config.yaml"


def harness(tmp_path, manager="systemd", existing=True):
    source = generated()
    if sys.platform == "darwin":
        source = source.replace("sed -i ", "sed -i '' ")
    for old, new in (
        ("/etc/", str(tmp_path / "etc") + "/"),
        ("/usr/local/bin", str(tmp_path / "local-bin")),
        ("/opt/configflow-agent", str(tmp_path / "agent")),
        ("/var/log", str(tmp_path / "logs")),
        ("/dev/tty", "/dev/stdin"),
    ):
        source = source.replace(old, new)
    installer = tmp_path / "install.sh"
    installer.write_text(source)
    commands = tmp_path / "commands"
    commands.mkdir()
    events = tmp_path / "events"
    agent = tmp_path / "fake-agent"
    agent.write_text(f'''#!{sys.executable}
import json, os, sys
args = sys.argv[1:]
if args == ['-h']:
    raise SystemExit(0)
cfg = json.load(open(args[args.index('-config') + 1]))
with open(os.environ['EVENTS'], 'a') as log:
    log.write('agent ' + cfg['service_type'] + ' ' + args[-1] + ' ' + args[1] + '\\n')
if '-check-environment' in args:
    if os.environ.get('VERBOSE_ENVIRONMENT') == 'yes':
        print('verbose-environment-detail-for-log-only')
    assert '-environment-peer-config' in args, 'AIO must inspect both services together'
    peer_path = args[args.index('-environment-peer-config') + 1]
    peer = json.load(open(peer_path))
    assert peer['service_type'] == 'oxidns'
    assert peer['service_manager'] in ('systemd', 'openrc')
    assert cfg['service_manager'] == peer['service_manager']
    assert cfg['agent_port'] != peer['agent_port']
    previous = open(os.environ['EVENTS']).read()
    assert 'agent mihomo -validate-service' in previous
    assert 'agent oxidns -validate-service' in previous
    with open(os.environ['EVENTS'], 'a') as log:
        log.write('environment peer oxidns ' + peer_path + '\\n')
    if os.environ.get('FAIL_ENVIRONMENT') == 'yes':
        print('IPv4 转发未开启；修复建议：确认网关用途后配置持久化转发。', file=sys.stderr)
        raise SystemExit(48)
if cfg['service_type'] == os.environ.get('FAIL_VALIDATION') and '-validate-service' in args:
    raise SystemExit(46)
if cfg['service_type'] == os.environ.get('FAIL_JOURNAL') and '-require-clean-deployments' in args:
    raise SystemExit(47)
if cfg['service_type'] == os.environ.get('FAIL_JOURNAL_AFTER_STOP') and '-require-clean-deployments' in args:
    if 'stop' in open(os.environ['EVENTS']).read():
        raise SystemExit(47)
''')
    agent.chmod(0o755)
    manager_mock = '''printf '%s %s\n' "$(basename "$0")" "$*" >> "$EVENTS"
case "$*" in
    *--property=LoadState*) printf 'loaded\n' ;;
esac
'''
    mocks = {"id": "printf '0\\n'\n", "uname": "printf 'x86_64\\n'\n",
             "wget": 'cp "$FAKE_AGENT" "$3"\n', "mihomo": "exit 0\n", "oxidns": "exit 0\n"}
    if manager == "systemd":
        mocks["systemctl"] = manager_mock
        units = tmp_path / "etc/systemd/system"
    else:
        mocks["rc-service"] = manager_mock
        mocks["rc-update"] = manager_mock
        units = tmp_path / "etc/init.d"
    units.mkdir(parents=True)
    for name, body in mocks.items():
        executable = commands / name
        executable.write_text("#!/bin/sh\nset -eu\n" + body)
        executable.chmod(0o755)
    cfgs = configs(source)
    for service in ("mihomo", "oxidns"):
        core = Path(cfgs[service]["config_path"])
        core.parent.mkdir(parents=True)
        core.write_text("# unchanged core config\n")
        core_unit = units / (service + (".service" if manager == "systemd" else ""))
        core_unit.write_text("# original core unit\n")
        if existing:
            directory = tmp_path / "agent" / service
            directory.mkdir(parents=True)
            cfg = dict(cfgs[service], agent_id=f"id-{service}", token=f"token-{service}")
            (directory / "config.json").write_text(json.dumps(cfg))
            binary = directory / "configflow-agent"
            binary.write_bytes(agent.read_bytes())
            binary.chmod(0o755)
            (directory / "keep-state").write_text(service)
            (units / (f"configflow-agent-{service}" + (".service" if manager == "systemd" else ""))).write_text("# old Agent unit\n")
    env = dict(os.environ, PATH=str(commands) + os.pathsep + os.environ["PATH"],
               EVENTS=str(events), FAKE_AGENT=str(agent), CONFIGFLOW_INSTALL_LOG_DIR=str(tmp_path))
    return installer, env, units


def run(installer, env, choice="1"):
    return subprocess.run(["sh", str(installer)], input=choice + "\n", env=env, text=True, capture_output=True)


def detail_output(result):
    path = next(line.removeprefix("详细日志：") for line in result.stdout.splitlines()
                if line.startswith("详细日志："))
    return Path(path).read_text()


@pytest.mark.parametrize("manager", ["systemd", "openrc"])
@pytest.mark.parametrize("failure", ["FAIL_VALIDATION", "FAIL_JOURNAL"])
def test_second_core_preflight_failure_leaves_both_agents_untouched(tmp_path, manager, failure):
    installer, env, units = harness(tmp_path, manager)
    before = {path: path.read_bytes() for path in (tmp_path / "agent").rglob("*") if path.is_file()}
    unit_before = {path: path.read_bytes() for path in units.iterdir() if path.is_file()}
    env[failure] = "oxidns"
    result = run(installer, env)
    assert result.returncode in (46, 47), result.stderr
    for path, data in {**before, **unit_before}.items():
        assert path.read_bytes() == data
    events = (tmp_path / "events").read_text().splitlines()
    assert any("agent mihomo -validate-service" in line for line in events)
    assert not any("stop" in line or "start" in line or "daemon-reload" in line for line in events)
    assert not list((tmp_path / "agent").glob(".aio-install.*"))


@pytest.mark.parametrize("manager", ["systemd", "openrc"])
def test_native_aio_environment_failure_preserves_both_agents_and_prints_guidance(tmp_path, manager):
    installer, env, units = harness(tmp_path, manager)
    before = {path: path.read_bytes() for path in (tmp_path / "agent").rglob("*") if path.is_file()}
    unit_before = {path: path.read_bytes() for path in units.iterdir() if path.is_file()}
    env["FAIL_ENVIRONMENT"] = "yes"
    result = run(installer, env)
    assert result.returncode == 48, result.stderr
    assert "IPv4 转发" in result.stderr and "修复建议" in result.stderr
    assert "未知 -check-environment 参数" in result.stderr
    assert "1.8.19-go 或更高" in result.stderr
    for path, data in {**before, **unit_before}.items():
        assert path.read_bytes() == data
    for service in ("mihomo", "oxidns"):
        assert (tmp_path / "etc" / service / "config.yaml").read_text() == "# unchanged core config\n"
    events = (tmp_path / "events").read_text().splitlines()
    checks = [i for i, line in enumerate(events) if " -check-environment " in line]
    assert len(checks) == 1
    for service in ("mihomo", "oxidns"):
        validation = next(i for i, line in enumerate(events) if f"agent {service} -validate-service" in line)
        assert validation < checks[0]
    assert sum(line.startswith("environment peer oxidns ") for line in events) == 1
    assert not any("stop" in line or "start" in line or "daemon-reload" in line for line in events)
    assert not list((tmp_path / "agent").glob(".aio-install.*"))


@pytest.mark.parametrize("manager", ["systemd", "openrc"])
@pytest.mark.parametrize("existing", [False, True])
def test_native_aio_install_isolated_identity_and_repeatable(tmp_path, manager, existing):
    installer, env, units = harness(tmp_path, manager, existing)
    for attempt in range(2):
        (tmp_path / "events").write_text("")
        result = run(installer, env)
        assert result.returncode == 0, result.stderr
        for service in ("mihomo", "oxidns"):
            directory = tmp_path / "agent" / service
            cfg = json.loads((directory / "config.json").read_text())
            assert cfg["service_manager"] == manager
            assert cfg["agent_id"] == (f"id-{service}" if existing else "")
            assert cfg["token"] == (f"token-{service}" if existing else "")
            assert cfg["agent_service_unit"] == f"configflow-agent-{service}"
            assert (directory / "configflow-agent").is_file()
            assert Path(cfg["config_path"]).read_text() == "# unchanged core config\n"
            if existing:
                assert (directory / "keep-state").read_text() == service
        events = (tmp_path / "events").read_text().splitlines()
        # Look only at the latest attempt; both preflights precede either stop.
        end_checks = [i for i, line in enumerate(events) if "agent oxidns -require-clean-deployments" in line and ".aio-install." in line]
        stops = [i for i, line in enumerate(events) if "stop" in line and "configflow-agent" in line]
        environment_checks = [i for i, line in enumerate(events) if " -check-environment " in line]
        assert len(environment_checks) == 1
        assert sum(line.startswith("environment peer oxidns ") for line in events) == 1
        for service in ("mihomo", "oxidns"):
            validation = next(i for i, line in enumerate(events) if f"agent {service} -validate-service" in line)
            assert validation < environment_checks[0]
        if existing or attempt:
            assert end_checks[0] < stops[0]
            assert end_checks[-1] > stops[-1]
            assert environment_checks[0] < stops[0]
        assert not any(line in {f"systemctl stop {service}", f"rc-service {service} stop"}
                       for service in ("mihomo", "oxidns") for line in events)
        assert not (tmp_path / "agent/config-mihomo.json").exists()
        assert not (tmp_path / "local-bin/configflow-agent").exists()


def test_native_aio_summary_preserves_existing_core_dns_guidance_without_verbose_output(tmp_path):
    installer, env, _ = harness(tmp_path)
    env["VERBOSE_ENVIRONMENT"] = "yes"
    result = run(installer, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "请输入选项 [1-3]: " in result.stdout
    assert "检查安装环境与依赖" in result.stdout
    assert "检查核心接管与下载 Agent" in result.stdout
    assert "检查运行环境" in result.stdout
    assert "安装并启动 Agent 服务" in result.stdout
    assert "Mihomo Agent：端口 8080，服务已启动" in result.stdout
    assert "OxiDNS Agent：端口 8082，服务已启动" in result.stdout
    assert "已有核心配置已保留；发布也会保留自定义 DNS 端口和 dns-hijack" in result.stdout
    assert "先在 ConfigFlow 发布" not in result.stdout
    assert "安装完成" in result.stdout and "详细日志：" in result.stdout
    assert "verbose-environment-detail-for-log-only" not in result.stdout + result.stderr
    assert "verbose-environment-detail-for-log-only" in detail_output(result)


@pytest.mark.parametrize("manager", ["systemd", "openrc"])
def test_native_aio_uninstall_preserves_other_directories_and_cores(tmp_path, manager):
    installer, env, units = harness(tmp_path, manager)
    sibling = tmp_path / "agent/unrelated/keep"
    sibling.parent.mkdir()
    sibling.write_text("keep")
    result = run(installer, env, "2")
    assert result.returncode == 0, result.stderr
    assert sibling.read_text() == "keep"
    for service in ("mihomo", "oxidns"):
        assert not (tmp_path / "agent" / service).exists()
        assert (tmp_path / "etc" / service / "config.yaml").read_text() == "# unchanged core config\n"
        assert (units / (service + (".service" if manager == "systemd" else ""))).read_text() == "# original core unit\n"
    events = (tmp_path / "events").read_text()
    assert "recover-and-exit" not in events
    assert "-check-environment" not in events
    assert events.index("agent oxidns -require-clean-deployments") < events.index("stop")


def test_native_aio_uninstall_blocks_pending_second_core(tmp_path):
    installer, env, _ = harness(tmp_path)
    env["FAIL_JOURNAL"] = "oxidns"
    result = run(installer, env, "2")
    assert result.returncode == 47
    assert (tmp_path / "agent/mihomo/config.json").exists()
    assert (tmp_path / "agent/oxidns/config.json").exists()
    assert "stop" not in (tmp_path / "events").read_text()


@pytest.mark.parametrize("manager", ["systemd", "openrc"])
@pytest.mark.parametrize("choice", ["1", "2"])
def test_racing_publication_restarts_original_agents_without_replacing_files(tmp_path, manager, choice):
    installer, env, units = harness(tmp_path, manager)
    before = {path: path.read_bytes() for path in (tmp_path / "agent").rglob("*") if path.is_file()}
    unit_before = {path: path.read_bytes() for path in units.iterdir() if path.is_file()}
    env["FAIL_JOURNAL_AFTER_STOP"] = "oxidns"
    result = run(installer, env, choice)
    assert result.returncode == 47, result.stderr
    for path, data in {**before, **unit_before}.items():
        assert path.read_bytes() == data
    events = (tmp_path / "events").read_text().splitlines()
    for service in ("mihomo", "oxidns"):
        stop = f"systemctl stop configflow-agent-{service}" if manager == "systemd" else f"rc-service configflow-agent-{service} stop"
        start = f"systemctl start configflow-agent-{service}" if manager == "systemd" else f"rc-service configflow-agent-{service} start"
        assert events.index(stop) < events.index(start)


def test_physical_core_directory_alias_is_rejected_before_download(tmp_path):
    installer, env, _ = harness(tmp_path, existing=False)
    source = installer.read_text().replace(str(tmp_path / "etc/oxidns/config.yaml"), str(tmp_path / "etc/alias/oxidns.yaml"))
    installer.write_text(source)
    (tmp_path / "etc/mihomo/oxidns.yaml").write_text("# independent file but shared journal root\n")
    (tmp_path / "etc/alias").symlink_to(tmp_path / "etc/mihomo", target_is_directory=True)
    result = run(installer, env)
    assert result.returncode == 1
    assert "实际位于同一目录" in result.stderr
    assert not (tmp_path / "agent").exists()


@pytest.mark.parametrize("kind", ["config_file", "config_directory", "binary"])
def test_core_location_inside_agent_directory_is_rejected_through_symlink(tmp_path, kind):
    installer, env, _ = harness(tmp_path, existing=False)
    protected = tmp_path / "agent/mihomo"
    protected.mkdir(parents=True)
    if kind == "binary":
        target = protected / "mihomo-core"
        target.write_text("#!/bin/sh\nexit 0\n")
        target.chmod(0o755)
        link = tmp_path / "commands/mihomo"
        link.unlink()
        link.symlink_to(target)
    else:
        target = protected / "config.yaml"
        target.write_text("# core must survive\n")
        link = tmp_path / "etc/mihomo/config.yaml"
        link.unlink()
        if kind == "config_file":
            link.symlink_to(target)
        else:
            link.parent.rmdir()
            link.parent.symlink_to(protected, target_is_directory=True)
    before = target.read_bytes()
    result = run(installer, env)
    assert result.returncode == 1
    assert "不能放在 Agent 目录内" in result.stderr
    assert target.read_bytes() == before
    assert not (tmp_path / "events").exists()


def test_native_aio_refuses_legacy_before_changes(tmp_path):
    installer, env, _ = harness(tmp_path, existing=False)
    legacy = tmp_path / "agent/config-mihomo.json"
    legacy.parent.mkdir()
    legacy.write_text('{"agent_id":"legacy"}')
    result = run(installer, env)
    assert result.returncode == 1
    assert "旧版共享 Agent" in result.stderr
    assert legacy.read_text() == '{"agent_id":"legacy"}'
    assert not (tmp_path / "events").exists()


@pytest.mark.parametrize("choice", ["1", "2"])
def test_single_installer_refuses_aio_install_and_uninstall(tmp_path, choice):
    installer, env, _ = harness(tmp_path)
    script = generate_go_agent_install_script("https://config.test", "single", "mihomo")
    script = script.replace("/opt/configflow-agent", str(tmp_path / "agent"))
    script = script.replace("/dev/tty", "/dev/stdin")
    installer.write_text(script)
    original = (tmp_path / "agent/oxidns/config.json").read_bytes()
    result = run(installer, env, choice)
    assert result.returncode == 1
    assert "同机" in result.stderr
    assert (tmp_path / "agent/oxidns/config.json").read_bytes() == original
    assert not (tmp_path / "events").exists()
