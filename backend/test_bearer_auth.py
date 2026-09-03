import pytest
from flask import Flask, jsonify

from backend.common import config as config_module
from backend.common.auth import (
    MAX_AUTH_TOKEN_LENGTH,
    generate_token,
    parse_bearer_token,
    require_auth,
)
from backend.common.config_repository import ProfileRepository
from backend.mcp_server import mcp_bp


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("Bearer abc.DEF-_~+/=", "abc.DEF-_~+/="),
        (f"Bearer {'x' * MAX_AUTH_TOKEN_LENGTH}", "x" * MAX_AUTH_TOKEN_LENGTH),
    ],
    ids=["printable-ascii", "maximum-length"],
)
def test_parse_bearer_token_accepts_exact_printable_ascii_token(header, expected):
    assert parse_bearer_token(header) == expected


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Bearer ",
        "Bearer  token",
        "Bearer\ttoken",
        "Bearer token extra",
        " Bearer token",
        "Bearer token ",
        "bearer token",
        "Basic token",
        "Bearer café",
        "Bearer 令牌🔐",
        "Bearer token\tmore",
        "Bearer token\n",
        f"Bearer {'x' * (MAX_AUTH_TOKEN_LENGTH + 1)}",
    ],
    ids=[
        "none",
        "empty",
        "empty-token",
        "double-space",
        "tab-separator",
        "trailing-field",
        "leading-whitespace",
        "trailing-whitespace",
        "wrong-case",
        "wrong-scheme",
        "latin-unicode",
        "unicode",
        "tab-in-token",
        "newline",
        "oversized",
    ],
)
def test_parse_bearer_token_rejects_malformed_headers(header):
    assert parse_bearer_token(header) is None


def _protected_app(monkeypatch):
    monkeypatch.setattr("backend.common.auth.is_auth_enabled", lambda: True)
    app = Flask(__name__)

    @app.get("/protected")
    @require_auth
    def protected():
        return jsonify({"success": True})

    return app


def test_require_auth_accepts_valid_jwt(monkeypatch):
    response = _protected_app(monkeypatch).test_client().get(
        "/protected",
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
def test_require_auth_rejects_malformed_bearer(monkeypatch, authorization):
    token = generate_token("admin")
    response = _protected_app(monkeypatch).test_client().get(
        "/protected", headers={"Authorization": authorization(token)}
    )

    assert response.status_code == 401


def _mcp_app(monkeypatch, tmp_path, *, config_token=""):
    repository = ProfileRepository(tmp_path)
    repository.save_profile("default", {"system_config": {"config_token": config_token}})
    config_module.set_repository(repository)
    monkeypatch.setattr("backend.mcp_server.auth.is_auth_enabled", lambda: True)
    app = Flask(__name__)
    app.register_blueprint(mcp_bp)
    return app


def _mcp_ping(client, *, authorization=None, query_token=None):
    headers = {"Authorization": authorization} if authorization is not None else {}
    query = {"token": query_token} if query_token is not None else {}
    return client.post(
        "/mcp",
        headers=headers,
        query_string=query,
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
    )


def test_mcp_accepts_valid_jwt_bearer(monkeypatch, tmp_path):
    app = _mcp_app(monkeypatch, tmp_path)

    response = _mcp_ping(
        app.test_client(), authorization=f"Bearer {generate_token('admin')}"
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
def test_mcp_rejects_malformed_bearer(monkeypatch, tmp_path, authorization):
    app = _mcp_app(monkeypatch, tmp_path)
    token = generate_token("admin")

    response = _mcp_ping(app.test_client(), authorization=authorization(token))

    assert response.status_code == 401


def test_mcp_query_token_keeps_non_bearer_grammar(monkeypatch, tmp_path):
    token = "token /&? 令牌"
    app = _mcp_app(monkeypatch, tmp_path, config_token=token)

    response = _mcp_ping(app.test_client(), query_token=token)

    assert response.status_code == 200


def test_mcp_rejects_oversized_query_token(monkeypatch, tmp_path):
    token = "x" * (MAX_AUTH_TOKEN_LENGTH + 1)
    app = _mcp_app(monkeypatch, tmp_path, config_token=token)

    response = _mcp_ping(app.test_client(), query_token=token)

    assert response.status_code == 401
