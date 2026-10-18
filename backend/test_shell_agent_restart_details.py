"""Exercise legacy Shell Agent diagnostics without restarting a real service."""
import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent / 'agents' / 'scripts' / 'agent.sh'


def _run_restart(tmp_path, command_body, *, url=False, http=False, token='known-agent-token', restart_command=None, no_temp=False):
    script = SCRIPT.read_text(encoding='utf-8').split('# 捕获退出信号', 1)[0]
    script = script.replace('LOG_FILE="/var/log/configflow-agent.log"', 'LOG_FILE="$TEST_LOG"')
    script = script.replace('CONFIG_FILE="/opt/configflow-agent/config.json"', 'CONFIG_FILE="$TEST_MISSING_CONFIG"')
    fake_bin = tmp_path / 'bin'
    fake_bin.mkdir(exist_ok=True)
    if no_temp:
        (fake_bin / 'mktemp').write_text('#!/bin/sh\nprintf cannot-create-diagnostic >&2\nexit 1\n')
        (fake_bin / 'mktemp').chmod(0o755)
    command = fake_bin / ('curl' if url else 'restart-fixture')
    command.write_text('#!/bin/sh\n' + command_body + '\n', encoding='utf-8')
    command.chmod(0o755)
    harness = tmp_path / 'harness.sh'
    ending = ('\nhandle_http_request\n' if http else
              '\nrestart_service\nstatus=$?\nprintf \'%s\\n\' "$status"\nprintf \'%s\' "$RESTART_ERROR_DETAILS"\n')
    harness.write_text(script + ending, encoding='utf-8')
    env = os.environ.copy()
    env.update(PATH=str(fake_bin) + os.pathsep + env['PATH'], TEST_LOG=str(tmp_path / 'agent.log'),
               TEST_MISSING_CONFIG=str(tmp_path / 'missing.json'), TOKEN=token,
               RESTART_CMD=(restart_command or ('https://user:url-password@restart.test/apply?token=query-secret' if url else str(command))))
    request = f'POST /api/restart HTTP/1.1\r\nAuthorization: Bearer {token}\r\nContent-Length: 0\r\n\r\n'
    result = subprocess.run(['sh', str(harness)], input=request.encode(), capture_output=True, env=env, timeout=10)
    assert result.returncode == 0, result.stderr.decode()
    return result.stdout, (tmp_path / 'agent.log').read_text(encoding='utf-8')


def test_shell_command_reports_stdout_stderr_and_exit_code_without_credentials(tmp_path):
    raw, logs = _run_restart(tmp_path, """printf '%s\\n' 'plugin forwarding: line 12, invalid address' 'known-agent-token'
printf '%s\\n' 'failed to bind 127.0.0.1:53: address already in use' >&2
printf '%s\\n' 'password: visible-password' 'Authorization: Bearer access-secret' 'api_key=key-secret'
printf '%s\\n' 'GET https://user:url-password@api.test/path?token=query-secret refused'
exit 7""")
    output = raw.decode()
    assert output.startswith('1\nCommand exit code: 7\n')
    assert 'line 12, invalid address' in output
    assert 'address already in use' in output
    assert 'refused' in output
    for secret in ('known-agent-token', 'visible-password', 'access-secret', 'key-secret', 'url-password', 'query-secret'):
        assert secret not in output
        assert secret not in logs
    assert 'plugin forwarding' not in logs
    assert 'restart-fixture' not in logs


def test_shell_restart_http_details_are_valid_json_and_utf8_content_length(tmp_path):
    raw, _ = _run_restart(tmp_path, """printf '%s\\n' '监听失败：端口 "53" 已占用，路径 C:\\dns' >&2
exit 4""", http=True)
    headers, body = raw.split(b'\r\n\r\n', 1)
    assert headers.startswith(b'HTTP/1.1 500 ')
    length = next(int(line.split(b': ', 1)[1]) for line in headers.split(b'\r\n') if line.startswith(b'Content-Length:'))
    assert length == len(body)
    result = json.loads(body)
    assert result['success'] is False
    assert result['message'] == 'Restart failed'
    assert 'Command exit code: 4' in result['error_details']
    assert '监听失败：端口 "53" 已占用，路径 C:\\dns' in result['error_details']


