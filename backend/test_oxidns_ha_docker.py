"""Docker HA admission uses live readiness and independent host identity proofs."""
import copy

import pytest

from backend.agents import oxidns_ha
from backend.test_oxidns_ha import PRIMARY_HOST, SECONDARY_HOST, VIP, enable, pair  # noqa: F401
from backend.test_oxidns_ha_publish import PRIMARY, SECONDARY, ha_publish  # noqa: F401


def prepare(pair, monkeypatch, network='host'):
    manager, devices, primary, secondary = pair
    manager._update_agents(lambda agents: [item.update(deployment_method='docker') for item in agents])
    capabilities = {
        host: {'oxidns_ha_protocols': [1], 'oxidns_ha_container_protocols': [1],
               'oxidns_ha_environment': {'kind': 'container', 'ready': True,
                                         'network': network, 'host_id': digit * 64, 'blockers': []}}
        for host, digit in ((PRIMARY_HOST, 'a'), (SECONDARY_HOST, 'b'))
    }

    def live(agent, method, suffix, **kwargs):
        devices.capabilities(agent, method, suffix, **kwargs)
        return copy.deepcopy(capabilities[agent['host']])

    monkeypatch.setattr(manager, '_deployment_request', live)
    return capabilities


@pytest.mark.parametrize('network', ['host', 'macvlan'])
def test_two_ready_docker_hosts_can_enable_secondary_first(pair, monkeypatch, network):
    caps = prepare(pair, monkeypatch, network)
    manager, devices, primary, secondary = pair
    enable(manager, primary, secondary)
    assert [call[1] for call in devices.calls if call[0] == 'apply'] == [SECONDARY_HOST, PRIMARY_HOST]
    assert manager.get_agent_by_id(primary['id'])['oxidns_ha']['enabled'] is True
    assert caps[PRIMARY_HOST]['oxidns_ha_environment']['network'] == network


@pytest.mark.parametrize('change', [
    {'oxidns_ha_container_protocols': []},
    {'oxidns_ha_environment': {}},
    {'oxidns_ha_environment': {'kind': 'container', 'ready': False, 'blockers': ['缺少 NET_ADMIN']}},
    {'oxidns_ha_environment': {'kind': 'container', 'ready': 'true', 'network': 'host'}},
    {'oxidns_ha_environment': {'kind': 'container', 'ready': True, 'network': 'bridge'}},
    {'oxidns_ha_environment': {'kind': 'native', 'ready': True, 'network': 'host'}},
    {'oxidns_ha_environment': {'kind': 'container', 'ready': False, 'blockers': None}},
])
@pytest.mark.parametrize('public', [False, True])
def test_unprepared_docker_is_rejected_before_preview_or_publication(pair, monkeypatch, change, public):
    caps = prepare(pair, monkeypatch)
    manager, devices, primary, secondary = pair
    caps[SECONDARY_HOST].update(change)
    configure = oxidns_ha.configure if public else oxidns_ha._configure_keepalived
    with pytest.raises(oxidns_ha.HAError, match='Docker 高可用环境未就绪'):
        configure(manager, primary['id'], {'peer_id': secondary['id'], 'vip': VIP})
    assert devices.kinds() == [('capabilities', PRIMARY_HOST), ('capabilities', SECONDARY_HOST)]
    assert 'oxidns_ha' not in manager.get_agent_by_id(primary['id'])


@pytest.mark.parametrize('host_id', ['', None, 'a', 123, 'x' * 64])
def test_docker_requires_both_verified_host_identities(pair, monkeypatch, host_id):
    caps = prepare(pair, monkeypatch)
    manager, devices, primary, secondary = pair
    caps[SECONDARY_HOST]['oxidns_ha_environment']['host_id'] = host_id
    with pytest.raises(oxidns_ha.HAError, match='独立宿主机'):
        enable(manager, primary, secondary)
    assert all(call[0] == 'capabilities' for call in devices.calls)


@pytest.mark.parametrize('mixed', [False, True])
def test_different_ips_on_the_same_host_do_not_count_as_ha(pair, monkeypatch, mixed):
    caps = prepare(pair, monkeypatch)
    manager, devices, primary, secondary = pair
    caps[SECONDARY_HOST]['oxidns_ha_environment']['host_id'] = 'a' * 64
    if mixed:
        manager._update_agents(lambda agents: next(item for item in agents if item['id'] == secondary['id'])
                               .update(deployment_method='shell'))
        caps[SECONDARY_HOST]['oxidns_ha_environment']['kind'] = 'native'
    with pytest.raises(oxidns_ha.HAError, match='同一台宿主机'):
        enable(manager, primary, secondary)
    assert all(call[0] == 'capabilities' for call in devices.calls)


