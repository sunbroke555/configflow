import json
import socket

import pytest
from flask import Flask

from backend.common import config as config_module
from backend.common.auth import MAX_AUTH_TOKEN_LENGTH, generate_token
from backend.common.config_repository import ProfileRepository
from backend.routes import register_blueprints
from backend.routes.auth import setup_before_request
from backend.routes import mosdns


def auth_app(monkeypatch):
    monkeypatch.setattr("backend.routes.auth.is_auth_enabled", lambda: True)
    app = Flask(__name__)
    setup_before_request(app)
    return app


def test_auth_public_paths_are_exact_and_stats_is_protected(monkeypatch):
    app = auth_app(monkeypatch)
    client = app.test_client()

    assert client.get("/").status_code == 404  # public matching, route absent
    assert client.get("/anything").status_code == 401
    assert client.get("/api/auth/status-extra").status_code == 401
    assert client.get("/api/stats/overview").status_code == 401


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer not-a-valid-jwt"},
    ],
    ids=["missing-jwt", "invalid-jwt"],
)
def test_profile_provider_rejects_anonymous_when_auth_enabled_and_config_token_empty(
    monkeypatch, tmp_path, headers
):
    repository = ProfileRepository(tmp_path)
    repository.create_profile({"id": "alpha", "name": "Alpha"})
    repository.save_profile("default", {"system_config": {"config_token": ""}})
    config_module.set_repository(repository)
    monkeypatch.setattr("backend.common.auth.is_auth_enabled", lambda: True)
    monkeypatch.setattr("backend.routes.auth.is_auth_enabled", lambda: True)
    app = Flask(__name__)
    register_blueprints(app)
    setup_before_request(app)

    response = app.test_client().get(
        "/api/profiles/alpha/aggregations/missing/provider",
        headers=headers,
    )

    assert response.status_code == 401


def _config_endpoint_app(monkeypatch, tmp_path, *, auth_enabled, config_token=None):
    repository = ProfileRepository(tmp_path)
    repository.create_profile({"id": "alpha", "name": "Alpha"})
    if config_token is not None:
        repository.save_profile("default", {"system_config": {"config_token": config_token}})
    config_module.set_repository(repository)
    monkeypatch.setattr("backend.common.auth.is_auth_enabled", lambda: auth_enabled)
    monkeypatch.setattr("backend.routes.auth.is_auth_enabled", lambda: auth_enabled)
    app = Flask(__name__)
    app.config["TEST_CONFIG_TOKEN"] = repository.get_system()["system_config"].get("config_token", "")
    app.config["TEST_RULE_PROXY_TOKEN"] = repository.get_system()["system_config"]["rule_proxy_token"]
    register_blueprints(app)
    setup_before_request(app)
    return app


@pytest.mark.parametrize("target", ["mihomo", "surge", "mosdns"])
@pytest.mark.parametrize("path", ["/api/config/{target}", "/api/config/alpha/{target}"])
@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer not-a-valid-jwt"}],
    ids=["missing-jwt", "invalid-jwt"],
)
def test_public_config_endpoints_require_valid_jwt_when_auth_enabled_and_config_token_empty(
    monkeypatch, tmp_path, target, path, headers
):
    app = _config_endpoint_app(monkeypatch, tmp_path, auth_enabled=True, config_token="")

    response = app.test_client().get(path.format(target=target), headers=headers)

    assert response.status_code == 401


@pytest.mark.parametrize("path", ["/api/config/mihomo", "/api/config/alpha/mihomo"])
def test_public_config_endpoints_accept_valid_jwt_when_config_token_empty(monkeypatch, tmp_path, path):
    app = _config_endpoint_app(monkeypatch, tmp_path, auth_enabled=True, config_token="")

    response = app.test_client().get(
        path,
        headers={"Authorization": f"Bearer {generate_token('admin')}"},
    )

    assert response.status_code == 200


