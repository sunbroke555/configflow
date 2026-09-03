import json
from flask import Flask
from urllib.parse import parse_qs, urlsplit

import pytest

from backend.common import config as config_module
from backend.common.config_repository import ProfileRepository
from backend.routes import register_blueprints
from backend.routes.aggregations import generate_aggregation_provider
from backend.converters.mihomo import get_mihomo_provider_downloads
from backend.converters.mosdns import get_mosdns_ruleset_downloads
from backend.routes.rules import get_ruleset_content, normalize_rule_config_url
from backend.utils import subscription_cache
from backend.utils.rule_utils import get_rules_dir


def setup_repository(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    repository.create_profile({"id": "alpha", "name": "Alpha"})
    repository.create_profile({"id": "beta", "name": "Beta"})
    config_module.set_repository(repository)
    return repository


def test_subscription_and_rule_caches_are_profile_scoped(tmp_path, monkeypatch):
    repository = setup_repository(tmp_path, monkeypatch)
    app = Flask(__name__)

    with app.test_request_context("/", headers={"X-ConfigFlow-Profile": "alpha"}):
        subscription_cache.save_subscription_nodes("same", [{"name": "alpha-node"}])
        alpha_rules = get_rules_dir()
    with app.test_request_context("/", headers={"X-ConfigFlow-Profile": "beta"}):
        subscription_cache.save_subscription_nodes("same", [{"name": "beta-node"}])
        beta_rules = get_rules_dir()

    with app.test_request_context("/", headers={"X-ConfigFlow-Profile": "alpha"}):
        assert subscription_cache.load_subscription_cache("same")["nodes"][0]["name"] == "alpha-node"
    with app.test_request_context("/", headers={"X-ConfigFlow-Profile": "beta"}):
        assert subscription_cache.load_subscription_cache("same")["nodes"][0]["name"] == "beta-node"
    assert alpha_rules == str(repository.rules_dir("alpha"))
    assert beta_rules == str(repository.rules_dir("beta"))
    assert alpha_rules != beta_rules


def test_aggregation_provider_is_profile_scoped(tmp_path, monkeypatch):
    repository = setup_repository(tmp_path, monkeypatch)
    app = Flask(__name__)
    aggregation = {"id": "agg-same", "name": "Same", "subscriptions": [], "nodes": []}

    with app.test_request_context("/", headers={"X-ConfigFlow-Profile": "alpha"}):
        alpha_path = generate_aggregation_provider(aggregation)["file_path"]
    with app.test_request_context("/", headers={"X-ConfigFlow-Profile": "beta"}):
        beta_path = generate_aggregation_provider(aggregation)["file_path"]

    assert alpha_path == str(repository.providers_dir("alpha") / "agg-same.yaml")
    assert beta_path == str(repository.providers_dir("beta") / "agg-same.yaml")
    assert alpha_path != beta_path


def test_generated_configuration_is_written_under_selected_profile(tmp_path, monkeypatch):
    repository = setup_repository(tmp_path, monkeypatch)
    app = Flask(__name__)
    register_blueprints(app)
    monkeypatch.setattr("backend.routes.generate.generate_mihomo_config", lambda data, base_url="": "profile-config")

    response = app.test_client().post(
        "/api/generate/mihomo",
        headers={"X-ConfigFlow-Profile": "alpha"},
        json={},
    )

    assert response.status_code == 200
    assert (repository.generated_dir("alpha") / "config.yaml").read_text(encoding="utf-8") == "profile-config"
    assert not (repository.generated_dir("beta") / "config.yaml").exists()


def test_generated_provider_urls_include_the_selected_profile(tmp_path, monkeypatch):
    repository = setup_repository(tmp_path, monkeypatch)
    config = repository.get_compat_config("alpha")
    config["system_config"]["server_domain"] = "http://configflow.test"
    config["subscriptions"] = [{
        "id": "sub-1",
        "name": "Primary",
        "url": "https://example.test/sub",
        "enabled": True,
    }]
    config["proxy_groups"] = [{
        "id": "group-1",
        "name": "Proxy",
        "type": "select",
        "enabled": True,
        "subscriptions": ["sub-1"],
    }]
    config_module.set_repository(repository)

    downloads = get_mihomo_provider_downloads(config, base_url="http://fallback.test")

    assert downloads[0]["url"] == "http://configflow.test/api/profiles/alpha/subscriptions/sub-1/proxies"


def test_mosdns_rule_proxy_urls_use_url_encoded_internal_token_for_profile_and_legacy():
    rule_set = {
        "id": "rules-1", "name": "Rules", "itemType": "ruleset",
        "url": "https://rules.test/list?a=1&b=2",
    }
    common = {
        "system_config": {
            "server_domain": "https://config.test",
            "config_token": "legacy-public-token",
            "rule_proxy_token": "internal /&?",
        },
        "mosdns": {"direct_rulesets": ["rules-1"], "proxy_rulesets": []},
        "rule_configs": [rule_set],
    }

    profile_url = get_mosdns_ruleset_downloads({**common, "profile_id": "alpha"})[0]["url"]
    legacy_url = get_mosdns_ruleset_downloads(common)[0]["url"]

    assert profile_url == (
        "https://config.test/api/profiles/alpha/mosdns/rule-proxy"
        "?url=https%3A%2F%2Frules.test%2Flist%3Fa%3D1%26b%3D2&token=internal%20%2F%26%3F"
    )
    assert legacy_url == (
        "https://config.test/api/mosdns/rule-proxy"
        "?url=https%3A%2F%2Frules.test%2Flist%3Fa%3D1%26b%3D2&token=internal%20%2F%26%3F"
    )


def test_mosdns_refuses_to_generate_rule_proxy_url_without_internal_token():
    config = {
        "system_config": {"server_domain": "https://config.test", "config_token": ""},
        "mosdns": {"direct_rulesets": ["rules-1"], "proxy_rulesets": []},
        "rule_configs": [{
            "id": "rules-1", "name": "Rules", "itemType": "ruleset",
            "url": "https://rules.test/list",
        }],
    }

    with pytest.raises(ValueError, match="rule proxy token"):
        get_mosdns_ruleset_downloads(config)


def _assert_generated_default_url_authenticates(repository, monkeypatch):
    repository.save_profile("default", {
        "system_config": {"server_domain": "https://config.test"},
        "mosdns": {"direct_rulesets": ["rules-1"], "proxy_rulesets": []},
        "rule_configs": [{
            "id": "rules-1", "name": "Rules", "itemType": "ruleset",
            "url": "https://rules.test/list",
        }],
    })
    config_module.set_repository(repository)
    token = repository.get_system()["system_config"]["rule_proxy_token"]
    generated_url = get_mosdns_ruleset_downloads(repository.get_compat_config("default"))[0]["url"]
    parsed = urlsplit(generated_url)
    assert parse_qs(parsed.query)["token"] == [token]
    monkeypatch.setattr("backend.routes.mosdns._fetch_remote_content", lambda url: "domain:example.com")
    monkeypatch.setattr(
        "backend.routes.mosdns.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    app = Flask(__name__)
    register_blueprints(app)
    response = app.test_client().get(f"{parsed.path}?{parsed.query}")
    assert response.status_code == 200
    assert response.get_data(as_text=True) == "domain:example.com"


def test_empty_data_directory_generates_mosdns_url_accepted_by_rule_proxy(tmp_path, monkeypatch):
    _assert_generated_default_url_authenticates(ProfileRepository(tmp_path), monkeypatch)


def test_legacy_empty_token_generates_mosdns_url_accepted_by_rule_proxy(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(
        json.dumps({"system_config": {"config_token": ""}}),
        encoding="utf-8",
    )
    _assert_generated_default_url_authenticates(ProfileRepository(tmp_path), monkeypatch)


def test_generated_profile_rule_proxy_url_downloads_with_auth_enabled(tmp_path, monkeypatch):
    repository = setup_repository(tmp_path, monkeypatch)
    repository.save_profile("alpha", {
        "system_config": {"server_domain": "https://config.test", "config_token": "profile token"},
        "mosdns": {"direct_rulesets": ["rules-1"], "proxy_rulesets": []},
        "rule_configs": [{
            "id": "rules-1", "name": "Rules", "itemType": "ruleset",
            "url": "https://rules.test/list",
        }],
    })
    config = repository.get_compat_config("alpha")
    generated_url = get_mosdns_ruleset_downloads(config)[0]["url"]
    parsed = urlsplit(generated_url)
    fetched = []
    monkeypatch.setattr("backend.common.auth.is_auth_enabled", lambda: True)
    monkeypatch.setattr("backend.routes.auth.is_auth_enabled", lambda: True)
    monkeypatch.setattr(
        "backend.routes.mosdns._fetch_remote_content",
        lambda url: fetched.append(url) or "domain:example.com",
    )
    monkeypatch.setattr(
        "backend.routes.mosdns.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    app = Flask(__name__)
    register_blueprints(app)
    from backend.routes.auth import setup_before_request
    setup_before_request(app)

    response = app.test_client().get(f"{parsed.path}?{parsed.query}")

    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_data(as_text=True) == "domain:example.com"
    assert fetched == ["https://rules.test/list"]


def test_profile_resource_aliases_include_rule_library(tmp_path, monkeypatch):
    repository = setup_repository(tmp_path, monkeypatch)
    repository.save_profile("alpha", {
        "rule_library": [{"id": "rule-1", "name": "Local", "source_type": "content", "content": "DOMAIN,example.test"}],
    })
    app = Flask(__name__)
    register_blueprints(app)

    response = app.test_client().get("/api/profiles/alpha/rule-library")

    assert response.status_code == 200
    assert response.get_json()[0]["id"] == "rule-1"


def test_aggregation_workers_keep_the_selected_profile_context(tmp_path, monkeypatch):
    repository = setup_repository(tmp_path, monkeypatch)
    repository.save_profile("alpha", {
        "subscriptions": [{
            "id": "sub-1",
            "name": "Primary",
            "url": "https://example.test/sub",
            "enabled": True,
        }],
    })
    app = Flask(__name__)
    seen_profiles = []
    monkeypatch.setattr(
        "backend.routes.aggregations.get_subscription_proxies_yaml",
        lambda sub_id, url: ("ignored", "test"),
    )
    monkeypatch.setattr(
        "backend.routes.aggregations.parse_proxies_from_yaml",
        lambda text: [{"name": "node-1"}],
    )
    monkeypatch.setattr(
        "backend.routes.aggregations.proxies_to_nodes",
        lambda proxies: proxies,
    )
    monkeypatch.setattr(
        "backend.routes.aggregations.save_subscription_nodes",
        lambda sub_id, nodes, metadata=None, profile_id=None: seen_profiles.append(profile_id) or {"nodes": nodes},
    )
    monkeypatch.setattr(
        "backend.routes.aggregations.load_subscription_cache",
        lambda sub_id, profile_id=None: seen_profiles.append(profile_id) or None,
    )

    with app.test_request_context("/", headers={"X-ConfigFlow-Profile": "alpha"}):
        generate_aggregation_provider({
            "id": "agg-1",
            "name": "Aggregation",
            "subscriptions": ["sub-1"],
            "nodes": [],
        })

    assert seen_profiles == ["alpha"]


def test_local_rule_refresh_uses_profile_repository_write(tmp_path, monkeypatch):
    repository = setup_repository(tmp_path, monkeypatch)
    repository.save_profile("alpha", {
        "rule_library": [{
            "id": "rule-1",
            "name": "Remote",
            "source_type": "url",
            "url": "https://example.test/rules",
        }],
    })
    app = Flask(__name__)
    register_blueprints(app)
    calls = []
    original_write = repository.write_profile_text

    def write_profile_text(profile_id, relative_path, content):
        calls.append(profile_id)
        return original_write(profile_id, relative_path, content)

    monkeypatch.setattr(repository, "write_profile_text", write_profile_text)
    monkeypatch.setattr(
        "backend.routes.rules.requests.get",
        lambda url, timeout: type("Response", (), {"status_code": 200, "text": "DOMAIN,remote.test"})(),
    )

    response = app.test_client().get(
        "/api/profiles/alpha/rules/local/Remote",
        headers={"X-ConfigFlow-Profile": "beta"},
    )

    assert response.status_code == 200
    assert calls == ["alpha"]


def test_rule_library_content_urls_include_the_selected_profile(tmp_path, monkeypatch):
    repository = setup_repository(tmp_path, monkeypatch)
    repository.save_profile("alpha", {
        "rule_library": [{"id": "rule-1", "source_type": "content"}],
    })
    app = Flask(__name__)
    config_module.set_repository(repository)

    with app.test_request_context("/", headers={"X-ConfigFlow-Profile": "alpha"}):
        rule = {"library_rule_id": "rule-1"}
        normalize_rule_config_url(rule)

    assert rule["url"] == "/api/profiles/alpha/rule-library/content/rule-1"


def test_rule_cache_workers_keep_the_selected_profile_context(tmp_path, monkeypatch):
    repository = setup_repository(tmp_path, monkeypatch)
    repository.save_profile("alpha", {
        "rule_library": [{
            "id": "rule-1",
            "name": "Remote",
            "source_type": "url",
            "url": "https://example.test/rules",
        }],
    })
    app = Flask(__name__)
    register_blueprints(app)
    seen_profiles = []
    monkeypatch.setattr(
        "backend.utils.rule_utils.save_rule_to_local",
        lambda rule, profile_id=None: seen_profiles.append(profile_id) or "cached.list",
    )

    response = app.test_client().post(
        "/api/rule-library/cache",
        headers={"X-ConfigFlow-Profile": "alpha"},
        json={"rule_ids": ["rule-1"]},
    )

    assert response.status_code == 200
    assert seen_profiles == ["alpha"]


def test_ruleset_content_cache_uses_profile_repository_write(tmp_path, monkeypatch):
    repository = setup_repository(tmp_path, monkeypatch)
    app = Flask(__name__)
    calls = []
    original_write = repository.write_profile_text

    def write_profile_text(profile_id, relative_path, content):
        calls.append(profile_id)
        return original_write(profile_id, relative_path, content)

    monkeypatch.setattr(repository, "write_profile_text", write_profile_text)
    monkeypatch.setattr(
        "backend.routes.rules.requests.get",
        lambda url, timeout: type("Response", (), {"status_code": 200, "text": "DOMAIN,remote.test"})(),
    )

    with app.test_request_context("/", headers={"X-ConfigFlow-Profile": "alpha"}):
        content = get_ruleset_content(
            {"name": "Remote"},
            {"name": "Remote", "source_type": "url", "url": "https://example.test/rules"},
        )

    assert content == "DOMAIN,remote.test"
    assert calls == ["alpha"]


def test_corrupt_subscription_cache_is_ignored(tmp_path, monkeypatch):
    repository = setup_repository(tmp_path, monkeypatch)
    (repository.cache_dir("alpha") / "broken.json").write_text("{", encoding="utf-8")
    app = Flask(__name__)

    with app.test_request_context("/", headers={"X-ConfigFlow-Profile": "alpha"}):
        cached = subscription_cache.load_subscription_cache("broken")

    assert cached is None
