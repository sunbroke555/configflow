"""Batch probe edits preserve all other group data and profile isolation."""
from copy import deepcopy

import pytest
from flask import Flask

from backend.common import config
from backend.common.config_repository import ProfileRepository
from backend.routes import register_blueprints


ENDPOINT = '/api/proxy-groups/batch-health-url'
NEW_URL = 'https://probe.example.test/generate_204'


@pytest.fixture
def batch_env(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    repository.save_shared({
        'nodes': [
            {'id': 'node', 'name': 'Node', 'type': 'http', 'server': 'example.test', 'port': 8080},
            {'id': 'relay', 'name': 'Relay', 'type': 'http', 'server': 'relay.example.test', 'port': 8080},
        ],
        'subscriptions': [{'id': 'subscription', 'name': 'Feed', 'url': 'https://example.test/sub'}],
    })
    repository.save_profile('default', {
        'proxy_groups': [
            {
                'id': 'auto', 'name': 'Auto', 'type': 'url-test',
                'manual_nodes': ['node', 'DIRECT'], 'subscriptions': ['subscription'],
                'regex': 'HK', 'proxies_order': [
                    {'type': 'node', 'id': 'node'}, {'type': 'node', 'id': 'DIRECT'},
                ],
                'url': 'http://legacy.example.test/auto', 'interval': 73,
                'timeout': 8900, 'lazy': True, 'expected-status': '200-299/304', 'tolerance': 29,
            },
            {'id': 'fallback', 'name': 'Fallback', 'type': 'fallback',
             'manual_nodes': ['DIRECT'], 'url': 'https://legacy.example.test/fallback'},
            {'id': 'balance', 'name': 'Balance', 'type': 'load-balance', 'enabled': False,
             'manual_nodes': ['DIRECT'], 'strategy': 'round-robin'},
            {'id': 'select', 'name': 'Select', 'type': 'select', 'manual_nodes': ['DIRECT']},
            {'id': 'following', 'name': 'Following', 'type': 'url-test', 'follow_group': 'auto',
             'url': 'https://legacy.example.test/following'},
            {'id': 'chain', 'name': 'Chain', 'type': 'chain',
             'chain': {'entry': {'type': 'node', 'id': 'relay'},
                       'exit': {'type': 'node', 'id': 'node'}}},
        ],
        'rule_configs': [
            {'id': 'rule', 'itemType': 'rule', 'rule_type': 'DOMAIN',
             'value': 'example.test', 'policy': 'Auto', 'enabled': True},
        ],
    })
    repository.create_profile({'id': 'other', 'name': 'Other'}, clone_from='default')
    monkeypatch.setattr(config, '_repository', repository)
    config.reset_config_context()
    app = Flask(__name__)
    register_blueprints(app)
    yield app.test_client(), repository
    config.reset_config_context()


@pytest.mark.parametrize('selection', ['header', 'query'])
def test_batch_changes_only_url_in_selected_profile(batch_env, selection):
    client, repository = batch_env
    default_before = repository.get_profile('default')
    shared_before = repository.get_shared()
    profile_before = repository.get_profile('other')
    selected_ids = ['balance', 'auto', 'fallback']
    headers = {'X-ConfigFlow-Profile': 'other'} if selection == 'header' else {}
    endpoint = ENDPOINT if selection == 'header' else ENDPOINT + '?profile=other'

    response = client.put(endpoint, headers=headers, json={'ids': selected_ids, 'url': NEW_URL})

    assert response.status_code == 200, response.get_data(as_text=True)
    profile_after = repository.get_profile('other')
    expected_groups = deepcopy(profile_before['proxy_groups'])
    for group in expected_groups:
        if group['id'] in selected_ids:
            group['url'] = NEW_URL
    assert profile_after['proxy_groups'] == expected_groups
    expected_by_id = {group['id']: group for group in expected_groups}
    assert response.json == {'success': True, 'data': [expected_by_id[ident] for ident in selected_ids]}
    for field in profile_before.keys() - {'proxy_groups', '_revision', 'updated_at'}:
        assert profile_after[field] == profile_before[field]
    assert profile_after['_revision'] == profile_before['_revision'] + 1
    assert repository.get_profile('default') == default_before
    assert repository.get_shared() == shared_before


@pytest.mark.parametrize('url', [
    'http://custom.example.test:8080/probe?mode=fast',
    'https://custom.example.test/probe',
])
def test_batch_keeps_explicit_http_and_https_urls(batch_env, url):
    client, repository = batch_env
    response = client.put(ENDPOINT, json={'ids': ['auto', 'fallback'], 'url': url})
    assert response.status_code == 200
    groups = {group['id']: group for group in repository.get_profile('default')['proxy_groups']}
    assert groups['auto']['url'] == url
    assert groups['fallback']['url'] == url


@pytest.mark.parametrize('payload', [
    None, [], False, 'bad', {},
    {'ids': [], 'url': NEW_URL},
    {'ids': 'auto', 'url': NEW_URL},
    {'ids': ['auto', 'auto'], 'url': NEW_URL},
    {'ids': ['auto', ''], 'url': NEW_URL},
    {'ids': ['auto', '   '], 'url': NEW_URL},
    {'ids': ['auto', None], 'url': NEW_URL},
    {'ids': ['auto', 1], 'url': NEW_URL},
    {'ids': ['auto', {}], 'url': NEW_URL},
    {'ids': ['auto', []], 'url': NEW_URL},
    {'ids': ['auto']},
])
def test_batch_rejects_malformed_payload_without_writes(batch_env, payload):
    client, repository = batch_env
    before = repository.export_all()
    response = client.put(ENDPOINT, json=payload)
    assert response.status_code == 400
    assert response.json['success'] is False
    assert repository.export_all() == before


@pytest.mark.parametrize('url', [
    None, False, 42, {}, [], '', '   ', 'ftp://probe.example.test',
    'https:///probe', 'https://name:password@probe.example.test',
    'https://@probe.example.test', 'https://probe.example.test:0',
    'https://probe.example.test:65536', 'https://probe.example.test/#fragment',
    'https://probe.example.test/a b', 'https://probe.example.test/\nprobe',
])
def test_batch_rejects_invalid_probe_urls_atomically(batch_env, url):
    client, repository = batch_env
    before = repository.export_all()
    response = client.put(ENDPOINT, json={'ids': ['auto', 'fallback'], 'url': url})
    assert response.status_code == 400
    assert response.json['success'] is False
    assert repository.export_all() == before


@pytest.mark.parametrize('invalid_id,status,message', [
    ('missing', 404, '不存在'),
    ('select', 400, '不支持节点测速'),
    ('chain', 400, '不支持节点测速'),
    ('following', 400, '来源组'),
])
def test_partial_invalid_selection_rejects_entire_batch(batch_env, invalid_id, status, message):
    client, repository = batch_env
    before = repository.export_all()
    response = client.put(ENDPOINT, json={'ids': ['auto', invalid_id], 'url': NEW_URL})
    assert response.status_code == status
    assert response.json['success'] is False
    assert message in response.json['message']
    assert repository.export_all() == before


def test_ids_are_resolved_only_inside_selected_profile(batch_env):
    client, repository = batch_env
    repository.update_profile_transaction('default', lambda profile: profile['proxy_groups'].append({
        'id': 'default-only', 'name': 'Default only', 'type': 'url-test', 'manual_nodes': ['DIRECT'],
    }))
    before = repository.export_all()
    response = client.put(ENDPOINT, headers={'X-ConfigFlow-Profile': 'other'},
                          json={'ids': ['auto', 'default-only'], 'url': NEW_URL})
    assert response.status_code == 404
    assert repository.export_all() == before
