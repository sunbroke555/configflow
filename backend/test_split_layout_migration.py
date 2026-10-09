"""Behavioral regressions for the published schema2 split-layout upgrade."""

import copy
import errno
import json
import os
from pathlib import Path

import pytest

from backend.common.config_repository import ProfileRepository, ProfileRepositoryError


RESOURCE_FIELDS = ("subscriptions", "nodes", "subscription_aggregations", "rule_library")
METADATA_FIELDS = ("id", "name", "description", "created_at", "updated_at")


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _profile(label):
    return {
        "subscriptions": [{
            "id": "sub", "name": f"Subscription {label}",
            "url": f"https://{label}.example/sub?token=private", "enabled": True,
        }],
        "nodes": [{
            "id": "node", "name": f"Node {label}", "type": "ss",
            "server": f"{label}.example", "port": 1443, "password": f"secret-{label}",
            "cipher": "aes-128-gcm", "subscription_id": "sub", "enabled": True,
        }],
        "subscription_aggregations": [{
            "id": "agg", "name": f"Aggregate {label}",
            "subscriptions": ["sub"], "nodes": ["node"], "enabled": True,
        }],
        "rule_library": [{
            "id": "library", "name": "Private domains", "source_type": "content",
            "content": f"{label}.example\n+.{label}.internal", "behavior": "domain",
            "format": "text", "enabled": True,
        }],
        "proxy_groups": [
            {
                "id": "main", "name": "Main", "type": "select",
                "subscriptions": ["sub"], "manual_nodes": ["node", "DIRECT"],
                "aggregations": ["agg"], "include_groups": ["legacy-node"],
                "proxies_order": [
                    {"type": "aggregation", "id": "agg"},
                    {"type": "node", "id": "node"},
                    {"type": "subscription", "id": "sub"},
                    {"type": "strategy", "id": "legacy-node"},
                    {"type": "node", "id": "DIRECT"},
                ],
            },
            {"id": "legacy-node", "name": "Legacy node", "type": "select",
             "source": "node", "proxies": ["node", "DIRECT"]},
            {"id": "legacy-sub", "name": "Legacy subscription", "type": "select",
             "source": "subscription", "proxies": ["sub"]},
            {"id": "legacy-agg", "name": "Legacy aggregation", "type": "select",
             "source": "aggregation", "proxies": ["agg"]},
        ],
        "rule_configs": [
            {"id": "rule-z", "itemType": "rule", "type": "DOMAIN-SUFFIX",
             "value": f"{label}.lan", "policy": "DIRECT", "enabled": True},
            {"id": "ruleset", "itemType": "ruleset", "library_rule_id": "library",
             "policy": "Main", "enabled": True},
            {"id": "rule-a", "itemType": "rule", "type": "MATCH", "value": "",
             "policy": "Main", "enabled": True},
        ],
        "mihomo": {"custom_config": f"mixed-port: 9876\n# {label}", "port": 9876},
        "surge": {"custom_config": f"[General]\nloglevel = warning\n# {label}",
                  "smart_groups": [{"group_id": "main", "enabled": True}]},
        "mosdns": {
            "direct_rulesets": ["ruleset"], "proxy_rulesets": [],
            "direct_rules": ["rule-z"], "proxy_rules": ["rule-a"],
            "local_dns": "192.0.2.53", "remote_dns": "tls://dns.example",
            "fallback_dns": "198.51.100.53", "default_forward": "forward_local",
            "custom_hosts": f"{label}.lan 192.0.2.9", "custom_config": f"# {label}",
            "custom_matches": [], "custom_match_position": "head",
            "cache_enabled": False, "cache_size": 321, "cache_lazy_ttl": 654,
            "cache_dump_enabled": False, "cache_dump_file": "./private.dump",
            "cache_dump_interval": 987,
        },
    }


def _bundle(*, identical=False):
    profiles = {"default": _profile("home"), "office": _profile("office")}
    if identical:
        for field in RESOURCE_FIELDS:
            profiles["office"][field] = copy.deepcopy(profiles["default"][field])
    metadata = [
        {"id": profile_id, "name": name, "description": f"Private {name} configuration",
         "created_at": f"2025-01-0{index}T03:04:05Z",
         "updated_at": f"2026-09-0{index}T06:07:08Z"}
        for index, (profile_id, name) in enumerate(
            (("default", "家庭网络"), ("office", "办公室")), start=1
        )
    ]
    system = {
        "schema_version": 2, "active_profile_id": "office", "profiles": metadata,
        "system_config": {
            "config_token": "live-config-token", "rule_proxy_token": "live-rule-token",
            "retired_rule_proxy_tokens": ["retired-rule-token"],
            "server_domain": "https://nas.example", "github_proxy_domain": "https://git.example",
        },
        "backup": {"webdav_url": "https://backup.example", "webdav_password": "live-secret"},
        "agents": [
            {"id": "router-home", "name": "Home router", "profile_id": "default",
             "token": "home-agent-secret", "enabled": True, "last_seen": "2026-10-01T02:03:04Z",
             "config_type": "mihomo", "interval": 73, "version": "2.7",
             "custom_state": {"delivery_hash": "home-hash", "labels": ["nas", "home"]}},
            {"id": "router-office", "name": "Office router", "profile_id": "office",
             "token": "office-agent-secret", "enabled": False, "last_seen": "2026-10-02T02:03:04Z",
             "config_type": "surge", "interval": 91, "version": "2.8",
             "custom_state": {"delivery_hash": "office-hash", "labels": ["office"]}},
        ],
    }
    return {"schema_version": 2, "system": system, "profiles": profiles}


