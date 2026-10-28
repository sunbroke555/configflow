"""Deployment-local listener settings, validation and failed-change recovery."""

import fcntl
import json
import socket

import pytest
from flask import Flask

from backend.routes import register_blueprints
from backend.utils import web_listener as listener


ENDPOINT = '/api/settings/web-port'
TEMPLATE = 'events {}\nhttp { server { __CONFIGFLOW_LISTEN__ location /_configflow/listener-health { return 200 "__CONFIGFLOW_LISTENER_TOKEN__"; } } }'


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    for constant, filename in (('TEMPLATE_PATH', 'template'), ('CONFIG_PATH', 'nginx.conf'),
                               ('STATE_PATH', 'web-listener.json'), ('LOCK_PATH', 'listener.lock')):
        monkeypatch.setattr(listener, constant, tmp_path / filename)
    monkeypatch.setattr(listener, 'NGINX_BINARY', '/bin/true')
    monkeypatch.delenv('CONFIGFLOW_WEB_PORT', raising=False)
    monkeypatch.delenv('SUB_STORE_BACKEND_API_PORT', raising=False)
    monkeypatch.setattr(listener, '_ipv6_available', lambda: True)
    monkeypatch.setattr(listener, '_nginx', lambda *args: None)
    monkeypatch.setattr(listener, '_check_available', lambda *args: None)
    monkeypatch.setattr(listener, '_wait_healthy', lambda active: None)
    listener.TEMPLATE_PATH.write_text(TEMPLATE)
    return listener


@pytest.fixture
def client(deployment, monkeypatch):
    monkeypatch.setattr('backend.common.auth.is_auth_enabled', lambda: False)
    app = Flask(__name__)
    register_blueprints(app)
    deployment.prepare_listener()
    return app.test_client()


def test_startup_default_dual_stack_then_saved_port_survives_restart(deployment):
    assert deployment.prepare_listener() == {
        'supported': True, 'port': 80, 'editable': True, 'ipv6': True, 'source': 'default'}
    config = deployment.CONFIG_PATH.read_text()
    assert 'listen 0.0.0.0:80;' in config and 'listen [::]:80 ipv6only=on;' in config
    assert not deployment.STATE_PATH.exists()
    changed = deployment.update_listener(18088)
    assert changed['port'] == 18088 and changed['source'] == 'saved'
    assert json.loads(deployment.STATE_PATH.read_text()) == {'port': 18088}
    assert deployment.prepare_listener()['port'] == 18088


def test_environment_override_preserves_saved_preference_and_is_read_only(deployment, monkeypatch):
    deployment.STATE_PATH.write_text('{"port":18088}')
    monkeypatch.setenv('CONFIGFLOW_WEB_PORT', '18089')
    status = deployment.prepare_listener()
    assert status['port'] == 18089 and status['source'] == 'environment' and not status['editable']
    with pytest.raises(deployment.WebListenerError, match='CONFIGFLOW_WEB_PORT') as error:
        deployment.update_listener(18090)
    assert error.value.status_code == 409
    assert json.loads(deployment.STATE_PATH.read_text()) == {'port': 18088}
    monkeypatch.delenv('CONFIGFLOW_WEB_PORT')
    assert deployment.prepare_listener()['port'] == 18088


@pytest.mark.parametrize('value', [True, False, None, '18088', 1.5, 0, 65536, [], {}])
def test_bad_api_port_does_not_change_deployment(client, deployment, value):
    before = deployment.CONFIG_PATH.read_bytes()
    response = client.post(ENDPOINT, json={'port': value})
    assert response.status_code == 400
    assert deployment.CONFIG_PATH.read_bytes() == before
    assert not deployment.STATE_PATH.exists()


@pytest.mark.parametrize('body', [{}, [], {'server_domain': 'http://host:1234'}])
def test_missing_port_is_rejected(client, body):
    assert client.post(ENDPOINT, json=body).status_code == 400


@pytest.mark.parametrize('port', [5001, 3001])
def test_internal_services_keep_their_ports_even_when_asleep(client, deployment, port):
    before = deployment.CONFIG_PATH.read_bytes()
    response = client.post(ENDPOINT, json={'port': port})
    assert response.status_code == 409
    assert '保留' in response.get_json()['message']
    assert deployment.CONFIG_PATH.read_bytes() == before
    assert not deployment.STATE_PATH.exists()


def test_custom_sub_store_port_is_reserved_at_startup_and_in_web(client, deployment, monkeypatch):
    monkeypatch.setenv('SUB_STORE_BACKEND_API_PORT', '18080')
    assert client.post(ENDPOINT, json={'port': 18080}).status_code == 409
    monkeypatch.setenv('CONFIGFLOW_WEB_PORT', '18080')
    with pytest.raises(deployment.WebListenerError, match='Sub-Store'):
        deployment.prepare_listener()


