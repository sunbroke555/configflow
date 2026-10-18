"""The native AIO download endpoint validates both instances before emitting a script."""
import subprocess

import pytest
from flask import Flask

from backend.routes.agents import get_install_script


@pytest.fixture
def client():
    app = Flask(__name__)
    app.add_url_rule('/api/agents/install-script', view_func=get_install_script)
    return app.test_client()


def test_native_aio_download_defaults_and_shell_syntax(client, tmp_path):
    response = client.get('/api/agents/install-script', query_string={
        'type': 'aio', 'name': 'gateway', 'server_url': 'https://config.test',
    })
    assert response.status_code == 200
    assert response.mimetype == 'text/plain'
    script = response.get_data(as_text=True)
    for expected in ('gateway-mihomo', 'gateway-oxidns', '"agent_port": 8080',
                     '"agent_port": 8082', '"native_aio": true',
                     '/etc/mihomo/config.yaml', '/etc/oxidns/config.yaml',
                     'https://config.test/api/agents/download'):
        assert expected in script
    path = tmp_path / 'install-aio.sh'
    path.write_text(script)
    result = subprocess.run(['sh', '-n', str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_native_aio_download_keeps_separate_custom_core_settings(client):
    response = client.get('/api/agents/install-script', query_string={
        'type': 'aio', 'name': 'gateway', 'mihomo_agent_port': '18080',
        'oxidns_agent_port': '18082', 'mihomo_config_path': '/srv/mihomo/config.yaml',
        'oxidns_config_path': '/opt/oxidns/config.yaml',
        'oxidns_service_unit': 'my-oxidns', 'oxidns_service_binary': '/opt/oxidns/oxidns',
    })
    assert response.status_code == 200
    script = response.get_data(as_text=True)
    for expected in ('"agent_port": 18080', '"agent_port": 18082',
                     '/srv/mihomo/config.yaml', '/opt/oxidns/config.yaml',
                     '"service_unit": "my-oxidns"', '/opt/oxidns/oxidns'):
        assert expected in script


@pytest.mark.parametrize('params', [
    {'agent_type': 'shell'},
    {'mihomo_agent_port': 'not-a-port'},
    {'oxidns_agent_port': '65536'},
    {'oxidns_agent_port': '8080'},
    {'oxidns_config_path': 'relative/config.yaml'},
    {'oxidns_config_path': '/etc/mihomo/config.yaml'},
    {'oxidns_service_unit': 'oxidns; touch /tmp/unwanted'},
])
def test_native_aio_download_refuses_invalid_pairs(client, params):
    response = client.get('/api/agents/install-script', query_string={
        'type': 'aio', 'name': 'gateway', **params,
    })
    assert response.status_code == 400
    assert response.get_json()['success'] is False


def test_single_service_download_keeps_legacy_layout(client):
    response = client.get('/api/agents/install-script', query_string={
        'type': 'mihomo', 'name': 'gateway',
    })
    assert response.status_code == 200
    script = response.get_data(as_text=True)
    assert '"agent_name": "gateway"' in script
    assert '/usr/local/bin/configflow-agent' in script
    assert '"native_aio": true' not in script
