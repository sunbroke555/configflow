"""The public core installer carries no Agent identity or caller-supplied config."""
import subprocess

import pytest
from flask import Flask

from backend.routes.agents import get_core_install_script
from backend.routes.auth import setup_before_request


@pytest.fixture
def client(monkeypatch):
    app = Flask(__name__)
    app.add_url_rule('/api/agents/core-install-script', view_func=get_core_install_script)
    app.add_url_rule('/api/agents/private', endpoint='private', view_func=lambda: 'private')
    app.add_url_rule('/api/agents/core-install-script/extra', endpoint='extra', view_func=lambda: 'private')
    monkeypatch.setattr('backend.routes.auth.is_auth_enabled', lambda: True)
    setup_before_request(app)
    return app.test_client()


def test_core_installer_is_public_and_shell_valid_without_agent_parameters(client, tmp_path):
    response = client.get('/api/agents/core-install-script')
    assert response.status_code == 200
    assert response.mimetype == 'text/plain'
    assert response.headers['Cache-Control'] == 'no-store'
    assert response.headers['X-Content-Type-Options'] == 'nosniff'
    script = response.get_data(as_text=True)
    assert script.startswith('#!/bin/sh\n')
    assert '/etc/mihomo' in script and '/etc/oxidns' in script
    assert '/api/agents/register' not in script
    assert '/api/agents/download' not in script
    assert 'agent_name' not in script
    path = tmp_path / 'install.sh'
    path.write_text(script)
    result = subprocess.run(['sh', '-n', str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_core_installer_does_not_embed_request_secrets_or_override_layout(client):
    normal = client.get('/api/agents/core-install-script').data
    response = client.get('/api/agents/core-install-script', query_string={
        'name': 'private-agent-name', 'token': 'private-agent-token',
        'config_path': '/tmp/unexpected.yaml', 'server_url': 'https://private.example',
    })
    assert response.status_code == 200
    assert response.data == normal


def test_core_installer_exception_does_not_expose_server_paths(client, monkeypatch):
    def missing():
        raise FileNotFoundError('/private/server/path/install-cores.sh')
    monkeypatch.setattr('backend.agents.core_install_script.generate_core_install_script', missing)
    response = client.get('/api/agents/core-install-script')
    assert response.status_code == 500
    assert response.json['success'] is False
    assert '请更新 ConfigFlow' in response.json['message']
    assert '/private/server/path' not in response.get_data(as_text=True)


@pytest.mark.parametrize('path', ['/api/agents/private', '/api/agents/core-install-script/extra'])
def test_core_installer_auth_exception_is_exact(client, path):
    assert client.get(path).status_code == 401
