"""Standalone core installation: real archives and checksum checks, isolated init/files."""
import gzip
import hashlib
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile

import pytest
import yaml

from backend.agents.core_install_script import generate_core_install_script


def harness(tmp_path, manager="systemd", **options):
    root = tmp_path / "root"
    commands = tmp_path / "commands"
    commands.mkdir()
    for directory in ("run", "etc/systemd/system", "etc/init.d", "usr/local/bin", "sbin", "proc/net"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    if manager == "systemd":
        (root / "run/systemd/system").mkdir(parents=True)
    else:
        (root / "sbin/openrc-run").write_text("#!/bin/sh\n")
        (root / "sbin/openrc-run").chmod(0o755)
    for network in ("tcp", "udp", "tcp6", "udp6"):
        (root / f"proc/net/{network}").write_text("sl local_address rem_address st\n")
    events = tmp_path / "events"
    events.touch()
    fake_core = f'''#!{sys.executable}
import os, sys
from pathlib import Path
name = 'mihomo' if '-t' in sys.argv else 'oxidns'
with open(os.environ['EVENTS'], 'a') as log: log.write('validate ' + name + '\\n')
config = Path(sys.argv[sys.argv.index('-f' if name == 'mihomo' else '-c') + 1])
assert config.is_file()
if os.environ.get('FAIL_VALIDATE') == name: raise SystemExit(29)
'''.encode()
    mihomo = gzip.compress(fake_core)
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
        member = tarfile.TarInfo("oxidns")
        member.size = len(fake_core)
        bundle.addfile(member, io.BytesIO(fake_core))
    oxidns = archive.getvalue()
    (tmp_path / "mihomo.gz").write_bytes(mihomo)
    (tmp_path / "oxidns.tar.gz").write_bytes(oxidns)
    source = generate_core_install_script()
    # Use real checksums against locally generated test archives.
    source = re.sub(r"MIHOMO_SHA=[0-9a-f]{64}", "MIHOMO_SHA=" + hashlib.sha256(mihomo).hexdigest(), source)
    source = re.sub(r"OXIDNS_SHA=[0-9a-f]{64}", "OXIDNS_SHA=" + hashlib.sha256(oxidns).hexdigest(), source)
    source = re.sub(r"(?<![A-Za-z0-9_])/(etc|run|usr|lib|sbin|proc)/", lambda m: str(root / m[1]) + "/", source)
    source = source.replace("in /run /etc ", f"in {root}/run {root}/etc ")
    source = source.replace("/tmp/configflow-cores.", str(tmp_path / "stage."))
    installer = tmp_path / "install.sh"
    installer.write_text(source)
    command_bodies = {
        "id": "print(os.environ.get('FAKE_UID', '0'))",
        "uname": "print('Linux' if sys.argv[1] == '-s' else os.environ.get('ARCH', 'x86_64'))",
        "sleep": "pass",
        "curl": """import shutil
with open(os.environ['EVENTS'], 'a') as log: log.write('download ' + sys.argv[-1] + '\\n')
if os.environ.get('FAIL_DOWNLOAD'): raise SystemExit(22)
source = 'mihomo.gz' if 'MetaCubeX' in sys.argv[-1] else 'oxidns.tar.gz'
shutil.copyfile(os.path.join(os.environ['FIXTURE'], source), sys.argv[sys.argv.index('--output') + 1])
if os.environ.get('BAD_CHECKSUM') == source:
    with open(sys.argv[sys.argv.index('--output') + 1], 'ab') as file: file.write(b'bad')
""",
        "sha256sum": """import hashlib
if sys.argv[1] == '-c':
    for line in open(sys.argv[2]):
        digest, file = line.rstrip().split('  ', 1)
        if hashlib.sha256(open(file, 'rb').read()).hexdigest() != digest:
            print(file + ': FAILED'); raise SystemExit(1)
        print(file + ': OK')
else:
    for file in sys.argv[1:]: print(hashlib.sha256(open(file, 'rb').read()).hexdigest() + '  ' + file)
""",
    }
    service_body = """name = os.path.basename(sys.argv[0])
args = sys.argv[1:]
with open(os.environ['EVENTS'], 'a') as log: log.write(name + ' ' + ' '.join(args) + '\\n')
if args[0] == 'show': print('not-found'); raise SystemExit(0)
action = args[0] if name != 'rc-service' else args[1]
service = args[-1] if name == 'systemctl' else (args[0] if name == 'rc-service' else args[1])
if os.environ.get('FAIL_SERVICE') == action + ':' + service or os.environ.get('FAIL_ROLLBACK') == action + ':' + service:
    if name == 'rc-service' and action == 'start':
        with open(os.path.join(os.environ['ROOT'], 'etc', service, service + '.log'), 'w') as log:
            log.write('core startup error: fixture failure\\n')
    raise SystemExit(38)
"""
    for name in ("systemctl",) if manager == "systemd" else ("rc-service", "rc-update"):
        command_bodies[name] = service_body
    command_bodies["dig"] = """with open(os.environ['EVENTS'], 'a') as log: log.write('dig ' + ' '.join(sys.argv[1:]) + '\\n')
if os.environ.get('FAIL_DIG') == 'exit': raise SystemExit(9)
if os.environ.get('FAIL_DIG') == 'servfail':
    print(';; ->>HEADER<<- opcode: QUERY, status: SERVFAIL, id: 17')
    print(';; flags: qr rd ra; QUERY: 1, ANSWER: 0, AUTHORITY: 0, ADDITIONAL: 0')
else:
    print(';; ->>HEADER<<- opcode: QUERY, status: NOERROR, id: 17')
    print(';; flags: qr rd ra; QUERY: 1, ANSWER: 1, AUTHORITY: 0, ADDITIONAL: 0')
"""
    if options.pop("NO_DIG", False):
        del command_bodies["dig"]
    for name, body in command_bodies.items():
        executable = commands / name
        executable.write_text(f"#!{sys.executable}\nimport os, sys\n" + body + "\n")
        executable.chmod(0o755)
    for name in ("awk", "cat", "chmod", "cp", "gzip", "mkdir", "mktemp", "mv", "rm", "rmdir", "sed", "sleep", "tail", "tar", "sh"):
        if not (commands / name).exists():
            (commands / name).symlink_to(shutil.which(name))
    env = dict(os.environ, PATH=str(commands), EVENTS=str(events), FIXTURE=str(tmp_path), ROOT=str(root), **options)
    return installer, env, root, events


def run(installer, env):
    return subprocess.run(["sh", str(installer)], env=env, text=True, capture_output=True, timeout=30)


def assert_uninstalled(root):
    for service in ("mihomo", "oxidns"):
        assert not (root / f"usr/local/bin/{service}").exists()
        assert not (root / f"etc/{service}").exists()
        assert not (root / f"etc/systemd/system/{service}.service").exists()
        assert not (root / f"etc/init.d/{service}").exists()
    assert not (root / "run/configflow-core-install.lock").exists()


def test_generated_script_syntax_and_bootstrap_config(tmp_path):
    source = generate_core_install_script()
    script = tmp_path / "script.sh"
    script.write_text(source)
    assert subprocess.run(["sh", "-n", str(script)], capture_output=True).returncode == 0
    mihomo = yaml.safe_load(source.split("<<'MIHOMO_CONFIG'\n", 1)[1].split("\nMIHOMO_CONFIG", 1)[0])
    oxidns = yaml.safe_load(source.split("<<'OXIDNS_CONFIG'\n", 1)[1].split("\nOXIDNS_CONFIG", 1)[0])
    assert mihomo["tun"]["enable"] is False
    assert mihomo["dns"]["listen"] == "0.0.0.0:1053"
    assert mihomo["dns"]["nameserver"] == ["223.5.5.5", "119.29.29.29"]
    assert all(mihomo[key] == 0 for key in ("port", "socks-port", "mixed-port", "redir-port", "tproxy-port"))
    assert oxidns["plugins"][0]["args"]["upstreams"] == [{"addr": "udp://127.0.0.1:1053"}]
    assert "/opt/configflow-agent" not in source
    assert "sysctl" not in source and "iptables" not in source


@pytest.mark.parametrize("manager", ["systemd", "openrc"])
def test_install_two_cores_and_preserve_configuration_on_second_run(tmp_path, manager):
    installer, env, root, events = harness(tmp_path, manager)
    result = run(installer, env)
    assert result.returncode == 0, result.stdout + result.stderr
    initial_events = events.read_text()
    assert initial_events.index("validate oxidns") < initial_events.index("enable mihomo" if manager == "systemd" else "add mihomo")
    assert initial_events.count("dig ") == 4
    for service in ("mihomo", "oxidns"):
        assert os.access(root / f"usr/local/bin/{service}", os.X_OK)
        assert (root / f"etc/{service}/.configflow-core-install").is_file()
    config = root / "etc/mihomo/config.yaml"
    config.write_text("# user's published configuration\n")
    result = run(installer, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "跳过" in result.stdout
    assert events.read_text() == initial_events
    assert config.read_text() == "# user's published configuration\n"
    assert not (root / "run/configflow-core-install.lock").exists()


@pytest.mark.parametrize("manager", ["systemd", "openrc"])
@pytest.mark.parametrize("failure", ["start:oxidns", "enable:oxidns"])
def test_service_failure_rolls_back_both_cores(tmp_path, manager, failure):
    if manager == "openrc" and failure.startswith("enable:"):
        failure = failure.replace("enable:", "add:")
    installer, env, root, events = harness(tmp_path, manager, FAIL_SERVICE=failure)
    result = run(installer, env)
    assert result.returncode != 0
    assert_uninstalled(root)
    log = events.read_text()
    assert ("systemctl stop mihomo" if manager == "systemd" else "rc-service mihomo stop") in log
    assert ("systemctl disable oxidns" if manager == "systemd" else "rc-update del oxidns default") in log


@pytest.mark.parametrize("options", [{"BAD_CHECKSUM": "mihomo.gz"}, {"BAD_CHECKSUM": "oxidns.tar.gz"}, {"FAIL_VALIDATE": "mihomo"}, {"FAIL_VALIDATE": "oxidns"}, {"FAIL_DOWNLOAD": "1"}])
def test_failed_download_checksum_or_validation_writes_no_cores(tmp_path, options):
    installer, env, root, events = harness(tmp_path, **options)
    result = run(installer, env)
    assert result.returncode != 0
    assert_uninstalled(root)
    assert " enable " not in events.read_text()
    assert " start " not in events.read_text()
    assert not list(tmp_path.glob("stage.*"))


@pytest.mark.parametrize("failure", ["exit", "servfail"])
def test_dns_failure_even_zero_exit_servfail_rolls_back(tmp_path, failure):
    installer, env, root, events = harness(tmp_path, FAIL_DIG=failure)
    result = run(installer, env)
    assert result.returncode != 0
    assert "DNS 查询" in result.stderr
    assert "systemctl stop oxidns" in events.read_text()
    assert_uninstalled(root)


def test_preserve_existing_configuration_and_refuse_install(tmp_path):
    installer, env, root, events = harness(tmp_path)
    path = root / "etc/oxidns/config.yaml"
    path.parent.mkdir()
    path.write_text("original config")
    result = run(installer, env)
    assert result.returncode != 0
    assert "已有核心、配置或服务" in result.stderr
    assert path.read_text() == "original config"
    assert "download " not in events.read_text()
    assert not (root / "usr/local/bin/mihomo").exists()


@pytest.mark.parametrize("network,port,state", [("udp", "0035", "07"), ("tcp", "041D", "0A"), ("udp6", "041D", "07"), ("tcp6", "0035", "0A")])
def test_port_conflict_prevents_download_and_mutation(tmp_path, network, port, state):
    installer, env, root, events = harness(tmp_path)
    with (root / f"proc/net/{network}").open("a") as file:
        file.write(f"0: 00000000:{port} 00000000:0000 {state}\n")
    result = run(installer, env)
    assert result.returncode != 0
    assert "端口被占用" in result.stderr
    assert "download " not in events.read_text()
    assert_uninstalled(root)


@pytest.mark.parametrize("options", [{"FAKE_UID": "1000"}, {"ARCH": "riscv64"}])
def test_unsupported_environment_writes_nothing(tmp_path, options):
    installer, env, root, events = harness(tmp_path, **options)
    result = run(installer, env)
    assert result.returncode != 0
    assert events.read_text() == ""
    assert_uninstalled(root)


def test_concurrent_install_keeps_other_process_lock(tmp_path):
    installer, env, root, events = harness(tmp_path)
    lock = root / "run/configflow-core-install.lock"
    lock.mkdir()
    (lock / "pid").write_text("123456")
    result = run(installer, env)
    assert result.returncode != 0
    assert "另一个核心安装" in result.stderr
    assert (lock / "pid").read_text() == "123456"
    assert events.read_text() == ""


@pytest.mark.parametrize("manager", ["systemd", "openrc"])
def test_failed_service_stop_preserves_manageable_core_files(tmp_path, manager):
    installer, env, root, events = harness(tmp_path, manager, FAIL_SERVICE="start:oxidns", FAIL_ROLLBACK="stop:mihomo")
    result = run(installer, env)
    assert result.returncode != 0
    assert "回滚未完成" in result.stderr
    assert (root / "usr/local/bin/mihomo").exists()
    assert (root / "etc/mihomo/config.yaml").exists()
    unit = "etc/systemd/system/mihomo.service" if manager == "systemd" else "etc/init.d/mihomo"
    assert (root / unit).exists()
    assert not (root / "etc/mihomo/.configflow-core-install").exists()
    assert not (root / "run/configflow-core-install.lock").exists()


def test_no_dig_reports_unverified_dns_without_rejecting_install(tmp_path):
    installer, env, root, events = harness(tmp_path, NO_DIG=True)
    result = run(installer, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "实际 DNS 解析尚未验证" in result.stdout
    assert "UDP/TCP DNS 查询均已通过" not in result.stdout
    assert (root / "etc/mihomo/.configflow-core-install").exists()


@pytest.mark.parametrize("target", ["usr/local/bin/mihomo", "etc/systemd/system/oxidns.service"])
def test_changed_binary_or_service_is_preserved_and_not_reinstalled(tmp_path, target):
    installer, env, root, events = harness(tmp_path)
    assert run(installer, env).returncode == 0
    path = root / target
    path.write_text("custom replacement")
    initial_events = events.read_text()
    result = run(installer, env)
    assert result.returncode != 0
    assert path.read_text() == "custom replacement"
    assert events.read_text() == initial_events


@pytest.mark.parametrize("arch,mihomo_asset,oxidns_asset", [
    ("aarch64", "mihomo-linux-arm64-v1.19.32.gz", "oxidns-aarch64-unknown-linux-musl.tar.gz"),
    ("armv7l", "mihomo-linux-armv7-v1.19.32.gz", "oxidns-armv7-unknown-linux-musleabihf.tar.gz"),
])
def test_architecture_selects_matching_official_assets(tmp_path, arch, mihomo_asset, oxidns_asset):
    installer, env, root, events = harness(tmp_path, ARCH=arch)
    result = run(installer, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert mihomo_asset in events.read_text()
    assert oxidns_asset in events.read_text()


def test_openrc_start_failure_shows_core_log_before_cleanup(tmp_path):
    installer, env, root, events = harness(tmp_path, "openrc", FAIL_SERVICE="start:oxidns")
    result = run(installer, env)
    assert result.returncode != 0
    assert "core startup error: fixture failure" in result.stderr
    assert_uninstalled(root)
