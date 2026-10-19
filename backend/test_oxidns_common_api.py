"""Authenticated common-editor responses must round-trip secret-bearing drafts."""
from copy import deepcopy

import pytest
import yaml

from backend.common.config_export import redact_oxidns_settings
from backend.common.oxidns_editor import prepare_editor
from backend.test_oxidns_integration import oxidns_app


@pytest.mark.parametrize('prefix', ['/api/oxidns', '/api/profiles/office/oxidns'])
def test_common_api_is_lossless_profile_scoped_and_does_not_persist(oxidns_app, prefix):
    app, repository = oxidns_app
    client = app.test_client()
    config = repository.get_compat_config('office')
    token = config['system_config']['rule_proxy_token']
    settings = prepare_editor({**config['oxidns'], 'api': {'enabled': True, 'listen': '127.0.0.1:9199',
                              'username': 'editor', 'password': token},
                              'rule_update': {'enabled': True}}, config, 'http://config.test')
    before = deepcopy(repository.get_profile('office'))
    read = client.post(prefix + '/common-settings?profile=office', json={'settings': settings})
    assert read.status_code == 200, read.get_json()
    assert read.get_json()['values']['api']['password'] == token
    patched = client.post(prefix + '/common-settings/apply?profile=office',
                          json={'settings': settings, 'changes': {'worker_threads': 6}})
    assert patched.status_code == 200, patched.get_json()
    payload = patched.get_json()
    assert payload['values']['api']['password'] == token
    assert yaml.safe_load(payload['settings']['native_config'])['api']['http']['auth']['password'] == token
    assert '[REDACTED]' not in payload['settings']['native_config']
    assert patched.headers['Cache-Control'] == 'no-store'
    disabled = client.post(prefix + '/common-settings/apply?profile=office',
                           json={'settings': payload['settings'], 'changes': {'api.enabled': False}})
    assert disabled.status_code == 200, disabled.get_json()
    draft = disabled.get_json()['settings']
    assert token in str(draft['_common_disabled'])
    assert token not in str(redact_oxidns_settings(draft))
    enabled = client.post(prefix + '/common-settings/apply?profile=office',
                          json={'settings': draft, 'changes': {'api.enabled': True}})
    assert enabled.status_code == 200, enabled.get_json()
    assert enabled.get_json()['values']['api']['password'] == token
    assert repository.get_profile('office') == before


@pytest.mark.parametrize('prefix', ['/api/oxidns', '/api/profiles/office/oxidns'])
def test_disabling_rule_updates_after_enabling_keeps_draft_renderable(oxidns_app, prefix):
    app, repository = oxidns_app
    client = app.test_client()
    config = repository.get_compat_config('office')
    settings = prepare_editor({**config['oxidns'], 'rule_update': {'enabled': False}},
                              config, 'http://config.test')
    before = deepcopy(repository.get_profile('office'))

    enabled = client.post(prefix + '/common-settings/apply?profile=office',
                          json={'settings': settings, 'changes': {'rule_update.enabled': True}})
    assert enabled.status_code == 200, enabled.get_json()
    enabled_draft = enabled.get_json()['settings']
    assert enabled.get_json()['values']['rule_update']['enabled'] is True
    enabled_render = client.post(prefix + '/render?profile=office', json={'settings': enabled_draft})
    assert enabled_render.status_code == 200, enabled_render.get_json()
    enabled_document = yaml.safe_load(enabled_render.get_json()['config'])
    assert any(plugin.get('tag') == 'rules_cron' and plugin['args']['jobs']
               for plugin in enabled_document['plugins'])
    assert repository.get_profile('office') == before

    disabled = client.post(prefix + '/common-settings/apply?profile=office',
                           json={'settings': enabled_draft, 'changes': {'rule_update.enabled': False}})
    assert disabled.status_code == 200, disabled.get_json()
    disabled_draft = disabled.get_json()['settings']
    assert disabled.get_json()['values']['rule_update']['enabled'] is False
    rendered = client.post(prefix + '/render?profile=office', json={'settings': disabled_draft})
    assert rendered.status_code == 200, rendered.get_json()
    for document in (yaml.safe_load(disabled_draft['native_config']),
                     yaml.safe_load(rendered.get_json()['config'])):
        assert not any(plugin.get('tag') == 'rules_cron' for plugin in document['plugins'])
        for tag in ('rules_download', 'rules_reload', 'rules_refresh'):
            assert next(plugin for plugin in document['plugins'] if plugin['tag'] == tag) == next(
                plugin for plugin in enabled_document['plugins'] if plugin['tag'] == tag)
        assert all(plugin['args']['jobs'] for plugin in document['plugins']
                   if plugin.get('type') == 'cron')
    assert rendered.headers['Cache-Control'] == 'no-store'
    assert repository.get_profile('office') == before
