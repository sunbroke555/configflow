"""Shared catalog ownership, protected mutations and profile-local chains."""
import copy

import pytest
from flask import Flask

from backend.common import config as config_module
from backend.common.config_repository import ProfileRepository
from backend.routes import register_blueprints
from backend.routes.aggregations import generate_aggregation_provider
from backend.utils.rule_utils import save_rule_to_local


@pytest.fixture
def catalog(tmp_path):
    repository = ProfileRepository(tmp_path)
    repository.save_shared({
        'subscriptions': [{'id': 'sub', 'name': 'Feed', 'url': 'https://example.test/sub', 'enabled': True}],
        'nodes': [{'id': 'node', 'name': 'Node', 'type': 'http', 'enabled': True,
                   'proxy_string': '{"type":"http","server":"example.test","port":80}'}],
        'subscription_aggregations': [{'id': 'agg', 'name': 'Aggregation', 'subscriptions': ['sub'], 'nodes': ['node'], 'enabled': True}],
        'rule_library': [{'id': 'library', 'name': 'Library', 'source_type': 'content',
                          'content': 'DOMAIN,example.test', 'behavior': 'classical', 'enabled': True}],
    })
    repository.save_profile('default', {
        'proxy_groups': [{'id': 'group', 'name': 'Proxy', 'type': 'select', 'manual_nodes': ['node'], 'subscriptions': ['sub'], 'aggregations': ['agg']}],
        'rule_configs': [{'id': 'rule', 'itemType': 'ruleset', 'library_rule_id': 'library', 'policy': 'Proxy', 'enabled': True}],
    })
    repository.create_profile({'id': 'other', 'name': 'Other'}, clone_from='default')
    repository.update_profile_transaction('other', lambda profile: profile['rule_configs'][0].update(policy='DIRECT', enabled=False))
    config_module.set_repository(repository)
    app = Flask(__name__)
    register_blueprints(app)
    return app.test_client(), repository


@pytest.mark.parametrize('endpoint', ['subscriptions', 'nodes', 'aggregations', 'rule-library'])
def test_catalog_ignores_unrelated_profile_selection(catalog, endpoint):
    client, _ = catalog
    default = client.get('/api/' + endpoint)
    stale = client.get('/api/' + endpoint, headers={'X-ConfigFlow-Profile': '../missing'})
    assert default.status_code == stale.status_code == 200
    assert default.json == stale.json
    assert len(default.json) == 1


@pytest.mark.parametrize('endpoint,resource_id', [('subscriptions', 'sub'), ('nodes', 'node'), ('aggregations', 'agg'), ('rule-library', 'library')])
def test_referenced_delete_preserves_all_profiles_sources_and_cache(catalog, endpoint, resource_id):
    client, repository = catalog
    save_rule_to_local(repository.get_shared()['rule_library'][0])
    cache = repository.shared_rules_dir() / 'Library.list'
    before = repository.export_all()
    response = client.delete(f'/api/{endpoint}/{resource_id}')
    assert response.status_code == 409, response.get_data(as_text=True)
    assert response.json['usages']
    assert 'Other' in str(response.json)
    assert repository.export_all() == before
    assert cache.read_text() == 'DOMAIN,example.test'


def test_library_changes_resolve_without_overwriting_composition(catalog):
    client, repository = catalog
    before = {key: repository.get_profile(key) for key in ('default', 'other')}
    source = repository.get_shared()['rule_library'][0]
    response = client.put('/api/rule-library/library', json={**source, 'name': 'Renamed', 'content': 'DOMAIN,new.test', 'enabled': False})
    assert response.status_code == 200
    for key, profile in before.items():
        assert repository.get_profile(key) == profile
        rule = repository.get_compat_config(key)['rule_configs'][0]
        assert rule['name'] == 'Renamed'
        assert rule['content'] == 'DOMAIN,new.test'
        assert rule['library_enabled'] is False
        assert rule['enabled'] == profile['rule_configs'][0]['enabled']
        assert rule['policy'] == profile['rule_configs'][0]['policy']


