"""Installers that persist IPv4 forwarding tell the environment check so.

Ordinary Shell AIO, the gateway wizard's full TUN install and single-service
Mihomo (by default) enable and persist forwarding themselves. Their environment
check runs with CONFIGFLOW_INSTALLER_FORWARDING=persist, so it reports what the
installer will do instead of asking for a manual sysctl repair. Installs that
do not manage forwarding keep their scripts byte for byte.
"""
from pathlib import Path
import subprocess

import pytest

from backend.agents import core_install_script
from backend.agents.core_install_script import generate_core_install_script
from backend.agents.go_install_script import (
    INSTALL_GO_AIO_FORWARDING, INSTALLER_FORWARDING_ENVIRONMENT, INSTALLER_FORWARDING_ENVIRONMENT_END,
    generate_go_agent_install_script, generate_go_aio_install_script,
)
from backend.agents.install_output import with_install_output
from backend import test_agent_native_aio_install as aio
from backend import test_standalone_forwarding_install as standalone

SCRIPTS = Path(__file__).resolve().parent / "agents" / "scripts"
VARIABLE = "CONFIGFLOW_INSTALLER_FORWARDING"
EXPORT = INSTALLER_FORWARDING_ENVIRONMENT + "if cf_environment_check "
FILTER_NOTE = "转发过滤：防火墙可能拦截转发到 Mihomo TUN 的 UDP 和 ping，安装不受影响；放行命令见上方「转发过滤」或完整日志。"


def single(service="mihomo", **options):
    return generate_go_agent_install_script(
        "https://config.test", "single", service, agent_ip="192.0.2.20",
        binary_download_url="https://config.test/api/agents/download", **options)


