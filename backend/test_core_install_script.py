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
    for directory in ("run", "etc/systemd/system", "etc/init.d", "usr/local/bin", "sbin", "proc/net", "proc/self", "proc/sys/net/ipv4", "dev/net", "sys/class/net"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    if manager == "systemd":
        (root / "run/systemd/system").mkdir(parents=True)
    else:
        (root / "sbin/openrc-run").write_text("#!/bin/sh\n")
        (root / "sbin/openrc-run").chmod(0o755)
    for network in ("tcp", "udp", "tcp6", "udp6"):
        (root / f"proc/net/{network}").write_text("sl local_address rem_address st\n")
    tun_device = options.pop("TUN_DEVICE", "ready")
    if tun_device == "regular":
        (root / "dev/net/tun").write_text("not a device")
    elif tun_device != "missing":
        (root / "dev/net/tun").symlink_to("/dev/tty" if tun_device == "denied" else "/dev/null")
    cap_status = options.pop("CAP_STATUS", "CapEff:\t0000000000001000\nCapBnd:\t0000000000001000\n")
    if cap_status is not None:
        (root / "proc/self/status").write_text(cap_status)
    forwarding = options.pop("FORWARDING", "1")
    if forwarding is not None:
        (root / "proc/sys/net/ipv4/ip_forward").write_text(forwarding + "\n")
    if options.pop("EXISTING_TUN_INTERFACE", False):
        (root / "sys/class/net/configflow-tun").mkdir()
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
    source = re.sub(r"(?<![A-Za-z0-9_])/(etc|run|usr|lib|sbin|proc|dev|sys)/", lambda m: str(root / m[1]) + "/", source)
    source = source.replace("in /run /etc ", f"in {root}/run {root}/etc ")
    source = source.replace("/tmp/configflow-cores.", str(tmp_path / "stage."))
    installer = tmp_path / "install.sh"
    installer.write_text(source)
    command_bodies = {
        "id": "print(os.environ.get('FAKE_UID', '0'))",
        "uname": "print('Linux' if sys.argv[1] == '-s' else os.environ.get('ARCH', 'x86_64'))",
        "sleep": """from pathlib import Path
if os.environ.get('LATE_TUN_READY') and sys.argv[1] == '1':
    root = Path(os.environ['ROOT'])
    counter = root / 'run/tun-wait-count'
    count = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(count))
    if count >= int(os.environ['LATE_TUN_READY']):
        interface = root / 'sys/class/net/configflow-tun'
        interface.mkdir(parents=True, exist_ok=True)
        (interface / 'tun_flags').write_text('0x1001')
        (interface / 'flags').write_text('0x1001')
        fdinfo = root / 'proc/12345/fdinfo'
        fdinfo.mkdir(parents=True, exist_ok=True)
        (fdinfo / '8').write_text('iff: configflow-tun')
""",
        "stat": "print(os.environ.get('DEVICE_NUMBER', 'a:c8'))",
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
if args[0] == 'show': print('12345' if '--property=MainPID' in args else 'not-found'); raise SystemExit(0)
action = args[0] if name != 'rc-service' else args[1]
service = args[-1] if name == 'systemctl' else (args[0] if name == 'rc-service' else args[1])
if os.environ.get('FAIL_SERVICE') == action + ':' + service or os.environ.get('FAIL_ROLLBACK') == action + ':' + service:
    if name == 'rc-service' and action == 'start':
        with open(os.path.join(os.environ['ROOT'], 'etc', service, service + '.log'), 'w') as log:
            log.write('core startup error: fixture failure\\n')
    raise SystemExit(38)
if name != 'rc-update' and service == 'mihomo':
    from pathlib import Path
    import shutil
    root = Path(os.environ['ROOT'])
    interface = root / 'sys/class/net/configflow-tun'
    fdinfo = root / 'proc/12345/fdinfo'
    if action == 'start':
        counter = root / 'run/mihomo-start-count'
        count = int(counter.read_text()) + 1 if counter.exists() else 1
        counter.write_text(str(count))
        if count > 1 and os.environ.get('FAIL_FALLBACK_START'): raise SystemExit(38)
        (root / 'run/mihomo.pid').write_text('12345')
        enabled = 'tun:\\n  enable: true\\n' in (root / 'etc/mihomo/config.yaml').read_text()
        if enabled and not os.environ.get('NO_TUN_INTERFACE') and not os.environ.get('LATE_TUN_READY'):
            interface.mkdir(parents=True, exist_ok=True)
            (interface / 'tun_flags').write_text(os.environ.get('TUN_FLAGS', '0x1001') + '\\n')
            (interface / 'flags').write_text(os.environ.get('TUN_LINK_FLAGS', '0x1001') + '\\n')
            fdinfo.mkdir(parents=True, exist_ok=True)
            (fdinfo / '8').write_text('iff:\\t' + os.environ.get('TUN_OWNER_DEVICE', 'configflow-tun') + '\\n')
    elif action == 'stop':
        shutil.rmtree(interface, ignore_errors=True)
        shutil.rmtree(fdinfo, ignore_errors=True)
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
    if options.pop("NO_STAT", False):
        del command_bodies["stat"]
    if options.pop("NO_DIG", False):
        del command_bodies["dig"]
    for name in ("sysctl", "ip", "iptables", "nft", "modprobe", "pct"):
        command_bodies[name] = "with open(os.environ['EVENTS'], 'a') as log: log.write('FORBIDDEN ' + os.path.basename(sys.argv[0]) + '\\n')\nraise SystemExit(99)"
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
    return subprocess.run(["sh", str(installer)], env=env, text=True, capture_output=True, timeout=30, start_new_session=True)


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
    mihomo = yaml.safe_load(source.split("<<MIHOMO_CONFIG\n", 1)[1].split("\nMIHOMO_CONFIG", 1)[0])
    oxidns = yaml.safe_load(source.split("<<'OXIDNS_CONFIG'\n", 1)[1].split("\nOXIDNS_CONFIG", 1)[0])
    assert mihomo["tun"]["enable"] == "$TUN_ENABLED"
    assert mihomo["tun"]["dns-hijack"] == ["any:1053", "tcp://any:1053"]
    assert mihomo["tun"]["auto-route"] is True
    assert mihomo["tun"]["auto-redirect"] is False
    assert mihomo["dns"]["listen"] == "0.0.0.0:1053"
    assert mihomo["dns"]["nameserver"] == ["223.5.5.5", "119.29.29.29"]
    assert all(mihomo[key] == 0 for key in ("port", "socks-port", "mixed-port", "redir-port", "tproxy-port"))
    assert oxidns["plugins"][0]["args"]["upstreams"] == [{"addr": "udp://127.0.0.1:1053"}]
    assert "/opt/configflow-agent" not in source
    assert "iptables" not in source
    assert "sysctl -w net.ipv4.ip_forward=1" in source  # Printed repair instructions only.


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


@pytest.mark.parametrize("manager", ["systemd", "openrc"])
@pytest.mark.parametrize("forwarding", ["1", "0", None])
def test_tun_ready_auto_enables_without_changing_forwarding(tmp_path, manager, forwarding):
    installer, env, root, events = harness(tmp_path, manager, FORWARDING=forwarding)
    result = run(installer, env)
    assert result.returncode == 0, result.stdout + result.stderr
    config = yaml.safe_load((root / "etc/mihomo/config.yaml").read_text())
    assert config["tun"] == {
        "enable": True, "device": "configflow-tun", "stack": "mixed",
        "auto-route": True, "auto-detect-interface": True, "auto-redirect": False,
        "dns-hijack": ["any:1053", "tcp://any:1053"],
    }
    assert config["dns"]["enable"] is True
    assert config["rules"] == ["MATCH,DIRECT"]
    assert config["proxies"] == []
    assert "已确认 Mihomo 创建并持有" in result.stdout
    assert "安装结果：tun.enable=true" in result.stdout
    assert "自动调整本机路由" in result.stdout
    assert "FORBIDDEN" not in events.read_text()
    forward_file = root / "proc/sys/net/ipv4/ip_forward"
    if forwarding is None:
        assert not forward_file.exists()
        assert "IPv4 转发：状态无法读取" in result.stdout
    else:
        assert forward_file.read_text() == forwarding + "\n"
        assert "当前为 " + forwarding in result.stdout
    if forwarding != "1":
        assert "sysctl -w net.ipv4.ip_forward=1" in result.stdout
        assert "99-configflow-forward.conf" in result.stdout
        assert "仅做二层网桥" in result.stdout
    assert not (root / "etc/sysctl.d").exists()


@pytest.mark.parametrize("options,reason", [
    ({"TUN_DEVICE": "missing"}, "未提供"),
    ({"TUN_DEVICE": "regular"}, "不是字符设备"),
    ({"TUN_DEVICE": "denied"}, "无法读写打开"),
    ({"DEVICE_NUMBER": "a:3"}, "无法确认"),
    ({"DEVICE_NUMBER": ""}, "无法确认"),
    ({"NO_STAT": True}, "缺少 stat"),
    ({"CAP_STATUS": None}, "无法读取或解析"),
    ({"CAP_STATUS": "CapEff:\t1000\n"}, "无法读取或解析"),
    ({"CAP_STATUS": "CapEff:\tXXXX\nCapBnd:\t1000\n"}, "无法读取或解析"),
    ({"CAP_STATUS": "CapEff:\t10000000000000000\nCapBnd:\t1000\n"}, "无法读取或解析"),
    ({"CAP_STATUS": "CapEff:\t0\nCapBnd:\t1000\n"}, "缺少有效 CAP_NET_ADMIN"),
    ({"CAP_STATUS": "CapEff:\t1000\nCapBnd:\t0\n"}, "能力上限 CapBnd"),
    ({"EXISTING_TUN_INTERFACE": True}, "接口 configflow-tun 已存在"),
])
def test_unavailable_tun_installs_dns_and_prints_repair_steps(tmp_path, options, reason):
    installer, env, root, events = harness(tmp_path, **options)
    result = run(installer, env)
    assert result.returncode == 0, result.stdout + result.stderr
    config = yaml.safe_load((root / "etc/mihomo/config.yaml").read_text())
    assert config["tun"]["enable"] is False
    assert config["dns"]["enable"] is True
    assert reason in result.stdout
    assert "PVE 宿主机" in result.stdout
    assert "pct set <CTID> --devN" in result.stdout
    assert "面板的 Mihomo 基础配置中设置 tun.enable: true" in result.stdout
    assert "首次从面板发布前" in result.stdout
    assert "发布以面板配置为准" in result.stdout
    assert "安装结果：tun.enable=false" in result.stdout
    assert events.read_text().count("dig ") == 4
    assert "FORBIDDEN" not in events.read_text()
    assert "已确认 Mihomo 创建并持有" not in result.stdout
    assert (root / "etc/oxidns/.configflow-core-install").exists()


@pytest.mark.parametrize("cap_status", [
    "CapEff:\t00000000FFFFFFFF\nCapBnd:\t00000000FFFFFFFF\n",
    "CapEff:\t1000\nCapBnd:\t1000\n",
    "CapEff:\t000000000000B000\nCapBnd:\t000000000000D000\n",
])
def test_capability_probe_supports_uppercase_and_32bit_masks(tmp_path, cap_status):
    installer, env, root, events = harness(tmp_path, CAP_STATUS=cap_status)
    result = run(installer, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert yaml.safe_load((root / "etc/mihomo/config.yaml").read_text())["tun"]["enable"] is True


@pytest.mark.parametrize("manager", ["systemd", "openrc"])
@pytest.mark.parametrize("options", [
    {"NO_TUN_INTERFACE": "1"},
    {"TUN_FLAGS": "0x1002"},  # TAP is not TUN.
    {"TUN_LINK_FLAGS": "0x1000"},  # Existing TUN never came UP.
    {"TUN_OWNER_DEVICE": "another-tun"},  # Device exists but belongs elsewhere.
])
def test_runtime_tun_failure_falls_back_to_dns_with_guidance(tmp_path, manager, options):
    installer, env, root, events = harness(tmp_path, manager, **options)
    result = run(installer, env)
    assert result.returncode == 0, result.stdout + result.stderr
    config = yaml.safe_load((root / "etc/mihomo/config.yaml").read_text())
    assert config["tun"]["enable"] is False
    assert config["dns"]["enable"] is True
    assert config["dns"]["listen"] == "0.0.0.0:1053"
    assert "将关闭本次初始配置的 TUN，保留 DNS 服务安装" in result.stdout
    assert "服务管理器权限或容器设备策略" in result.stdout
    assert "安装结果：tun.enable=false" in result.stdout
    log = events.read_text()
    stop = "systemctl stop mihomo" if manager == "systemd" else "rc-service mihomo stop"
    start = "systemctl start mihomo" if manager == "systemd" else "rc-service mihomo start"
    assert log.count(start) == 2
    assert log.count(stop) == 1
    assert log.index(start) < log.index(stop) < log.rindex(start) < log.index("dig ")
    assert log.count("dig ") == 4
    assert "FORBIDDEN" not in log
    for service in ("mihomo", "oxidns"):
        assert (root / f"etc/{service}/.configflow-core-install").exists()


@pytest.mark.parametrize("manager", ["systemd", "openrc"])
def test_runtime_fallback_stop_failure_preserves_existing_running_config(tmp_path, manager):
    installer, env, root, events = harness(tmp_path, manager, NO_TUN_INTERFACE="1", FAIL_ROLLBACK="stop:mihomo")
    result = run(installer, env)
    assert result.returncode != 0
    assert "关闭 TUN 前停止 Mihomo 失败" in result.stderr
    assert "回滚未完成" in result.stderr
    assert yaml.safe_load((root / "etc/mihomo/config.yaml").read_text())["tun"]["enable"] is True
    assert (root / "usr/local/bin/mihomo").exists()
    assert not (root / "etc/mihomo/.configflow-core-install").exists()
    assert "dig " not in events.read_text()


@pytest.mark.parametrize("manager", ["systemd", "openrc"])
def test_runtime_fallback_start_failure_rolls_back_installation(tmp_path, manager):
    installer, env, root, events = harness(tmp_path, manager, NO_TUN_INTERFACE="1", FAIL_FALLBACK_START="1")
    result = run(installer, env)
    assert result.returncode != 0
    assert "关闭 TUN 后 Mihomo 启动失败" in result.stderr
    assert_uninstalled(root)


def test_reinstall_does_not_enable_tun_after_device_is_fixed(tmp_path):
    installer, env, root, events = harness(tmp_path, TUN_DEVICE="missing")
    assert run(installer, env).returncode == 0
    config = root / "etc/mihomo/config.yaml"
    old_config = config.read_text()
    old_events = events.read_text()
    (root / "dev/net/tun").symlink_to("/dev/null")
    result = run(installer, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "跳过" in result.stdout
    assert config.read_text() == old_config
    assert yaml.safe_load(old_config)["tun"]["enable"] is False
    assert events.read_text() == old_events


@pytest.mark.parametrize("manager", ["systemd", "openrc"])
def test_slow_tun_start_gets_time_to_initialize(tmp_path, manager):
    installer, env, root, events = harness(tmp_path, manager, LATE_TUN_READY="3")
    result = run(installer, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert yaml.safe_load((root / "etc/mihomo/config.yaml").read_text())["tun"]["enable"] is True
    assert (root / "run/tun-wait-count").read_text() == "3"
    assert "已确认 Mihomo 创建并持有" in result.stdout
    assert "保留 DNS 服务安装" not in result.stdout


def test_tun_wait_is_bounded_before_dns_fallback(tmp_path):
    installer, env, root, events = harness(tmp_path, LATE_TUN_READY="99")
    result = run(installer, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert yaml.safe_load((root / "etc/mihomo/config.yaml").read_text())["tun"]["enable"] is False
    assert (root / "run/tun-wait-count").read_text() == "10"