@pytest.mark.parametrize(
    "authorization",
    [
        lambda token: f"Bearer {token} extra",
        lambda token: f"Bearer  {token}",
        lambda token: f"Bearer\t{token}",
        lambda token: "Bearer café",
        lambda token: f"Bearer {'x' * (MAX_AUTH_TOKEN_LENGTH + 1)}",
    ],
    ids=["trailing-field", "double-space", "tab", "unicode", "oversized"],
)
def test_public_config_endpoint_rejects_malformed_bearer(monkeypatch, tmp_path, authorization):
    app = _config_endpoint_app(monkeypatch, tmp_path, auth_enabled=True, config_token="")
    token = generate_token("admin")

    response = app.test_client().get(
        "/api/config/mihomo",
        headers={"Authorization": authorization(token)},
    )

    assert response.status_code == 401


def test_public_config_endpoint_rejects_oversized_query_token(monkeypatch, tmp_path):
    token = "x" * (MAX_AUTH_TOKEN_LENGTH + 1)
    app = _config_endpoint_app(monkeypatch, tmp_path, auth_enabled=True, config_token=token)

    response = app.test_client().get("/api/config/mihomo", query_string={"token": token})

    assert response.status_code == 401


@pytest.mark.parametrize("path", ["/api/config/mihomo", "/api/config/alpha/mihomo"])
def test_public_config_endpoints_remain_anonymous_when_auth_disabled(monkeypatch, tmp_path, path):
    app = _config_endpoint_app(monkeypatch, tmp_path, auth_enabled=False)

    assert app.test_client().get(path).status_code == 200


def test_anonymous_config_token_response_does_not_expose_internal_rule_proxy_token(monkeypatch, tmp_path):
    app = _config_endpoint_app(monkeypatch, tmp_path, auth_enabled=False, config_token="")

    response = app.test_client().get("/api/config-token")

    assert response.status_code == 200
    assert response.get_json() == {"config_token": ""}
    assert app.config["TEST_RULE_PROXY_TOKEN"] not in response.get_data(as_text=True)


def test_config_token_post_rejects_internal_rule_proxy_token(monkeypatch, tmp_path):
    app = _config_endpoint_app(monkeypatch, tmp_path, auth_enabled=False, config_token="public-token")
    repository = config_module.get_repository()
    before = repository.get_system()["system_config"]
    internal_token = app.config["TEST_RULE_PROXY_TOKEN"]

    response = app.test_client().post("/api/config-token", json={"token": internal_token})

    assert response.status_code == 400
    assert response.get_json()["success"] is False
    assert repository.get_system()["system_config"] == before


@pytest.mark.parametrize("layers", [0, 1, 2, 8, 20])
def test_config_token_post_rejects_embedded_encoded_current_or_retired_internal_token(
    monkeypatch, tmp_path, layers
):
    app = _config_endpoint_app(monkeypatch, tmp_path, auth_enabled=False, config_token="public-token")
    repository = config_module.get_repository()
    system = repository.get_system()
    current = system["system_config"]["rule_proxy_token"]
    retired = "retired-internal-token"
    system["system_config"]["retired_rule_proxy_tokens"] = [retired]
    repository._write_system(system)
    before = repository.get_system()["system_config"]

    for internal in (current, retired):
        encoded = internal
        for index in range(layers):
            encoded = (
                __import__("urllib.parse", fromlist=["quote"]).quote(encoded, safe="")
                if index % 2 == 0
                else __import__("urllib.parse", fromlist=["quote_plus"]).quote_plus(encoded, safe="")
            )
        response = app.test_client().post(
            "/api/config-token", json={"token": f"managed::{encoded}::suffix"}
        )
        assert response.status_code == 400
        assert repository.get_system()["system_config"] == before


