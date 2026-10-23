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


@pytest.fixture
def host(monkeypatch):
    """The Agent status a full TUN publish reads; no gateway record by default."""
    state = {'status': {'success': True, 'managed': {'active': False, 'healthy': False, 'preserves_forwarding': False}},
             'calls': []}
    def call(manager, agent_id, operation, method, profile_id, body=None):
        state['calls'].append((agent_id, operation, method, profile_id))
        if isinstance(state['status'], Exception):
            raise state['status']
        return deepcopy(state['status'])
    monkeypatch.setattr('backend.agents.linux_gateway.call', call)
    return state


@pytest.mark.parametrize('version', ['1.8.25-go', '1.8.39-go', ''])
def test_full_tun_profile_needs_a_forwarding_agent_on_shell(host, version):
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


def test_full_tun_profile_without_tun_is_never_published(host):
    from backend.common.router_split import GENERATED_TUN_MISSING
    agent = {'id': 'new', 'service_type': 'mihomo', 'deployment_method': 'shell', 'version': '1.8.40-go'}
    with pytest.raises(ValueError) as error:
        _prepare_transactional_content(agent, full_tun('mode: rule\ndns: {enable: false}\n'))
    assert str(error.value) == GENERATED_TUN_MISSING


def test_full_tun_profile_with_the_gateway_enabled_cannot_generate(host):
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
def test_gateway_and_ordinary_profiles_keep_their_agent_requirements(host, method):
    agent = {'id': 'old', 'service_type': 'mihomo', 'deployment_method': method, 'version': MIN_AGENT_VERSION}
    # goal=gateway: the selective gateway still needs only the gateway version.
    assert 'x-configflow-linux-gateway:' in _prepare_transactional_content(agent, config(True))['config_content']
    ordinary = {**agent, 'version': '1.8.0-go'}
    assert _prepare_transactional_content(ordinary, config(False))['bundle'].manifest['service_type'] == 'mihomo'
    oxidns = {'id': 'dns', 'service_type': 'oxidns', 'deployment_method': method, 'version': '1.8.0-go'}
    assert _prepare_transactional_content(oxidns, full_tun())['bundle'].manifest['service_type'] == 'oxidns'


@pytest.mark.parametrize('managed', [
    {'active': True, 'healthy': True}, {'active': False, 'pending': True}, {'active': False, 'error': 'journal unreadable'},
])
def test_full_tun_publish_never_lets_an_old_gateway_record_turn_forwarding_off(host, managed):
    """A legacy host rebound to a full TUN profile: the deployed config has no
    gateway marker, so the Agent removes its record and, without the persisted
    AIO forwarding, writes ip_forward back to the journal's original 0."""
    from backend.agents.linux_gateway import FORWARDING_NOT_KEPT
    agent = {'id': 'rebound', 'profile_id': 'home', 'service_type': 'mihomo', 'deployment_method': 'shell',
             'version': '1.8.40-go'}
    host['status'] = {'success': True, 'managed': {**managed, 'preserves_forwarding': False}}
    with pytest.raises(ValueError) as error:
        _prepare_transactional_content(agent, full_tun())
    assert str(error.value) == FORWARDING_NOT_KEPT.format(action='发布完整 TUN 后，')
    assert host['calls'] == [('rebound', 'status', 'GET', 'home')]
    # Once the forwarding is persisted, removing the record keeps it on.
    host['status']['managed']['preserves_forwarding'] = True
    assert 'auto-redirect: true' in _prepare_transactional_content(agent, full_tun())['config_content']


def test_full_tun_publish_refuses_when_the_gateway_record_cannot_be_checked(host):
    from backend.agents.oxidns_monitor import MonitorError
    agent = {'id': 'm', 'profile_id': 'home', 'service_type': 'mihomo', 'deployment_method': 'shell', 'version': '1.8.40-go'}
    host['status'] = MonitorError('AGENT_UNREACHABLE', 502, '暂时无法连接 Agent，请检查设备连接。')
    with pytest.raises(ValueError, match='无法确认设备上是否仍有旧版 Linux 网关记录.*暂时无法连接 Agent'):
        _prepare_transactional_content(agent, full_tun())


def test_status_is_only_read_for_a_full_tun_shell_mihomo(host):
    shell = {'id': 'm', 'profile_id': 'home', 'service_type': 'mihomo', 'deployment_method': 'shell', 'version': '1.8.40-go'}
    _prepare_transactional_content({**shell, 'deployment_method': 'docker'}, full_tun())
    _prepare_transactional_content(shell, config(False))
    _prepare_transactional_content({**shell, 'version': MIN_AGENT_VERSION}, config(True))
    _prepare_transactional_content({**shell, 'service_type': 'oxidns'}, full_tun())
    assert host['calls'] == []
