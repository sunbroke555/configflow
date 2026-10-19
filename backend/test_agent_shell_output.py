"""User-facing Shell reports retain diagnostics without changing command results."""
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess


MODULE = Path(__file__).parent / "agents/scripts/cli-output.sh"


def run_report(tmp_path, body, *, verbose=False, extra_env=None):
    script = tmp_path / "report.sh"
    script.write_text('set -eu\n. "$CF_TEST_MODULE"\ncf_verbose_options "$@"\ncf_init "测试任务"\n' + body)
    env = dict(os.environ, CF_TEST_MODULE=str(MODULE), CONFIGFLOW_LOG_DIR=str(tmp_path / "logs"), NO_COLOR="1")
    # Tests must not inherit another installer's reporting descriptors/context.
    for key in ("CONFIGFLOW_CLI_ACTIVE", "CONFIGFLOW_LOG_CURSOR", "CONFIGFLOW_LOG_FILE", "CONFIGFLOW_LOG_STATE_DIR", "CONFIGFLOW_VERBOSE"):
        env.pop(key, None)
    env.update(extra_env or {})
    result = subprocess.run(["sh", str(script), *(["--verbose"] if verbose else [])],
                            env=env, capture_output=True, text=True, timeout=10)
    paths = re.findall(r"完整日志：([^\n]+)", result.stdout + result.stderr)
    return result, Path(paths[-1]) if paths else None


def test_shell_default_report_is_compact_and_retains_private_full_log(tmp_path):
    result, logfile = run_report(tmp_path, '''
cf_step "检查环境"
printf 'diagnostic-only-line\\n'
cf_success "环境已检查"
cf_warn "TUN 接口尚未创建"
cf_result "Agent 更新完成"
''')
    assert result.returncode == 0, result.stderr
    assert "diagnostic-only-line" not in result.stdout + result.stderr
    assert "\n\n[进行] 检查环境" in result.stdout
    assert "完成 · 有提醒：Agent 更新完成" in result.stderr
    assert "\x1b" not in result.stdout + result.stderr
    assert logfile and "diagnostic-only-line" in logfile.read_text()
    assert stat.S_IMODE(logfile.stat().st_mode) == 0o600
    assert result.stdout.rstrip().endswith(f"完整日志：{logfile}")


def test_shell_verbose_replays_detail_once_and_preserves_failure_exit_code(tmp_path):
    result, logfile = run_report(tmp_path, '''
cf_run "执行检查" sh -c 'echo command-detail; echo specific-error >&2; exit 37'
echo must-not-run
''', verbose=True)
    combined = result.stdout + result.stderr
    assert result.returncode == 37, combined
    assert combined.count("command-detail") == 1
    assert combined.count("specific-error") == 1
    assert "must-not-run" not in combined
    assert "退出码 37" in combined
    assert logfile and "specific-error" in logfile.read_text()


def test_shell_failure_expands_raw_cause_without_verbose(tmp_path):
    result, logfile = run_report(tmp_path, '''
cf_step "启动 Agent"
printf 'service-specific-failure\\n' >&2
exit 42
''')
    assert result.returncode == 42
    assert "service-specific-failure" in result.stderr
    assert "退出码 42" in result.stderr
    assert logfile and "service-specific-failure" in logfile.read_text()


def test_nested_shell_report_shares_log_and_prints_one_header_and_footer(tmp_path):
    child = tmp_path / "child.sh"
    child.write_text('''set -eu
. "$CF_TEST_MODULE"
cf_init "子任务"
cf_step "子任务检查"
echo nested-detail
cf_success "子任务检查完成"
''')
    result, logfile = run_report(tmp_path, 'sh "$CF_TEST_CHILD"\ncf_result "全部完成"\n',
                                verbose=True, extra_env={"CF_TEST_CHILD": str(child)})
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert combined.count("ConfigFlow ·") == 1
    assert combined.count("完整日志：") == 1
    assert combined.count("nested-detail") == 1
    assert logfile and "子任务检查" in logfile.read_text()
    assert len(list((tmp_path / "logs").glob("configflow-install.*/install.log"))) == 1


def test_shell_log_creation_failure_falls_back_and_reports_actual_path(tmp_path):
    commands = tmp_path / "commands"
    commands.mkdir()
    primary = tmp_path / "primary"
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    executable = commands / "mktemp"
    executable.write_text(f'''#!/bin/sh
case "$2" in "$CF_TEST_PRIMARY"/*) exit 1 ;; esac
exec "{shutil.which('mktemp')}" "$@"
''')
    executable.chmod(0o755)
    result, logfile = run_report(tmp_path, 'cf_result "检查完成"\n', extra_env={
        "PATH": str(commands) + os.pathsep + os.environ["PATH"],
        "CONFIGFLOW_LOG_DIR": str(primary), "CF_TEST_PRIMARY": str(primary), "TMPDIR": str(fallback),
    })
    assert result.returncode == 0, result.stderr
    assert "日志目录不可写，已改用" in result.stderr
    assert logfile and logfile.parent.parent == fallback
    assert logfile.is_file()


def test_shell_binary_diagnostic_does_not_hide_exit_status_or_log_footer(tmp_path):
    script = tmp_path / "binary-output.sh"
    script.write_text('''set -eu
. "$CF_TEST_MODULE"
cf_init "诊断输出"
printf '\\377raw-byte-detail\\n' >&2
exit 19
''')
    env = dict(os.environ, CF_TEST_MODULE=str(MODULE), CONFIGFLOW_LOG_DIR=str(tmp_path / "logs"))
    for key in ("CONFIGFLOW_CLI_ACTIVE", "CONFIGFLOW_LOG_CURSOR", "CONFIGFLOW_LOG_FILE", "CONFIGFLOW_LOG_STATE_DIR", "CONFIGFLOW_VERBOSE"):
        env.pop(key, None)
    result = subprocess.run(["sh", str(script)], env=env, capture_output=True, timeout=10)
    assert result.returncode == 19
    assert b"raw-byte-detail" in result.stderr
    assert b"illegal byte sequence" not in result.stderr
    assert "退出码 19".encode() in result.stderr
    assert "完整日志：".encode() in result.stdout


def test_child_log_degradation_restores_parent_raw_command_output(tmp_path):
    result, logfile = run_report(tmp_path, '''
( cf_output_degrade )
cf_step "继续父任务"
printf 'parent-raw-after-degradation\\n'
printf 'parent-error-after-degradation\\n' >&2
cf_result "父任务结束"
''')
    assert result.returncode == 0, result.stderr
    assert 'parent-raw-after-degradation' in result.stdout
    assert 'parent-error-after-degradation' in result.stderr
    assert '后续输出未完整保存' in result.stderr
    assert logfile is None
    assert '完整日志：' not in result.stdout + result.stderr
