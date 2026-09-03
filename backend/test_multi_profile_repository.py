import json
import multiprocessing
import os
import shutil
import threading
import time
from pathlib import Path

import pytest

from backend.common.config_repository import (
    ProfileRepository,
    ProfileRepositoryError,
    ProfileValidationError,
)


def _initialize_repository_rounds_worker(data_dirs, barrier, results):
    rounds = []
    for round_index, data_dir in enumerate(data_dirs):
        try:
            barrier.wait(timeout=20)
            repository = ProfileRepository(data_dir)
            rounds.append(
                {
                    "round": round_index,
                    "ok": True,
                    "profile": (repository.profile_dir("default") / "config.json").read_text(
                        encoding="utf-8"
                    ),
                    "system": repository.system_file.read_text(encoding="utf-8"),
                    "derived": (
                        repository.profile_dir("default") / "rules" / "legacy.list"
                    ).read_text(encoding="utf-8"),
                }
            )
        except BaseException as exc:  # pragma: no cover - parent reports exact failure
            rounds.append(
                {
                    "round": round_index,
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    results.put(rounds)


def _hold_repository_lock_worker(lock_path, hold_seconds, ready):
    repository = ProfileRepository.__new__(ProfileRepository)
    with repository._lock(Path(lock_path)):
        ready.set()
        time.sleep(hold_seconds)


def _acquire_repository_lock_worker(lock_path, results, raise_inside=False):
    repository = ProfileRepository.__new__(ProfileRepository)
    started = time.monotonic()
    try:
        with repository._lock(Path(lock_path)):
            elapsed = time.monotonic() - started
            if raise_inside:
                raise RuntimeError("injected protected operation failure")
        results.put({"ok": True, "elapsed": elapsed})
    except BaseException as exc:  # pragma: no cover - parent reports exact failure
        results.put(
            {
                "ok": False,
                "elapsed": time.monotonic() - started,
                "type": type(exc).__name__,
                "error": str(exc),
            }
        )


def _crash_while_holding_repository_lock_worker(lock_path, ready):
    repository = ProfileRepository.__new__(ProfileRepository)
    with repository._lock(Path(lock_path)):
        ready.set()
        os._exit(23)


@pytest.mark.skipif(os.name != "nt", reason="exercises the Windows msvcrt retry path")
def test_windows_cross_process_lock_waits_beyond_msvcrt_implicit_retry_window(tmp_path):
    lock_path = tmp_path / "long-held.lock"
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    results = context.Queue()
    holder = context.Process(
        target=_hold_repository_lock_worker,
        args=(str(lock_path), 11.5, ready),
    )
    contender = context.Process(
        target=_acquire_repository_lock_worker,
        args=(str(lock_path), results),
    )

    holder.start()
    assert ready.wait(timeout=10)
    contender.start()
    result = results.get(timeout=30)
    contender.join(timeout=10)
    holder.join(timeout=10)

    assert holder.exitcode == 0
    assert contender.exitcode == 0
    assert result["ok"], result
    assert 10.5 <= result["elapsed"] < 25


@pytest.mark.skipif(os.name == "nt", reason="exercises the POSIX flock retry path")
def test_posix_cross_process_lock_waits_then_succeeds(tmp_path):
    lock_path = tmp_path / "posix-held.lock"
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    results = context.Queue()
    holder = context.Process(
        target=_hold_repository_lock_worker,
        args=(str(lock_path), 0.4, ready),
    )
    contender = context.Process(
        target=_acquire_repository_lock_worker,
        args=(str(lock_path), results),
    )

    holder.start()
    assert ready.wait(timeout=10)
    contender.start()
    result = results.get(timeout=10)
    contender.join(timeout=10)
    holder.join(timeout=10)

    assert holder.exitcode == contender.exitcode == 0
    assert result["ok"], result
    assert 0.2 <= result["elapsed"] < 5


def test_cross_process_lock_timeout_is_short_configurable_and_path_safe(tmp_path, monkeypatch):
    lock_path = tmp_path / "sensitive-profile-name.lock"
    monkeypatch.setenv("CONFIGFLOW_LOCK_TIMEOUT_SECONDS", "0.2")
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    results = context.Queue()
    holder = context.Process(
        target=_hold_repository_lock_worker,
        args=(str(lock_path), 1.0, ready),
    )
    contender = context.Process(
        target=_acquire_repository_lock_worker,
        args=(str(lock_path), results),
    )

    holder.start()
    assert ready.wait(timeout=10)
    contender.start()
    result = results.get(timeout=10)
    contender.join(timeout=10)
    holder.join(timeout=10)

    assert holder.exitcode == contender.exitcode == 0
    assert not result["ok"]
    assert result["type"] == ProfileRepositoryError.__name__
    assert 0.1 <= result["elapsed"] < 0.9
    assert "Timed out waiting for profile repository lock" in result["error"]
    assert str(lock_path) not in result["error"]


@pytest.mark.parametrize("configured", ["-1", "0", "nan", "inf", "999999", "not-a-number"])
def test_lock_timeout_environment_is_bounded_and_nonfinite_values_are_safe(monkeypatch, configured):
    monkeypatch.setenv("CONFIGFLOW_LOCK_TIMEOUT_SECONDS", configured)

    timeout = ProfileRepository._lock_timeout_seconds()

    assert 0.1 <= timeout <= 3600.0


def test_file_lock_is_released_when_protected_operation_raises(tmp_path):
    lock_path = tmp_path / "exception.lock"
    context = multiprocessing.get_context("spawn")
    failed_results = context.Queue()
    failed = context.Process(
        target=_acquire_repository_lock_worker,
        args=(str(lock_path), failed_results, True),
    )
    failed.start()
    failed_result = failed_results.get(timeout=10)
    failed.join(timeout=10)

    retry_results = context.Queue()
    retry = context.Process(
        target=_acquire_repository_lock_worker,
        args=(str(lock_path), retry_results),
    )
    retry.start()
    retry_result = retry_results.get(timeout=10)
    retry.join(timeout=10)

    assert failed.exitcode == retry.exitcode == 0
    assert failed_result["type"] == "RuntimeError"
    assert retry_result["ok"], retry_result
    assert retry_result["elapsed"] < 2


def test_operating_system_releases_file_lock_when_holder_process_crashes(tmp_path):
    lock_path = tmp_path / "crashed-holder.lock"
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    crashed = context.Process(
        target=_crash_while_holding_repository_lock_worker,
        args=(str(lock_path), ready),
    )
    crashed.start()
    assert ready.wait(timeout=10)
    crashed.join(timeout=10)
    assert crashed.exitcode == 23

    results = context.Queue()
    retry = context.Process(
        target=_acquire_repository_lock_worker,
        args=(str(lock_path), results),
    )
    retry.start()
    result = results.get(timeout=10)
    retry.join(timeout=10)

    assert retry.exitcode == 0
    assert result["ok"], result
    assert result["elapsed"] < 2


def test_thread_lock_remains_reentrant_for_same_thread(tmp_path):
    repository = ProfileRepository.__new__(ProfileRepository)
    lock = repository._thread_lock(tmp_path / "reentrant.lock")

    with lock:
        with lock:
            assert repository._thread_lock(tmp_path / "reentrant.lock") is lock


def test_repository_creates_isolated_default_profile(tmp_path):
    repository = ProfileRepository(tmp_path)

    assert repository.list_profiles()[0]["id"] == "default"
    assert repository.profile_dir("default") == tmp_path / "profiles" / "default"
    assert (tmp_path / "profiles" / "default" / "config.json").exists()
    assert (tmp_path / "profiles" / "default" / "subscribes").is_dir()
    assert (tmp_path / "profiles" / "default" / "providers").is_dir()
    assert (tmp_path / "profiles" / "default" / "rules").is_dir()
    assert (tmp_path / "profiles" / "default" / "generated").is_dir()


def test_empty_data_directory_generates_and_atomically_persists_rule_proxy_token(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.common.config_repository.secrets.token_urlsafe", lambda size: "generated-default-token")

    repository = ProfileRepository(tmp_path)

    assert repository.get_system()["system_config"]["rule_proxy_token"] == "generated-default-token"
    assert repository.get_system()["system_config"].get("config_token", "") == ""
    persisted = json.loads((tmp_path / "system.json").read_text(encoding="utf-8"))
    assert persisted["system_config"]["rule_proxy_token"] == "generated-default-token"
    assert not list(tmp_path.glob(".system.json.*.tmp"))


@pytest.mark.parametrize(
    "legacy_system_config",
    [
        {},
        {"rule_proxy_token": ""},
        {"config_token": "legacy-public-token"},
        {"config_token": "shared-token", "rule_proxy_token": "shared-token"},
    ],
)
def test_legacy_missing_or_empty_rule_proxy_token_is_generated_and_persisted(
    tmp_path, monkeypatch, legacy_system_config
):
    monkeypatch.setattr("backend.common.config_repository.secrets.token_urlsafe", lambda size: "generated-legacy-token")
    legacy = {
        "system_config": legacy_system_config,
        "rule_configs": [],
    }
    (tmp_path / "config.json").write_text(json.dumps(legacy), encoding="utf-8")

    repository = ProfileRepository(tmp_path)

    assert repository.get_system()["system_config"]["rule_proxy_token"] == "generated-legacy-token"
    if legacy_system_config.get("config_token"):
        assert repository.get_system()["system_config"]["config_token"] == legacy_system_config["config_token"]
    persisted = json.loads((tmp_path / "system.json").read_text(encoding="utf-8"))
    assert persisted["system_config"]["rule_proxy_token"] == "generated-legacy-token"


def test_new_repository_rotates_equal_factory_tokens(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "backend.common.config_repository.secrets.token_urlsafe",
        lambda size: "generated-new-token",
    )

    repository = ProfileRepository(
        tmp_path,
        initial_config_factory=lambda: {
            "system_config": {
                "config_token": "factory-shared-token",
                "rule_proxy_token": "factory-shared-token",
            },
        },
    )

    assert repository.get_system()["system_config"] == {
        "server_domain": "",
        "github_proxy_domain": "",
        "config_token": "factory-shared-token",
        "rule_proxy_token": "generated-new-token",
        "retired_rule_proxy_tokens": ["factory-shared-token"],
    }


def test_existing_nonempty_rule_proxy_token_is_preserved(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    repository.save_profile("default", {"system_config": {"rule_proxy_token": "keep-existing"}})
    monkeypatch.setattr(
        "backend.common.config_repository.secrets.token_urlsafe",
        lambda size: pytest.fail("must not replace an existing token"),
    )

    reloaded = ProfileRepository(tmp_path)

    assert reloaded.get_system()["system_config"]["rule_proxy_token"] == "keep-existing"


def test_restart_atomically_rotates_rule_proxy_token_equal_to_config_token(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    system = repository.get_system()
    system["system_config"].update({
        "config_token": "shared-token",
        "rule_proxy_token": "shared-token",
    })
    repository._write_system(system)
    monkeypatch.setattr(
        "backend.common.config_repository.secrets.token_urlsafe",
        lambda size: "rotated-internal-token",
    )

    restarted = ProfileRepository(tmp_path)

    system_config = restarted.get_system()["system_config"]
    assert system_config["config_token"] == "shared-token"
    assert system_config["rule_proxy_token"] == "rotated-internal-token"
    assert json.loads((tmp_path / "system.json").read_text(encoding="utf-8"))["system_config"] == system_config
    assert restarted.rule_proxy_tokens_for_sanitization() == {
        "shared-token",
        "rotated-internal-token",
    }
    assert system_config["retired_rule_proxy_tokens"] == ["shared-token"]
    for _ in range(3):
        restarted = ProfileRepository(tmp_path)
        assert restarted.rule_proxy_tokens_for_sanitization() == {
            "shared-token",
            "rotated-internal-token",
        }
        assert restarted.get_system()["system_config"]["retired_rule_proxy_tokens"] == [
            "shared-token"
        ]
    assert not list(tmp_path.glob(".system.json.*.tmp"))


def test_legacy_retired_rule_proxy_tokens_are_normalized_and_preserved(tmp_path):
    legacy = {
        "system_config": {
            "rule_proxy_token": "current-internal",
            "retired_rule_proxy_tokens": [
                "retired-one",
                "",
                None,
                "retired-one",
                "retired-two",
            ],
        }
    }
    (tmp_path / "config.json").write_text(json.dumps(legacy), encoding="utf-8")

    repository = ProfileRepository(tmp_path)

    assert repository.get_system()["system_config"]["retired_rule_proxy_tokens"] == [
        "retired-one",
        "retired-two",
    ]
    assert repository.rule_proxy_tokens_for_sanitization() == {
        "current-internal",
        "retired-one",
        "retired-two",
    }
    assert ProfileRepository(tmp_path).get_system()["system_config"][
        "retired_rule_proxy_tokens"
    ] == ["retired-one", "retired-two"]


def test_system_transaction_never_persists_equal_config_and_rule_proxy_tokens(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    internal_token = repository.get_system()["system_config"]["rule_proxy_token"]
    monkeypatch.setattr(
        "backend.common.config_repository.secrets.token_urlsafe",
        lambda size: "transaction-rotated-token",
    )

    repository.update_system_transaction(
        lambda system: system["system_config"].update({"config_token": internal_token})
    )

    persisted_system_config = json.loads(
        (tmp_path / "system.json").read_text(encoding="utf-8")
    )["system_config"]
    assert persisted_system_config["config_token"] == internal_token
    assert persisted_system_config["rule_proxy_token"] == "transaction-rotated-token"
    assert repository.get_system()["system_config"] == persisted_system_config


@pytest.mark.parametrize(
    "old_token",
    ["legacy token/&?=秘密", " whitespace ", "🔐/+=?%token", "\n\t"],
)
def test_existing_nonempty_rule_proxy_token_is_preserved_across_restarts(
    tmp_path, monkeypatch, old_token
):
    original = ProfileRepository(tmp_path)
    system = original.get_system()
    system["system_config"]["rule_proxy_token"] = old_token
    original._write_system(system)
    monkeypatch.setattr(
        "backend.common.config_repository.secrets.token_urlsafe",
        lambda size: pytest.fail("must not replace any existing nonempty token"),
    )

    for _ in range(3):
        restarted = ProfileRepository(tmp_path)
        assert restarted.get_system()["system_config"]["rule_proxy_token"] == old_token
        assert restarted.rule_proxy_tokens_for_sanitization() == {old_token}

    persisted = json.loads((tmp_path / "system.json").read_text(encoding="utf-8"))
    assert persisted["system_config"]["rule_proxy_token"] == old_token
    assert not list(tmp_path.glob(".system.json.*.tmp"))


def test_profile_id_cannot_escape_profiles_directory(tmp_path):
    repository = ProfileRepository(tmp_path)

    for profile_id in ("../outside", "..", "a/b", "a\\b", "C:"):
        with pytest.raises(ProfileValidationError):
            repository.profile_dir(profile_id)
        with pytest.raises(ProfileValidationError):
            repository.get_profile(profile_id)

    for relative_path in ("../outside", "", None):
        with pytest.raises(ProfileValidationError):
            repository.profile_path("default", relative_path)


def test_legacy_config_migrates_without_data_loss_and_is_idempotent(tmp_path):
    legacy = {
        "subscriptions": [{"id": "sub-1", "name": "legacy"}],
        "nodes": [{"id": "node-1", "name": "node"}],
        "rule_configs": [{"id": "rule-1", "value": "example.com"}],
        "proxy_groups": [],
        "rule_library": [],
        "system_config": {"server_domain": "https://config.example", "config_token": "keep"},
        "agents": [{"id": "agent-1", "name": "legacy-agent"}],
        "backup": {"webdav_url": "https://backup.example"},
    }
    (tmp_path / "config.json").write_text(json.dumps(legacy), encoding="utf-8")
    (tmp_path / "subscribes").mkdir()
    (tmp_path / "subscribes" / "sub-1.json").write_text("{\"nodes\": []}", encoding="utf-8")
    (tmp_path / "providers").mkdir()
    (tmp_path / "providers" / "agg.yaml").write_text("proxies: []", encoding="utf-8")
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "legacy.list").write_text("DOMAIN,example.com", encoding="utf-8")

    repository = ProfileRepository(tmp_path)
    first_profile = repository.get_profile("default")
    first_system = repository.get_system()

    assert first_profile["subscriptions"] == legacy["subscriptions"]
    assert first_profile["rule_configs"] == legacy["rule_configs"]
    assert first_system["agents"] == [{**legacy["agents"][0], "profile_id": "default"}]
    assert first_system["system_config"]["server_domain"] == "https://config.example"
    assert first_system["system_config"]["config_token"] == "keep"
    assert first_system["system_config"]["rule_proxy_token"]
    assert first_system["backup"] == legacy["backup"]
    assert (tmp_path / "profiles" / "default" / "subscribes" / "sub-1.json").exists()
    assert (tmp_path / "profiles" / "default" / "providers" / "agg.yaml").exists()
    assert (tmp_path / "profiles" / "default" / "rules" / "legacy.list").exists()

    second = ProfileRepository(tmp_path)
    assert second.get_profile("default") == first_profile
    assert second.get_system() == first_system
    assert len(list((tmp_path / "migrations").glob("*/config.json"))) == 1


def test_legacy_initialization_is_serialized_across_processes_over_multiple_rounds(
    tmp_path,
):
    round_count = 6
    data_dirs = []
    for round_index in range(round_count):
        data_dir = tmp_path / f"round-{round_index}"
        data_dir.mkdir()
        legacy = {
            "subscriptions": [{"id": f"legacy-{round_index}"}],
            "agents": [{"id": "agent-1", "token": f"agent-token-{round_index}"}],
            "system_config": {"config_token": f"config-token-{round_index}"},
        }
        (data_dir / "config.json").write_text(json.dumps(legacy), encoding="utf-8")
        rules_dir = data_dir / "rules"
        rules_dir.mkdir()
        (rules_dir / "legacy.list").write_text(
            f"DOMAIN,round-{round_index}.example", encoding="utf-8"
        )
        data_dirs.append(str(data_dir))

    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    workers = [
        context.Process(
            target=_initialize_repository_rounds_worker,
            args=(data_dirs, barrier, results),
        )
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()

    worker_results = [results.get(timeout=90) for _ in workers]
    for worker in workers:
        worker.join(timeout=30)
        assert worker.exitcode == 0

    for round_index, data_dir_text in enumerate(data_dirs):
        data_dir = Path(data_dir_text)
        first = worker_results[0][round_index]
        second = worker_results[1][round_index]
        assert first["ok"] and second["ok"], (first, second)
        assert first["profile"] == second["profile"]
        assert first["derived"] == second["derived"] == f"DOMAIN,round-{round_index}.example"
        assert first["system"] == second["system"]
        assert len(list((data_dir / "migrations").glob("*/config.json"))) == 1
        assert len(list(data_dir.glob("system.json"))) == 1
        assert not list((data_dir / "profiles").glob(".*staging*"))
        assert not list((data_dir / "profiles").glob(".*migration-backup*"))


def test_legacy_migration_cleans_partial_derived_copy_and_retry_succeeds(tmp_path, monkeypatch):
    legacy = {
        "subscriptions": [{"id": "legacy-sub"}],
        "system_config": {"config_token": "public-token"},
    }
    legacy_bytes = json.dumps(legacy).encode("utf-8")
    (tmp_path / "config.json").write_bytes(legacy_bytes)
    for dirname, filename, content in (
        ("subscribes", "sub.json", "{}"),
        ("providers", "provider.yaml", "proxies: []"),
        ("rules", "legacy.list", "DOMAIN,example.com"),
    ):
        source_dir = tmp_path / dirname
        source_dir.mkdir()
        (source_dir / filename).write_text(content, encoding="utf-8")

    original_copytree = shutil.copytree
    copied = 0

    def fail_midway(source, destination, *args, **kwargs):
        nonlocal copied
        result = original_copytree(source, destination, *args, **kwargs)
        copied += 1
        if copied == 2:
            raise OSError("injected derived-data copy failure")
        return result

    monkeypatch.setattr("backend.common.config_repository.shutil.copytree", fail_midway)
    with pytest.raises(OSError, match="injected derived-data copy failure"):
        ProfileRepository(tmp_path)

    assert (tmp_path / "config.json").read_bytes() == legacy_bytes
    assert not (tmp_path / "system.json").exists()
    assert not (tmp_path / "profiles" / "default").exists()
    assert not list((tmp_path / "profiles").glob(".*staging*"))

    monkeypatch.setattr("backend.common.config_repository.shutil.copytree", original_copytree)
    repository = ProfileRepository(tmp_path)

    assert repository.get_profile("default")["subscriptions"] == legacy["subscriptions"]
    assert (repository.profile_dir("default") / "subscribes" / "sub.json").exists()
    assert (repository.profile_dir("default") / "providers" / "provider.yaml").exists()
    assert (repository.profile_dir("default") / "rules" / "legacy.list").exists()
    assert repository.get_system()["system_config"]["config_token"] == "public-token"
    assert not list((tmp_path / "profiles").glob(".*staging*"))


def test_legacy_migration_restores_preexisting_default_when_system_commit_fails(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(
        json.dumps({"subscriptions": [{"id": "legacy"}]}),
        encoding="utf-8",
    )
    previous_default = tmp_path / "profiles" / "default"
    previous_default.mkdir(parents=True)
    (previous_default / "config.json").write_bytes(b"previous-profile")
    (previous_default / "keep.bin").write_bytes(b"previous-derived")
    before = {
        path.relative_to(previous_default).as_posix(): path.read_bytes()
        for path in previous_default.rglob("*")
        if path.is_file()
    }
    original_write_system = ProfileRepository._write_system

    def fail_system_commit(self, system):
        original_write_system(self, system)
        raise OSError("injected system commit failure")

    monkeypatch.setattr(ProfileRepository, "_write_system", fail_system_commit)
    with pytest.raises(OSError, match="injected system commit failure"):
        ProfileRepository(tmp_path)

    after = {
        path.relative_to(previous_default).as_posix(): path.read_bytes()
        for path in previous_default.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert not (tmp_path / "system.json").exists()
    assert not list((tmp_path / "profiles").glob(".*migration-staging*"))
    assert not list((tmp_path / "profiles").glob(".*migration-backup*"))

    monkeypatch.setattr(ProfileRepository, "_write_system", original_write_system)
    repository = ProfileRepository(tmp_path)
    assert repository.get_profile("default")["subscriptions"] == [{"id": "legacy"}]
    assert (tmp_path / "system.json").exists()


def test_atomic_profile_save_keeps_previous_file_when_replace_fails(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    repository.save_profile("default", {"subscriptions": [{"id": "old"}]})
    config_path = repository.profile_dir("default") / "config.json"

    def fail_replace(source, target):
        raise OSError("replace failed")

    monkeypatch.setattr("backend.common.config_repository.os.replace", fail_replace)
    with pytest.raises(OSError):
        repository.save_profile("default", {"subscriptions": [{"id": "new"}]})

    assert json.loads(config_path.read_text(encoding="utf-8"))["subscriptions"] == [{"id": "old"}]


def test_partial_system_metadata_save_keeps_other_fields(tmp_path):
    repository = ProfileRepository(tmp_path)
    repository.save_profile("default", {"system_config": {"config_token": "token"}})
    repository.save_profile("default", {"system_config": {"server_domain": "http://configflow.test"}})

    system_config = repository.get_system()["system_config"]
    assert system_config["rule_proxy_token"]
    assert {key: value for key, value in system_config.items() if key != "rule_proxy_token"} == {
        "server_domain": "http://configflow.test",
        "github_proxy_domain": "",
        "config_token": "token",
        "retired_rule_proxy_tokens": [],
    }


def test_concurrent_profile_saves_do_not_cross_contaminate(tmp_path):
    repository = ProfileRepository(tmp_path)
    repository.create_profile({"id": "alpha", "name": "Alpha"})
    repository.create_profile({"id": "beta", "name": "Beta"})
    failures = []

    def save(profile_id, value):
        try:
            for _ in range(20):
                repository.save_profile(profile_id, {"subscriptions": [{"id": value}]})
        except Exception as exc:  # pragma: no cover - assertion below reports it
            failures.append(exc)

    threads = [
        threading.Thread(target=save, args=("alpha", "alpha")),
        threading.Thread(target=save, args=("beta", "beta")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not failures
    assert repository.get_profile("alpha")["subscriptions"] == [{"id": "alpha"}]
    assert repository.get_profile("beta")["subscriptions"] == [{"id": "beta"}]


def test_concurrent_profile_creation_preserves_system_index(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    original_write_system = repository._write_system
    barrier = threading.Barrier(2)

    def delayed_write_system(system):
        barrier.wait(timeout=5)
        original_write_system(system)

    monkeypatch.setattr(repository, "_write_system", delayed_write_system)
    failures = []

    def create(profile_id):
        try:
            repository.create_profile({"id": profile_id})
        except Exception as exc:
            failures.append(exc)

    workers = [threading.Thread(target=create, args=(profile_id,)) for profile_id in ("alpha", "beta")]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert not failures
    assert {profile["id"] for profile in repository.list_profiles()} >= {"default", "alpha", "beta"}


def test_profile_transaction_preserves_concurrent_list_updates(tmp_path):
    repository = ProfileRepository(tmp_path)
    failures = []

    def append_subscription(index):
        try:
            repository.update_profile_transaction(
                "default",
                lambda profile: profile["subscriptions"].append({"id": f"sub-{index}"}),
            )
        except Exception as exc:  # pragma: no cover - assertion below reports it
            failures.append(exc)

    workers = [threading.Thread(target=append_subscription, args=(index,)) for index in range(12)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert not failures
    assert {item["id"] for item in repository.get_profile("default")["subscriptions"]} == {
        f"sub-{index}" for index in range(12)
    }


def test_save_profile_rolls_back_profile_when_system_commit_fails(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    repository.save_profile("default", {"subscriptions": [{"id": "old"}]})
    system_path = repository.system_file
    original_write_json = repository._write_json

    def fail_system_commit(path, data):
        if path == system_path:
            raise OSError("injected system commit failure")
        return original_write_json(path, data)

    monkeypatch.setattr(repository, "_write_json", fail_system_commit)
    with pytest.raises(OSError, match="injected system commit failure"):
        repository.save_profile("default", {"subscriptions": [{"id": "new"}]})

    assert repository.get_profile("default")["subscriptions"] == [{"id": "old"}]


def test_save_profile_rolls_back_system_when_commit_fails_after_replace(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    before = repository.get_system()
    system_path = repository.system_file
    original_write_json = repository._write_json

    def write_then_fail(path, data):
        if path == system_path:
            original_write_json(path, data)
            raise OSError("injected post-replace failure")
        return original_write_json(path, data)

    monkeypatch.setattr(repository, "_write_json", write_then_fail)
    with pytest.raises(OSError, match="injected post-replace failure"):
        repository.save_profile("default", {"subscriptions": [{"id": "new"}]})

    assert repository.get_system() == before


def test_create_profile_cleans_directory_when_system_commit_fails(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    system_path = repository.system_file
    original_write_json = repository._write_json

    def fail_system_commit(path, data):
        if path == system_path:
            raise OSError("injected system commit failure")
        return original_write_json(path, data)

    monkeypatch.setattr(repository, "_write_json", fail_system_commit)
    with pytest.raises(OSError, match="injected system commit failure"):
        repository.create_profile({"id": "orphan"})

    assert not repository.profile_dir("orphan").exists()
    assert "orphan" not in {profile["id"] for profile in repository.list_profiles()}


def test_create_profile_restores_preexisting_orphan_directory_on_commit_failure(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    orphan_dir = repository.profile_dir("orphan")
    orphan_dir.mkdir()
    original_config = b'{"unregistered": "original"}\n'
    (orphan_dir / "config.json").write_bytes(original_config)
    (orphan_dir / "important.txt").write_text("keep me", encoding="utf-8")
    original_write_json = repository._write_json

    def fail_system_commit(path, data):
        if path == repository.system_file:
            raise OSError("system commit failed")
        return original_write_json(path, data)

    monkeypatch.setattr(repository, "_write_json", fail_system_commit)
    with pytest.raises(OSError, match="system commit failed"):
        repository.create_profile({"id": "orphan"})

    assert (orphan_dir / "config.json").read_bytes() == original_config
    assert (orphan_dir / "important.txt").read_text(encoding="utf-8") == "keep me"
    assert sorted(path.name for path in orphan_dir.iterdir()) == ["config.json", "important.txt"]
    assert "orphan" not in {profile["id"] for profile in repository.list_profiles()}


def test_delete_profile_serializes_with_in_flight_profile_write(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    repository.create_profile({"id": "deletable"})
    write_started = threading.Event()
    allow_write = threading.Event()
    original_write_json = repository._write_json

    def blocked_profile_write(path, data):
        if path == repository.profile_dir("deletable") / "config.json":
            write_started.set()
            assert allow_write.wait(timeout=5)
        return original_write_json(path, data)

    monkeypatch.setattr(repository, "_write_json", blocked_profile_write)
    write_error = []

    def write_profile():
        try:
            repository.save_profile("deletable", {"subscriptions": [{"id": "write"}]})
        except Exception as exc:  # pragma: no cover - assertion below reports it
            write_error.append(exc)

    writer = threading.Thread(target=write_profile)
    writer.start()
    assert write_started.wait(timeout=5)

    deleter = threading.Thread(target=repository.delete_profile, args=("deletable",))
    deleter.start()
    assert deleter.is_alive()
    allow_write.set()
    writer.join(timeout=5)
    deleter.join(timeout=5)

    assert not write_error
    assert not deleter.is_alive()
    assert not repository.profile_dir("deletable").exists()
    assert "deletable" not in {profile["id"] for profile in repository.list_profiles()}


def test_update_profile_transaction_rolls_back_profile_when_system_commit_fails(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    repository.save_profile("default", {"subscriptions": [{"id": "old"}]})
    system_path = repository.system_file
    original_write_json = repository._write_json

    def fail_system_commit(path, data):
        if path == system_path:
            raise OSError("system commit failed")
        return original_write_json(path, data)

    monkeypatch.setattr(repository, "_write_json", fail_system_commit)
    with pytest.raises(OSError, match="system commit failed"):
        repository.update_profile_transaction(
            "default", lambda profile: profile.update({"subscriptions": [{"id": "new"}]}),
        )

    assert repository.get_profile("default")["subscriptions"] == [{"id": "old"}]


def test_delete_profile_keeps_tombstone_when_post_commit_cleanup_fails(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    repository.create_profile({"id": "keep", "name": "Keep"})
    (repository.profile_dir("keep") / "important.txt").write_text("keep me", encoding="utf-8")

    def fail_rmtree(path):
        raise OSError("directory removal failed")

    monkeypatch.setattr("backend.common.config_repository.shutil.rmtree", fail_rmtree)
    repository.delete_profile("keep")

    assert "keep" not in {profile["id"] for profile in repository.list_profiles()}
    assert not repository.profile_dir("keep").exists()
    tombstones = list(repository.profiles_dir.glob(".keep.tombstone-*"))
    assert len(tombstones) == 1
    assert (tombstones[0] / "important.txt").read_text(encoding="utf-8") == "keep me"


def test_delete_profile_restores_directory_when_system_commit_fails(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    repository.create_profile({"id": "keep", "name": "Keep"})
    marker = repository.profile_dir("keep") / "important.txt"
    marker.write_text("original", encoding="utf-8")
    before = repository.get_system()
    original_write_json = repository._write_json

    def fail_system_commit(path, data):
        if path == repository.system_file:
            raise OSError("system commit failed")
        return original_write_json(path, data)

    monkeypatch.setattr(repository, "_write_json", fail_system_commit)
    with pytest.raises(OSError, match="system commit failed"):
        repository.delete_profile("keep")

    assert repository.get_system() == before
    assert marker.read_text(encoding="utf-8") == "original"
    assert not list(repository.profiles_dir.glob(".keep.tombstone-*"))


def test_independent_repositories_merge_locked_field_and_list_updates(tmp_path):
    first = ProfileRepository(tmp_path)
    second = ProfileRepository(tmp_path)
    first.save_profile("default", {"mihomo": {"custom_config": "before"}})
    failures = []

    def update_field():
        try:
            first.update_profile_fields("default", {"mihomo": {"custom_config": "field"}})
        except Exception as exc:
            failures.append(exc)

    def append_item():
        try:
            second.update_profile_transaction(
                "default", lambda profile: profile["subscriptions"].append({"id": "concurrent"}),
            )
        except Exception as exc:
            failures.append(exc)

    threads = [threading.Thread(target=update_field), threading.Thread(target=append_item)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not failures
    result = first.get_profile("default")
    assert result["mihomo"]["custom_config"] == "field"
    assert result["subscriptions"] == [{"id": "concurrent"}]


def test_update_profile_fields_three_way_merges_stale_list_appends(tmp_path):
    first = ProfileRepository(tmp_path)
    second = ProfileRepository(tmp_path)
    first.save_profile("default", {"subscriptions": [{"id": "existing"}]})

    first_baseline = first.get_profile("default")
    second_baseline = second.get_profile("default")
    first_value = first_baseline["subscriptions"] + [{"id": "from-first"}]
    second_value = second_baseline["subscriptions"] + [{"id": "from-second"}]

    first.update_profile_fields(
        "default",
        {"subscriptions": first_value},
        baseline={"subscriptions": first_baseline["subscriptions"]},
    )
    second.update_profile_fields(
        "default",
        {"subscriptions": second_value},
        baseline={"subscriptions": second_baseline["subscriptions"]},
    )

    assert first.get_profile("default")["subscriptions"] == [
        {"id": "existing"},
        {"id": "from-first"},
        {"id": "from-second"},
    ]