def check_sh(tmp_path, script):
    path = tmp_path / "script.sh"
    path.write_text(script)
    result = subprocess.run(["sh", "-n", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# -- Generated scripts -------------------------------------------------------

@pytest.mark.parametrize("options", [
    {"manage_ipv4_forwarding": None}, {"manage_ipv4_forwarding": True},
    {"manage_ipv4_forwarding": None, "install_core": True},
])
def test_single_service_mihomo_that_persists_forwarding_tells_the_environment_check(tmp_path, options):
    script = single(**options)
    assert script.count(EXPORT) == 1
    assert script.count(INSTALLER_FORWARDING_ENVIRONMENT_END + 'cf_step_done "核心接管与运行环境检查通过"') == 1
    # Exported only around the check: never before it, never left for the services.
    assert script.count(VARIABLE) == 3
    check_sh(tmp_path, script)


@pytest.mark.parametrize("service,options", [
    ("mihomo", {"manage_ipv4_forwarding": False}),
    ("mihomo", {"manage_ipv4_forwarding": False, "install_core": True}),
    ("mihomo", {"manage_ipv4_forwarding": None, "service_manager": "command", "stop_command": "stop-it",
                "start_command": "start-it", "status_command": "status-it"}),
    ("oxidns", {"manage_ipv4_forwarding": None}),
    ("oxidns", {"manage_ipv4_forwarding": None, "install_core": True}),
    ("mosdns", {"manage_ipv4_forwarding": None}),
])
def test_installs_that_do_not_manage_forwarding_never_set_the_context(service, options):
    script = single(service, **options)
    assert VARIABLE not in script
    assert "{ipv4_forwarding_environment" not in script


def test_aio_that_persists_forwarding_tells_the_environment_check(tmp_path):
    for script in (aio.generated(), aio.generated(install_core=True)):
        assert script.count(EXPORT + '"$BINARY_TMP" -config "$STAGE_DIR/mihomo-config.json"') == 1
        assert script.count(INSTALLER_FORWARDING_ENVIRONMENT_END + 'cf_step_done "运行环境检查通过"') == 1
        assert script.count(VARIABLE) == 3
        check_sh(tmp_path, script)
    selective = generate_go_aio_install_script("https://config.test", "gw", manage_ipv4_forwarding=False,
                                               manage_tun=False)
    assert VARIABLE not in selective
    assert "{ipv4_forwarding_environment" not in selective


# -- Core phase text ---------------------------------------------------------

def test_core_only_installer_is_install_cores_sh_unchanged():
    expected = with_install_output((SCRIPTS / "install-cores.sh").read_text(encoding="utf-8"))
    assert generate_core_install_script() == expected
    assert generate_core_install_script(installer_forwarding=False) == expected
    assert "旁路网关修复：" in expected


@pytest.mark.parametrize("script", [
    pytest.param(lambda: single(manage_ipv4_forwarding=None, install_core=True), id="single"),
    pytest.param(lambda: aio.generated(install_core=True), id="aio"),
])
def test_core_phase_of_a_forwarding_installer_defers_to_the_agent_step(script):
    source = script()
    assert generate_core_install_script(installer_forwarding=True).rstrip() in source
    assert "旁路网关修复：" not in source
    assert "99-configflow-forward.conf" not in source
    # The Agent step writes the forwarding note after persisting it.
    assert "cf_note forwarding 'IPv4 转发：当前已开启" not in source
    assert "安装器将在 Agent 步骤开启并持久化（/etc/sysctl.d/99-configflow-aio-forward.conf）" in source


def test_core_phase_text_requires_the_exact_install_cores_block(monkeypatch, tmp_path):
    changed = tmp_path / "agents" / "scripts"
    changed.mkdir(parents=True)
    (changed / "install-cores.sh").write_text("#!/bin/sh\necho changed\n")
    monkeypatch.setattr(core_install_script, "__file__", str(tmp_path / "agents" / "core_install_script.py"))
    assert "changed" in generate_core_install_script()
    with pytest.raises(RuntimeError, match="install-cores.sh"):
        generate_core_install_script(installer_forwarding=True)


@pytest.mark.parametrize("value,expected", [
    ("0\n", "IPv4 转发：当前为 0，安装器将在 Agent 步骤开启并持久化（/etc/sysctl.d/99-configflow-aio-forward.conf）。"),
    ("1\n", "IPv4 转发：当前为 1，安装器将在 Agent 步骤持久化（/etc/sysctl.d/99-configflow-aio-forward.conf）。"),
    (None, "IPv4 转发：状态无法读取，安装器将在 Agent 步骤再次检查并开启。"),
])
def test_core_phase_forwarding_block_reports_the_current_value(tmp_path, value, expected):
    proc = tmp_path / "ip_forward"
    if value is not None:
        proc.write_text(value)
    block = core_install_script._INSTALLER_FORWARDING_BLOCK.replace("/proc/sys/net/ipv4/ip_forward", str(proc))
    result = subprocess.run(["sh", "-eu", "-c", "cf_note() { echo \"note $*\"; }\n" + block],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected + "\n"


# -- Shell semantics ---------------------------------------------------------

def test_context_reaches_only_the_environment_check(tmp_path):
    stub = tmp_path / "agent"
    stub.write_text('#!/bin/sh\nprintf "check:%s\\n" "${CONFIGFLOW_INSTALLER_FORWARDING-unset}"\n')
    stub.chmod(0o755)
    body = (INSTALLER_FORWARDING_ENVIRONMENT
            + 'if cf_environment_check "$BINARY_TMP" -check-environment; then\n    :\nelse\n    exit 1\nfi\n'
            + INSTALLER_FORWARDING_ENVIRONMENT_END
            + 'printf "after:%s\\n" "${CONFIGFLOW_INSTALLER_FORWARDING-unset}"\n'
            + 'sh -c \'printf "service:%s\\n" "${CONFIGFLOW_INSTALLER_FORWARDING-unset}"\'\n')
    result = subprocess.run(["sh", "-eu", "-c", 'cf_environment_check() { "$@"; }\n' + body],
                            env={"PATH": "/usr/bin:/bin", "BINARY_TMP": str(stub)}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "check:persist\nafter:unset\nservice:unset\n"


CONFLICT_NOTE = "IPv4 转发冲突：其他 sysctl 设置会在重启后把 net.ipv4.ip_forward 改回 0；处理方法见上方「IPv4 转发冲突」或完整日志。"


@pytest.mark.parametrize("filtered", [True, False])
@pytest.mark.parametrize("conflict", [True, False])
def test_summary_repeats_warnings_from_the_detailed_log(tmp_path, filtered, conflict):
    log = tmp_path / "install.log"
    log.write_text("[通过] IPv4 转发（安装器管理）：当前 net.ipv4.ip_forward=0\n"
                   + ("[提醒] IPv4 转发冲突：/etc/sysctl.d/99-zz.conf:1 在 ConfigFlow 的持久化文件之后加载\n"
                      if conflict else "")
                   + ("[提醒] 转发过滤：这台 Linux 的防火墙会拦截转发到 Mihomo TUN（Meta）的流量\n"
                      "  命令：iptables -I FORWARD -o Meta -j ACCEPT\n" if filtered else
                      "[信息] 转发过滤：未能确认转发过滤（timeout），不影响安装。\n"))
    harness = ('cf_output_is_active() { return 1; }\n'
               'cf_note() { printf "note %s %s\\n" "$1" "$2"; }\n'
               'cf_fact() { printf "fact %s\\n" "$1"; }\n'
               + INSTALL_GO_AIO_FORWARDING + '\nsummarize_ipv4_forwarding\n')
    result = subprocess.run(["sh", "-eu", "-c", harness], env={"PATH": "/usr/bin:/bin", "CONFIGFLOW_LOG_FILE": str(log)},
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (f"note forward_filter {FILTER_NOTE}\n" in result.stdout) is filtered
    assert (f"note forwarding_conflict {CONFLICT_NOTE}\n" in result.stdout) is conflict
    assert "note forwarding IPv4 转发已设置持久化" in result.stdout
    assert result.stdout.endswith("fact ipv4_forwarding\n")


def test_summary_without_a_log_file_adds_no_forward_filter_note():
    harness = ('cf_output_is_active() { return 1; }\ncf_note() { printf "note %s\\n" "$1"; }\n'
               'cf_fact() { :; }\n' + INSTALL_GO_AIO_FORWARDING + '\nsummarize_ipv4_forwarding\n')
    result = subprocess.run(["sh", "-eu", "-c", harness], env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "note forwarding\n"


# -- Running the installers --------------------------------------------------

def wrap_agent(tmp_path, agent, warn):
    """Record the context of every Agent call; optionally log a forward-filter warning."""
    real = tmp_path / "fake-agent.real"
    agent.rename(real)
    contexts = tmp_path / "contexts"
    agent.write_text(f'''#!/bin/sh
for arg in "$@"; do last=$arg; done
printf '%s %s\\n' "${{last:-none}}" "${{{VARIABLE}-unset}}" >> {contexts}
if [ "${{last:-}}" = -check-environment ] && [ {"yes" if warn else "no"} = yes ] && [ -n "${{CONFIGFLOW_LOG_FILE:-}}" ]; then
    printf '%s\\n' '[提醒] 转发过滤：这台 Linux 的防火墙会拦截转发到 Mihomo TUN（Meta）的流量。' \\
        '  命令：iptables -I FORWARD -o Meta -j ACCEPT' >> "$CONFIGFLOW_LOG_FILE"
fi
exec {real} "$@"
''')
    agent.chmod(0o755)
    return contexts


def recorded(contexts):
    return [tuple(line.split(" ", 1)) for line in contexts.read_text().splitlines()]


@pytest.mark.parametrize("warn", [False, True])
def test_aio_install_passes_the_context_to_its_environment_check_only(tmp_path, warn):
    installer, env, _ = aio.harness(tmp_path)
    contexts = wrap_agent(tmp_path, tmp_path / "fake-agent", warn)
    result = aio.run(installer, env)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = recorded(contexts)
    assert ("-check-environment", "persist") in calls
    assert all(value == "unset" for flag, value in calls if flag != "-check-environment"), calls
    # The final summary prints notes as warnings on stderr.
    assert (FILTER_NOTE in result.stderr) is warn
    assert "IPv4 转发已开启并持久化" in result.stdout


@pytest.mark.parametrize("forwarding,expected", [(None, "persist"), (False, "unset")])
def test_single_service_install_passes_the_context_only_when_it_persists_forwarding(
        tmp_path, monkeypatch, forwarding, expected):
    script, env, _ = standalone.harness(tmp_path, monkeypatch, manage_ipv4_forwarding=forwarding)
    contexts = wrap_agent(tmp_path, tmp_path / "fake-agent", warn=True)
    result = standalone.run(script, env)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = recorded(contexts)
    assert ("-check-environment", expected) in calls
    assert all(value == "unset" for flag, value in calls if flag != "-check-environment"), calls
    # Only an installer that persists forwarding summarises the warning.
    assert (FILTER_NOTE in result.stderr) is (forwarding is None)
