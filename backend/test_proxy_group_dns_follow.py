"""Group defaults and opted-in OxiDNS routes change in the same transaction."""
import pytest
import yaml
from flask import Flask

from backend.common import config as config_store
from backend.common.config_repository import ProfileRepository
from backend.common.oxidns_editor import prepare_editor
from backend.common.rule_dns import rule_dns_state
from backend.routes import register_blueprints


HEADERS = {'X-ConfigFlow-Profile': 'other'}
PROXY_FIRST = [{'type': 'node', 'id': 'node'}, {'type': 'node', 'id': 'DIRECT'}]


@pytest.fixture
def group_dns(tmp_path):
    repository = ProfileRepository(tmp_path)
    repository.save_shared({
        'nodes': [{'id': 'node', 'name': 'Proxy node', 'type': 'http',
                   'server': 'proxy.example.test', 'port': 8080}],
        'rule_library': [{'id': 'library', 'name': 'Domains', 'source_type': 'content',
                          'content': 'DOMAIN,service.example.test', 'behavior': 'classical', 'enabled': True}],
    })
    repository.save_profile('default', {
        'proxy_groups': [
            {'id': 'source', 'name': 'Source', 'type': 'select', 'manual_nodes': ['DIRECT', 'node'],
             'proxies_order': list(reversed(PROXY_FIRST))},
            {'id': 'proxy', 'name': 'Proxy', 'type': 'select', 'manual_nodes': ['node']},
            {'id': 'following', 'name': 'Following', 'type': 'select', 'follow_group': 'source'},
            {'id': 'nested', 'name': 'Nested', 'type': 'select', 'include_groups': ['following']},
        ],
        'rule_configs': [
            {'id': 'followed', 'itemType': 'rule', 'rule_type': 'DOMAIN',
             'value': 'followed.example.test', 'policy': 'Following', 'enabled': True},
            {'id': 'nested', 'itemType': 'ruleset', 'library_rule_id': 'library',
             'policy': 'Nested', 'enabled': True},
            {'id': 'fixed', 'itemType': 'rule', 'rule_type': 'DOMAIN',
             'value': 'fixed.example.test', 'policy': 'Following', 'enabled': True},
            {'id': 'default', 'itemType': 'rule', 'rule_type': 'DOMAIN',
             'value': 'default.example.test', 'policy': 'Following', 'enabled': True},
            {'id': 'blocked', 'itemType': 'rule', 'rule_type': 'DOMAIN',
             'value': 'blocked.example.test', 'policy': 'REJECT', 'enabled': True},
        ],
        'oxidns': {
            'direct_rules': ['followed', 'fixed'], 'direct_rulesets': ['nested'],
            'rule_dns_follow': ['followed', 'nested'],
            'rule_actions': {'blocked': {'action': 'blackhole'}},
        },
    })
    repository.create_profile({'id': 'other', 'name': 'Other'}, clone_from='default')
    previous = config_store._repository
    config_store.set_repository(repository)
    app = Flask(__name__)
    register_blueprints(app)
    yield app, app.test_client(), repository
    config_store.set_repository(previous)


def make_native(repository, *, locked=False):
    snapshot, _ = repository.get_publish_snapshot('other')
    settings = prepare_editor(snapshot['oxidns'], snapshot, base_url='https://config.test')
    if locked:
        document = yaml.safe_load(settings['native_config'])
        plugin = next(plugin for plugin in document['plugins'] if plugin['tag'] == 'cf_dns_blackhole')
        plugin['args']['mode'] = 'nxdomain'
        settings['native_config'] = yaml.safe_dump(document, sort_keys=False)
    repository.update_profile_fields('other', {'oxidns': settings})


def assert_followed_routes(repository, route):
    snapshot, _ = repository.get_publish_snapshot('other')
    state = rule_dns_state(snapshot)['rules']
    for identity in ('followed', 'nested'):
        assert state[identity] == {'state': 'follow', 'route': route, 'follow_route': route}
    assert state['fixed'] == {'state': 'explicit', 'route': 'direct'}
    assert state['blocked'] == {'state': 'explicit', 'route': 'blackhole'}
    assert 'default' not in state


