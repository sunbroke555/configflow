"""Whole-editor saves preserve all-or-nothing writes and unrelated current values."""
from copy import deepcopy

import pytest
from flask import Flask

from backend.common import config as config_store
from backend.common.config_repository import ProfileRepository
from backend.routes import register_blueprints


@pytest.fixture
def editors(tmp_path):
    previous = config_store._repository
    repository = ProfileRepository(tmp_path)
    repository.create_profile({'id': 'office', 'name': 'Office'})
    repository.save_shared({'rule_library': [
        {'id': 'library', 'name': 'Shared', 'source_type': 'content',
         'content': 'example.test', 'behavior': 'domain', 'enabled': True}]})
    repository.save_profile('office', {
        'rule_configs': [
            {'id': 'r1', 'itemType': 'rule', 'rule_type': 'DOMAIN', 'value': 'a.test',
             'policy': 'DIRECT', 'enabled': True, 'group_name': 'Old'},
            {'id': 'rs1', 'itemType': 'ruleset', 'library_rule_id': 'library',
             'policy': 'DIRECT', 'enabled': False, 'group_name': 'Old'},
            {'id': 'r2', 'itemType': 'rule', 'rule_type': 'DOMAIN', 'value': 'b.test',
             'policy': 'REJECT', 'enabled': True, 'group_name': 'Other'},
        ],
        'mosdns': {'local_dns': 'old-dns', 'log_file': 'old.log', 'api_addr': ':8080',
                   'custom_extension': {'keep': True}},
    })
    config_store.set_repository(repository)
    app = Flask(__name__)
    register_blueprints(app)
    yield app.test_client(), repository
    config_store.set_repository(previous)


def mosdns_draft():
    return {
        'custom_config': 'log:\n  level: debug\n',
        'direct_rulesets': ['rs1'], 'proxy_rulesets': [],
        'direct_rules': ['r1'], 'proxy_rules': [],
        'custom_matches': [{'id': 'match', 'enabled': True, 'matches': 'qname example.test', 'exec': 'accept'}],
        'custom_match_position': 'head', 'local_dns': 'new-dns',
        'remote_dns': 'remote-dns', 'fallback_dns': '', 'default_forward': 'forward_remote',
        'custom_hosts': 'example.test 192.0.2.1', 'log_enabled': False,
        'log_level': 'debug', 'log_file': 'new.log',
        'api_enabled': False, 'api_address': '127.0.0.1:8338',
        'cache_enabled': False, 'cache_size': '128', 'cache_lazy_ttl': '60',
        'cache_dump_enabled': False, 'cache_dump_file': None, 'cache_dump_interval': 0,
    }


def test_mosdns_whole_save_commits_once_with_exact_publish_guard_and_profile_isolation(editors):
    client, repository = editors
    other = repository.get_profile('default')
    revision = repository.config_revision('office')
    response = client.post('/api/mosdns/settings?profile=office', json=mosdns_draft())
    assert response.status_code == 200, response.json
    assert response.json['publish_guard'] == {
        'expected_profile_id': 'office', 'expected_service_type': 'mosdns',
        'expected_config_revision': revision + 1}
    assert repository.config_revision('office') == revision + 1
    settings = repository.get_profile('office')['mosdns']
    assert settings == {**mosdns_draft(), 'cache_size': 128, 'cache_lazy_ttl': 60,
                        'cache_dump_file': './cache.dump', 'cache_dump_interval': 300,
                        'custom_extension': {'keep': True}}
    assert repository.get_profile('default') == other


def test_mosdns_invalid_rule_selection_cannot_save_other_sections(editors):
    client, repository = editors
    before = repository.get_profile('office')
    body = {**mosdns_draft(), 'direct_rules': ['deleted-rule']}
    response = client.post('/api/mosdns/settings?profile=office', json=body)
    assert response.status_code == 409, response.json
    assert repository.get_profile('office') == before
    assert 'publish_guard' not in response.json


@pytest.mark.parametrize('kind', ['mosdns', 'group-name'])
def test_editor_disk_failure_rolls_back_every_field(editors, monkeypatch, kind):
    client, repository = editors
    before = repository.get_profile('office')
    document = repository._store.fingerprint()
    def fail(*args):
        raise OSError('injected write failure')
    monkeypatch.setattr(repository._store, '_apply', fail)
    endpoint, body = ('/mosdns/settings', mosdns_draft()) if kind == 'mosdns' else (
        '/rules/group-name', {'ids': ['r1', 'rs1'], 'group_name': 'New'})
    response = client.post('/api' + endpoint + '?profile=office', json=body)
    assert response.status_code == 500
    assert repository.get_profile('office') == before
    assert repository._store.fingerprint() == document


