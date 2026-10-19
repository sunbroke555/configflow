"""Every publication entry enforces gateway Agent support before any upload."""
from copy import deepcopy

import pytest

from backend.agents.deployment_bundle import build_deployment_bundle
from backend.agents.linux_gateway import MIN_AGENT_VERSION
from backend.routes.agents import _prepare_transactional_content
from backend.test_agent_deployments import env  # noqa: F401
from backend.test_agent_upgrade_auth import authenticated_app  # noqa: F401


def config(enabled):
    return {'profile_id': 'default', 'system_config': {}, 'rule_configs': [], 'rule_library': [],
            'mihomo': {'custom_config': 'mode: rule\ndns: {enable: false}\n',
                       'linux_gateway': {'enabled': enabled, 'rule_set_ids': []}}}


@pytest.mark.parametrize('version', ['', '1.8.24-go', 'invalid'])
def test_shared_preparation_rejects_old_gateway_but_preserves_ordinary(version):
    agent = {'id': 'old', 'service_type': 'mihomo', 'version': version}
    with pytest.raises(ValueError, match=MIN_AGENT_VERSION):
        _prepare_transactional_content(agent, config(True))
    assert _prepare_transactional_content(agent, config(False))['bundle'].manifest['service_type'] == 'mihomo'


def test_supported_agent_can_prepare_gateway():
    prepared = _prepare_transactional_content(
        {'id': 'new', 'service_type': 'mihomo', 'version': MIN_AGENT_VERSION}, config(True))
    assert 'x-configflow-linux-gateway:' in prepared['config_content']


@pytest.mark.parametrize('enabled', [False, True])
def test_direct_bundle_upload_does_not_bypass_support_check(env, monkeypatch, enabled):
    _, manager, agent, snapshot = env
    calls = []
    def request(agent, method, path, **kwargs):
        calls.append(path)
        if path == 'capabilities':
            return {'deployment_protocols': [1], 'service_type': 'mihomo'}
        return {'success': True, 'status': 'ready'}
    monkeypatch.setattr(manager, '_deployment_request', request)
    marker = 'x-configflow-linux-gateway: {enabled: true, version: 1}\n' if enabled else ''
    bundle = build_deployment_bundle(agent, snapshot, 'mode: rule\n' + marker)
    result = manager.publish_deployment(agent['id'], bundle, activate=False)
    if enabled:
        assert result['status'] == 'failed' and MIN_AGENT_VERSION in result['message']
        assert calls == []
    else:
        assert result['status'] == 'ready' and calls == ['capabilities', 'deployments']


@pytest.mark.parametrize('enabled', [False, True])
def test_normal_publish_route_enforces_gateway_support(authenticated_app, monkeypatch, enabled):
    client, manager, registrations, admin, _ = authenticated_app
    agent_id = registrations[0]['id']
    def update(profile):
        profile.update(deepcopy(config(enabled)))
    manager.repository.update_profile_transaction('default', update)
    calls = []
    def request(agent, method, path, **kwargs):
        calls.append(path)
        if path == 'capabilities':
            return {'deployment_protocols': [1], 'service_type': 'mihomo'}
        return {'success': True, 'status': 'ready'}
    monkeypatch.setattr(manager, '_deployment_request', request)
    response = client.post(f'/api/agents/{agent_id}/push-config', headers=admin, json={'restart': False})
    result = response.get_json()
    if enabled:
        assert response.status_code >= 400 and MIN_AGENT_VERSION in result['message']
        assert calls == []
    else:
        assert response.status_code < 300 and result['status'] == 'ready'
        assert calls == ['capabilities', 'deployments']