@pytest.mark.parametrize('native', [False, True], ids=['template', 'native'])
@pytest.mark.parametrize('entry', ['http', 'mcp'])
def test_group_default_reorder_updates_follow_dns_once_in_selected_profile(group_dns, native, entry):
    app, client, repository = group_dns
    if native:
        make_native(repository)
    untouched = repository.get_profile('default')
    shared = repository.get_shared()
    before = repository.get_profile('other')
    if entry == 'mcp':
        from backend.mcp_server.tools import call_tool
        with app.app_context():
            result = call_tool('manage_proxy_group', {
                'profile_id': 'other', 'action': 'update', 'id': 'source',
                'data': {'proxies_order': PROXY_FIRST},
            })
        assert result['action'] == 'update'
        assert result['profile_id'] == 'other'
        assert result['item']['id'] == 'source'
        assert result['item']['proxies_order'] == PROXY_FIRST
    else:
        response = client.put('/api/proxy-groups/source', headers=HEADERS, json={'proxies_order': PROXY_FIRST})
        assert response.status_code == 200, response.get_json()
    assert_followed_routes(repository, 'proxy')
    after = repository.get_profile('other')
    assert after['_revision'] == before['_revision'] + 1
    assert after['rule_configs'] == before['rule_configs']
    assert after['oxidns']['rule_dns_follow'] == ['followed', 'nested']
    assert repository.get_profile('default') == untouched
    assert repository.get_shared() == shared


@pytest.mark.parametrize('native', [False, True], ids=['template', 'native'])
def test_change_follow_source_updates_rules_using_nested_groups(group_dns, native):
    _, client, repository = group_dns
    if native:
        make_native(repository)
    response = client.put('/api/proxy-groups/following', headers=HEADERS, json={'follow_group': 'proxy'})
    assert response.status_code == 200, response.get_json()
    assert_followed_routes(repository, 'proxy')
    response = client.put('/api/proxy-groups/following', headers=HEADERS, json={'follow_group': 'source'})
    assert response.status_code == 200, response.get_json()
    assert_followed_routes(repository, 'direct')


def test_locked_native_routing_rolls_back_group_and_dns_together(group_dns):
    _, client, repository = group_dns
    make_native(repository, locked=True)
    # A rename does not change the resolved route, so it can still save.
    native = repository.get_profile('other')['oxidns']
    response = client.put('/api/proxy-groups/source', headers=HEADERS, json={'name': 'Renamed source'})
    assert response.status_code == 200, response.get_json()
    assert repository.get_profile('other')['oxidns'] == native
    before = repository.export_all()
    response = client.put('/api/proxy-groups/source', headers=HEADERS, json={'proxies_order': PROXY_FIRST})
    assert response.status_code == 409, response.get_json()
    assert response.json['usages'][0]['code'] == 'oxidns_routing_custom'
    assert repository.export_all() == before


def test_locked_native_explicit_dns_does_not_block_group_default_edit(group_dns):
    _, client, repository = group_dns
    repository.update_profile_transaction('other', lambda profile: profile['oxidns'].update(rule_dns_follow=[]))
    make_native(repository, locked=True)
    before = repository.get_profile('other')['oxidns']
    response = client.put('/api/proxy-groups/source', headers=HEADERS, json={'proxies_order': PROXY_FIRST})
    assert response.status_code == 200, response.get_json()
    assert repository.get_profile('other')['oxidns'] == before


@pytest.mark.parametrize('inactive', ['rule', 'library'])
def test_locked_native_inactive_follow_does_not_block_group_default_edit(group_dns, inactive):
    _, client, repository = group_dns
    def only_nested_follow(profile):
        profile['oxidns']['rule_dns_follow'] = ['nested']
        if inactive == 'rule':
            next(rule for rule in profile['rule_configs'] if rule['id'] == 'nested')['enabled'] = False
    repository.update_profile_transaction('other', only_nested_follow)
    if inactive == 'library':
        repository.update_shared_transaction(lambda shared: shared['rule_library'][0].update(enabled=False))
    make_native(repository, locked=True)
    before = repository.get_profile('other')['oxidns']
    response = client.put('/api/proxy-groups/source', headers=HEADERS, json={'proxies_order': PROXY_FIRST})
    assert response.status_code == 200, response.get_json()
    assert repository.get_profile('other')['oxidns'] == before
    snapshot, _ = repository.get_publish_snapshot('other')
    assert rule_dns_state(snapshot)['rules']['nested'] == {
        'state': 'disabled', 'route': 'proxy', 'follow': True}


@pytest.mark.parametrize('native', [False, True], ids=['template', 'native'])
def test_newly_selected_shared_node_changes_follow_dns(group_dns, native):
    _, client, repository = group_dns
    repository.update_shared_transaction(lambda shared: shared['nodes'].append({
        'id': 'unused', 'name': 'New proxy node', 'type': 'http',
        'server': 'new.example.test', 'port': 8080}))
    snapshot, _ = repository.get_publish_snapshot('other')
    assert not any(node['id'] == 'unused' for node in snapshot['nodes'])
    if native:
        make_native(repository)
    response = client.put('/api/proxy-groups/source', headers=HEADERS, json={
        'manual_nodes': ['unused', 'DIRECT'],
        'proxies_order': [{'type': 'node', 'id': 'unused'}, {'type': 'node', 'id': 'DIRECT'}],
    })
    assert response.status_code == 200, response.get_json()
    assert_followed_routes(repository, 'proxy')