def _install_split(root, bundle, *, legacy="stale", backups=False):
    root.mkdir(parents=True, exist_ok=True)
    if legacy == "stale":
        stale = _profile("obsolete")
        stale["system_config"] = {"config_token": "obsolete-token"}
        stale["agents"] = [{"id": "retired-router", "token": "obsolete-agent-token"}]
        _write_json(root / "config.json", stale)
    elif legacy == "corrupt":
        (root / "config.json").write_bytes(b'{"subscriptions": [')
    _write_json(root / "system.json", bundle["system"])
    for profile_id, profile in bundle["profiles"].items():
        _write_json(root / "profiles" / profile_id / "config.json", profile)
    if backups:
        for path in list(root.rglob("*.json")):
            # Deliberately distinct bytes make copying the primary over its backup detectable.
            path.with_name(path.name + ".bak").write_bytes(path.read_bytes() + b" \n")
    return _source_bytes(root)


def _source_bytes(root):
    paths = [root / "config.json", root / "config.json.bak",
             root / "system.json", root / "system.json.bak"]
    paths.extend((root / "profiles").rglob("config.json"))
    paths.extend((root / "profiles").rglob("config.json.bak"))
    return {str(path.relative_to(root)): path.read_bytes() for path in paths if path.is_file()}


def _assert_preserved(root, before, *, migrated=False):
    after = _source_bytes(root)
    if migrated:
        before = {key: value for key, value in before.items()
                  if key not in {"config.json", "config.json.bak"}}
        after = {key: value for key, value in after.items()
                 if key not in {"config.json", "config.json.bak"}}
    assert after == before


def _no_template():
    raise AssertionError("An existing split repository must never instantiate a template")


def _assert_migrated(repository, bundle, *, identical=False):
    exported = repository.export_all()
    assert exported["schema_version"] == 5
    assert set(exported["profiles"]) == set(bundle["profiles"])
    actual_system = repository.get_system()
    for field in ("agents", "backup"):
        assert actual_system[field] == bundle["system"][field]
    for key, value in bundle["system"]["system_config"].items():
        if key == 'retired_rule_proxy_tokens':
            assert set(value) <= set(actual_system['system_config'][key])
        else:
            assert actual_system['system_config'][key] == value
    shared = repository.get_shared()
    expected_count = 1 if identical else 2
    for field in RESOURCE_FIELDS:
        assert len(shared[field]) == expected_count
        assert len({item["id"] for item in shared[field]}) == expected_count
    assert len({item["name"] for item in shared["rule_library"]}) == expected_count
    for metadata in bundle["system"]["profiles"]:
        profile_id = metadata["id"]
        original = bundle["profiles"][profile_id]
        profile = repository.get_profile(profile_id)
        assert {key: profile[key] for key in METADATA_FIELDS} == metadata
        for engine in ("mihomo", "surge", "mosdns"):
            assert profile[engine] == original[engine]
        resolved = repository.get_compat_config(profile_id)
        resources = {}
        for field in RESOURCE_FIELDS[:3]:
            assert len(resolved[field]) == 1
            resources[field] = resolved[field][0]
        subscription = resources["subscriptions"]
        node = resources["nodes"]
        aggregation = resources["subscription_aggregations"]
        assert {**subscription, "id": "sub"} == original["subscriptions"][0]
        assert {**node, "id": "node", "subscription_id": "sub"} == original["nodes"][0]
        assert node["subscription_id"] == subscription["id"]
        assert {**aggregation, "id": "agg", "subscriptions": ["sub"], "nodes": ["node"]} == original["subscription_aggregations"][0]
        assert aggregation["subscriptions"] == [subscription["id"]]
        assert aggregation["nodes"] == [node["id"]]
        expected_groups = copy.deepcopy(original["proxy_groups"])
        main = expected_groups[0]
        main["subscriptions"] = [subscription["id"]]
        main["manual_nodes"] = [node["id"], "DIRECT"]
        main["aggregations"] = [aggregation["id"]]
        for item, resource in zip(main["proxies_order"], (aggregation, node, subscription)):
            item["id"] = resource["id"]
        expected_groups[1]["proxies"] = [node["id"], "DIRECT"]
        expected_groups[2]["proxies"] = [subscription["id"]]
        expected_groups[3]["proxies"] = [aggregation["id"]]
        assert profile["proxy_groups"] == expected_groups
        assert [rule["id"] for rule in profile["rule_configs"]] == ["rule-z", "ruleset", "rule-a"]
        migrated_rule = profile["rule_configs"][1]
        library = next(item for item in shared["rule_library"] if item["id"] == migrated_rule["library_rule_id"])
        assert {**library, "id": "library", "name": "Private domains"} == original["rule_library"][0]
        expected_rules = copy.deepcopy(original["rule_configs"])
        expected_rules[1]["library_rule_id"] = library["id"]
        assert profile["rule_configs"] == expected_rules
        assert resolved["rule_configs"][1]["content"] == original["rule_library"][0]["content"]