def test_shared_dialer_fields_rejected_and_chains_are_profile_local(catalog):
    client, repository = catalog
    node = repository.get_shared()['nodes'][0]
    before = repository.export_all()
    assert client.put('/api/nodes/node', json={**node, 'dialer_ref': {'type': 'group', 'id': 'group'}}).status_code == 400
    assert repository.export_all() == before
    assert client.post('/api/proxy-groups', json={'id': 'out', 'name': 'Outbound', 'type': 'select', 'manual_nodes': ['DIRECT']}).status_code == 200
    chain = {'id': 'via-node', 'name': 'Via Node', 'type': 'chain',
             'chain': {'entry': {'type': 'group', 'id': 'out'}, 'exit': {'type': 'node', 'id': 'node'}}}
    other = repository.get_profile('other')
    shared = repository.get_shared()
    assert client.post('/api/proxy-groups', json=chain).status_code == 200
    stored = next(group for group in client.get('/api/proxy-groups').json if group['id'] == 'via-node')
    assert stored['type'] == 'chain'
    assert stored['chain'] == chain['chain']
    assert repository.get_profile('other') == other
    assert repository.get_shared() == shared
    assert 'node_dialers' not in repository.get_profile('default')
    before = repository.export_all()
    disabled = client.put('/api/nodes/node', json={**node, 'enabled': False})
    assert disabled.status_code == 409
    assert repository.export_all() == before



def test_shared_aggregation_dependencies_block_source_deletion_from_empty_profile(catalog):
    client, repository = catalog
    repository.create_profile({'id': 'empty', 'name': 'Empty'})
    response = client.delete('/api/subscriptions/sub', headers={'X-ConfigFlow-Profile': 'empty'})
    assert response.status_code == 409
    assert 'Aggregation' in str(response.json)


def test_provider_output_is_scoped_and_does_not_mutate_source(catalog):
    _, repository = catalog
    config = repository.get_compat_config('default')
    config['subscriptions'][0]['enabled'] = False
    before = copy.deepcopy(config)
    first = generate_aggregation_provider(config['subscription_aggregations'][0], config=config)
    other = copy.deepcopy(config)
    other['profile_id'] = 'other'
    second = generate_aggregation_provider(other['subscription_aggregations'][0], config=other)
    assert first['file_path'] != second['file_path']
    assert '/profiles/default/providers/' in str(first['file_path'])
    assert '/profiles/other/providers/' in str(second['file_path'])
    assert config == before


def test_stats_uses_global_catalog_and_selected_composition(catalog):
    client, repository = catalog
    repository.create_profile({'id': 'empty', 'name': 'Empty'})
    response = client.get('/api/stats/overview', headers={'X-ConfigFlow-Profile': 'empty'})
    assert response.status_code == 200
    assert response.json['data']['nodes']['total'] == 1
    assert response.json['data']['subscriptions']['total'] == 1
    assert response.json['data']['rules']['total'] == 0
    assert response.json['data']['proxyGroups']['total'] == 0


def test_shared_node_rename_validates_other_profiles_raw_dialers(catalog):
    client, repository = catalog
    source = {'id': 'source', 'name': 'Chained', 'type': 'http', 'enabled': True,
              'proxy_string': '{"type":"http","server":"source.test","port":80,"dialer-proxy":"Node"}'}
    assert client.post('/api/nodes', json=source).status_code == 200
    assert client.post('/api/proxy-groups', headers={'X-ConfigFlow-Profile': 'other'}, json={
        'id': 'raw-chain', 'name': 'Raw Chain', 'type': 'select', 'manual_nodes': ['source'],
    }).status_code == 200
    before = repository.export_all()
    node = repository.get_shared()['nodes'][0]
    response = client.put('/api/nodes/node', headers={'X-ConfigFlow-Profile': 'default'}, json={**node, 'name': 'Renamed'})
    assert response.status_code == 409
    assert 'Other' in str(response.json)
    assert repository.export_all() == before
