"""Managed subscription edits must not mutate custom YAML alias consumers."""
from copy import deepcopy
import io
import json
import tarfile

import pytest
import requests
import yaml

from backend.common.oxidns_common import apply_common_settings
from backend.common.oxidns_editor import prepare_editor
from backend.common.oxidns_native import load_native_yaml, native_document
from backend.common.oxidns_native_apply import apply_native_settings
from backend.common.oxidns_validation import validate_settings
from backend.converters.oxidns import generate_oxidns_config, get_oxidns_ruleset_downloads
from backend.test_dns_follow import follow_env  # noqa: F401
from backend.test_oxidns_common_routing import fixture_config
from backend.test_oxidns_integration import oxidns_app  # noqa: F401
from backend.test_oxidns_withdrawn_downloads import setup_sources, plugin, delete_provider


def share_download_args(settings, document, alias='args'):
    args = plugin(document, 'rules_download')['args']
    args['future_option'] = {'keep': [False, 0, None]}
    args['downloads'][0]['future_option'] = {'keep': False}
    custom = deepcopy(args) if alias != 'args' else args
    if alias == 'list':
        custom['downloads'] = args['downloads']
    for tag in ('user_download', 'another_user_download'):
        document['plugins'].append({'tag': tag, 'type': 'download', 'args': custom})
    plugin(document, 'rules_cron')['args']['jobs'].append({
        'name': 'user-job', 'schedule': '0 * * * *', 'executors': ['$user_download']})
    settings['native_config'] = yaml.safe_dump(document, sort_keys=False)
    parsed = load_native_yaml(settings['native_config'])
    managed, user = plugin(parsed, 'rules_download'), plugin(parsed, 'user_download')
    assert (managed['args'] is user['args']) == (alias == 'args')
    assert (managed['args']['downloads'] is user['args']['downloads']) == (alias in ('args', 'list'))
    return deepcopy(plugin(parsed, 'user_download'))


def assert_custom_preserved(document, expected):
    custom = plugin(document, 'user_download')
    assert custom == expected
    other = plugin(document, 'another_user_download')
    assert other == {**expected, 'tag': 'another_user_download'}
    # Only the edited consumer is detached; unaffected aliases stay shared.
    assert custom['args'] is other['args']
    assert any(job['name'] == 'user-job' for job in plugin(document, 'rules_cron')['args']['jobs'])


@pytest.mark.parametrize('alias', ['none', 'list', 'args'])
@pytest.mark.parametrize('editor', ['native-delete', 'native-clear', 'common'])
@pytest.mark.parametrize('keep', [True, False])
def test_withdrawal_detaches_only_managed_downloads_and_survives_reopen(alias, editor, keep):
    config, settings, document, source = setup_sources(keep=keep)
    expected = share_download_args(settings, document, alias)
    original = deepcopy(settings)
    if editor == 'common':
        result = apply_common_settings(settings, {'direct_rulesets': []}, config)['settings']
    else:
        candidate = load_native_yaml(settings['native_config'])
        if editor == 'native-delete':
            delete_provider(candidate, source['tag'])
        else:
            plugin(candidate, source['tag'])['args']['files'] = []
        result = apply_native_settings(settings, yaml.safe_dump(candidate, sort_keys=False), config)['settings']
    assert settings == original
    reopened = prepare_editor(json.loads(json.dumps(result)), config)
    assert reopened['native_config'] == result['native_config']
    assert validate_settings(reopened, config)['valid']
    for actual in (native_document(reopened), yaml.safe_load(generate_oxidns_config({**config, 'oxidns': reopened}))):
        assert_custom_preserved(actual, expected)
        if keep:
            managed = plugin(actual, 'rules_download')
            assert len(managed['args']['downloads']) == 1
            assert managed['args']['future_option'] == {'keep': [False, 0, None]}
            assert managed['args'] is not plugin(actual, 'user_download')['args']
        else:
            assert not any(p['tag'] in ('rules_download', 'rules_reload', 'rules_refresh') for p in actual['plugins'])
            assert [job['name'] for job in plugin(actual, 'rules_cron')['args']['jobs']] == ['user-job']


@pytest.mark.parametrize('editor', ['native', 'common'])
@pytest.mark.parametrize('keep', [True, False])
def test_adguard_withdrawal_uses_the_same_alias_protection(editor, keep):
    config = fixture_config()
    settings = prepare_editor({'direct_rulesets': ['rs'] if keep else [],
                               'adguard': {'enabled': True, 'urls': ['https://example.test/adguard.txt']},
                               'rule_update': {'enabled': True}}, config)
    document = load_native_yaml(settings['native_config'])
    expected = share_download_args(settings, document)
    if editor == 'common':
        result = apply_common_settings(settings, {'adguard.enabled': False}, config)['settings']
    else:
        candidate = load_native_yaml(settings['native_config'])
        candidate['plugins'] = [p for p in candidate['plugins'] if p['tag'] != 'adguard_rules']
        result = apply_native_settings(settings, yaml.safe_dump(candidate), config)['settings']
    assert validate_settings(result, config)['valid']
    actual = native_document(result)
    assert_custom_preserved(actual, expected)
    if keep:
        assert len(plugin(actual, 'rules_download')['args']['downloads']) == 1
    else:
        assert not any(p['tag'] == 'rules_download' for p in actual['plugins'])