@pytest.mark.parametrize("legacy", ["stale", "missing", "corrupt"])
def test_split_is_authoritative_without_a_readable_legacy_root(tmp_path, legacy):
    bundle = _bundle()
    before = _install_split(tmp_path, bundle, legacy=legacy)
    repository = ProfileRepository(tmp_path, initial_config_factory=_no_template)
    _assert_migrated(repository, bundle)
    _assert_preserved(tmp_path, before, migrated=True)


def test_identical_resources_are_deduplicated_without_merging_profiles(tmp_path):
    bundle = _bundle(identical=True)
    _install_split(tmp_path, bundle)
    _assert_migrated(ProfileRepository(tmp_path), bundle, identical=True)


def test_same_name_rules_with_distinct_ids_preserve_both_contents(tmp_path):
    bundle = _bundle()
    office = bundle["profiles"]["office"]
    office["rule_library"][0]["id"] = "office-library"
    office["rule_configs"][1]["library_rule_id"] = "office-library"
    _install_split(tmp_path, bundle)
    repository = ProfileRepository(tmp_path)
    library = repository.get_shared()["rule_library"]
    assert len(library) == len({rule["name"] for rule in library}) == 2
    for profile_id, original in bundle["profiles"].items():
        ruleset = repository.get_compat_config(profile_id)["rule_configs"][1]
        assert ruleset["content"] == original["rule_library"][0]["content"]


def test_complete_snapshot_preserves_every_original_primary_and_backup(tmp_path):
    bundle = _bundle()
    before = _install_split(tmp_path, bundle, backups=True)
    ProfileRepository(tmp_path)
    snapshots = [path.parent for path in (tmp_path / "migrations").rglob("system.json")]
    assert len(snapshots) == 1
    for relative, content in before.items():
        assert (snapshots[0] / relative).read_bytes() == content
    _assert_preserved(tmp_path, before, migrated=True)


def test_restart_keeps_current_schema5_and_never_reimports_old_split(tmp_path):
    bundle = _bundle()
    _install_split(tmp_path, bundle)
    repository = ProfileRepository(tmp_path)
    _assert_migrated(repository, bundle)
    repository.update_profile_fields("office", {"mihomo": {"custom_config": "mixed-port: 9999"}})
    repository.update_system_transaction(lambda system: system["agents"][1].update(profile_id="default"))
    before = repository.export_all()
    root_bytes = repository.path.read_bytes()
    snapshots = sorted(str(path) for path in (tmp_path / "migrations").rglob("system.json"))
    (tmp_path / "system.json").write_bytes(b"invalid obsolete index")
    (tmp_path / "profiles" / "office" / "config.json").unlink()
    reopened = ProfileRepository(tmp_path, initial_config_factory=_no_template)
    assert reopened.export_all() == before
    assert reopened.path.read_bytes() == root_bytes
    assert sorted(str(path) for path in (tmp_path / "migrations").rglob("system.json")) == snapshots


@pytest.mark.parametrize("legacy", ["stale", "missing"])
@pytest.mark.parametrize("damage", [
    "missing-profile", "corrupt-profile", "corrupt-index", "index-list", "empty-index",
    "duplicate-id", "missing-default", "unknown-version", "invalid-agent-binding",
])
def test_invalid_split_never_falls_back_or_creates_templates(tmp_path, legacy, damage):
    bundle = _bundle()
    _install_split(tmp_path, bundle, legacy=legacy)
    profile_path = tmp_path / "profiles" / "office" / "config.json"
    if damage == "missing-profile":
        profile_path.unlink()
    elif damage == "corrupt-profile":
        profile_path.write_bytes(b'{"nodes": [')
    elif damage == "corrupt-index":
        (tmp_path / "system.json").write_bytes(b'{"schema_version": 2,')
    else:
        system = bundle["system"]
        if damage == "index-list":
            system["profiles"] = {}
        elif damage == "empty-index":
            system["profiles"] = []
        elif damage == "duplicate-id":
            system["profiles"].append(copy.deepcopy(system["profiles"][1]))
        elif damage == "missing-default":
            system["profiles"] = system["profiles"][1:]
        elif damage == "unknown-version":
            system["schema_version"] = 999
        elif damage == "invalid-agent-binding":
            system["agents"][1]["profile_id"] = "removed"
        _write_json(tmp_path / "system.json", system)
    before = _source_bytes(tmp_path)
    with pytest.raises(ProfileRepositoryError):
        ProfileRepository(tmp_path, initial_config_factory=_no_template)
    _assert_preserved(tmp_path, before)


