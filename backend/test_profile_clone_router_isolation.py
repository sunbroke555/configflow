"""Cloning configuration must not duplicate access to production routers."""
from copy import deepcopy
from unittest.mock import Mock

import pytest
from flask import Flask

from backend.common import config as config_module
from backend.common.config_repository import PROFILE_FIELDS, ProfileRepository
from backend.routers.rule_sync_store import job_lock
from backend.routes import register_blueprints


@pytest.fixture
def production_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, 'DATA_DIR', str(tmp_path))
    repository = ProfileRepository(tmp_path)
    monkeypatch.setattr(config_module, '_repository', repository)
    monkeypatch.setattr('backend.common.auth.is_auth_enabled', lambda: False)
    network_request = Mock(side_effect=AssertionError('cloning must not contact routers or agents'))
    monkeypatch.setattr('requests.sessions.Session.request', network_request)
    config_module.reset_config_context()

    repository.save_shared({
        'subscriptions': [{'id': 'feed', 'name': 'Shared subscription', 'url': 'https://subscription.test/feed'}],
        'rule_library': [{'id': 'shared-ip', 'name': 'Shared IPs', 'source_type': 'content',
                          'behavior': 'ipcidr', 'content': '1.1.1.0/24\n8.8.8.0/24'}],
    })
    repository.create_profile({'id': 'production', 'name': 'Production'})
    ros = {
        'id': 'production-ros', 'name': 'Production ROS', 'type': 'routeros',
        'endpoint': 'https://ros.example.test', 'username': 'production-ros-user',
        'password': 'production-ros-test-password', 'verify_tls': True,
        'proxy_gateway_agent_id': 'mihomo-production', 'dns_agent_id': 'oxidns-production',
        'ip_rule_sets': ['static-ips'], 'ros_policy_rule_sets': ['policy-ips'],
        'manage_return_routing': True, 'auto_rule_sync': True, 'rule_sync_interval': 15,
    }
    ikuai = {
        'id': 'production-ikuai', 'name': 'Production iKuai', 'type': 'ikuai',
        'endpoint': 'https://ikuai.example.test', 'username': 'production-ikuai-user',
        'password': 'production-ikuai-test-password', 'verify_tls': False,
        'proxy_gateway_agent_id': 'mihomo-production', 'dns_agent_id': 'oxidns-production',
        'ip_rule_sets': ['static-ips'], 'ros_policy_rule_sets': [],
        'manage_return_routing': True, 'auto_rule_sync': False, 'rule_sync_interval': 720,
    }
    repository.save_profile('production', {
        'routers': {'items': [ros, ikuai]},
        'proxy_groups': [{'id': 'proxy', 'name': 'Proxy', 'type': 'select', 'subscriptions': ['feed']}],
        'rule_configs': [
            {'id': 'static-ips', 'itemType': 'ruleset', 'library_rule_id': 'shared-ip',
             'policy': 'Proxy', 'enabled': True},
            {'id': 'policy-ips', 'itemType': 'ruleset', 'library_rule_id': 'shared-ip',
             'policy': 'Proxy', 'enabled': True},
            {'id': 'direct', 'itemType': 'rule', 'rule_type': 'DOMAIN',
             'value': 'example.test', 'policy': 'DIRECT'},
        ],
        'mihomo': {'custom_config': 'mixed-port: 7890\nmode: rule\n'},
        'surge': {'custom_config': '[General]\nloglevel = notify\n'},
        'loon': {'custom_config': '[General]\nip-mode = ipv4-only\n'},
        'mosdns': {'local_dns': 'https://dns.example.test/query', 'direct_rulesets': ['static-ips']},
        'oxidns': {'local_dns': 'https://dns.example.test/query', 'proxy_rulesets': ['policy-ips']},
    })
    with job_lock('production', ros['id'], ros) as acquired:
        assert acquired
        repository.save_router_publication('production', ros, manifest={
            'owner': 'configflow:production-ros',
            'resources': [{'path': '/ip/firewall/mangle', 'id': '*1',
                           'fields': {'comment': 'configflow:production-ros'}}],
        }, last_release={'ok': True, 'at': '2026-10-19T00:00:00Z'})
    repository.save_system({'agents': [
        {'id': 'mihomo-production', 'name': 'Mihomo production', 'profile_id': 'production',
         'service_type': 'mihomo', 'host': '192.168.1.2', 'token': 'mihomo-test-token'},
        {'id': 'oxidns-production', 'name': 'OxiDNS production', 'profile_id': 'production',
         'service_type': 'oxidns', 'host': '192.168.1.2', 'token': 'oxidns-test-token'},
    ]})
    app = Flask(__name__)
    app.config['TESTING'] = True
    register_blueprints(app)
    yield app.test_client(), repository, network_request
    config_module.reset_config_context()


def clone_via_api(client, entrypoint):
    payload = {'id': 'test-space', 'name': 'Test space', 'description': 'Configuration-only copy'}
    if entrypoint == 'clone':
        return client.post('/api/profiles/production/clone', json=payload)
    return client.post('/api/profiles', json={**payload, 'clone_from': 'production'})


@pytest.mark.parametrize('entrypoint', ['clone', 'create-with-clone-from'])
def test_profile_clone_preserves_configuration_without_copying_router_access(production_profile, entrypoint):
    client, repository, network_request = production_profile
    source = repository.get_profile('production')
    shared = repository.get_shared()
    system = repository.get_system()

    response = clone_via_api(client, entrypoint)

    assert response.status_code == 201, response.get_json()
    copied = repository.get_profile('test-space')
    assert copied['id'] == 'test-space'
    assert copied['name'] == 'Test space'
    assert copied['description'] == 'Configuration-only copy'
    # The entire connection records are absent, including credentials, Agent
    # references, synchronization preferences and live ownership manifests.
    assert copied['routers'] == {'items': []}
    for field in PROFILE_FIELDS:
        if field != 'routers':
            assert copied[field] == source[field], field
    listed = client.get('/api/routers', headers={'X-ConfigFlow-Profile': 'test-space'})
    assert listed.status_code == 200
    assert listed.get_json()['routers'] == []
    assert repository.get_profile('production') == source
    assert repository.get_shared() == shared
    assert repository.get_system() == system
    network_request.assert_not_called()


@pytest.mark.parametrize('entrypoint', ['clone', 'create-with-clone-from'])
def test_copied_space_can_be_deleted_without_withdrawing_production_ownership(production_profile, entrypoint):
    client, repository, network_request = production_profile
    before = deepcopy(repository.export_all())
    assert before['profiles']['production']['routers']['items'][0]['ros_policy_state']
    assert clone_via_api(client, entrypoint).status_code == 201

    deleted = client.delete('/api/profiles/test-space')

    assert deleted.status_code == 204, deleted.get_data(as_text=True)
    # Deletion needs neither a production Agent move nor a RouterOS withdrawal.
    assert repository.export_all() == before
    assert not repository.profile_dir('test-space').exists()
    network_request.assert_not_called()