def test_http_apply_save_reopen_and_render_preserve_custom_aliases(oxidns_app, monkeypatch):
    app, repository = oxidns_app
    config = repository.get_compat_config('office')
    settings = prepare_editor({'direct_rulesets': ['rs'], 'rule_update': {'enabled': True}}, config)
    document = load_native_yaml(settings['native_config'])
    expected = share_download_args(settings, document)
    candidate = load_native_yaml(settings['native_config'])
    source = next(p for p in candidate['plugins'] if p['type'] == 'domain_set')
    delete_provider(candidate, source['tag'])
    def forbidden(*args, **kwargs):
        pytest.fail('draft edits must not contact the network or an Agent')
    monkeypatch.setattr('requests.sessions.Session.request', forbidden)
    other = repository.get_profile('default')
    client = app.test_client()
    response = client.post('/api/profiles/office/oxidns/native-settings/apply', json={
        'settings': settings, 'native_config': yaml.safe_dump(candidate)})
    assert response.status_code == 200, response.json
    saved = client.post('/api/profiles/office/oxidns/settings', json=response.json['settings'])
    assert saved.status_code == 200, saved.json
    reopened = client.get('/api/profiles/office/oxidns/settings').json
    rendered = client.post('/api/profiles/office/oxidns/render', json={'settings': reopened})
    assert rendered.status_code == 200, rendered.json
    assert_custom_preserved(load_native_yaml(reopened['native_config']), expected)
    assert_custom_preserved(yaml.safe_load(rendered.json['config']), expected)
    assert repository.get_profile('default') == other


def test_actual_publish_bundle_contains_pruned_managed_and_unchanged_custom_downloads(monkeypatch):
    from backend.agents.deployment_bundle import build_deployment_bundle
    config, settings, document, source = setup_sources()
    expected = share_download_args(settings, document)
    candidate = load_native_yaml(settings['native_config'])
    delete_provider(candidate, source['tag'])
    result = apply_native_settings(settings, yaml.safe_dump(candidate), config)['settings']
    config['oxidns'] = result
    fetched = []
    def fetch(url, **kwargs):
        fetched.append(url)
        response = requests.Response()
        response.status_code = 200
        response._content = b'example.test\n'
        response._content_consumed = True
        return response
    monkeypatch.setattr('backend.agents.deployment_bundle.request_rule', fetch)
    bundle = build_deployment_bundle({'id': 'test-oxidns', 'service_type': 'oxidns'}, config,
                                    generate_oxidns_config(config),
                                    ruleset_downloads=get_oxidns_ruleset_downloads(config))
    assert fetched == ['https://example.test/keep.txt']
    with tarfile.open(fileobj=io.BytesIO(bundle.archive), mode='r:gz') as archive:
        actual = yaml.safe_load(archive.extractfile('files/config.yaml').read())
    assert_custom_preserved(actual, expected)
    assert len(plugin(actual, 'rules_download')['args']['downloads']) == 1


def test_dns_follow_after_alias_withdrawal_preserves_custom_downloads_and_skips_unchanged_address(follow_env, monkeypatch):
    from backend.agents import dns_follow
    from backend.routes.agents import _prepare_transactional_content
    env = follow_env
    config, settings, document, source = setup_sources()
    plugin(document, 'forward_remote')['args']['upstreams'][0]['addr'] = 'udp://192.0.2.10:5353'
    expected = share_download_args(settings, document)
    candidate = load_native_yaml(settings['native_config'])
    delete_provider(candidate, source['tag'])
    settings = apply_native_settings(settings, yaml.safe_dump(candidate), config)['settings']
    env.repository.save_shared({'rule_library': [
        {'id': 'lib_' + rule['id'], 'name': rule['id'], 'source_type': 'url',
         'url': rule['url'], 'behavior': 'domain', 'enabled': True} for rule in config['rule_configs']]})
    env.repository.save_profile('default', {'oxidns': settings, 'rule_configs': [
        {'id': rule['id'], 'itemType': 'ruleset', 'library_rule_id': 'lib_' + rule['id'],
         'policy': 'DIRECT', 'enabled': True} for rule in config['rule_configs']]})
    fetched = []
    def fetch(url, **kwargs):
        fetched.append(url)
        response = requests.Response()
        response.status_code = 200
        response._content = b'example.test\n'
        response._content_consumed = True
        return response
    monkeypatch.setattr('backend.agents.deployment_bundle.request_rule', fetch)
    config, revision = env.repository.get_publish_snapshot('default')
    prepared = _prepare_transactional_content(env.agent(), config, base_url='https://config.test')
    published = env.manager.publish_deployment(env.target_id, prepared['bundle'], config_revision=revision,
                                               base_url='https://config.test', resource_config=config)
    assert published['status'] == 'succeeded'
    assert fetched == ['https://example.test/keep.txt']
    env.bind(upstream_index=0)
    env.uploads.clear()
    fetched.clear()
    env.publish_source('source-after-withdrawal')
    env.advance('succeeded')
    assert len(env.uploads) == 1
    actual = env.uploaded_document()
    assert plugin(actual, 'forward_remote')['args']['upstreams'][0]['addr'] == 'udp://127.0.0.1:2053'
    assert_custom_preserved(actual, expected)
    assert len(plugin(actual, 'rules_download')['args']['downloads']) == 1
    # Address changes may prepare resources (library sources come from the
    # scheduled cache); withdrawn sources must stay out.
    assert set(fetched) <= {'https://example.test/keep.txt'}
    env.uploads.clear()
    def unavailable(*args, **kwargs):
        pytest.fail('an unchanged upstream must not prepare or download another bundle')
    monkeypatch.setattr(dns_follow, '_prepare', unavailable)
    monkeypatch.setattr('backend.agents.deployment_bundle.request_rule', unavailable)
    env.publish_source('source-same-address')
    env.advance('succeeded')
    assert env.state()['target_deployment_id'] is None
    assert env.uploads == []
    assert_custom_preserved(native_document(env.repository.get_profile('default')['oxidns']), expected)