@pytest.mark.parametrize("profile_id", ["../outside", "/absolute", "office/child", "a" * 65])
def test_split_rejects_unsafe_profile_ids_without_mutating_sources(tmp_path, profile_id):
    bundle = _bundle()
    _install_split(tmp_path, bundle)
    bundle["system"]["profiles"][1]["id"] = profile_id
    bundle["system"]["agents"][1]["profile_id"] = profile_id
    _write_json(tmp_path / "system.json", bundle["system"])
    before = _source_bytes(tmp_path)
    with pytest.raises(ProfileRepositoryError):
        ProfileRepository(tmp_path, initial_config_factory=_no_template)
    _assert_preserved(tmp_path, before)


@pytest.mark.parametrize("relative", [
    "config.json", "config.json.bak",
    "system.json", "system.json.bak", "profiles", "profiles/office",
    "profiles/office/config.json", "profiles/office/config.json.bak",
])
def test_split_rejects_symlinked_sources(tmp_path, relative):
    root = tmp_path / "data"
    _install_split(root, _bundle(), backups=True)
    source = root / relative
    external = tmp_path / "outside"
    source.rename(external)
    try:
        source.symlink_to(external, target_is_directory=external.is_dir())
    except (OSError, NotImplementedError):
        pytest.skip("Creating symlinks is not supported on this platform")
    before = _source_bytes(root)
    external_before = ({str(path.relative_to(external)): path.read_bytes()
                        for path in external.rglob("*") if path.is_file()}
                       if external.is_dir() else external.read_bytes())
    with pytest.raises(ProfileRepositoryError):
        ProfileRepository(root, initial_config_factory=_no_template)
    assert source.is_symlink()
    _assert_preserved(root, before)
    external_after = ({str(path.relative_to(external)): path.read_bytes()
                       for path in external.rglob("*") if path.is_file()}
                      if external.is_dir() else external.read_bytes())
    assert external_after == external_before


@pytest.mark.parametrize("legacy", ["stale", "missing", "corrupt"])
@pytest.mark.parametrize("after_replace", [False, True])
def test_failed_commit_leaves_originals_retryable(tmp_path, monkeypatch, legacy, after_replace):
    bundle = _bundle()
    before = _install_split(tmp_path, bundle, legacy=legacy, backups=True)
    replace = os.replace
    failed = False

    def fail_root_commit(source, destination):
        nonlocal failed
        if Path(destination) == tmp_path / "config.json" and not failed:
            failed = True
            if after_replace:
                replace(source, destination)
            raise OSError(errno.ENOSPC, "injected full disk at root commit")
        return replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", fail_root_commit)
        with pytest.raises((OSError, ProfileRepositoryError)):
            ProfileRepository(tmp_path, initial_config_factory=_no_template)
    assert failed
    _assert_preserved(tmp_path, before)
    repository = ProfileRepository(tmp_path, initial_config_factory=_no_template)
    _assert_migrated(repository, bundle)


def test_full_schema2_import_matches_disk_migration(tmp_path):
    bundle = _bundle()
    _install_split(tmp_path / "disk", bundle)
    migrated = ProfileRepository(tmp_path / "disk")
    restored = ProfileRepository(tmp_path / "import")
    original_bundle = copy.deepcopy(bundle)
    restored.import_all(bundle)
    assert bundle == original_bundle
    _assert_migrated(migrated, bundle)
    _assert_migrated(restored, bundle)
    assert restored.get_system()["agents"] == migrated.get_system()["agents"]
    for profile_id in bundle["profiles"]:
        assert restored.get_profile(profile_id)["mihomo"] == migrated.get_profile(profile_id)["mihomo"]


@pytest.mark.parametrize("damage", ["bare-index", "missing-profiles", "missing-office", "extra-profile", "unknown-version"])
def test_incomplete_schema2_import_preserves_current_repository(tmp_path, damage):
    repository = ProfileRepository(tmp_path)
    bundle = _bundle()
    if damage == "bare-index":
        bundle = bundle["system"]
    elif damage == "missing-profiles":
        del bundle["profiles"]
    elif damage == "missing-office":
        del bundle["profiles"]["office"]
    elif damage == "extra-profile":
        bundle["profiles"]["unindexed"] = _profile("orphan")
    elif damage == "unknown-version":
        bundle["schema_version"] = 999
    before = repository.export_all()
    root_bytes = repository.path.read_bytes()
    with pytest.raises(ProfileRepositoryError):
        repository.import_all(bundle)
    assert repository.export_all() == before
    assert repository.path.read_bytes() == root_bytes


@pytest.mark.parametrize("damage", ["node-dialer", "resource-refs", "node-overrides", "missing-subscription", "invalid-settings"])
@pytest.mark.parametrize("entrypoint", ["disk", "import"])
def test_schema2_migration_does_not_strip_invalid_or_unsupported_fields(tmp_path, damage, entrypoint):
    bundle = _bundle()
    profile = bundle["profiles"]["office"]
    if damage == "node-dialer":
        profile["nodes"][0]["dialer_ref"] = {"type": "node", "id": "other"}
    elif damage == "resource-refs":
        profile["resource_refs"] = {"nodes": ["node"]}
    elif damage == "node-overrides":
        profile["node_dialers"] = {"node": {"type": "group", "id": "main"}}
    elif damage == "missing-subscription":
        profile["nodes"][0]["subscription_id"] = "missing"
    elif damage == "invalid-settings":
        profile["mihomo"] = "not an object"
    if entrypoint == "disk":
        before = _install_split(tmp_path, bundle)
        with pytest.raises(ProfileRepositoryError):
            ProfileRepository(tmp_path, initial_config_factory=_no_template)
        _assert_preserved(tmp_path, before)
    else:
        repository = ProfileRepository(tmp_path)
        before = repository.path.read_bytes()
        with pytest.raises(ProfileRepositoryError):
            repository.import_all(bundle)
        assert repository.path.read_bytes() == before


