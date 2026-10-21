"""Rules share sources while composition, group references and MosDNS stay local."""
import pytest
from flask import Flask

from backend.common import config as config_module
from backend.common.config_repository import ProfileRepository
from backend.routes import register_blueprints


@pytest.fixture
def compositions(tmp_path):
    repository = ProfileRepository(tmp_path)
    repository.save_shared({
        'subscriptions': [{'id': 'subscription', 'name': 'Feed', 'url': 'https://example.test/sub'}],
        'subscription_aggregations': [{'id': 'aggregation', 'name': 'Aggregate', 'subscriptions': ['subscription'], 'nodes': []}],
        'rule_library': [{'id': 'library', 'name': 'Shared Rules', 'source_type': 'content', 'content': 'DOMAIN,example.test', 'behavior': 'classical', 'enabled': True}],
    })
    repository.save_profile('default', {
        'proxy_groups': [
            {'id': 'group', 'name': 'Proxy', 'type': 'select', 'aggregations': ['aggregation'], 'manual_nodes': ['DIRECT']},
            {'id': 'following', 'name': 'Following', 'type': 'select', 'follow_group': 'group'},
            {'id': 'including', 'name': 'Including', 'type': 'select', 'include_groups': ['group'], 'proxies_order': [{'type': 'strategy', 'id': 'group'}]},
        ],
        'rule_configs': [
            {'id': 'set', 'itemType': 'ruleset', 'library_rule_id': 'library', 'policy': 'Proxy', 'enabled': True},
            {'id': 'inline', 'itemType': 'rule', 'rule_type': 'DOMAIN', 'value': 'local.test', 'policy': 'DIRECT', 'enabled': True},
        ],
    })
    repository.create_profile({'id': 'other', 'name': 'Other'}, clone_from='default')
    config_module.set_repository(repository)
    app = Flask(__name__)
    register_blueprints(app)
    return app.test_client(), repository, {'X-ConfigFlow-Profile': 'other'}


def test_source_fields_cannot_override_shared_library_and_reorder_only_moves_ids(compositions):
    client, repository, headers = compositions
    before = repository.get_profile('default')
    response = client.put('/api/rules/set', headers=headers, json={
        'policy': 'REJECT', 'name': 'Override', 'url': 'https://attacker.invalid',
        'content': 'override', 'behavior': 'ipcidr', 'source_type': 'url',
    })
    assert response.status_code == 200
    item = next(item for item in client.get('/api/rules', headers=headers).json if item['id'] == 'set')
    assert item['name'] == 'Shared Rules'
    assert item['behavior'] == 'classical'
    assert item['content'] == 'DOMAIN,example.test'
    assert item['policy'] == 'REJECT'
    assert client.post('/api/rules/reorder', headers=headers, json={'rule_configs': [{'id': 'inline'}, {'id': 'set', 'policy': 'DIRECT'}]}).status_code == 200
    profile = repository.get_profile('other')
    assert [item['id'] for item in profile['rule_configs']] == ['inline', 'set']
    assert profile['rule_configs'][1]['policy'] == 'REJECT'
    assert 'content' not in profile['rule_configs'][1]
    assert 'url' not in profile['rule_configs'][1]
    assert repository.get_profile('default') == before


def test_missing_library_is_rejected_atomically(compositions):
    client, repository, headers = compositions
    before = repository.export_all()
    response = client.post('/api/rule-sets', headers=headers, json={'id': 'bad', 'name': 'Private', 'url': 'https://example.test', 'policy': 'DIRECT'})
    assert response.status_code == 400
    assert repository.export_all() == before


def test_group_rename_preserves_id_references_and_profile_isolation(compositions):
    client, repository, headers = compositions
    before = repository.get_profile('default')
    assert client.put('/api/proxy-groups/group', headers=headers, json={'name': 'Renamed'}).status_code == 200
    profile = repository.get_profile('other')
    assert profile['rule_configs'][0]['policy'] == 'Renamed'
    assert profile['rule_configs'][1]['policy'] == 'DIRECT'
    assert profile['proxy_groups'][1]['follow_group'] == 'group'
    assert profile['proxy_groups'][2]['include_groups'] == ['group']
    assert profile['proxy_groups'][2]['proxies_order'] == [{'type': 'strategy', 'id': 'group'}]
    assert repository.get_profile('default') == before
    snapshot = repository.export_all()
    assert client.delete('/api/proxy-groups/group', headers=headers).status_code == 409
    assert repository.export_all() == snapshot