def test_config_token_get_scrubs_legacy_embedded_internal_token(monkeypatch, tmp_path):
    app = _config_endpoint_app(monkeypatch, tmp_path, auth_enabled=False, config_token="public-token")
    repository = config_module.get_repository()
    system = repository.get_system()
    internal = system["system_config"]["rule_proxy_token"]
    system["system_config"]["config_token"] = f"legacy::{internal}::embedded"
    repository._write_system(system)

    response = app.test_client().get("/api/config-token")

    assert response.status_code == 200
    assert response.get_json() == {"config_token": "[REDACTED]"}
    assert internal not in response.get_data(as_text=True)


def test_retired_rule_proxy_token_never_authorizes_config_rule_proxy_or_mcp(monkeypatch, tmp_path):
    repository = ProfileRepository(tmp_path)
    repository.save_profile("default", {"system_config": {"config_token": "public-token"}})
    system = repository.get_system()
    retired = "retired-internal-token"
    system["system_config"]["retired_rule_proxy_tokens"] = [retired]
    system["system_config"]["config_token"] = retired
    repository._write_system(system)
    config_module.set_repository(repository)
    monkeypatch.setattr("backend.common.auth.is_auth_enabled", lambda: True)
    monkeypatch.setattr("backend.routes.auth.is_auth_enabled", lambda: True)
    monkeypatch.setattr(mosdns, "_fetch_remote_content", lambda url: "must-not-fetch")
    from backend.mcp_server import mcp_bp

    app = Flask(__name__)
    register_blueprints(app)
    app.register_blueprint(mcp_bp)
    setup_before_request(app)
    client = app.test_client()

    assert client.get("/api/config/mihomo", query_string={"token": retired}).status_code == 401
    assert client.get(
        "/api/mosdns/rule-proxy",
        query_string={"url": "https://example.com/rules", "token": retired},
    ).status_code == 401
    monkeypatch.setattr(
        "backend.common.auth.verify_token",
        lambda token: {"username": "admin"} if token == retired else None,
    )
    assert client.get(
        "/api/mosdns/rule-proxy",
        query_string={"url": "https://example.com/rules"},
        headers={"Authorization": f"Bearer {retired}"},
    ).status_code == 401
    assert client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {retired}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
    ).status_code == 401


@pytest.mark.parametrize("path", ["/api/config/mihomo", "/api/config/alpha/mihomo"])
def test_public_config_endpoints_accept_encoded_query_token(monkeypatch, tmp_path, path):
    token = "token /&?"
    app = _config_endpoint_app(monkeypatch, tmp_path, auth_enabled=True, config_token=token)

    response = app.test_client().get(path, query_string={"token": token})

    assert response.status_code == 200


def test_config_endpoint_rejects_rule_proxy_token_before_equal_config_token(monkeypatch, tmp_path):
    app = _config_endpoint_app(monkeypatch, tmp_path, auth_enabled=True, config_token="public-token")
    shared_token = app.config["TEST_RULE_PROXY_TOKEN"]
    equal_config = config_module.get_repository().get_compat_config("default")
    equal_config["system_config"]["config_token"] = shared_token
    monkeypatch.setattr("backend.routes.config.get_config", lambda profile_id=None: equal_config)

    response = app.test_client().get(
        "/api/config/mihomo",
        query_string={"token": shared_token},
    )

    assert response.status_code == 401


def test_rule_proxy_requires_auth_even_when_global_auth_is_disabled(monkeypatch, tmp_path):
    repository = ProfileRepository(tmp_path)
    config_module.set_repository(repository)
    monkeypatch.setattr("backend.common.auth.is_auth_enabled", lambda: False)
    monkeypatch.setattr("backend.routes.auth.is_auth_enabled", lambda: False)
    app = Flask(__name__)
    register_blueprints(app)
    setup_before_request(app)

    response = app.test_client().get("/api/mosdns/rule-proxy?url=https://example.com/rules.txt")
    assert response.status_code == 401