@pytest.mark.parametrize("relative", ["system.json", "profiles/office/config.json"])
@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_valid_split_backup_is_read_without_repairing_legacy_files(tmp_path, relative, damage):
    bundle = _bundle()
    _install_split(tmp_path, bundle, backups=True)
    primary = tmp_path / relative
    if damage == "missing":
        primary.unlink()
    else:
        primary.write_bytes(b'{"broken":')
    before = _source_bytes(tmp_path)
    repository = ProfileRepository(tmp_path, initial_config_factory=_no_template)
    _assert_migrated(repository, bundle)
    _assert_preserved(tmp_path, before, migrated=True)


@pytest.mark.parametrize("legacy", ["stale", "missing"])
def test_orphan_split_profiles_are_not_replaced_by_legacy_or_template(tmp_path, legacy):
    _install_split(tmp_path, _bundle(), legacy=legacy)
    (tmp_path / "system.json").unlink()
    before = _source_bytes(tmp_path)
    with pytest.raises(ProfileRepositoryError):
        ProfileRepository(tmp_path, initial_config_factory=_no_template)
    _assert_preserved(tmp_path, before)


def test_identical_node_and_aggregation_ids_do_not_hide_conflicting_dependencies(tmp_path):
    bundle = _bundle(identical=True)
    bundle["profiles"]["office"]["subscriptions"][0]["url"] = "https://office.example/different-sub"
    _install_split(tmp_path, bundle)
    repository = ProfileRepository(tmp_path)
    shared = repository.get_shared()
    for field in RESOURCE_FIELDS[:3]:
        assert len(shared[field]) == 2
    assert len(shared["rule_library"]) == 1
    for profile_id, original in bundle["profiles"].items():
        resolved = repository.get_compat_config(profile_id)
        assert len(resolved["subscriptions"]) == len(resolved["nodes"]) == len(resolved["subscription_aggregations"]) == 1
        subscription = resolved["subscriptions"][0]
        node = resolved["nodes"][0]
        aggregation = resolved["subscription_aggregations"][0]
        assert subscription["url"] == original["subscriptions"][0]["url"]
        assert node["subscription_id"] == subscription["id"]
        assert aggregation["subscriptions"] == [subscription["id"]]
        assert aggregation["nodes"] == [node["id"]]


def test_inline_rulesets_with_same_name_keep_each_profiles_source_and_order(tmp_path):
    bundle = _bundle()
    for profile_id, profile in bundle["profiles"].items():
        profile["rule_configs"].insert(0, {
            "id": "inline", "itemType": "ruleset", "name": "Private domains",
            "source_type": "content", "content": f"inline-{profile_id}.example",
            "behavior": "domain", "format": "text", "policy": "Main", "enabled": True,
        })
    _install_split(tmp_path, bundle)
    repository = ProfileRepository(tmp_path)
    library = repository.get_shared()["rule_library"]
    assert len(library) == len({item["name"] for item in library}) == 4
    for profile_id, profile in bundle["profiles"].items():
        resolved = repository.get_compat_config(profile_id)
        assert [item["id"] for item in resolved["rule_configs"]] == ["inline", "rule-z", "ruleset", "rule-a"]
        assert resolved["rule_configs"][0]["content"] == profile["rule_configs"][0]["content"]
        assert resolved["rule_configs"][2]["content"] == profile["rule_library"][0]["content"]


def test_repeated_conflicting_definition_reuses_the_same_shared_variant(tmp_path):
    bundle = _bundle()
    bundle['profiles']['third'] = copy.deepcopy(bundle['profiles']['office'])
    bundle['system']['profiles'].append({'id': 'third', 'name': 'Third'})
    _install_split(tmp_path, bundle)
    repository = ProfileRepository(tmp_path)
    for field in RESOURCE_FIELDS:
        assert len(repository.get_shared()[field]) == 2
    office = repository.get_compat_config('office')
    third = repository.get_compat_config('third')
    for field in RESOURCE_FIELDS[:3]:
        assert third[field] == office[field]