def test_any_profile_can_reference_shared_aggregation_sources_directly(compositions):
    client, repository, headers = compositions
    default = repository.get_profile('default')
    shared = repository.get_shared()
    response = client.put('/api/proxy-groups/group', headers=headers, json={'subscriptions': ['subscription']})
    assert response.status_code == 200, response.get_data(as_text=True)
    assert repository.get_profile('other')['proxy_groups'][0]['subscriptions'] == ['subscription']
    assert repository.get_compat_config('other')['subscriptions'][0]['id'] == 'subscription'
    assert repository.get_profile('default') == default
    assert repository.get_shared() == shared
    assert 'resource_refs' not in repository.get_profile('other')



def test_dns_selection_is_cleaned_atomically_when_rule_is_deleted(compositions):
    client, repository, headers = compositions
    before = repository.get_profile('default')
    response = client.post('/api/mosdns/rulesets', headers=headers, json={'direct_rulesets': ['set'], 'direct_rules': ['inline']})
    assert response.status_code == 200
    for rule_id in ('set', 'inline'):
        response = client.delete('/api/rules/' + rule_id, headers=headers)
        assert response.status_code == 200
        current = repository.get_profile('other')
        assert all(item['id'] != rule_id for item in current['rule_configs'])
        assert rule_id not in current['mosdns']['direct_rulesets']
        assert rule_id not in current['mosdns']['direct_rules']
    assert repository.get_profile('default') == before


def test_selected_dns_rule_cannot_change_to_an_unconvertible_type(compositions):
    client, repository, headers = compositions
    assert client.post('/api/mosdns/rulesets', headers=headers, json={'proxy_rules': ['inline']}).status_code == 200
    before = repository.get_profile('other')
    response = client.put('/api/rules/inline', headers=headers, json={'rule_type': 'GEOIP', 'value': 'CN'})
    assert response.status_code == 400
    assert 'MosDNS 代理规则' in response.get_json()['message']
    assert repository.get_profile('other') == before
    # Convertible types and unrelated edits still save.
    assert client.put('/api/rules/inline', headers=headers, json={'rule_type': 'IP-CIDR', 'value': '10.0.0.0/8'}).status_code == 200
    assert client.put('/api/rules/inline', headers=headers, json={'policy': 'Proxy'}).status_code == 200
    # Unselected rules may use any type.
    assert client.put('/api/rules/inline', headers={'X-ConfigFlow-Profile': 'default'}, json={'rule_type': 'GEOIP'}).status_code == 200


def test_native_oxidns_type_guard_follows_the_routing_document(compositions):
    """Native drafts keep stale selectors as a catalog; the YAML decides what is selected."""
    client, repository, headers = compositions
    def save(settings):
        assert client.post('/api/oxidns/settings', headers=headers, json=settings).status_code == 200
    def apply(settings, proxy_rules):
        response = client.post('/api/oxidns/common-settings/apply', headers=headers,
                               json={'settings': settings, 'changes': {'proxy_rules': proxy_rules}})
        assert response.status_code == 200, response.get_json()
        return response.get_json()['settings']
    def retype(rule_type, value):
        return client.put('/api/rules/inline', headers=headers, json={'rule_type': rule_type, 'value': value})
    settings = client.get('/api/oxidns/settings', headers=headers).get_json()
    native = client.post('/api/oxidns/editor', headers=headers, json={'settings': settings}).get_json()['settings']
    save(apply(native, ['inline']))
    response = retype('GEOIP', 'CN')
    assert response.status_code == 400 and 'OxiDNS 代理规则' in response.get_json()['message']
    unselected = apply(client.get('/api/oxidns/settings', headers=headers).get_json(), [])
    save(unselected)
    stored = client.get('/api/oxidns/settings', headers=headers).get_json()
    assert stored['config_mode'] == 'native' and 'inline' in stored['proxy_rules']
    assert retype('GEOIP', 'CN').status_code == 200


def test_scoped_content_rule_proxy_avoids_network(compositions, monkeypatch):
    client, repository, headers = compositions
    def unexpected_network(*args, **kwargs):
        raise AssertionError('Local shared content must not perform HTTP self-requests')
    monkeypatch.setattr('requests.get', unexpected_network)
    query = {'url': '/api/profiles/other/rule-library/content/library'}
    assert client.get('/api/profiles/other/mosdns/rule-proxy', query_string=query, headers=headers).status_code == 401
    query['token'] = repository.get_system()['system_config']['rule_proxy_token']
    response = client.get('/api/profiles/other/mosdns/rule-proxy', query_string=query, headers=headers)
    assert response.status_code == 200
    assert response.get_data(as_text=True).strip() == 'full:example.test'