@pytest.mark.parametrize('raw', ['', 'abc', '0', '65536', '-80', '１', '9' * 5000])
def test_invalid_environment_is_rejected_before_overwriting_config(deployment, monkeypatch, raw):
    deployment.prepare_listener()
    old = deployment.CONFIG_PATH.read_bytes()
    monkeypatch.setenv('CONFIGFLOW_WEB_PORT', raw)
    with pytest.raises(deployment.WebListenerError):
        deployment.prepare_listener()
    assert deployment.CONFIG_PATH.read_bytes() == old


def test_recovery_environment_works_with_corrupt_saved_file(deployment, monkeypatch):
    deployment.STATE_PATH.write_text('{bad json')
    with pytest.raises(deployment.WebListenerError, match='CONFIGFLOW_WEB_PORT'):
        deployment.prepare_listener()
    monkeypatch.setenv('CONFIGFLOW_WEB_PORT', '18088')
    assert deployment.prepare_listener()['port'] == 18088
    assert deployment.STATE_PATH.read_text() == '{bad json'


def test_no_ipv6_kernel_emits_only_ipv4_listener(deployment, monkeypatch):
    monkeypatch.setattr(deployment, '_ipv6_available', lambda: False)
    assert deployment.prepare_listener()['ipv6'] is False
    assert 'listen 0.0.0.0:80;' in deployment.CONFIG_PATH.read_text()
    assert '[::]' not in deployment.CONFIG_PATH.read_text()


def test_reload_failure_restores_original_config_and_confirms_old_listener(deployment, monkeypatch):
    deployment.prepare_listener()
    old_config = deployment.CONFIG_PATH.read_bytes()
    old_active = deployment._read_active()
    probes = []

    def verify(active):
        probes.append(active)
        if active['port'] != 80:
            raise deployment.WebListenerError('新端口未生效')

    monkeypatch.setattr(deployment, '_wait_healthy', verify)
    with pytest.raises(deployment.WebListenerError, match='新端口未生效'):
        deployment.update_listener(18088)
    assert deployment.CONFIG_PATH.read_bytes() == old_config
    assert not deployment.STATE_PATH.exists()
    assert probes[-1] == old_active


def test_persistent_state_write_failure_does_not_block_live_rollback(deployment, monkeypatch):
    deployment.STATE_PATH.write_text('{"port":18087}')
    deployment.prepare_listener()
    old_config, old_state = deployment.CONFIG_PATH.read_bytes(), deployment.STATE_PATH.read_bytes()
    real_write = deployment._atomic_write
    probes = []

    def fail_state(path, content, mode=0o644):
        if path == deployment.STATE_PATH:
            raise OSError('read-only filesystem')
        real_write(path, content, mode)

    monkeypatch.setattr(deployment, '_atomic_write', fail_state)
    monkeypatch.setattr(deployment, '_wait_healthy', lambda active: probes.append(active['port']))
    with pytest.raises(deployment.WebListenerError, match='已恢复'):
        deployment.update_listener(18088)
    assert probes == [18088, 18087]
    assert deployment.CONFIG_PATH.read_bytes() == old_config
    assert deployment.STATE_PATH.read_bytes() == old_state


def test_nginx_validation_failure_restores_config_without_reload(deployment, monkeypatch):
    deployment.prepare_listener()
    old_config = deployment.CONFIG_PATH.read_bytes()
    calls = []

    def fail_check(*args):
        calls.append(args)
        raise deployment.WebListenerError('配置检查失败')

    monkeypatch.setattr(deployment, '_nginx', fail_check)
    with pytest.raises(deployment.WebListenerError, match='配置检查失败'):
        deployment.update_listener(18088)
    assert calls == [('-t',)]
    assert deployment.CONFIG_PATH.read_bytes() == old_config
    assert not deployment.STATE_PATH.exists()


def test_concurrent_change_is_rejected_without_touching_state(deployment):
    deployment.prepare_listener()
    with open(deployment.LOCK_PATH, 'a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(deployment.WebListenerError, match='正在更新') as error:
            deployment.update_listener(18088)
        assert error.value.status_code == 409
    assert deployment.listener_status()['port'] == 80


def test_get_and_post_require_authentication(client, monkeypatch):
    monkeypatch.setattr('backend.common.auth.is_auth_enabled', lambda: True)
    assert client.get(ENDPOINT).status_code == 401
    assert client.post(ENDPOINT, json={'port': 18088}).status_code == 401


def test_unsupported_deployment_is_read_only(client, deployment):
    deployment.TEMPLATE_PATH.unlink()
    status = client.get(ENDPOINT).get_json()
    assert status['supported'] is False and status['editable'] is False and status['port'] is None
    assert client.post(ENDPOINT, json={'port': 18088}).status_code == 503


def test_success_api_returns_active_settings(client, deployment):
    response = client.post(ENDPOINT, json={'port': 18088})
    assert response.status_code == 200
    assert response.get_json() == {'success': True, 'supported': True, 'port': 18088,
                                   'ipv6': True, 'editable': True, 'source': 'saved'}
    assert client.get(ENDPOINT).get_json()['port'] == 18088
