"""Shared rule caches must follow resource identity, never a lossy filename."""
import pytest
from flask import Flask

from backend.common import config as config_store
from backend.common.config_repository import ProfileRepository
from backend.routes import register_blueprints
from backend.routes.rules import get_ruleset_content
from backend.utils.rule_utils import save_rule_to_local


@pytest.fixture
def library_app(tmp_path):
    previous = config_store._repository
    repository = ProfileRepository(tmp_path)
    config_store.set_repository(repository)
    app = Flask(__name__)
    register_blueprints(app)
    yield repository, app.test_client()
    config_store.set_repository(previous)


def add_rules(repository, names):
    rules = [dict(id=f'r{i}', name=name, source_type='content',
                  content=f'host{i}.test', enabled=True, behavior='domain')
             for i, name in enumerate(names)]
    repository.update_shared_transaction(lambda shared: shared.update(rule_library=rules))
    return rules


@pytest.mark.parametrize('names', [('a:b', 'a_b'), ('x' * 201, 'x' * 200 + 'y'), ('a.', 'a')])
def test_colliding_names_have_distinct_downloads_and_matcher_content(library_app, names):
    repository, client = library_app
    rules = add_rules(repository, names)
    filenames = [save_rule_to_local(rule) for rule in rules]
    assert len(set(filenames)) == 2
    for rule in rules:
        response = client.get('/api/rules/local/' + rule['name'])
        assert response.status_code == 200
        assert response.get_data(as_text=True) == rule['content']
        assert get_ruleset_content({}, rule, allow_network=False) == rule['content']


def test_ambiguous_legacy_cache_is_not_read_as_either_source(library_app):
    repository, _ = library_app
    rules = add_rules(repository, ('a:b', 'a_b'))
    repository.write_shared_text('rules/a_b.list', 'unknown-owner.test')
    for rule in rules:
        assert get_ruleset_content({}, rule, allow_network=False) == rule['content']


def test_unique_legacy_url_cache_remains_available_offline(library_app):
    repository, _ = library_app
    rule = dict(id='remote', name='Remote', source_type='url', url='https://rules.test/file')
    repository.update_shared_transaction(lambda shared: shared.update(rule_library=[rule]))
    repository.write_shared_text('rules/Remote.list', 'legacy.test')
    assert get_ruleset_content({}, rule, allow_network=False) == 'legacy.test'


def test_delete_one_collision_preserves_the_other_resource(library_app):
    repository, client = library_app
    rules = add_rules(repository, ('a:b', 'a_b'))
    for rule in rules:
        save_rule_to_local(rule)
    assert client.delete('/api/rule-library/r0').status_code == 200
    response = client.get('/api/rules/local/a_b')
    assert response.status_code == 200
    assert response.get_data(as_text=True) == 'host1.test'


def test_rename_preserves_resource_cache_identity(library_app):
    repository, _ = library_app
    rule = add_rules(repository, ('Before',))[0]
    before = save_rule_to_local(rule)
    after = save_rule_to_local({**rule, 'name': 'After'})
    assert before == after


def test_all_local_consumers_keep_colliding_sources_separate(library_app):
    from backend.utils.domain_matcher_cache import local_matcher, clear_cache
    from backend.utils.domain_discovery_service import _served_rule_content
    from backend.routes.mosdns import _load_cached_rule_content_for_url
    from backend.routers.rule_sync_rules import load_sources
    from backend.routers.rule_sync import _cache_sources

    repository, _ = library_app
    rules = add_rules(repository, ('a:b', 'a_b'))
    for index, rule in enumerate(rules):
        rule.update(source_type='url', url=f'https://rules.test/{index}', behavior='classical')
    repository.update_shared_transaction(lambda shared: shared.update(rule_library=rules))
    config = {'rule_library': rules, 'rule_configs': [
        dict(id=f'ref{i}', itemType='ruleset', library_rule_id=rule['id'],
             name=rule['name'], policy=f'policy{i}', url=rule['url'])
        for i, rule in enumerate(rules)]}
    router = {'ip_rule_sets': ['ref0', 'ref1']}
    contents = {'ref0': 'DOMAIN,first.test\nIP-CIDR,1.1.1.0/24',
                'ref1': 'DOMAIN,second.test\nIP-CIDR,8.8.8.0/24'}
    _cache_sources(config, router, contents)
    assert load_sources(config, router)[0] == contents
    clear_cache()
    try:
        matcher = local_matcher(config, 'default')
        assert matcher.match('first.test')['policy'] == 'policy0'
        assert matcher.match('second.test')['policy'] == 'policy1'
        for i, rule in enumerate(rules):
            assert _served_rule_content(rule).decode() == contents[f'ref{i}']
            assert _load_cached_rule_content_for_url(rule['url'], config) == contents[f'ref{i}']
        _cache_sources(config, router, {**contents, 'ref0': 'DOMAIN,updated.test'})
        updated = local_matcher(config, 'default')
        assert updated is not matcher
        assert updated.match('updated.test')['policy'] == 'policy0'
        assert updated.match('first.test') is None
        assert updated.match('second.test')['policy'] == 'policy1'
    finally:
        clear_cache()


def test_legacy_collision_with_unreferenced_rule_is_not_used(library_app):
    from backend.utils.domain_matcher_cache import local_matcher, clear_cache
    repository, _ = library_app
    rules = add_rules(repository, ('a:b', 'a_b'))
    repository.write_shared_text('rules/a_b.list', 'DOMAIN,wrong.test')
    config = {'rule_library': rules, 'rule_configs': [
        dict(id='ref0', itemType='ruleset', library_rule_id='r0', policy='DIRECT')]}
    clear_cache()
    try:
        matcher = local_matcher(config, 'default')
        assert matcher.match('wrong.test') is None
        assert matcher.match('host0.test')['policy'] == 'DIRECT'
    finally:
        clear_cache()
