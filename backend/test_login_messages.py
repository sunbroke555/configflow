"""Localize login feedback without changing authentication or machine codes."""
import pytest
from flask import Flask, jsonify

from backend.common import auth
from backend.test_agent_upgrade_auth import authenticated_app  # noqa: F401


def test_wrong_credentials_return_chinese_and_still_reject_login(authenticated_app):
    client, _, _, _, _ = authenticated_app
    result = client.post('/api/auth/login', json={'username': 'upgrade-admin', 'password': 'wrong'})
    assert result.status_code == 401
    assert result.json == {'success': False, 'message': '用户名或密码错误，请重新输入。'}
    assert 'token' not in result.json


def test_login_validation_and_missing_session_messages_are_chinese(authenticated_app):
    client, _, _, admin, _ = authenticated_app
    verified = client.get('/api/auth/verify', headers=admin)
    assert verified.status_code == 200
    assert verified.json['message'] == '登录状态有效。'
    missing = client.get('/api/agents')
    assert missing.status_code == 401
    assert missing.json['message'] == '请先登录后再操作。'


@pytest.mark.parametrize('error, code, expected', [
    ('expired', 'token_expired', '登录已过期'),
    ('invalid', 'token_invalid', '登录凭据无效'),
])
def test_jwt_failure_codes_and_original_diagnostics_are_preserved(monkeypatch, error, code, expected):
    monkeypatch.setattr(auth, 'is_auth_enabled', lambda: True)
    detail = 'Signature verification failed'
    monkeypatch.setattr(auth, 'verify_token', lambda *_: {'error': error, 'detail': detail})
    app = Flask(__name__)

    @app.get('/protected')
    @auth.require_auth
    def protected():
        return jsonify(success=True)

    result = app.test_client().get('/protected', headers={'Authorization': 'Bearer token'})
    assert result.status_code == 401
    assert result.json['error'] == code
    assert expected in result.json['message']
    if error == 'invalid':
        assert result.json['message'].endswith(detail)