def test_shell_url_failure_reports_body_status_and_curl_error(tmp_path):
    raw, logs = _run_restart(tmp_path, """printf '%s\\n' 'reload rejected: unknown plugin cache2' 'password: body-secret'
printf '\\n503'
exit 0""", url=True)
    assert raw.startswith(b'1\nHTTP status: 503; curl exit code: 0\n')
    assert b'unknown plugin cache2' in raw
    assert b'body-secret' not in raw
    assert 'body-secret' not in logs
    assert 'restart.test' not in logs


def test_shell_url_transport_failure_keeps_curl_reason(tmp_path):
    raw, _ = _run_restart(tmp_path, """printf '%s\\n' 'curl: (7) Failed to connect: Connection refused' >&2
printf '\\n000'
exit 7""", url=True)
    assert b'HTTP status: 000; curl exit code: 7' in raw
    assert b'Connection refused' in raw


@pytest.mark.parametrize('status', ['200', '204'])
def test_shell_url_success_still_uses_existing_http_status_semantics(tmp_path, status):
    raw, _ = _run_restart(tmp_path, f"printf '%s\\n' 'response text' '{status}'\nexit 0", url=True)
    assert raw == b'0\n'


def test_shell_command_success_does_not_return_diagnostic_output(tmp_path):
    raw, _ = _run_restart(tmp_path, "printf '%s\\n' 'started successfully'\nexit 0")
    assert raw == b'0\n'


def test_shell_diagnostic_output_is_bounded_and_preserves_command_exit_code(tmp_path):
    raw, _ = _run_restart(tmp_path, """i=0
while [ "$i" -lt 5000 ]; do printf '%s\\n' '重复诊断：插件初始化失败'; i=$((i + 1)); done
exit 9""")
    assert raw.startswith(b'1\nCommand exit code: 9\n')
    assert len(raw) < 8400
    assert '[output truncated]' in raw.decode('utf-8')


def test_shell_diagnostics_keep_final_error_after_many_startup_lines(tmp_path):
    raw, _ = _run_restart(tmp_path, """i=0
while [ "$i" -lt 5000 ]; do printf 'INFO startup step %s\\n' "$i"; i=$((i + 1)); done
printf '%s\\n' 'FATAL 插件启动失败：端口 53 已占用' >&2
printf '%s\\n' 'password: final-private-value' >&2
exit 8""")
    output = raw.decode('utf-8')
    assert raw.startswith(b'1\nCommand exit code: 8\n')
    assert len(raw.split(b'\n', 2)[2]) <= 8192
    assert 'FATAL 插件启动失败：端口 53 已占用' in output
    assert 'INFO startup step 4999' in output
    assert 'INFO startup step 0\n' not in output
    assert 'final-private-value' not in output
    assert '[output truncated]' in output


def test_shell_multiline_credential_values_are_not_exposed(tmp_path):
    raw, _ = _run_restart(tmp_path, """printf '%s\\n' 'invalid configuration at line 8' 'password: |' '  multiline-private-1' '  multiline-private-2'
exit 1""")
    assert b'invalid configuration at line 8' in raw
    assert b'multiline-private' not in raw


def test_shell_diagnostic_line_at_buffer_boundary_finishes(tmp_path):
    limit = 8192 - len(b'[output truncated]\n')
    raw, _ = _run_restart(tmp_path, "printf '%s\\n' '" + 'x' * limit + "'\nexit 1")
    assert raw == b'1\nCommand exit code: 1\n' + b'x' * limit


def test_shell_diagnostics_temporary_failure_does_not_prevent_restart(tmp_path):
    raw, _ = _run_restart(tmp_path, 'exit 13', no_temp=True)
    assert raw == b'1\nCommand exit code: 13'


def test_shell_explicit_exit_in_configured_command_retains_its_exit_code(tmp_path):
    raw, _ = _run_restart(tmp_path, 'exit 0', restart_command='printf missing-executable\\n; exit 127')
    assert raw.startswith(b'1\nCommand exit code: 127\n')
    assert b'missing-executable' in raw


def test_shell_known_token_is_redacted_literally_not_as_a_regular_expression(tmp_path):
    raw, _ = _run_restart(tmp_path, "printf '%s\\n' 'connection failed [a-z].*+$end'\nexit 1", token='[a-z].*+$end')
    assert b'[a-z].*+$end' not in raw
    assert b'connection failed [REDACTED]' in raw


def test_shell_http_status_still_decides_url_success(tmp_path):
    raw, _ = _run_restart(tmp_path, "printf '\\n200'\nexit 7", url=True)
    assert raw == b'0\n'
