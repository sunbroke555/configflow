"""Per-request config reads: validate a profile once and copy no whole document, with the same semantics."""

import copy

import pytest
from flask import Flask

from backend.common import config as config_module
from backend.common.config_repository import ProfileNotFound, ProfileRepository
from backend.common.profile_context import resolve_profile_id


@pytest.fixture
def repository(tmp_path):
    previous = config_module._repository
    repository = ProfileRepository(tmp_path)
    repository.create_profile({'id': 'other', 'name': 'Other'})
    config_module.set_repository(repository)
    yield repository
    config_module.set_repository(previous)
    config_module.reset_config_context()


@pytest.fixture
def app():
    return Flask(__name__)


def test_reads_and_saves_never_modify_the_committed_document(repository):
    repository.update_shared_transaction(lambda shared: shared.update(
        nodes=[{'id': 'n', 'name': 'Node'}],
        rule_library=[{'id': 'r', 'name': 'Rule', 'enabled': {'custom': []}}],
    ))
    repository.update_profile_fields('default', {
        'proxy_groups': [{'id': 'g', 'name': 'Group', 'type': 'select', 'manual_nodes': ['n']}],
        'rule_configs': [{'id': 'rule', 'itemType': 'ruleset', 'library_rule_id': 'r'}],
    })
    entry = repository._read_entry()
    before = copy.deepcopy(entry.document)

    config = config_module.get_config('default')
    config['proxy_groups'][0]['name'] = 'Changed'
    config['rule_configs'][0]['library_enabled']['custom'].append('Changed')
    shared = config_module.get_shared_config()
    shared['nodes'][0]['name'] = 'Changed'
    system = config_module.get_system_config()
    system['system_config']['server_domain'] = 'https://changed.example'
    config_module.save_config(config, 'default')
    config_module.save_shared_config(shared)
    config_module.save_system_config(system)

    assert repository._read_entry() is not entry
    assert entry.document == before
    assert repository.get_profile('default')['proxy_groups'][0]['name'] == 'Changed'
    assert repository.get_shared()['nodes'][0]['name'] == 'Changed'


def test_profile_baseline_is_the_read_view_and_keeps_concurrent_edits(repository):
    config = config_module.get_config('default')
    read_view = copy.deepcopy(config)
    config['mihomo']['custom_config'] = 'mixed-port: 7901'
    repository.update_profile_fields('default', {'proxy_groups': [{'id': 'g', 'name': 'Concurrent', 'type': 'select'}]})

    assert config_module._baseline('default') == read_view
    config_module.save_config(config, 'default')
    saved = repository.get_profile('default')
    assert saved['mihomo']['custom_config'] == 'mixed-port: 7901'
    assert saved['proxy_groups'] == [{'id': 'g', 'name': 'Concurrent', 'type': 'select'}]


def test_shared_baseline_keeps_concurrent_edits(repository):
    shared = config_module.get_shared_config()
    shared['nodes'] = [{'id': 'mine', 'name': 'Mine'}]
    repository.update_shared_transaction(lambda current: current.update(
        subscriptions=[{'id': 's', 'name': 'Concurrent', 'url': 'https://example.test/s'}]))
    config_module.save_shared_config(shared)
    saved = repository.get_shared()
    assert saved['nodes'] == [{'id': 'mine', 'name': 'Mine'}]
    assert [item['id'] for item in saved['subscriptions']] == ['s']


def test_transaction_reads_keep_an_eager_baseline(repository):
    seen = {}

    def update(profile):
        seen['view'] = config_module.get_config('default')
        seen['baseline'] = config_module._CONFIG_BASELINES.get()['default']
        profile['name'] = 'Changed in transaction'

    repository.update_profile_transaction('default', update)
    # The transaction's working copy may still change: never keep it as a baseline source.
    assert type(seen['baseline']) is dict
    assert seen['baseline'] == seen['view'] and seen['baseline'] is not seen['view']


def test_request_reuses_the_validated_profile(repository, app, monkeypatch):
    calls = []
    metadata = repository._profile_metadata
    monkeypatch.setattr(repository, '_profile_metadata', lambda profile_id: calls.append(profile_id) or metadata(profile_id))
    with app.test_request_context('/api/rules', headers={'X-ConfigFlow-Profile': 'other'}):
        config_module.reset_config_context()
        assert resolve_profile_id() == 'other'
        assert config_module.get_config()['profile_id'] == 'other'
        assert config_module.get_config('other') is config_module.get_config()
        assert resolve_profile_id('other') == 'other'
        assert resolve_profile_id('default') == 'default'
        assert resolve_profile_id('default') == 'default'
    assert calls == ['other', 'default']


def test_deleting_a_profile_in_the_same_request_is_noticed(repository, app):
    with app.test_request_context('/api/rules', headers={'X-ConfigFlow-Profile': 'other'}):
        config_module.reset_config_context()
        assert resolve_profile_id() == 'other'
        assert resolve_profile_id('other') == 'other'
        config_module.get_config()
        repository.delete_profile('other')
        with pytest.raises(ProfileNotFound):
            resolve_profile_id()
        with pytest.raises(ProfileNotFound):
            resolve_profile_id('other')
        with pytest.raises(ProfileNotFound):
            config_module.get_config()


def test_switching_the_default_in_the_same_request_is_noticed(repository, app):
    with app.test_request_context('/api/rules'):
        config_module.reset_config_context()
        assert resolve_profile_id() == 'default'
        repository.set_default_profile('other')
        assert resolve_profile_id() == 'other'


def test_another_request_does_not_reuse_the_validated_profile(repository, app):
    with app.test_request_context('/api/rules', headers={'X-ConfigFlow-Profile': 'other'}):
        assert resolve_profile_id() == 'other'
        with app.test_request_context('/api/rules'):
            assert resolve_profile_id() == 'default'
        assert resolve_profile_id() == 'other'