def test_auth_enabled_rule_proxy_accepts_valid_config_token(monkeypatch, tmp_path):
    repository = ProfileRepository(tmp_path)
    repository.save_profile("default", {"system_config": {"config_token": "valid /&?"}})
    config_module.set_repository(repository)
    monkeypatch.setattr("backend.common.auth.is_auth_enabled", lambda: True)
    monkeypatch.setattr("backend.routes.auth.is_auth_enabled", lambda: True)
    monkeypatch.setattr(
        mosdns.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    monkeypatch.setattr(mosdns, "_fetch_remote_content", lambda url: "domain:example.com")
    app = Flask(__name__)
    register_blueprints(app)
    setup_before_request(app)

    response = app.test_client().get(
        "/api/mosdns/rule-proxy",
        query_string={"url": "https://example.com/rules.txt", "token": "valid /&?"},
    )

    assert response.status_code == 200
    assert response.get_data(as_text=True) == "domain:example.com"


def test_rule_proxy_rejects_oversized_query_token(monkeypatch, tmp_path):
    token = "x" * (MAX_AUTH_TOKEN_LENGTH + 1)
    repository = ProfileRepository(tmp_path)
    repository.save_profile("default", {"system_config": {"config_token": token}})
    config_module.set_repository(repository)
    monkeypatch.setattr(mosdns, "_fetch_remote_content", lambda url: "must-not-fetch")
    app = Flask(__name__)
    register_blueprints(app)

    response = app.test_client().get(
        "/api/mosdns/rule-proxy",
        query_string={"url": "https://example.com/rules.txt", "token": token},
    )

    assert response.status_code == 401


def test_auth_enabled_rule_proxy_accepts_valid_jwt(monkeypatch, tmp_path):
    repository = ProfileRepository(tmp_path)
    config_module.set_repository(repository)
    monkeypatch.setattr("backend.common.auth.is_auth_enabled", lambda: True)
    monkeypatch.setattr("backend.routes.auth.is_auth_enabled", lambda: True)
    monkeypatch.setattr(
        mosdns.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    monkeypatch.setattr(mosdns, "_fetch_remote_content", lambda url: "domain:example.com")
    app = Flask(__name__)
    register_blueprints(app)
    setup_before_request(app)

    response = app.test_client().get(
        "/api/mosdns/rule-proxy",
        query_string={"url": "https://example.com/rules.txt"},
        headers={"Authorization": f"Bearer {generate_token('admin')}"},
    )

    assert response.status_code == 200


@pytest.mark.parametrize(
    "authorization",
    [
        lambda token: f"Bearer {token} extra",
        lambda token: f"Bearer  {token}",
        lambda token: f"Bearer\t{token}",
        lambda token: "Bearer café",
        lambda token: f"Bearer {'x' * (MAX_AUTH_TOKEN_LENGTH + 1)}",
    ],
    ids=["trailing-field", "double-space", "tab", "unicode", "oversized"],
)
def test_rule_proxy_rejects_malformed_bearer(monkeypatch, tmp_path, authorization):
    repository = ProfileRepository(tmp_path)
    config_module.set_repository(repository)
    monkeypatch.setattr(mosdns, "_fetch_remote_content", lambda url: "must-not-fetch")
    app = Flask(__name__)
    register_blueprints(app)
    token = generate_token("admin")

    response = app.test_client().get(
        "/api/mosdns/rule-proxy",
        query_string={"url": "https://example.com/rules.txt"},
        headers={"Authorization": authorization(token)},
    )

    assert response.status_code == 401


@pytest.mark.parametrize("token", [None, "wrong"], ids=["missing-token", "wrong-token"])
def test_auth_enabled_rule_proxy_rejects_invalid_config_token(monkeypatch, tmp_path, token):
    repository = ProfileRepository(tmp_path)
    repository.save_profile("default", {"system_config": {"config_token": "valid"}})
    config_module.set_repository(repository)
    monkeypatch.setattr("backend.common.auth.is_auth_enabled", lambda: True)
    monkeypatch.setattr("backend.routes.auth.is_auth_enabled", lambda: True)
    monkeypatch.setattr(mosdns, "_fetch_remote_content", lambda url: "must-not-download")
    app = Flask(__name__)
    register_blueprints(app)
    setup_before_request(app)
    query = {"url": "https://example.com/rules.txt"}
    if token is not None:
        query["token"] = token

    response = app.test_client().get("/api/mosdns/rule-proxy", query_string=query)

    assert response.status_code == 401


def test_rule_proxy_rejects_non_http_and_private_targets():
    for url in (
        "ftp://example.com/rules.txt",
        "file:///etc/passwd",
        "http://127.0.0.1/rules.txt",
        "http://localhost/rules.txt",
        "http://169.254.169.254/latest/meta-data/",
    ):
        with pytest.raises(ValueError):
            mosdns._validate_remote_url(url)


@pytest.mark.parametrize(
    "address",
    [
        "224.0.0.1",
        "ff02::1",
        "100.64.0.1",
        "192.0.2.1",
        "2001:db8::1",
        "240.0.0.1",
        "198.18.0.1",
        "fd00::1",
        "169.254.1.1",
        "fe80::1",
        "127.0.0.1",
        "::1",
        "0.0.0.0",
        "::",
    ],
    ids=[
        "ipv4-multicast",
        "ipv6-multicast",
        "cgnat",
        "ipv4-documentation",
        "ipv6-documentation",
        "reserved",
        "benchmark",
        "ula",
        "ipv4-link-local",
        "ipv6-link-local",
        "ipv4-loopback",
        "ipv6-loopback",
        "ipv4-unspecified",
        "ipv6-unspecified",
    ],
)
def test_rule_proxy_rejects_every_non_global_dns_result(monkeypatch, address):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    monkeypatch.setattr(
        mosdns.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(family, socket.SOCK_STREAM, 6, "", (address, 443))],
    )

    with pytest.raises(ValueError, match="Public network"):
        mosdns._resolve_remote_url("https://rules.example/list")


def test_rule_proxy_rejects_mixed_public_and_private_dns_results(monkeypatch):
    monkeypatch.setattr(
        mosdns.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443)),
        ],
    )

    with pytest.raises(ValueError, match="Public network"):
        mosdns._resolve_remote_url("https://rules.example/list")