def test_mixed_native_and_container_need_live_identity_from_native_too(pair, monkeypatch):
    caps = prepare(pair, monkeypatch)
    manager, _, primary, secondary = pair
    manager._update_agents(lambda agents: next(item for item in agents if item['id'] == secondary['id'])
                           .update(deployment_method='shell'))
    caps[SECONDARY_HOST] = {'oxidns_ha_protocols': [1]}
    with pytest.raises(oxidns_ha.HAError, match='升级两端 Agent'):
        enable(manager, primary, secondary)
    caps[SECONDARY_HOST]['oxidns_ha_environment'] = {'kind': 'native', 'host_id': 'b' * 64}
    enable(manager, primary, secondary)


def test_registration_cannot_hide_the_live_container_environment(pair, monkeypatch):
    caps = prepare(pair, monkeypatch)
    manager, devices, primary, secondary = pair
    manager._update_agents(lambda agents: [item.update(deployment_method='shell') for item in agents])
    caps[SECONDARY_HOST]['oxidns_ha_environment']['ready'] = False
    with pytest.raises(oxidns_ha.HAError, match='Docker 高可用环境未就绪'):
        enable(manager, primary, secondary)
    assert all(call[0] == 'capabilities' for call in devices.calls)


def test_old_docker_agent_needs_recreation_instead_of_an_unconditional_ban(pair):
    manager, devices, primary, secondary = pair
    manager._update_agents(lambda agents: next(item for item in agents if item['id'] == secondary['id'])
                           .update(deployment_method='docker'))
    with pytest.raises(oxidns_ha.HAError, match='重新创建容器'):
        enable(manager, primary, secondary)
    assert all(call[0] == 'capabilities' for call in devices.calls)


@pytest.mark.parametrize('ready', [False, True])
@pytest.mark.parametrize('reload_only', [False, True])
def test_existing_docker_pair_is_rechecked_before_publication_changes_either_core(ha_publish, monkeypatch, ready, reload_only):
    env = ha_publish
    env.pair()
    if reload_only:
        baseline = env.bundle(ident='ha-baseline', rule='domain:baseline.example')
        env.manager.publish_deployment(env.primary['id'], baseline)
        env.drain()
        assert env.manager.get_deployment(env.primary['id'], baseline.manifest['deployment_id'])['status'] == 'succeeded'
        env.devices.uploads.clear()
    env.manager._update_agents(lambda agents: [item.update(deployment_method='docker') for item in agents])
    original = env.devices.request

    def request(agent, method, path, **kwargs):
        response = original(agent, method, path, **kwargs)
        if (method, path) == ('get', 'capabilities'):
            response.update(oxidns_ha_container_protocols=[1], oxidns_ha_environment={
                'kind': 'container', 'network': 'host', 'ready': ready,
                'host_id': ('a' if agent['host'] == PRIMARY else 'b') * 64,
                'blockers': [] if ready else ['独立高可用服务不可用']})
        return response

    monkeypatch.setattr(env.devices, 'request', request)
    candidate = env.bundle()
    if reload_only:
        from backend.agents.deployment_bundle import _pack
        from backend.agents.rule_sync_store import bundle_files
        candidate = _pack({**candidate.manifest, 'reload_only': True}, bundle_files(candidate), candidate.config_version,
                          comparison_config=candidate.comparison_config)
    env.manager.publish_deployment(env.primary['id'], candidate)
    env.drain()
    state = env.manager.get_deployment(env.primary['id'], candidate.manifest['deployment_id'])
    if ready:
        assert state['status'] == 'succeeded'
        assert [item['host'] for item in env.devices.uploads] == [SECONDARY, PRIMARY]
        assert env.devices.handoffs == {}
    else:
        assert state['status'] == 'failed'
        assert env.devices.uploads == []


def test_staged_docker_pair_rechecks_environment_before_resumed_activation(ha_publish, monkeypatch):
    env = ha_publish
    env.pair()
    env.manager._update_agents(lambda agents: [item.update(deployment_method='docker') for item in agents])
    original = env.devices.request
    ready = True

    def request(agent, method, path, **kwargs):
        response = original(agent, method, path, **kwargs)
        if (method, path) == ('get', 'capabilities'):
            response.update(oxidns_ha_container_protocols=[1], oxidns_ha_environment={
                'kind': 'container', 'network': 'host', 'ready': ready,
                'host_id': ('a' if agent['host'] == PRIMARY else 'b') * 64})
        return response

    monkeypatch.setattr(env.devices, 'request', request)
    candidate = env.bundle()
    env.manager.publish_deployment(env.primary['id'], candidate, activate=False)
    env.drain()
    assert env.manager.get_deployment(env.primary['id'], candidate.manifest['deployment_id'])['status'] == 'ready'
    assert len(env.devices.uploads) == 2 and not any(item['activate'] for item in env.devices.uploads)
    ready = False
    from backend.agents.manager import AgentManager
    restarted = AgentManager(env.repository)
    restarted.activate_deployment(env.primary['id'], candidate.manifest['deployment_id'])
    env.drain(manager=restarted)
    state = restarted.get_deployment(env.primary['id'], candidate.manifest['deployment_id'])
    assert state['status'] == 'failed'
    assert not any(path.endswith('/activate') for _, path in env.devices.mutations())