def test_mosdns_save_preserves_concurrent_unrelated_fields_and_returns_its_own_revision(editors, monkeypatch):
    client, repository = editors
    original = repository.update_profile_transaction
    captured = []
    def interleave(profile_id, updater, **kwargs):
        original('office', lambda profile: profile['surge'].update(custom_config='[General]\nloglevel = notify'))
        saved, revision = original(profile_id, updater, **kwargs)
        captured.append(revision)
        original('office', lambda profile: profile['mihomo'].update(custom_config='mode: direct'))
        return saved, revision
    monkeypatch.setattr(repository, 'update_profile_transaction', interleave)
    response = client.post('/api/mosdns/settings?profile=office', json=mosdns_draft())
    assert response.status_code == 200, response.json
    assert response.json['publish_guard']['expected_config_revision'] == captured[0]
    assert repository.config_revision('office') == captured[0] + 1
    assert repository.get_profile('office')['surge']['custom_config'] == '[General]\nloglevel = notify'


def test_rule_group_rename_only_updates_labels_in_the_selected_profile(editors):
    client, repository = editors
    other = repository.get_profile('default')
    assert client.put('/api/rules/r1?profile=office', json={'policy': 'REJECT', 'value': 'latest.test'}).status_code == 200
    before = repository.get_profile('office')
    response = client.post('/api/rules/group-name?profile=office', json={
        'ids': ['r1', 'rs1'], 'group_name': ' New ', 'policy': 'DIRECT'})
    assert response.status_code == 200, response.json
    expected = deepcopy(before['rule_configs'])
    for rule in expected[:2]:
        rule['group_name'] = 'New'
    assert repository.get_profile('office')['rule_configs'] == expected
    assert repository.get_profile('default') == other
    assert repository.get_profile('office')['_revision'] == before['_revision'] + 1


def test_rule_group_missing_member_does_not_rename_existing_members(editors):
    client, repository = editors
    before = repository.get_profile('office')
    response = client.post('/api/rules/group-name?profile=office', json={
        'ids': ['r1', 'deleted-rule', 'rs1'], 'group_name': 'New'})
    assert response.status_code == 404
    assert repository.get_profile('office') == before


@pytest.mark.parametrize('body', [[], {'ids': 'r1', 'group_name': 'New'},
                                 {'ids': ['r1'], 'group_name': 1},
                                 {'ids': ['r1', 'r1'], 'group_name': 'New'},
                                 {'ids': [], 'group_name': 'New'}])
def test_rule_group_rejects_malformed_requests_without_changes(editors, body):
    client, repository = editors
    before = repository.get_profile('office')
    assert client.post('/api/rules/group-name?profile=office', json=body).status_code == 400
    assert repository.get_profile('office') == before


def test_rule_group_blank_label_restores_default_display(editors):
    client, repository = editors
    response = client.post('/api/rules/group-name?profile=office', json={
        'ids': ['r1', 'rs1'], 'group_name': '  '})
    assert response.status_code == 200
    assert [rule['group_name'] for rule in repository.get_profile('office')['rule_configs'][:2]] == ['', '']


def test_explicitly_clearing_follow_switches_generated_candidates_and_keeps_other_profiles(editors):
    import yaml
    from backend.converters.mihomo import generate_mihomo_config

    client, repository = editors
    groups = [
        {'id': 'parent', 'name': 'Parent', 'type': 'select', 'manual_nodes': ['DIRECT']},
        {'id': 'child', 'name': 'Child', 'type': 'select', 'follow_group': 'parent'},
    ]
    for profile_id in ('default', 'office'):
        repository.save_profile(profile_id, {'proxy_groups': groups})
    other = repository.get_profile('default')

    def child_candidates():
        config = yaml.safe_load(generate_mihomo_config(repository.get_compat_config('office')))
        return next(group['proxies'] for group in config['proxy-groups'] if group['name'] == 'Child')

    assert child_candidates() == ['DIRECT']
    response = client.put('/api/proxy-groups/child?profile=office', json={
        'follow_group': '', 'manual_nodes': ['REJECT']})
    assert response.status_code == 200, response.json
    assert child_candidates() == ['REJECT']
    assert repository.get_profile('default') == other