@pytest.mark.parametrize(
    "address,family",
    [
        ("93.184.216.34", socket.AF_INET),
        ("2606:4700:4700::1111", socket.AF_INET6),
    ],
    ids=["public-ipv4", "public-ipv6"],
)
def test_rule_proxy_accepts_public_ipv4_and_ipv6(monkeypatch, address, family):
    monkeypatch.setattr(
        mosdns.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(family, socket.SOCK_STREAM, 6, "", (address, 443))],
    )

    parsed, selected = mosdns._resolve_remote_url("https://rules.example/list")

    assert parsed.hostname == "rules.example"
    assert selected == address


def test_rule_proxy_pins_validated_ip_and_preserves_https_identity(monkeypatch):
    resolutions = iter([
        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
    ])
    monkeypatch.setattr(mosdns.socket, "getaddrinfo", lambda *args, **kwargs: next(resolutions))
    observed = {}

    class Response:
        status = 200
        headers = {}

        def stream(self, chunk_size):
            yield b"domain:example.com"

        def release_conn(self):
            observed["released"] = True

    class Pool:
        def __init__(self, host, port, **kwargs):
            observed.update(host=host, port=port, pool_kwargs=kwargs)

        def urlopen(self, method, path, **kwargs):
            observed.update(method=method, path=path, request_kwargs=kwargs)
            return Response()

        def close(self):
            observed["closed"] = True

    monkeypatch.setattr("urllib3.HTTPSConnectionPool", Pool)

    assert mosdns._fetch_remote_content("https://rules.example/path/list?format=txt") == "domain:example.com"
    assert observed["host"] == "93.184.216.34"
    assert observed["port"] == 443
    assert observed["pool_kwargs"]["assert_hostname"] == "rules.example"
    assert observed["pool_kwargs"]["server_hostname"] == "rules.example"
    assert observed["request_kwargs"]["headers"]["Host"] == "rules.example"
    assert observed["path"] == "/path/list?format=txt"
    assert observed["released"] and observed["closed"]