@pytest.mark.parametrize('names', [('a/b', 'a?b'), ('x' * 205 + 'one', 'x' * 205 + 'two')])
def test_rule_cache_names_and_native_rule_references_remain_distinct(tmp_path, names):
    from backend.utils.rule_utils import sanitize_rule_name
    bundle = _bundle()
    for profile, name in zip(bundle['profiles'].values(), names):
        profile['rule_library'][0]['name'] = name
        profile['rule_configs'].append({'id': 'native', 'itemType': 'rule',
                                       'rule_type': 'RULE-SET', 'value': name, 'policy': 'Main'})
    _install_split(tmp_path, bundle)
    repository = ProfileRepository(tmp_path)
    shared = repository.get_shared()['rule_library']
    assert len({sanitize_rule_name(item['name']) for item in shared}) == 2
    for profile_id, old in bundle['profiles'].items():
        resolved = repository.get_compat_config(profile_id)
        library_id = resolved['rule_configs'][1]['library_rule_id']
        library = next(item for item in shared if item['id'] == library_id)
        assert resolved['rule_configs'][-1]['value'] == library['name']
        cache = repository.shared_rules_dir() / (sanitize_rule_name(library['name']) + '.list')
        assert cache.read_text() == old['rule_library'][0]['content']


def test_subscription_and_aggregation_provider_names_do_not_overwrite_each_other(tmp_path):
    bundle = _bundle()
    for profile in bundle['profiles'].values():
        profile['subscription_aggregations'][0]['name'] = profile['subscriptions'][0]['name']
    _install_split(tmp_path, bundle)
    repository = ProfileRepository(tmp_path)
    for profile_id in bundle['profiles']:
        resolved = repository.get_compat_config(profile_id)
        assert resolved['subscriptions'][0]['name'] != resolved['subscription_aggregations'][0]['name']
        assert resolved['subscription_aggregations'][0]['subscriptions'] == [resolved['subscriptions'][0]['id']]


def test_linked_rules_keep_their_original_emitted_behavior_and_name(tmp_path):
    bundle = _bundle()
    profile = bundle['profiles']['office']
    profile['rule_configs'][1].update(name='Custom source', behavior='classical')
    profile['rule_configs'].append({'id': 'native', 'itemType': 'rule',
                                   'rule_type': 'RULE-SET', 'value': 'Custom source', 'policy': 'Main'})
    _install_split(tmp_path, bundle)
    repository = ProfileRepository(tmp_path)
    rules = repository.get_compat_config('office')['rule_configs']
    assert rules[1]['name'] == 'Custom source'
    assert rules[1]['behavior'] == 'classical'
    assert rules[1]['content'] == profile['rule_library'][0]['content']
    assert rules[-1]['value'] == rules[1]['name']


def test_subscription_cache_uses_each_profiles_source_not_stale_shared_cache(tmp_path):
    bundle = _bundle()
    _install_split(tmp_path, bundle)
    _write_json(tmp_path / 'shared/subscribes/sub.json', {'nodes': [{'server': 'obsolete.example'}]})
    for profile_id in bundle['profiles']:
        _write_json(tmp_path / 'profiles' / profile_id / 'subscribes/sub.json', {
            'subscription_id': 'sub', 'nodes': [{'server': profile_id + '.example', 'subscription_id': 'sub'}],
        })
    repository = ProfileRepository(tmp_path)
    for profile_id in bundle['profiles']:
        sub = repository.get_compat_config(profile_id)['subscriptions'][0]
        cache = repository.read_shared_json('subscribes/' + sub['id'] + '.json')
        assert cache['subscription_id'] == sub['id']
        assert cache['nodes'] == [{'server': profile_id + '.example', 'subscription_id': sub['id']}]


def test_missing_profile_cache_does_not_reuse_unrelated_shared_nodes(tmp_path):
    _install_split(tmp_path, _bundle())
    _write_json(tmp_path / 'shared/subscribes/sub.json', {'nodes': [{'server': 'unrelated.example'}]})
    repository = ProfileRepository(tmp_path)
    assert not (repository.shared_cache_dir() / 'sub.json').exists()


def test_missing_local_resource_is_not_resolved_from_another_profile(tmp_path):
    bundle = _bundle()
    bundle['profiles']['office']['subscriptions'] = []
    before = _install_split(tmp_path, bundle)
    with pytest.raises(ProfileRepositoryError):
        ProfileRepository(tmp_path)
    _assert_preserved(tmp_path, before)


def test_unknown_index_version_cannot_be_hidden_by_older_backup(tmp_path):
    bundle = _bundle()
    _install_split(tmp_path, bundle, backups=True)
    bundle['system']['schema_version'] = 999
    _write_json(tmp_path / 'system.json', bundle['system'])
    before = _source_bytes(tmp_path)
    with pytest.raises(ProfileRepositoryError):
        ProfileRepository(tmp_path)
    _assert_preserved(tmp_path, before)


def test_binary_stale_root_is_restored_if_commit_fails_after_replace(tmp_path, monkeypatch):
    bundle = _bundle()
    _install_split(tmp_path, bundle)
    (tmp_path / 'config.json').write_bytes(b'\xff\x00broken legacy root')
    before = _source_bytes(tmp_path)
    real_replace = os.replace
    failed = False
    def fail_once(source, destination):
        nonlocal failed
        real_replace(source, destination)
        if Path(destination) == tmp_path / 'config.json' and not failed:
            failed = True
            raise OSError(errno.ENOSPC, 'Injected full disk after replace')
    with monkeypatch.context() as patch:
        patch.setattr(os, 'replace', fail_once)
        with pytest.raises(OSError):
            ProfileRepository(tmp_path)
    _assert_preserved(tmp_path, before)
    _assert_migrated(ProfileRepository(tmp_path), bundle)


