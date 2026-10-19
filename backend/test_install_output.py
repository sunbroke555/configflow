"""Installer output must not change shell failures, traps or nested installs."""
import os
from pathlib import Path
import shutil
import stat
import subprocess

import pytest

from backend.agents.install_output import with_install_output


SHELLS = [path for name in ("sh", "dash", "bash") if (path := shutil.which(name))]


def run_script(tmp_path, body, shell, *, args=(), **overrides):
    script = tmp_path / "install.sh"
    script.write_text(with_install_output("#!/bin/sh\n" + body))
    env = dict(os.environ, CONFIGFLOW_INSTALL_LOG_DIR=str(tmp_path),
               TEST_EVENTS=str(tmp_path / "events"), **overrides)
    return subprocess.run([shell, str(script), *args], env=env, input="1\n", text=True,
                          capture_output=True, timeout=10)


def log_file(tmp_path):
    logs = list(tmp_path.glob("configflow-install.*/install.log"))
    assert len(logs) == 1
    return logs[0]


@pytest.mark.parametrize("shell", SHELLS)
def test_summary_keeps_details_private_and_interaction_visible(tmp_path, shell):
    result = run_script(tmp_path, '''set -eu
cf_ui '1) 安装'
cf_prompt '请选择: '
read -r choice
[ "$choice" = 1 ]
cf_step '下载核心'
printf 'raw-download-progress\n'
printf 'raw-stderr-detail\n' >&2
cf_step_done '下载完成'
cf_fact core_mihomo_new yes
cf_fact service_mihomo 'Mihomo：运行中'
cf_note direct '没有节点，当前直连'
cf_complete '安装完成'
''', shell)
    assert result.returncode == 0, result.stderr
    assert '1) 安装\n请选择: \n[进行] 下载核心' in result.stdout
    assert '  Mihomo：运行中' in result.stdout
    assert '完成 · 有提醒：安装完成' in result.stderr
    assert '没有节点，当前直连' in result.stderr
    assert 'raw-' not in result.stdout + result.stderr
    assert '\n  yes\n' not in result.stdout
    log = log_file(tmp_path)
    assert 'raw-download-progress' in log.read_text()
    assert 'raw-stderr-detail' in log.read_text()
    assert log.read_text().index('Mihomo：运行中') < log.read_text().index('完成 · 有提醒：安装完成')
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert stat.S_IMODE(log.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("failure,code", [("false", 1), ("exit 42", 42)])
def test_original_errexit_exit_status_and_cleanup_are_preserved(tmp_path, shell, failure, code):
    result = run_script(tmp_path, f'''set -eu
trap 'printf "original-cleanup\\n" >> "$TEST_EVENTS"' 0
cf_step '启动服务'
printf '修复建议：检查服务配置\n' >&2
{failure}
printf 'must-not-run\n' >> "$TEST_EVENTS"
cf_complete '安装完成'
''', shell)
    assert result.returncode == code
    assert (tmp_path / "events").read_text() == "original-cleanup\n"
    assert f'[失败] 启动服务（退出码 {code}）' in result.stderr
    assert '修复建议：检查服务配置' in result.stderr
    assert '安装完成' not in result.stdout
    assert str(log_file(tmp_path)) in result.stdout + result.stderr


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("core_fails", [False, True])
def test_nested_core_shares_log_without_early_success(tmp_path, shell, core_fails):
    nested = with_install_output('''#!/bin/sh
set -eu
cf_step '安装核心'
trap 'printf "core-cleanup\\n" >> "$TEST_EVENTS"' 0
''' + ("exit 7\n" if core_fails else "cf_fact service_mihomo '核心已启动'\ncf_complete '核心安装完成'\n"))
    result = run_script(tmp_path, '''set -eu
trap 'printf "parent-cleanup\\n" >> "$TEST_EVENTS"' 0
(
''' + nested + ''')
cf_step '安装 Agent'
exit 19
''', shell)
    assert result.returncode == (7 if core_fails else 19)
    assert (tmp_path / "events").read_text() == "core-cleanup\nparent-cleanup\n"
    assert '核心安装完成' not in result.stdout
    assert result.stderr.count('[失败]') == 1
    assert ('[进行] 安装 Agent' in result.stdout) == (not core_fails)
    log_file(tmp_path)


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("broken_mktemp", [False, True])
def test_unavailable_log_falls_back_before_dependency_install(tmp_path, shell, broken_mktemp):
    commands = tmp_path / "commands"
    commands.mkdir()
    if broken_mktemp:
        mktemp = commands / "mktemp"
        mktemp.write_text(f'#!{shell}\nprintf "%s\\n" "{tmp_path}/missing/log-dir"\n')
        mktemp.chmod(0o755)
    result = run_script(tmp_path, '''set -eu
cf_step '安装依赖'
printf 'original-output\n'
cf_fact service_mihomo '服务已启动'
cf_complete '安装完成'
''', shell, PATH=str(commands))
    assert result.returncode == 0, result.stderr
    assert '无法创建详细日志' in result.stderr
    assert 'original-output' in result.stdout
    assert '服务已启动' in result.stdout
    assert '安装完成' in result.stdout


@pytest.mark.parametrize("shell", SHELLS)
def test_metadata_failure_does_not_abort_installer(tmp_path, shell):
    result = run_script(tmp_path, '''set -eu
CF_OUTPUT_DIR="$CF_OUTPUT_LOG"
cf_fact service_mihomo '服务已启动'
printf 'original-output-after-log-failure\n'
cf_complete '安装完成'
''', shell)
    assert result.returncode == 0, result.stderr
    assert '日志记录不可用' in result.stderr
    assert 'original-output-after-log-failure' in result.stdout
    assert '安装完成' in result.stdout
    log_file(tmp_path)


@pytest.mark.parametrize("verified", [False, True])
def test_environment_summary_preserves_precise_core_state(tmp_path, verified):
    preflight = tmp_path / "preflight"
    preflight.write_text('''#!/bin/sh
printf '%s\n' 'raw-environment-details' '[提醒] TUN 实际启动：未验证实际接口' '[提醒] IPv4 转发持久化：未知'
''')
    preflight.chmod(0o755)
    result = run_script(tmp_path, '''set -eu
cf_note forwarding 'IPv4 转发：当前已开启；重启后是否保持尚未确认。'
''' + ('cf_fact tun_verified yes\n' if verified else '') + '''cf_environment_check "$TEST_PREFLIGHT"
cf_complete '安装完成'
''', SHELLS[0], TEST_PREFLIGHT=str(preflight))
    assert result.returncode == 0, result.stderr
    assert 'IPv4 转发：当前已开启' in result.stderr
    assert '承担网关转发时请检查' not in result.stdout
    assert 'TUN 实际启动：未验证实际接口' in result.stderr
    assert '预检未验证核心实际创建接口' not in result.stdout + result.stderr
    assert 'raw-environment-details' not in result.stdout
    assert 'raw-environment-details' in log_file(tmp_path).read_text()


@pytest.mark.parametrize("helper", ["cf_environment_check", "cf_deployment_check"])
@pytest.mark.parametrize("status", [0, 37])
def test_external_check_status_and_actionable_warning_are_preserved(tmp_path, helper, status):
    check = tmp_path / "check"
    check.write_text(f'''#!/bin/sh
printf '%s\n' '2026/10/18 Deployment check requires attention: recover-needed' '检查详情及修复建议'
exit {status}
''')
    check.chmod(0o755)
    result = run_script(tmp_path, f'''set -eu
cf_step '运行检查'
{helper} "$TEST_CHECK"
cf_complete '安装完成'
''', SHELLS[0], TEST_CHECK=str(check))
    assert result.returncode == status
    if status:
        assert '检查详情及修复建议' in result.stderr
        assert '[失败] 运行检查' in result.stderr
        assert '安装完成' not in result.stdout
    elif helper == 'cf_deployment_check':
        assert '发布记录需要处理：recover-needed' in result.stderr
    assert 'recover-needed' in log_file(tmp_path).read_text()


@pytest.mark.parametrize('shell', SHELLS)
def test_verbose_option_replays_details_once_and_final_result_is_last(tmp_path, shell):
    result = run_script(tmp_path, '''set -eu
cf_step '检查环境'
printf 'unique-stdout-detail\\n'
printf 'unique-stderr-detail\\n' >&2
cf_step_done '检查完成'
cf_fact service_mihomo 'Mihomo：已保留'
cf_note existing '已有配置继续使用'
cf_complete '安装完成'
''', shell, args=('--verbose',))
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert output.count('unique-stdout-detail') == 1
    assert output.count('unique-stderr-detail') == 1
    assert '完成 · 有提醒：安装完成' in result.stderr
    log = log_file(tmp_path).read_text()
    assert log.index('Mihomo：已保留') < log.index('完成 · 有提醒：安装完成')
    assert log.rstrip().endswith('完整日志：' + str(log_file(tmp_path)))
    assert '\x1b' not in output


@pytest.mark.parametrize('verbose', [False, True])
def test_agent_shared_log_and_report_fd_keep_real_tun_result(tmp_path, verbose):
    preflight = tmp_path / 'preflight'
    preflight.write_text('''#!/bin/sh
printf '%s\\n' '[通过] TUN 实际启动：Mihomo 核心 PID 123 正持有内核 TUN/TAP 接口 tun0，TUN probe detail: fdinfo and target net/dev verified' >> "$CONFIGFLOW_LOG_FILE"
printf '%s\\n' '[通过] TUN tun0 · Mihomo PID 123 已持有' >&3
''')
    preflight.chmod(0o755)
    result = run_script(tmp_path, '''set -eu
cf_step '检查环境'
cf_environment_check "$TEST_PREFLIGHT"
cf_complete '安装完成'
''', SHELLS[0], args=('--verbose',) if verbose else (), TEST_PREFLIGHT=str(preflight))
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert output.count('TUN tun0 · Mihomo PID 123 已持有') == 1
    assert 'TUN：tun0 · Mihomo PID 123 已持有' in result.stdout
    assert result.stdout.index('TUN：tun0 · Mihomo PID 123 已持有') < result.stdout.index('[通过] 安装完成')
    assert ('TUN probe detail:' in output) == verbose
    assert 'TUN probe detail:' in log_file(tmp_path).read_text()
    assert '预检未验证核心实际创建接口' not in output


@pytest.mark.parametrize('shell', SHELLS)
def test_external_nested_installer_reuses_log_and_defers_completion(tmp_path, shell):
    child = tmp_path / 'child.sh'
    child.write_text(with_install_output('''#!/bin/sh
set -eu
cf_step '子安装阶段'
echo child-raw-detail
cf_fact service_mihomo 'Mihomo：运行中'
cf_complete '子安装完成'
'''))
    result = run_script(tmp_path, '''set -eu
sh "$TEST_CHILD"
cf_step '父安装后续阶段'
cf_complete '全部安装完成'
''', shell, args=('--verbose',), TEST_CHILD=str(child))
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert output.count('ConfigFlow ·') == 1
    assert output.count('完整日志：') == 1
    assert output.count('child-raw-detail') == 1
    assert '子安装完成' not in output
    assert 'Mihomo：运行中' in output
    assert '全部安装完成' in output
    log_file(tmp_path)
