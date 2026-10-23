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


def full_tun(custom_config=None, gateway=False):
    from backend.common.router_split import full_tun_base_yaml
    value = config(gateway)
    value['mihomo']['custom_config'] = (full_tun_base_yaml(value['mihomo']['custom_config'])
                                        if custom_config is None else custom_config)
    value['mihomo']['router_split'] = {'version': 1, 'takeover': 'full_tun', 'route_rule_set_ids': [], 'origin': 'setup'}
    return value


@pytest.mark.parametrize('version', ['1.8.25-go', '1.8.39-go', ''])
def test_full_tun_profile_needs_a_forwarding_agent_on_shell(version):
    from backend.agents.linux_gateway import FORWARDING_MIN_AGENT_VERSION
    agent = {'id': 'old', 'service_type': 'mihomo', 'deployment_method': 'shell', 'version': version}
    with pytest.raises(ValueError, match=FORWARDING_MIN_AGENT_VERSION):
        _prepare_transactional_content(agent, full_tun())
    # Docker never runs the host gateway, so an older image keeps publishing.
    docker = {**agent, 'deployment_method': 'docker'}
    prepared = _prepare_transactional_content(docker, full_tun())
    assert 'auto-redirect: true' in prepared['config_content']
    current = {**agent, 'version': FORWARDING_MIN_AGENT_VERSION}
    assert 'x-configflow-linux-gateway' not in _prepare_transactional_content(current, full_tun())['config_content']


def test_full_tun_profile_without_tun_is_never_published():
    from backend.common.router_split import GENERATED_TUN_MISSING
    agent = {'id': 'new', 'service_type': 'mihomo', 'deployment_method': 'shell', 'version': '1.8.40-go'}
    with pytest.raises(ValueError) as error:
        _prepare_transactional_content(agent, full_tun('mode: rule\ndns: {enable: false}\n'))
    assert str(error.value) == GENERATED_TUN_MISSING


def test_full_tun_profile_with_the_gateway_enabled_cannot_generate():
    from backend.common.router_split import RouterSplitError
    agent = {'id': 'new', 'service_type': 'mihomo', 'deployment_method': 'shell', 'version': '1.8.40-go'}
    with pytest.raises(RouterSplitError) as error:
        _prepare_transactional_content(agent, full_tun(gateway=True))
    assert error.value.code == 'ROUTER_SPLIT_FULL_TUN'
    invalid = full_tun()
    invalid['mihomo']['router_split']['version'] = 2
    with pytest.raises(RouterSplitError) as error:
        _prepare_transactional_content(agent, invalid)
    assert error.value.code == 'ROUTER_SPLIT_INVALID'


@pytest.mark.parametrize('method', ['shell', 'docker'])
def test_gateway_and_ordinary_profiles_keep_their_agent_requirements(method):
    agent = {'id': 'old', 'service_type': 'mihomo', 'deployment_method': method, 'version': MIN_AGENT_VERSION}
    # goal=gateway: the selective gateway still needs only the gateway version.
    assert 'x-configflow-linux-gateway:' in _prepare_transactional_content(agent, config(True))['config_content']
    ordinary = {**agent, 'version': '1.8.0-go'}
    assert _prepare_transactional_content(ordinary, config(False))['bundle'].manifest['service_type'] == 'mihomo'
    oxidns = {'id': 'dns', 'service_type': 'oxidns', 'deployment_method': method, 'version': '1.8.0-go'}
    assert _prepare_transactional_content(oxidns, full_tun())['bundle'].manifest['service_type'] == 'oxidns'