def _raw_dialer_bundle(representation, *, identical_source=False):
    bundle = _bundle()
    for profile_id in bundle["profiles"]:
        target = {
            "id": "node-a", "name": "A", "type": "ss",
            "server": f"a-{profile_id}.example", "port": 1443, "enabled": True,
            "params": {"cipher": "aes-128-gcm", "password": "fixture-password"},
        }
        source = {
            "id": "node-b", "name": "B", "type": "ss",
            "server": "b-shared.example" if identical_source else f"b-{profile_id}.example",
            "port": 2443, "enabled": True,
            "params": {"cipher": "aes-128-gcm", "password": "fixture-password",
                       "dialer-proxy": "A"},
        }
        if representation != "params":
            import yaml
            proxy = {key: source[key] for key in ("name", "type", "server", "port")}
            proxy.update(source.pop("params"))
            source["proxy_string"] = (json.dumps(proxy) if representation == "json"
                                      else yaml.safe_dump(proxy, sort_keys=False))
        bundle["profiles"][profile_id] = {
            **{field: [] for field in RESOURCE_FIELDS},
            "nodes": [target, source],
            "proxy_groups": [{
                "id": "main", "name": "Main", "type": "select",
                "manual_nodes": ["node-a", "node-b"],
                "proxies_order": [{"type": "node", "id": "node-a"},
                                  {"type": "node", "id": "node-b"}],
            }],
            "rule_configs": [], "mihomo": {}, "surge": {}, "mosdns": {},
        }
    return bundle


def _assert_local_raw_dialer_topology(repository, bundle):
    import yaml
    from backend.converters.mihomo import generate_mihomo_config
    from backend.utils.dialer_references import dialer_target

    source_ids = set()
    for profile_id, original in bundle["profiles"].items():
        expected_target, expected_source = original["nodes"]
        resolved = repository.get_compat_config(profile_id)
        nodes_by_server = {node["server"]: node for node in resolved["nodes"]}
        assert len(resolved["nodes"]) == len(nodes_by_server) == 2
        assert set(nodes_by_server) == {expected_target["server"], expected_source["server"]}
        target = nodes_by_server[expected_target["server"]]
        source = nodes_by_server[expected_source["server"]]
        source_ids.add(source["id"])
        assert dialer_target(resolved, source) == ("node", target["id"])
        group = resolved["proxy_groups"][0]
        assert group["manual_nodes"] == [target["id"], source["id"]]
        assert group["proxies_order"] == [
            {"type": "node", "id": target["id"]},
            {"type": "node", "id": source["id"]},
        ]

        generated = yaml.safe_load(generate_mihomo_config(resolved))
        emitted = {proxy["name"]: proxy for proxy in generated["proxies"]}
        assert len(generated["proxies"]) == len(emitted) == 2
        assert {proxy["server"] for proxy in emitted.values()} == set(nodes_by_server)
        emitted_source = emitted[source["name"]]
        emitted_target = emitted[emitted_source["dialer-proxy"]]
        assert emitted_source["server"] == expected_source["server"]
        assert emitted_target["server"] == expected_target["server"]
        assert emitted_target["name"] == target["name"]
        emitted_group = next(item for item in generated["proxy-groups"]
                             if item["name"] == group["name"])
        assert emitted_group["proxies"] == [target["name"], source["name"]]
    return source_ids


@pytest.mark.parametrize("representation", ["params", "json", "yaml"])
@pytest.mark.parametrize("entrypoint", ["disk", "import"])
def test_cross_profile_same_name_raw_dialers_keep_local_targets(tmp_path, representation, entrypoint):
    bundle = _raw_dialer_bundle(representation)
    original = copy.deepcopy(bundle)
    if entrypoint == "disk":
        before = _install_split(tmp_path, bundle, backups=True)
        repository = ProfileRepository(tmp_path, initial_config_factory=_no_template)
        _assert_preserved(tmp_path, before, migrated=True)
    else:
        repository = ProfileRepository(tmp_path)
        repository.import_all(bundle)
    assert bundle == original
    assert repository.export_all()["schema_version"] == 5
    _assert_local_raw_dialer_topology(repository, bundle)
    _assert_local_raw_dialer_topology(ProfileRepository(tmp_path), bundle)


@pytest.mark.parametrize("representation", ["params", "json", "yaml"])
@pytest.mark.parametrize("entrypoint", ["disk", "import"])
def test_identical_raw_dialer_sources_do_not_merge_distinct_local_targets(tmp_path, representation, entrypoint):
    bundle = _raw_dialer_bundle(representation, identical_source=True)
    home_nodes = bundle["profiles"]["default"]["nodes"]
    office_nodes = bundle["profiles"]["office"]["nodes"]
    assert home_nodes[1] == office_nodes[1]
    assert home_nodes[0]["server"] != office_nodes[0]["server"]
    if entrypoint == "disk":
        before = _install_split(tmp_path, bundle, backups=True)
        repository = ProfileRepository(tmp_path, initial_config_factory=_no_template)
        _assert_preserved(tmp_path, before, migrated=True)
    else:
        repository = ProfileRepository(tmp_path)
        repository.import_all(bundle)
    assert len(_assert_local_raw_dialer_topology(repository, bundle)) == 2
    assert len(_assert_local_raw_dialer_topology(ProfileRepository(tmp_path), bundle)) == 2


