"""Dependency setup runs inside native installers before touching deployments."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from backend.agents.go_install_script import AGENT_INSTALL_DEPENDENCIES
from backend import test_agent_native_aio_install as aio
from backend import test_aio_agent_core_install as aio_core
from backend import test_single_agent_core_install as single


TOOLS = "awk basename cat chmod cmp cp dirname grep head ln mkdir mktemp mv readlink rm sed wc".split()


def isolate_dependencies(tmp_path, env, missing=(), package_manager="apk"):
    """No host package manager can be reached, including after a mock failure."""
    commands = tmp_path / "commands"
    commands.mkdir(exist_ok=True)
    unavailable = tmp_path / "unavailable"
    unavailable.mkdir()
    for tool in [*TOOLS, "sh"]:
        if not (commands / tool).exists():
            (commands / tool).symlink_to(shutil.which(tool))
    if not (commands / "wget").exists():
        (commands / "wget").write_text("#!/bin/sh\nexit 0\n")
        (commands / "wget").chmod(0o755)
    for tool in missing:
        (commands / tool).rename(unavailable / tool)
    if package_manager:
        manager = commands / package_manager
        manager.write_text(f'''#!{sys.executable}
import json, os, sys
from pathlib import Path
with open(os.environ['PACKAGE_EVENTS'], 'a') as log:
    log.write(json.dumps([Path(sys.argv[0]).name, sys.argv[1:], os.environ.get('DEBIAN_FRONTEND')]) + '\\n')
updating = sys.argv[1:] == ['update']
if os.environ.get('FAIL_PACKAGE') == ('update' if updating else 'install'): raise SystemExit(41)
if updating or os.environ.get('PACKAGE_NO_COMMANDS'): raise SystemExit(0)
for executable in Path(os.environ['TEST_UNAVAILABLE']).iterdir():
    executable.rename(Path(os.environ['TEST_COMMANDS']) / executable.name)
''')
        manager.chmod(0o755)
    env.pop("DEBIAN_FRONTEND", None)
    events = tmp_path / "packages"
    events.touch()
    # Existing mock core binaries are in the second PATH entry, never host bins.
    core_bins = tmp_path / "root/usr/local/bin"
    env.update(PATH=f"{commands}:{core_bins}", TEST_COMMANDS=str(commands),
               TEST_UNAVAILABLE=str(unavailable), PACKAGE_EVENTS=str(events))
    return events


def dependency_harness(tmp_path, missing=(), manager="apk", **options):
    script = tmp_path / "dependencies.sh"
    script.write_text("set -eu\n" + AGENT_INSTALL_DEPENDENCIES + "\nagent_ensure_dependencies\nprintf 'ready\\n'\n")
    env = dict(os.environ, **options)
    events = isolate_dependencies(tmp_path, env, missing, manager)
    return script, env, events


def run(script, env, choice="1"):
    return subprocess.run(["/bin/sh", str(script)], env=env, input=choice + "\n",
                          capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("manager,prefix", [
    ("apk", ["add", "--no-cache"]),
    ("apt-get", ["install", "-y", "--no-install-recommends"]),
    ("dnf", ["install", "-y"]),
    ("yum", ["install", "-y"]),
    ("pacman", ["-S", "--needed", "--noconfirm"]),
    ("zypper", ["--non-interactive", "install", "--no-recommends"]),
])
def test_missing_runtime_tools_installed_and_rechecked(tmp_path, manager, prefix):
    script, env, events = dependency_harness(
        tmp_path, ("awk", "basename", "grep", "sed", "wget"), manager)
    result = run(script, env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.endswith("ready\n")
    transactions = [json.loads(line) for line in events.read_text().splitlines()]
    expected = [manager, prefix + ["gawk", "coreutils", "grep", "sed", "curl", "ca-certificates"],
                "noninteractive" if manager == "apt-get" else None]
    assert transactions == ([["apt-get", ["update"], None], expected] if manager == "apt-get" else [expected])
    # Repeated installs with tools already available make no package transaction.
    assert run(script, env).returncode == 0
    assert len(events.read_text().splitlines()) == len(transactions)


def test_existing_wget_only_installs_missing_runtime_package(tmp_path):
    script, env, events = dependency_harness(tmp_path, ("sed",))
    result = run(script, env)
    assert result.returncode == 0, result.stderr
    assert json.loads(events.read_text()) == ["apk", ["add", "--no-cache", "sed"], None]


def test_no_package_manager_gives_manual_repair_without_continuing(tmp_path):
    script, env, events = dependency_harness(tmp_path, ("awk",), None)
    result = run(script, env)
    assert result.returncode != 0
    assert "未找到支持的包管理器" in result.stderr
    assert "请手动安装 gawk 后重新运行" in result.stderr
    assert "ready" not in result.stdout
    assert events.read_text() == ""


@pytest.mark.parametrize("manager, failure", [("apk", "install"), ("apt-get", "update"), ("apt-get", "install")])
def test_failed_package_transaction_does_not_continue(tmp_path, manager, failure):
    script, env, events = dependency_harness(tmp_path, ("grep",), manager, FAIL_PACKAGE=failure)
    result = run(script, env)
    assert result.returncode != 0
    assert "Agent 系统依赖安装失败" in result.stderr
    assert "ready" not in result.stdout
    if failure == "update":
        assert len(events.read_text().splitlines()) == 1


def test_nominal_package_success_must_provide_actual_tools(tmp_path):
    script, env, events = dependency_harness(tmp_path, ("grep",), PACKAGE_NO_COMMANDS="yes")
    result = run(script, env)
    assert result.returncode != 0
    assert "包管理器执行后仍缺少 Agent 工具：grep" in result.stderr
    assert "ready" not in result.stdout


def installer_harness(tmp_path, monkeypatch, mode, combined):
    if mode == "aio":
        if combined:
            script, env, _ = aio_core.harness(tmp_path, monkeypatch, manager="openrc")
        else:
            script, env, _ = aio.harness(tmp_path, manager="openrc")
    else:
        original = single.generated
        monkeypatch.setattr(single, "generated", lambda service="mihomo", **options:
                            original(service, **{**options, "install_core": combined}))
        script, env, root, _ = single.harness(tmp_path, monkeypatch, manager="openrc")
        # Existing identity needs sed/head before any replacement is safe.
        agent_dir = root / "opt/configflow-agent"
        agent_dir.mkdir()
        (agent_dir / "config-mihomo.json").write_text(json.dumps({
            "service_type": "mihomo", "service_unit": "mihomo", "service_manager": "openrc",
            "agent_name": "single", "agent_id": "old-id", "token": "old-token"}))
    return script, env


@pytest.mark.parametrize("mode", ["single", "aio"])
@pytest.mark.parametrize("combined", [False, True])
def test_install_repairs_tools_before_inspecting_existing_identity(tmp_path, monkeypatch, mode, combined):
    script, env = installer_harness(tmp_path, monkeypatch, mode, combined)
    events = isolate_dependencies(tmp_path, env, ("grep", "head", "sed"))
    result = run(script, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(events.read_text().splitlines()) == 1
    config = (tmp_path / "agent/mihomo/config.json" if mode == "aio"
              else tmp_path / "root/opt/configflow-agent/config-mihomo.json")
    assert json.loads(config.read_text())["agent_id"] == ("id-mihomo" if mode == "aio" else "old-id")


@pytest.mark.parametrize("mode", ["single", "aio"])
@pytest.mark.parametrize("choice", ["2", "3"])
def test_cancel_and_uninstall_never_install_system_dependencies(tmp_path, monkeypatch, mode, choice):
    script, env = installer_harness(tmp_path, monkeypatch, mode, combined=True)
    # wc is required by the installed OpenRC service, not by its removal.
    events = isolate_dependencies(tmp_path, env, ("wc",))
    result = run(script, env, choice)
    assert result.returncode == 0, result.stdout + result.stderr
    assert events.read_text() == ""
    assert not (tmp_path / "core-completed").exists()


@pytest.mark.parametrize("mode", ["single", "aio"])
def test_unsupported_architecture_does_not_install_missing_dependencies(tmp_path, monkeypatch, mode):
    script, env = installer_harness(tmp_path, monkeypatch, mode, combined=True)
    events = isolate_dependencies(tmp_path, env, ("awk",))
    env["TEST_ARCH"] = "unsupported"
    result = run(script, env)
    assert result.returncode != 0
    assert "不支持的 CPU 架构" in result.stdout + result.stderr
    assert events.read_text() == ""
    assert not (tmp_path / "core-completed").exists()


@pytest.mark.parametrize("mode", ["single", "aio"])
def test_dependency_failure_preserves_existing_agents_before_core_phase(tmp_path, monkeypatch, mode):
    script, env = installer_harness(tmp_path, monkeypatch, mode, combined=True)
    agent_dir = tmp_path / ("agent" if mode == "aio" else "root/opt/configflow-agent")
    before = {path: path.read_bytes() for path in agent_dir.rglob("*") if path.is_file()}
    isolate_dependencies(tmp_path, env, ("grep",))
    env["FAIL_PACKAGE"] = "install"
    result = run(script, env)
    assert result.returncode != 0
    assert "Agent 系统依赖安装失败" in result.stderr
    assert not (tmp_path / "core-completed").exists()
    assert "agent-download" not in (tmp_path / "events").read_text()
    assert {path: path.read_bytes() for path in agent_dir.rglob("*") if path.is_file()} == before