def _install_fake_http_pool(monkeypatch, responses, observed=None):
    observed = observed if observed is not None else []

    class Pool:
        def __init__(self, host, port, **kwargs):
            self.host = host
            observed.append((host, port, kwargs))

        def urlopen(self, method, path, **kwargs):
            response = responses.pop(0)
            response.request_host = self.host
            return response

        def close(self):
            pass

    monkeypatch.setattr("urllib3.HTTPConnectionPool", Pool)
    return observed


class _FakePoolResponse:
    def __init__(self, status=200, headers=None, chunks=()):
        self.status = status
        self.headers = headers or {}
        self._chunks = chunks
        self.released = False

    def stream(self, chunk_size):
        yield from self._chunks

    def release_conn(self):
        self.released = True


@pytest.mark.parametrize(
    "redirect_address",
    ["224.0.0.1", "ff02::1", "100.64.0.1", "fd00::1"],
    ids=["ipv4-multicast", "ipv6-multicast", "cgnat", "ula"],
)
def test_rule_proxy_revalidates_and_rejects_forbidden_redirect_hop(
    monkeypatch, redirect_address
):
    redirect_family = socket.AF_INET6 if ":" in redirect_address else socket.AF_INET
    resolutions = iter([
        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))],
        [(redirect_family, socket.SOCK_STREAM, 6, "", (redirect_address, 80))],
    ])
    monkeypatch.setattr(mosdns.socket, "getaddrinfo", lambda *args, **kwargs: next(resolutions))
    redirect = _FakePoolResponse(302, {"Location": "http://forbidden.example/rules"})
    observed = _install_fake_http_pool(monkeypatch, [redirect])

    with pytest.raises(ValueError, match="Public network"):
        mosdns._fetch_remote_content("http://public.example/rules")

    assert [entry[0] for entry in observed] == ["93.184.216.34"]
    assert redirect.released


def test_rule_proxy_rejects_non_2xx_response(monkeypatch):
    import requests

    monkeypatch.setattr(
        mosdns.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))],
    )
    response = _FakePoolResponse(503)
    _install_fake_http_pool(monkeypatch, [response])

    with pytest.raises(requests.exceptions.HTTPError, match="503"):
        mosdns._fetch_remote_content("http://public.example/rules")
    assert response.released


def test_rule_proxy_enforces_streamed_response_limit(monkeypatch):
    monkeypatch.setattr(mosdns, "_MAX_RULE_PROXY_BYTES", 5)
    monkeypatch.setattr(
        mosdns.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))],
    )
    response = _FakePoolResponse(200, chunks=[b"123", b"456"])
    _install_fake_http_pool(monkeypatch, [response])

    with pytest.raises(ValueError, match="size limit"):
        mosdns._fetch_remote_content("http://public.example/rules")
    assert response.released


def test_repository_without_factory_uses_string_github_proxy_default(tmp_path):
    repository = ProfileRepository(tmp_path)
    system = repository.get_system()
    profile = repository.get_profile("default")

    assert isinstance(system["system_config"]["github_proxy_domain"], str)
    assert "github_proxy_domain" not in profile or isinstance(
        profile["github_proxy_domain"], str
    )


def test_gitignore_does_not_hide_source_lock_or_backup_files():
    gitignore = open(".gitignore", encoding="utf-8").read()
    assert "*.lock" not in gitignore
    assert "*.bak" not in gitignore
    assert "*.tmp" not in gitignore
    assert "/profiles/" in gitignore
    assert "/cache/" in gitignore
    assert "/generated/" in gitignore
