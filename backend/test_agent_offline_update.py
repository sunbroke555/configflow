"""Offline update reporting and core isolation, with all OS paths/services mocked."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


SCRIPTS = Path(__file__).parent / "agents/scripts"


def update_harness(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    agent_root = tmp_path / "agents"
    agent = agent_root / "mihomo"
    agent.mkdir(parents=True)
    core = tmp_path / "mihomo"
    core.mkdir()
    (core / "config.yaml").write_text("tun:\n  enable: true\n")
    proc = tmp_path / "proc"
    for pid in (42, 43, 84):
        (proc / str(pid)).mkdir(parents=True)
        (proc / str(pid) / "stat").write_text(f"{pid} (mihomo worker) S " + "0 " * 18 + "12345\n")
    binary = agent / "configflow-agent"
    binary.write_text("#!/bin/sh\nprintf '1.8.30-go\\n'\n")
    binary.chmod(0o755)
    (proc / "84/exe").symlink_to(binary)
    config = {
        "native_aio": True, "service_type": "mihomo", "service_manager": "systemd",
        "service_unit": "mihomo", "agent_service_unit": "configflow-agent-mihomo",
        "agent_host": "0.0.0.0", "agent_port": 8080,
        "config_path": str(core / "config.yaml"), "token": "PRIVATE-AGENT-TOKEN",
    }
    config_path = agent / "config.json"
    config_path.write_text(json.dumps(config, indent=2))
    source = (SCRIPTS / "update-agent.sh").read_text()
    source = source.replace("/opt/configflow-agent", str(agent_root)).replace("/proc/", str(proc) + "/")
    updater = package / "update-agent.sh"
    updater.write_text(source)
    shutil.copyfile(SCRIPTS / "cli-output.sh", package / "cli-output.sh")
    candidate = package / "configflow-agent-linux-amd64"
    candidate.write_text(f'''#!{sys.executable}
import os, sys
if '-version' in sys.argv:
    print('1.8.31-go')
elif '-check-environment' in sys.argv:
    print('FULL-ENVIRONMENT-EVIDENCE: interface Meta belongs to PID 42')
    os.write(3, '[通过] TUN：Meta · 已确认核心持有\\n'.encode())
    if os.environ.get('ENVIRONMENT_FAIL'):
        print('environment-specific-failure', file=sys.stderr)
        sys.exit(7)
else:
    print('FULL-DEPLOYMENT-EVIDENCE: no pending journals')
''')
    candidate.chmod(0o755)
    (package / "VERSION").write_text("1.8.31-go\n")
    (package / "SHA256SUMS").write_text(hashlib.sha256(candidate.read_bytes()).hexdigest() + "  " + candidate.name + "\n")
    commands = tmp_path / "commands"
    commands.mkdir()
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"running": True, "core_pid": 42}))
    events = tmp_path / "events"
    for name, body in {
        "id": "printf '0\\n'",
        "uname": "printf 'x86_64\\n'",
        "sleep": ":",
        "flock": 'if [ "$1" = -n ] && [ "$2" = 9 ] && [ "${LOCK_BUSY:-0}" = 1 ]; then exit 1; fi',
        "curl": '''if [ "${HEALTH_FAIL:-0}" = 1 ]; then echo health-specific-failure >&2; exit 7; fi
printf '{"status":"ok"}\\n' ''',
    }.items():
        path = commands / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)
    systemctl = commands / "systemctl"
    systemctl.write_text(f'''#!{sys.executable}
import json, os, pathlib, sys
state_path=pathlib.Path(os.environ['STATE'])
state=json.loads(state_path.read_text())
args=sys.argv[1:]
unit=next((a for a in args if a.endswith('.service')), '')
agent_unit='configflow-agent-mihomo.service'
if args[0] in ('stop','start','restart'):
    with open(os.environ['EVENTS'],'a') as out: out.write(' '.join(args)+'\\n')
    assert unit==agent_unit, 'core lifecycle was invoked'
    if args[0]=='start' and os.environ.get('START_FAIL') and '1.8.31-go' in pathlib.Path({str(binary)!r}).read_text():
        print('start-specific-failure',file=sys.stderr)
        sys.exit(1)
    state['running']=args[0]!='stop'
    if args[0]=='stop' and os.environ.get('CORE_CHANGED'): state['core_pid']=43
    state_path.write_text(json.dumps(state))
    print('RAW-SERVICE-DIAGNOSTIC: '+args[0])
elif args[0]=='is-active':
    sys.exit(0 if unit!=agent_unit or state['running'] else 3)
elif args[0]=='show':
    prop=next(a.split('=',1)[1] for a in args if a.startswith('--property='))
    values={{'LoadState':'loaded','ExecStart':'{{ path={binary} ; argv[]={binary} -config {config_path} ; ignore_errors=no ; }}',
             'ControlGroup':'/system.slice/'+unit,
             'MainPID':str((84 if state['running'] else 0) if unit==agent_unit else state['core_pid'])}}
    if os.environ.get('UNKNOWN_HOOK'): values['ExecStop']='custom-hook'
    print(values.get(prop,''))
else: raise AssertionError(args)
''')
    systemctl.chmod(0o755)
    env = {**os.environ, "PATH": str(commands) + os.pathsep + os.environ["PATH"],
           "CONFIGFLOW_LOG_DIR": str(tmp_path / "logs"), "STATE": str(state_path), "EVENTS": str(events),
           "NO_COLOR": "1"}
    for key in ("CONFIGFLOW_CLI_ACTIVE", "CONFIGFLOW_LOG_FILE", "CONFIGFLOW_LOG_CURSOR", "CONFIGFLOW_REPORT_FD", "CONFIGFLOW_REPORT_NESTED", "CONFIGFLOW_VERBOSE"):
        env.pop(key, None)
    return updater, env, binary, config_path, events


@pytest.mark.parametrize("verbose", [False, True])
def test_offline_update_retains_full_log_and_only_restarts_agent(tmp_path, verbose):
    updater, env, binary, config, events = update_harness(tmp_path)
    config_before = config.read_bytes()
    args = ["sh", str(updater), "mihomo"] + (["--verbose"] if verbose else [])
    result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert config.read_bytes() == config_before
    assert "1.8.31-go" in binary.read_text()
    assert events.read_text().splitlines() == ["stop configflow-agent-mihomo.service", "start configflow-agent-mihomo.service"]
    output = result.stdout + result.stderr
    assert "核心持续运行" in output and "42" in output
    assert "\x1b[" not in output and "PRIVATE-AGENT-TOKEN" not in output
    assert ("FULL-ENVIRONMENT-EVIDENCE" in output) is verbose
    logs = list((tmp_path / "logs").rglob("install.log"))
    assert len(logs) == 1
    detail = logs[0].read_text()
    assert "FULL-ENVIRONMENT-EVIDENCE" in detail and "FULL-DEPLOYMENT-EVIDENCE" in detail
    assert "RAW-SERVICE-DIAGNOSTIC" in detail
    assert logs[0].stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("scenario", ["START_FAIL", "HEALTH_FAIL", "CORE_CHANGED", "LOCK_BUSY", "UNKNOWN_HOOK", "ENVIRONMENT_FAIL"])
def test_offline_update_failure_boundaries_do_not_touch_core(tmp_path, scenario):
    updater, env, binary, config, events = update_harness(tmp_path)
    original = binary.read_bytes()
    config_before = config.read_bytes()
    env[scenario] = "1"
    result = subprocess.run(["sh", str(updater), "--verbose", "mihomo"], env=env,
                            capture_output=True, text=True, timeout=30)
    output = result.stdout + result.stderr
    assert (result.returncode == 0) is (scenario == "ENVIRONMENT_FAIL"), output
    assert config.read_bytes() == config_before
    if scenario != "ENVIRONMENT_FAIL":
        assert binary.read_bytes() == original
    else:
        assert "1.8.31-go" in binary.read_text()
        assert "需要单独处理" in output
    actions = events.read_text().splitlines() if events.exists() else []
    assert all(line in ("stop configflow-agent-mihomo.service", "start configflow-agent-mihomo.service") for line in actions)
    if scenario in ("LOCK_BUSY", "UNKNOWN_HOOK"):
        assert actions == []
    assert "完整日志" in output and "PRIVATE-AGENT-TOKEN" not in output


def test_offline_update_missing_tool_stops_with_readable_cause_and_log(tmp_path):
    updater, env, binary, config, events = update_harness(tmp_path)
    original = binary.read_bytes()
    updater.write_text(updater.read_text().replace(
        "for command_name in systemctl ", "for command_name in configflow-missing-command systemctl ", 1))
    result = subprocess.run(["sh", str(updater)], env=env, capture_output=True, text=True, timeout=10)
    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert "缺少命令：configflow-missing-command" in output
    assert "完整日志" in output and "unbound variable" not in output
    assert binary.read_bytes() == original and not events.exists()
