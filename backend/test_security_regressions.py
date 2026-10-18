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
    repository.save_system({"system_config": {"config_token": ""}})
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
        repository.save_system({"system_config": {"config_token": config_token}})
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
    repository.save_system(system)
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
    repository.save_system(system)

    response = app.test_client().get("/api/config-token")

    assert response.status_code == 200
    assert response.get_json() == {"config_token": "[REDACTED]"}
    assert internal not in response.get_data(as_text=True)


def test_retired_rule_proxy_token_never_authorizes_config_rule_proxy_or_mcp(monkeypatch, tmp_path):
    repository = ProfileRepository(tmp_path)
    repository.save_system({"system_config": {"config_token": "public-token"}})
    system = repository.get_system()
    retired = "retired-internal-token"
    system["system_config"]["retired_rule_proxy_tokens"] = [retired]
    system["system_config"]["config_token"] = retired
    repository.save_system(system)
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
    repository.save_system({"system_config": {"config_token": "valid /&?"}})
    config_module.set_repository(repository)
    monkeypatch.setattr("backend.common.auth.is_auth_enabled", lambda: True)
    monkeypatch.setattr("backend.routes.auth.is_auth_enabled", lambda: True)
    monkeypatch.setattr(
        socket,
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
    repository.save_system({"system_config": {"config_token": token}})
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
        socket,
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
    repository.save_system({"system_config": {"config_token": "valid"}})
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


@pytest.mark.parametrize("url", [
    "ftp://example.com/rules.txt", "file:///etc/passwd", "/relative/rules",
    "https://", "http://example.com:99999/rules", "http://example.com\n/rules",
])
def test_rule_proxy_rejects_invalid_http_urls(url):
    with pytest.raises(ValueError):
        mosdns._validate_remote_url(url)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/rules", "http://localhost/rules",
    "http://192.168.0.2/rules", "http://198.18.0.53/rules",
    "http://[fd00::1]/rules", "https://rules.example/list",
])
def test_rule_proxy_accepts_normal_hostname_and_private_network_routing(monkeypatch, url):
    def no_preflight(*args, **kwargs):
        pytest.fail("Rule URL validation must not pre-resolve DNS")
    monkeypatch.setattr(socket, "getaddrinfo", no_preflight)
    assert mosdns._validate_remote_url(url) == url


class _FakeRuleResponse:
    def __init__(self, status=200, headers=None, chunks=()):
        self.status_code = status
        self.headers = headers or {}
        self._chunks = chunks
        self.closed = False

    def iter_content(self, chunk_size):
        yield from self._chunks

    def close(self):
        self.closed = True


def _install_rule_transport(monkeypatch, responses):
    monkeypatch.setattr(config_module, "get_system_config", lambda: {"system_config": {}})
    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return responses.pop(0)
    monkeypatch.setattr("requests.get", get)
    monkeypatch.setattr("backend.utils.rule_fetch._RuleSession.get", lambda self, url, **kwargs: get(url, **kwargs))
    return calls


def test_rule_proxy_uses_original_hostname_tls_and_timeouts(monkeypatch):
    response = _FakeRuleResponse(chunks=[b"domain:example.com"])
    calls = _install_rule_transport(monkeypatch, [response])
    url = "https://rules.example/path/list?format=txt"
    assert mosdns._fetch_remote_content(url) == "domain:example.com"
    assert calls == [(url, {
        "timeout": (3, 10), "stream": True, "allow_redirects": False, "verify": True,
        "proxies": {"http": "", "https": ""},
    })]
    assert response.closed


@pytest.mark.parametrize("location", ["file:///etc/passwd", "ftp://example.com/rules"])
def test_rule_proxy_validates_redirect_scheme(monkeypatch, location):
    response = _FakeRuleResponse(302, {"Location": location})
    calls = _install_rule_transport(monkeypatch, [response])
    with pytest.raises(ValueError):
        mosdns._fetch_remote_content("https://rules.example/list")
    assert len(calls) == 1
    assert response.closed


def test_rule_proxy_follows_private_network_redirect(monkeypatch):
    redirect = _FakeRuleResponse(302, {"Location": "http://198.18.0.53/rules"})
    response = _FakeRuleResponse(chunks=[b"domain:example.com"])
    calls = _install_rule_transport(monkeypatch, [redirect, response])
    assert mosdns._fetch_remote_content("http://rules.example/list") == "domain:example.com"
    assert calls[-1][0] == "http://198.18.0.53/rules"
    assert redirect.closed and response.closed


def test_rule_proxy_limits_redirects(monkeypatch):
    responses = [_FakeRuleResponse(302, {"Location": "/next"}) for _ in range(4)]
    calls = _install_rule_transport(monkeypatch, list(responses))
    with pytest.raises(ValueError, match="Too many redirects"):
        mosdns._fetch_remote_content("https://rules.example/list")
    assert len(calls) == 4
    assert all(response.closed for response in responses)


def test_rule_proxy_does_not_buffer_redirect_body_before_enforcing_limits(monkeypatch):
    import io
    import requests

    class UnreadableBody(io.BytesIO):
        def read(self, *args, **kwargs):
            pytest.fail('Redirect body must not be buffered')

    redirect = requests.Response()
    redirect.status_code = 302
    redirect.headers.update({'Location': '/final', 'Content-Length': '999999999'})
    redirect.raw = UnreadableBody()
    final = requests.Response()
    final.status_code = 200
    final.raw = io.BytesIO(b'domain:example.com')
    responses = [redirect, final]
    monkeypatch.setattr(config_module, 'get_system_config', lambda: {'system_config': {}})
    monkeypatch.setattr('requests.adapters.HTTPAdapter.send', lambda *a, **kw: responses.pop(0))
    assert mosdns._fetch_remote_content('https://rules.example/list') == 'domain:example.com'
    assert redirect.raw.closed
    assert not responses


def test_rule_proxy_rejects_non_2xx_response(monkeypatch):
    import requests
    response = _FakeRuleResponse(503)
    _install_rule_transport(monkeypatch, [response])
    with pytest.raises(requests.exceptions.HTTPError, match="503"):
        mosdns._fetch_remote_content("http://rules.example/list")
    assert response.closed


@pytest.mark.parametrize("headers,chunks", [
    ({}, [b"123", b"456"]),
    ({"Content-Length": "6"}, []),
])
def test_rule_proxy_enforces_response_limit(monkeypatch, headers, chunks):
    monkeypatch.setattr(mosdns, "_MAX_RULE_PROXY_BYTES", 5)
    response = _FakeRuleResponse(headers=headers, chunks=chunks)
    _install_rule_transport(monkeypatch, [response])
    with pytest.raises(ValueError, match="size limit"):
        mosdns._fetch_remote_content("http://rules.example/list")
    assert response.closed


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