@pytest.mark.parametrize("representation", ["params", "json", "yaml"])
def test_ambiguous_raw_dialer_within_one_profile_preserves_split_sources(tmp_path, representation):
    bundle = _raw_dialer_bundle(representation)
    profile = bundle["profiles"]["default"]
    duplicate = copy.deepcopy(profile["nodes"][0])
    duplicate.update(id="other-a", server="ambiguous-a.example")
    profile["nodes"].append(duplicate)
    profile["proxy_groups"][0]["manual_nodes"].append(duplicate["id"])
    profile["proxy_groups"][0]["proxies_order"].append({"type": "node", "id": duplicate["id"]})
    before = _install_split(tmp_path, bundle, backups=True)
    with pytest.raises(ProfileRepositoryError):
        ProfileRepository(tmp_path, initial_config_factory=_no_template)
    _assert_preserved(tmp_path, before)


def test_multihop_raw_dialers_keep_implicit_targets_and_node_policies(tmp_path):
    import yaml
    from backend.converters.mihomo import generate_mihomo_config

    bundle = _raw_dialer_bundle('params', identical_source=True)
    for profile in bundle['profiles'].values():
        profile['nodes'].append({'id': 'node-c', 'name': 'C', 'type': 'socks5',
                                 'server': 'c-shared.example', 'port': 1080,
                                 'params': {'dialer-proxy': 'B'}})
        profile['proxy_groups'][0]['manual_nodes'] = ['node-c']
        profile['proxy_groups'][0]['proxies_order'] = [{'type': 'node', 'id': 'node-c'}]
        profile['rule_configs'] = [{'id': 'direct-node', 'itemType': 'rule',
                                   'rule_type': 'DOMAIN', 'value': 'example.com', 'policy': 'A'}]
    _install_split(tmp_path, bundle)
    repository = ProfileRepository(tmp_path)
    for profile_id in bundle['profiles']:
        resolved = repository.get_compat_config(profile_id)
        generated = yaml.safe_load(generate_mihomo_config(resolved))
        proxies = {proxy['name']: proxy for proxy in generated['proxies']}
        c = next(proxy for proxy in proxies.values() if proxy['server'] == 'c-shared.example')
        b = proxies[c['dialer-proxy']]
        a = proxies[b['dialer-proxy']]
        assert b['server'] == 'b-shared.example'
        assert a['server'] == f'a-{profile_id}.example'
        assert generated['rules'] == [f'DOMAIN,example.com,{a["name"]}']


@pytest.mark.parametrize("representation", ["params", "json", "yaml"])
@pytest.mark.parametrize("entrypoint", ["disk", "import"])
@pytest.mark.parametrize("different_subscription", [True, False])
def test_identical_raw_dialer_nodes_preserve_subscription_source_identity(
    tmp_path, representation, entrypoint, different_subscription
):
    bundle = _raw_dialer_bundle(representation, identical_source=True)
    for profile_id, profile in bundle["profiles"].items():
        profile["nodes"][0].update(server="a-shared.example", subscription_id="sub")
        hostname = profile_id if different_subscription else "shared"
        profile["subscriptions"] = [{
            "id": "sub", "name": "Source", "url": f"https://{hostname}.example/sub",
            "enabled": True,
        }]
    assert bundle["profiles"]["default"]["nodes"] == bundle["profiles"]["office"]["nodes"]
    original = copy.deepcopy(bundle)
    if entrypoint == "disk":
        before = _install_split(tmp_path, bundle, backups=True)
        repository = ProfileRepository(tmp_path, initial_config_factory=_no_template)
        _assert_preserved(tmp_path, before, migrated=True)
    else:
        repository = ProfileRepository(tmp_path)
        repository.import_all(bundle)
    assert bundle == original
    expected_variants = 2 if different_subscription else 1
    for current in (repository, ProfileRepository(tmp_path)):
        source_ids = _assert_local_raw_dialer_topology(current, bundle)
        assert len(source_ids) == expected_variants
        target_ids = set()
        subscription_ids = set()
        for profile_id, old in bundle["profiles"].items():
            resolved = current.get_compat_config(profile_id)
            target = next(node for node in resolved["nodes"] if node["server"] == "a-shared.example")
            assert len(resolved["subscriptions"]) == 1
            subscription = resolved["subscriptions"][0]
            assert subscription["url"] == old["subscriptions"][0]["url"]
            assert target["subscription_id"] == subscription["id"]
            target_ids.add(target["id"])
            subscription_ids.add(subscription["id"])
        assert len(target_ids) == len(subscription_ids) == expected_variants
